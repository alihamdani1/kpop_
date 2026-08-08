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
    route_concert: int = 0
    filtered: int = 0
    filtered_reviewed: int = 0
    analysis_failed: int = 0
    sent: int = 0
    send_failed: int = 0
    tiktok_generated: int = 0
    tiktok_generation_failed: int = 0
    tiktok_sent: int = 0
    tiktok_send_failed: int = 0
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
            f"tiktok_generes={self.tiktok_generated} tiktok_echecs_gen="
            f"{self.tiktok_generation_failed} tiktok_envoyes={self.tiktok_sent} "
            f"tiktok_echecs_envoi={self.tiktok_send_failed} "
            f"filet_france={self.france_overrides} filet_record={self.milestone_flags} "
            f"tokens_in={self.tokens_in} tokens_out={self.tokens_out}"
        )


def _api_keys(settings: Settings) -> list[str]:
    keys = [settings.gemini_api_key]
    if settings.gemini_api_key_2:
        keys.append(settings.gemini_api_key_2)
    return keys


def _tiktok_api_keys(settings: Settings) -> list[str]:
    """Mêmes clés que l'analyseur principal, mais clé 2 en priorité si elle est présente (voir
    TODO.md T14) — la génération de scripts TikTok (Route A, volume plus faible) utilise ainsi
    en priorité un pool de quota distinct de classify()/write(), plutôt que de n'intervenir
    qu'en dernier recours comme le prévoit la chaîne de secours par défaut."""
    return list(reversed(_api_keys(settings)))


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
    # Instance séparée, clé 2 en priorité — voir _tiktok_api_keys. None si le salon dédié
    # n'est pas configuré : aucune instance construite, aucun appel Gemini supplémentaire.
    gemini_tiktok = (
        analyzer.GeminiAnalyzer(
            api_keys=_tiktok_api_keys(settings),
            models=models_chain,
            artist_tiers=artist_tiers,
            min_seconds_between_calls=settings.gemini_min_seconds_between_calls,
        )
        if settings.discord_webhook_tiktok
        else None
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

        tiktok_result = None
        tin3 = tout3 = 0
        if route in (Route.A, Route.CONCERT) and gemini_tiktok is not None:
            # Échec non bloquant : le tweet/résumé vidéo a déjà réussi, un script TikTok raté
            # ne doit ni faire échouer l'article ni arrêter le cycle — c'est un bonus, pas un
            # pré-requis de diffusion (voir T14).
            try:
                tiktok_result, tin3, tout3 = gemini_tiktok.write_tiktok_script(
                    article, classification
                )
                stats.tiktok_generated += 1
            except analyzer.QuotaExceededError:
                logger.warning(
                    "Quota Gemini (script TikTok) atteint pour %s — script ignoré pour cet "
                    "article, cycle poursuivi.",
                    article.url,
                )
                stats.tiktok_generation_failed += 1
            except analyzer.AnalysisError as exc:
                logger.warning(
                    "Échec de génération du script TikTok pour %s : %s", article.url, exc
                )
                stats.tiktok_generation_failed += 1

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
            tiktok=tiktok_result,
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
    tiktok_send_index = 0  # compteur séparé — voir notifier.notify_tiktok
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

        # Envoi TikTok best-effort, uniquement après un envoi principal réussi ci-dessus — un
        # échec ici ne doit jamais faire revenir l'article en arrière (voir T14).
        if (
            settings.discord_webhook_tiktok
            and article.route in (Route.A, Route.CONCERT)
            and article.tiktok_script_body
        ):
            tiktok_send_index += 1
            try:
                notifier.notify_tiktok(
                    article,
                    url=settings.discord_webhook_tiktok,
                    timeout=settings.request_timeout_seconds,
                    index=tiktok_send_index,
                )
                stats.tiktok_sent += 1
            except notifier.NotificationError as exc:
                stats.tiktok_send_failed += 1
                stats.errors.append(f"tiktok_send({article.url}): {exc}")

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
