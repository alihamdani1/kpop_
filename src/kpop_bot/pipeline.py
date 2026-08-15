"""Orchestration (T7) : collecte -> classification -> routage -> rédaction -> diffusion."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kpop_bot import analyzer, fetcher, notifier, scraper, storage
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
    route_concert: int = 0
    filtered: int = 0
    filtered_reviewed: int = 0
    analysis_failed: int = 0
    sent: int = 0
    send_failed: int = 0
    social_visual_content_generated: int = 0
    social_visual_content_generation_failed: int = 0
    scraped_pages: int = 0
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
            f"routeConcert={self.route_concert} "
            f"filtrés={self.filtered} vers_verif={self.filtered_reviewed} "
            f"échecs_analyse={self.analysis_failed} "
            f"envoyés={self.sent} échecs_envoi={self.send_failed} "
            f"pages_scrapees={self.scraped_pages} "
            f"visuel_social_genere={self.social_visual_content_generated} "
            f"visuel_social_echecs_gen={self.social_visual_content_generation_failed} "
            f"filet_france={self.france_overrides} filet_record={self.milestone_flags} "
            f"tokens_in={self.tokens_in} tokens_out={self.tokens_out}"
        )


def _api_keys(settings: Settings) -> list[str]:
    keys = [settings.gemini_api_key]
    if settings.gemini_api_key_2:
        keys.append(settings.gemini_api_key_2)
    return keys


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
    models_chain = [
        settings.gemini_model,
        settings.gemini_fallback_model,
        settings.gemini_second_fallback_model,
    ]
    gemini = analyzer.GeminiAnalyzer(
        api_keys=_api_keys(settings),
        models=models_chain,
        artist_tiers=artist_tiers,
        min_seconds_between_calls=settings.gemini_min_seconds_between_calls,
    )

    pending_new = storage.pending(conn, ArticleStatus.NEW, limit=limit)
    for article in pending_new:
        # Scraping best-effort de la page article (T18) — après dédup, comme le veut le
        # principe du pipeline (coût proportionnel aux vraies nouveautés). Ne lève jamais :
        # None si la page n'apporte rien d'exploitable, l'article continue avec le seul
        # extrait RSS, exactement comme avant l'introduction de ce module.
        scraped = scraper.fetch_article_page(article.url, timeout=settings.request_timeout_seconds)
        page_text = scraped.text if scraped else None
        if scraped is not None:
            stats.scraped_pages += 1

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
                article, france_flag=france_flag, milestone_flag=milestone_flag, page_text=page_text
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
                writing, tin2, tout2 = gemini.write(
                    article, classification, route, page_text=page_text
                )
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

        social_visual_result = None
        tin3 = tout3 = 0
        if route != Route.IGNORED and settings.discord_webhook_social:
            # Échec non bloquant : le tweet/résumé a déjà réussi, un raté ici ne doit ni faire
            # échouer l'article ni arrêter le cycle — c'est un bonus pour social_pipeline.py,
            # pas un pré-requis de diffusion (même principe que pour l'ex-post Instagram).
            # Toutes les routes retenues sont concernées (A, B, CONCERT) : le visuel social
            # couvre tous les articles envoyés, pas seulement Route A/CONCERT.
            try:
                social_visual_result, tin3, tout3 = gemini.write_social_visual(
                    article, classification, page_text=page_text
                )
                stats.social_visual_content_generated += 1
            except analyzer.QuotaExceededError:
                logger.warning(
                    "Quota Gemini (visuel social) atteint pour %s — contenu ignoré pour cet "
                    "article, cycle poursuivi.",
                    article.url,
                )
                stats.social_visual_content_generation_failed += 1
            except analyzer.AnalysisError as exc:
                logger.warning(
                    "Échec de génération du contenu visuel social pour %s : %s", article.url, exc
                )
                stats.social_visual_content_generation_failed += 1

        storage.save_analysis(
            conn,
            article.id,
            classification,
            route,
            france_flag,
            writing,
            tin1 + tin2 + tin3,
            tout1 + tout2 + tout3,
            analyzer.PROMPT_VERSION,
            social_visual=social_visual_result,
            image_url=scraped.main_image_url if scraped else None,
            extra_image_urls=scraped.extra_image_urls if scraped else None,
        )
        stats.classified += 1
        stats.tokens_in += tin1 + tin2 + tin3
        stats.tokens_out += tout1 + tout2 + tout3
        if route == Route.A:
            stats.route_a += 1
        elif route == Route.B:
            stats.route_b += 1
        elif route == Route.CONCERT:
            stats.route_concert += 1
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
                url_concert=settings.discord_webhook_concert,
                timeout=settings.request_timeout_seconds,
                index=send_index,
            )
            storage.mark_sent(conn, article.id)
            stats.sent += 1
        except notifier.NotificationError as exc:
            storage.mark_failed(conn, article.id, str(exc))
            stats.send_failed += 1
            stats.errors.append(f"send({article.url}): {exc}")
            continue

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
            url_concert=settings.discord_webhook_concert,
            timeout=settings.request_timeout_seconds,
            index=index,
        )
        route_label = article.route.value if article.route else "?"
        logger.info("Renvoyé : [Route %s] %s", route_label, article.title)
    conn.close()
    return len(articles)
