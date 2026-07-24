"""Orchestration (T7) : collecte -> classification -> routage -> rédaction -> diffusion."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kpop_bot import analyzer, fetcher, notifier, storage
from kpop_bot.models import ArticleStatus, Route, determine_route
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
    analysis_failed: int = 0
    sent: int = 0
    send_failed: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    france_overrides: int = 0
    quota_exceeded: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"collectés={self.fetched} nouveaux={self.new} repris={self.reprocessed_failed} "
            f"classifiés={self.classified} routeA={self.route_a} routeB={self.route_b} "
            f"filtrés={self.filtered} échecs_analyse={self.analysis_failed} "
            f"envoyés={self.sent} échecs_envoi={self.send_failed} "
            f"filet_france={self.france_overrides} "
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
    gemini = analyzer.GeminiAnalyzer(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        artist_tiers=artist_tiers,
    )

    pending_new = storage.pending(conn, ArticleStatus.NEW, limit=limit)
    for article in pending_new:
        france_flag = analyzer.matches_france_keywords(article, settings.france_keywords)
        if france_flag:
            stats.france_overrides += 1
        try:
            classification, tin1, tout1 = gemini.classify(article, france_flag=france_flag)
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
    for article in to_send:
        if dry_run:
            route_label = article.route.value if article.route else "?"
            logger.info("[dry-run] enverrait sur Route %s : %s", route_label, article.title)
            continue
        try:
            notifier.notify(
                article,
                url_a=settings.discord_webhook_route_a,
                url_b=settings.discord_webhook_route_b,
                timeout=settings.request_timeout_seconds,
            )
            storage.mark_sent(conn, article.id)
            stats.sent += 1
        except notifier.NotificationError as exc:
            storage.mark_failed(conn, article.id, str(exc))
            stats.send_failed += 1
            stats.errors.append(f"send({article.url}): {exc}")

    conn.close()
    return stats
