"""Orchestration (T7) : collecte -> classification -> routage -> rédaction -> diffusion."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kpop_bot import analyzer, fetcher, notifier, storage
from kpop_bot.models import (
    TWEET_TAG_LABELS,
    ArticleStatus,
    Route,
    determine_route,
    determine_tweet_tag,
)
from kpop_bot.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class CycleStats:
    fetched: int = 0
    new: int = 0
    reprocessed_failed: int = 0
    classified: int = 0
    route_a: int = 0
    route_b: int = 0
    filtered: int = 0
    filtered_reviewed: int = 0
    analysis_failed: int = 0
    sent: int = 0
    send_failed: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    france_overrides: int = 0
    milestone_flags: int = 0
    quota_exceeded: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"collectés={self.fetched} nouveaux={self.new} repris={self.reprocessed_failed} "
            f"classifiés={self.classified} routeA={self.route_a} routeB={self.route_b} "
            f"filtrés={self.filtered} vers_verif={self.filtered_reviewed} "
            f"échecs_analyse={self.analysis_failed} "
            f"envoyés={self.sent} échecs_envoi={self.send_failed} "
            f"filet_france={self.france_overrides} filet_record={self.milestone_flags} "
            f"tokens_in={self.tokens_in} tokens_out={self.tokens_out}"
        )


def run_cycle(settings: Settings, *, limit: int, dry_run: bool) -> CycleStats:
    stats = CycleStats()
    conn = storage.init_db(settings.db_path)

    stats.reprocessed_failed = storage.reset_failed_to_new(conn)

    sources = fetcher.load_sources(settings.sources_path)
    fetched_items = fetcher.fetch_all(sources, timeout=settings.request_timeout_seconds)
    stats.fetched = len(fetched_items)
    for item in fetched_items:
        if not storage.fingerprint_exists(conn, item.fingerprint):
            storage.insert_new_article(conn, item)
            stats.new += 1

    artist_tiers = analyzer.load_artist_tiers(settings.artist_tiers_path)
    api_keys = [settings.gemini_api_key]
    if settings.gemini_api_key_2:
        api_keys.append(settings.gemini_api_key_2)
    gemini = analyzer.GeminiAnalyzer(
        api_keys=api_keys,
        models=[
            settings.gemini_model,
            settings.gemini_fallback_model,
            settings.gemini_second_fallback_model,
        ],
        artist_tiers=artist_tiers,
        min_seconds_between_calls=settings.gemini_min_seconds_between_calls,
    )

    pending_new = storage.pending(conn, ArticleStatus.NEW, limit=limit)
    for article in pending_new:
        france_flag = analyzer.matches_france_keywords(article, settings.france_keywords)
        if france_flag:
            stats.france_overrides += 1
        milestone_flag = analyzer.matches_viral_milestone(
            article, settings.viral_milestone_keywords, artist_tiers
        )
        if milestone_flag:
            stats.milestone_flags += 1
        try:
            classification, tin1, tout1 = gemini.classify(
                article, france_flag=france_flag, milestone_flag=milestone_flag
            )
        except analyzer.QuotaExceededError:
            logger.warning(
                "Quota Gemini atteint — arrêt du cycle d'analyse, reprise au prochain run."
            )
            stats.quota_exceeded = True
            break
        except analyzer.AnalysisError as exc:
            storage.mark_failed(conn, article.id, str(exc))
            stats.analysis_failed += 1
            stats.errors.append(f"classify({article.url}): {exc}")
            continue

        route = determine_route(classification.category, classification.virality)
        writing = None
        tin2 = tout2 = 0
        if route != Route.IGNORED:
            try:
                writing, tin2, tout2 = gemini.write(article, classification, route)
                tag = determine_tweet_tag(
                    classification.category, classification.importance, classification.virality
                )
                tagged_tweet = f"{TWEET_TAG_LABELS[tag]}\n\n{writing.tweet_draft}"
                if len(tagged_tweet) > 280:  # garde-fou — ne devrait jamais arriver, voir models.py
                    logger.warning(
                        "Tweet taggé au-delà de 280 caractères (%d) pour %s.",
                        len(tagged_tweet),
                        article.url,
                    )
                writing = writing.model_copy(update={"tweet_draft": tagged_tweet})
            except analyzer.QuotaExceededError:
                logger.warning("Quota Gemini atteint pendant la rédaction — arrêt du cycle.")
                stats.quota_exceeded = True
                break
            except analyzer.AnalysisError as exc:
                storage.mark_failed(conn, article.id, str(exc))
                stats.analysis_failed += 1
                stats.errors.append(f"write({article.url}): {exc}")
                continue

        storage.save_analysis(
            conn,
            article.id,
            classification,
            route,
            france_flag,
            writing,
            tin1 + tin2,
            tout1 + tout2,
            analyzer.PROMPT_VERSION,
        )
        stats.classified += 1
        stats.tokens_in += tin1 + tin2
        stats.tokens_out += tout1 + tout2
        if route == Route.A:
            stats.route_a += 1
        elif route == Route.B:
            stats.route_b += 1
        else:
            stats.filtered += 1

    to_send = storage.pending(conn, ArticleStatus.ANALYZED)
    send_index = 0  # numérote uniquement les envois réels, voir notifier.build_info_header
    for article in to_send:
        if dry_run:
            route_label = article.route.value if article.route else "?"
            logger.info("[dry-run] enverrait sur Route %s : %s", route_label, article.title)
            continue
        send_index += 1
        try:
            notifier.notify(
                article,
                url_a=settings.discord_webhook_route_a,
                url_b=settings.discord_webhook_route_b,
                timeout=settings.request_timeout_seconds,
                index=send_index,
            )
            storage.mark_sent(conn, article.id)
            stats.sent += 1
        except notifier.NotificationError as exc:
            storage.mark_failed(conn, article.id, str(exc))
            stats.send_failed += 1
            stats.errors.append(f"send({article.url}): {exc}")

    if settings.discord_webhook_info_a_verifier:
        to_review = storage.pending(conn, ArticleStatus.FILTERED)
        for article in to_review:
            if dry_run:
                logger.info("[dry-run] enverrait vers #info-a-verifier : %s", article.title)
                continue
            try:
                notifier.notify_review(
                    article,
                    url=settings.discord_webhook_info_a_verifier,
                    timeout=settings.request_timeout_seconds,
                )
                storage.mark_filtered_sent(conn, article.id)
                stats.filtered_reviewed += 1
            except notifier.NotificationError as exc:
                storage.mark_failed(conn, article.id, str(exc))
                stats.send_failed += 1
                stats.errors.append(f"review({article.url}): {exc}")

    conn.close()
    return stats


def resend_sent(settings: Settings, *, limit: int | None = None) -> int:
    """Renvoie sur Discord les articles déjà au statut SENT — pour valider un changement de
    mise en forme (embed, message) sans reconsommer de quota Gemini. N'appelle jamais l'IA,
    ne modifie aucun état en base : c'est un outil de vérification, pas une étape du cycle."""
    conn = storage.init_db(settings.db_path)
    articles = storage.pending(conn, ArticleStatus.SENT, limit=limit)
    for index, article in enumerate(articles, start=1):
        notifier.notify(
            article,
            url_a=settings.discord_webhook_route_a,
            url_b=settings.discord_webhook_route_b,
            timeout=settings.request_timeout_seconds,
            index=index,
        )
        route_label = article.route.value if article.route else "?"
        logger.info("Renvoyé : [Route %s] %s", route_label, article.title)
    conn.close()
    return len(articles)
