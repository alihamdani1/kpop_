"""Orchestration du visuel social 9:16 (RSS image + tweet -> PNG -> Discord). Séparé de
`pipeline.py`, même principe que `thread_pipeline.py` : cadence et dépendances (Chromium via
Playwright) totalement différentes du cycle articles existant, aucun risque de régression sur
`run_cycle` déjà en production."""

from __future__ import annotations

import datetime as dt
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kpop_bot import media_library, notifier, storage, visual_generator
from kpop_bot.models import TWEET_TAG_LABELS, ArticleRecord, TweetTag, determine_tweet_tag
from kpop_bot.settings import Settings

logger = logging.getLogger(__name__)

_MOIS_FR = [
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
]


def _format_date_fr(moment: dt.datetime) -> str:
    """Formatage manuel plutôt que `strftime('%B')` — évite une dépendance à la locale du
    système, non garantie fr_FR sur un runner GitHub Actions."""
    return f"{moment.day} {_MOIS_FR[moment.month - 1]} {moment.year} · {moment.strftime('%Hh%M')}"


def _category_label_and_body(article: ArticleRecord) -> tuple[str, str]:
    """Le tag (`[GOSSIP]`/`[RELEASE]`/...) est déjà préfixé au `tweet_draft` stocké par
    pipeline.py (T5quater, dérivé de category/importance/virality, jamais par l'IA) — recalculé
    ici via la même fonction pure pour l'afficher comme étiquette de catégorie séparée dans le
    visuel, et retiré du corps du texte pour ne pas apparaître deux fois."""
    if article.category is not None and article.importance is not None:
        tag = determine_tweet_tag(article.category, article.importance, article.virality)
    else:  # ne devrait jamais arriver pour un article SENT (category/importance toujours
        # fixés par save_analysis) — filet défensif plutôt qu'une exception ici.
        tag = TweetTag.INFO
    label = TWEET_TAG_LABELS[tag]
    body = (article.tweet_draft or "").removeprefix(f"{label}\n\n")
    return tag.value, body


def _resolve_image_bytes(article: ArticleRecord, settings: Settings) -> bytes | None:
    """Image RSS de l'article si exploitable, sinon repli sur la bibliothèque interne
    (media_library.py), sinon None (aucun visuel généré pour cet article — dégradation
    gracieuse, même principe que partout ailleurs dans ce projet)."""
    if article.image_url:
        image_bytes = visual_generator.download_image(
            article.image_url, timeout=settings.request_timeout_seconds
        )
        if image_bytes is not None:
            return image_bytes

    fallback_path = media_library.select_image_for_article(
        settings.media_library_path, artists=article.artists
    )
    if fallback_path is not None:
        return fallback_path.read_bytes()

    return None


@dataclass
class SocialVisualStats:
    candidates: int = 0
    skipped_no_image: int = 0
    render_failed: int = 0
    sent: int = 0
    send_failed: int = 0

    def summary(self) -> str:
        return (
            f"candidats={self.candidates} sans_image={self.skipped_no_image} "
            f"échecs_rendu={self.render_failed} envoyés={self.sent} "
            f"échecs_envoi={self.send_failed}"
        )


def run_social_visuals(settings: Settings, *, dry_run: bool = False) -> SocialVisualStats:
    """Génère et envoie le visuel social des articles déjà `SENT` qui n'en ont pas encore reçu
    un. Bonus non bloquant : un échec (rendu, téléchargement, envoi) sur un article n'interrompt
    jamais le traitement des suivants — même philosophie que le script TikTok (T14)."""
    stats = SocialVisualStats()
    if not settings.discord_webhook_social:
        logger.info("discord_webhook_social non configuré — fonctionnalité inactive.")
        return stats

    conn = storage.init_db(settings.db_path)
    try:
        articles = storage.pending_social_visuals(conn, limit=settings.social_visual_batch_limit)
        stats.candidates = len(articles)
        if not articles:
            return stats

        if dry_run:
            for article in articles:
                logger.info("[dry-run] générerait un visuel pour : %s", article.title)
            return stats

        with (
            visual_generator.SocialVisualRenderer(settings.social_visual_template_path) as renderer,
            tempfile.TemporaryDirectory() as tmp_dir,
        ):
            index = 0
            for article in articles:
                image_bytes = _resolve_image_bytes(article, settings)
                if image_bytes is None:
                    logger.warning(
                        "Aucune image exploitable (RSS ni bibliothèque interne) pour %s — "
                        "visuel ignoré.",
                        article.url,
                    )
                    stats.skipped_no_image += 1
                    continue

                category_label, tweet_body = _category_label_and_body(article)
                try:
                    png_bytes = renderer.render(
                        image_bytes=image_bytes,
                        tweet_text=tweet_body,
                        category_label=category_label,
                        formatted_date=_format_date_fr(article.published_at),
                    )
                except Exception as exc:  # bonus non bloquant — jamais fatal pour le run
                    logger.warning("Échec de rendu du visuel pour %s : %s", article.url, exc)
                    stats.render_failed += 1
                    continue

                index += 1
                tmp_path = Path(tmp_dir) / f"visual_{article.id}.png"
                tmp_path.write_bytes(png_bytes)
                try:
                    notifier.notify_social_visual(
                        article,
                        tmp_path,
                        url=settings.discord_webhook_social,
                        timeout=settings.request_timeout_seconds,
                        index=index,
                    )
                    storage.mark_social_visual_sent(conn, article.id)
                    stats.sent += 1
                except notifier.NotificationError as exc:
                    logger.warning("Échec d'envoi du visuel pour %s : %s", article.url, exc)
                    stats.send_failed += 1

        return stats
    finally:
        conn.close()
