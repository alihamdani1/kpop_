"""Orchestration des visuels sociaux (RSS image + titre -> PNG -> Discord). Séparé de
`pipeline.py`, même principe que `thread_pipeline.py` : cadence et dépendances (Chromium via
Playwright) totalement différentes du cycle articles existant, aucun risque de régression sur
`run_cycle` déjà en production. Aucun appel Gemini ici — ce module ne fait que lire des champs
déjà calculés par `run_cycle` (`headline_fr`/`key_points_fr`, rédigés ensemble par le 3e appel
`SocialVisualContent`, voir analyzer.py/pipeline.py) et rendre/envoyer les visuels.

Deux formats générés et envoyés pour chaque article, même image/texte source, même salon Discord
(pas de webhook séparé) : `templates/social_post.html` (9:16, TikTok/Reels/Stories) et
`templates/social_post_instagram.html` (4:5, publication de feed) — voir
`_TIKTOK_DIMENSIONS`/`_INSTAGRAM_DIMENSIONS`.

Texte affiché : `headline_fr` en titre, `key_points_fr` en points clés — rédigés ENSEMBLE par un
appel dédié pour garantir qu'ils se complètent sans se répéter, pour TOUTE route retenue (A, B,
CONCERT). Si cet appel a échoué ou que l'article a été traité avant son introduction (colonnes
NULL/'[]' en base), repli automatique dérivé de `summary_fr` — moins percutant mais jamais vide.
Ni l'un ni l'autre n'est `tweet_draft` (tag, hashtags, question d'engagement — pensé pour
Twitter, pas pour une card d'actu façon HugoDécrypte)."""

from __future__ import annotations

import datetime as dt
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kpop_bot import media_library, notifier, storage, visual_generator
from kpop_bot.models import ArticleRecord, TweetTag, determine_tweet_tag
from kpop_bot.settings import Settings

logger = logging.getLogger(__name__)

_TIKTOK_DIMENSIONS = (1080, 1920)  # 9:16 — TikTok/Reels/Stories
_INSTAGRAM_DIMENSIONS = (1080, 1350)  # 4:5 — publication de feed

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
    """Jour/mois/année uniquement (pas d'heure — cahier des charges du redesign du visuel) —
    formatage manuel plutôt que `strftime('%B')` : évite une dépendance à la locale du système,
    non garantie fr_FR sur un runner GitHub Actions."""
    return f"{moment.day} {_MOIS_FR[moment.month - 1]} {moment.year}"


_FALLBACK_HEADLINE_MAX_WORDS = 14


def _fallback_headline(summary_fr: str) -> str:
    """Repli pour les articles sans `headline_fr` (générés avant son introduction — colonne
    migrée, NULL sur l'historique, voir storage._MIGRATED_COLUMNS). Tronque `summary_fr` aux
    ~14 premiers mots plutôt que d'afficher le résumé complet en pavé — moins percutant qu'une
    vraie accroche rédigée, mais jamais vide."""
    words = summary_fr.split()
    return " ".join(words[:_FALLBACK_HEADLINE_MAX_WORDS]).rstrip(".,;: ")


def _headline_text(article: ArticleRecord) -> str:
    if article.headline_fr:
        return article.headline_fr
    return _fallback_headline(article.summary_fr or "")


def _fallback_key_points(summary_fr: str) -> list[str]:
    """Repli si `key_points_fr` est vide (appel `SocialVisualContent` pas encore introduit pour
    cet article, ou échoué — voir `run_social_visuals`) — découpage mécanique de `summary_fr`
    (déjà 2 phrases, ton neutre) sur les limites de phrase. Moins pertinent que de vrais points
    clés rédigés pour compléter le titre (peut se répéter avec `_headline_text` en repli lui
    aussi), mais jamais vide."""
    summary = summary_fr.strip()
    if not summary:
        return []
    sentences = [sentence.strip() for sentence in summary.split(". ")]
    return [
        sentence if sentence.endswith((".", "!", "?")) else f"{sentence}."
        for sentence in sentences
        if sentence
    ][:3]


def _key_points(article: ArticleRecord) -> list[str]:
    if article.key_points_fr:
        return article.key_points_fr
    return _fallback_key_points(article.summary_fr or "")


def _category_label(article: ArticleRecord) -> str:
    """Étiquette affichée séparément sur le visuel (`GOSSIP`/`RELEASE`/...) — dérivée par la même
    fonction pure que le tag préfixé au tweet Twitter (T5quater), pour rester cohérente avec la
    catégorie déjà affichée ailleurs, sans appel IA supplémentaire."""
    if article.category is not None and article.importance is not None:
        tag = determine_tweet_tag(article.category, article.importance, article.virality)
    else:  # ne devrait jamais arriver pour un article SENT (category/importance toujours
        # fixés par save_analysis) — filet défensif plutôt qu'une exception ici.
        tag = TweetTag.INFO
    return tag.value


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
            visual_generator.SocialVisualRenderer() as renderer,
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

                headline = _headline_text(article)
                key_points = _key_points(article)
                category_label = _category_label(article)
                formatted_date = _format_date_fr(article.published_at)

                try:
                    tiktok_width, tiktok_height = _TIKTOK_DIMENSIONS
                    tiktok_png = renderer.render(
                        template_path=settings.social_visual_template_path,
                        width=tiktok_width,
                        height=tiktok_height,
                        image_bytes=image_bytes,
                        headline=headline,
                        key_points=key_points,
                        category_label=category_label,
                        formatted_date=formatted_date,
                    )
                    instagram_width, instagram_height = _INSTAGRAM_DIMENSIONS
                    instagram_png = renderer.render(
                        template_path=settings.social_visual_instagram_template_path,
                        width=instagram_width,
                        height=instagram_height,
                        image_bytes=image_bytes,
                        headline=headline,
                        key_points=key_points,
                        category_label=category_label,
                        formatted_date=formatted_date,
                    )
                except Exception as exc:  # bonus non bloquant — jamais fatal pour le run
                    logger.warning("Échec de rendu du visuel pour %s : %s", article.url, exc)
                    stats.render_failed += 1
                    continue

                index += 1
                tiktok_path = Path(tmp_dir) / f"visual_{article.id}_tiktok.png"
                instagram_path = Path(tmp_dir) / f"visual_{article.id}_instagram.png"
                tiktok_path.write_bytes(tiktok_png)
                instagram_path.write_bytes(instagram_png)
                try:
                    notifier.notify_social_visual(
                        article,
                        tiktok_path,
                        instagram_path,
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
