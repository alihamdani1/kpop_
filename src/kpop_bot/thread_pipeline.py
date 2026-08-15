"""Orchestration des threads Twitter quotidiens (T15) : réapprovisionnement du backlog de
Topics, sélection quotidienne (picker Discord), résolution (réaction humaine -> génération ->
envoi). Séparé de `pipeline.py` : cadence et entités totalement différentes du cycle articles
existant, aucun risque de régression sur `run_cycle` déjà en production."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kpop_bot import analyzer, discord_reactions, media_library, notifier, storage
from kpop_bot.models import ThreadAngle, ThreadConcept, ThreadTopicIdea
from kpop_bot.settings import Settings

logger = logging.getLogger(__name__)

_OPTION_EMOJIS = ["🇦", "🇧", "🇨"]
# Photos alternatives envoyées en plus du thread (T16bis) — pour substituer une image sur un
# tweet précis avant publication manuelle sur X.
_EXTRA_IMAGE_COUNT = 5


def _thread_api_keys(settings: Settings) -> list[str]:
    """Clé 2 en priorité pour les threads (demande explicite, pas le comportement par défaut de
    la chaîne de secours) — liste de clés inversée plutôt que de modifier
    `_generate_with_fallback`, qui reste inchangée et partagée avec le pipeline articles. Repli
    sur la clé 1 seulement si la clé 2 épuise toute sa chaîne de modèles (ou si elle est
    absente, auquel cas cette inversion est un no-op)."""
    keys = [settings.gemini_api_key]
    if settings.gemini_api_key_2:
        keys.append(settings.gemini_api_key_2)
    return list(reversed(keys))


def _gemini(settings: Settings) -> analyzer.GeminiAnalyzer:
    """Chaîne de modèles dédiée aux threads (T15ter) — distincte de gemini_model/*_fallback_model
    (réservés au pipeline articles, ~225 appels/jour). Volume threads négligeable (1-2
    appels/jour) : marge pour un modèle de meilleure qualité rédactionnelle sans impact sur le
    quota qui compte vraiment."""
    artist_tiers = analyzer.load_artist_tiers(settings.artist_tiers_path)
    return analyzer.GeminiAnalyzer(
        api_keys=_thread_api_keys(settings),
        models=[
            settings.thread_gemini_model,
            settings.thread_gemini_fallback_model,
            settings.thread_gemini_second_fallback_model,
        ],
        artist_tiers=artist_tiers,
        min_seconds_between_calls=settings.gemini_min_seconds_between_calls,
    )


def _load_viral_groups(settings: Settings) -> list[str]:
    """Groupes viraux (T15bis) — réutilise `config/artist_tiers.yaml`, déjà curé et maintenu
    pour le reste du pipeline, plutôt qu'une liste redondante dédiée aux threads."""
    tiers = analyzer.load_artist_tiers(settings.artist_tiers_path)
    groups: list[str] = []
    for tier_name in sorted(tiers):
        for group in tiers[tier_name]:
            if group not in groups:
                groups.append(group)
    return groups


_THEME_CAP_RATIO = 0.25  # part maximale d'un même thème dans un lot (T15quater)


def _candidate_pairs(
    groups: list[str],
    concepts: list[ThreadConcept],
    used_pairs: set[tuple[str, str]],
    *,
    limit: int,
) -> list[tuple[str, ThreadConcept]]:
    """Croisement déterministe groupe × concept (T15bis/T15quater) — exclut les paires déjà
    présentes en backlog. Exclusion dure et définitive (pas une fenêtre glissante) : un couple
    groupe/concept curé est fini et identifiable exactement, contrairement à l'ancienne idéation
    libre où deux sujets reformulés différemment pouvaient être des quasi-doublons indétectables
    (voir `storage.used_group_concept_pairs`).

    Deux garde-fous ajoutés en T15quater, suite à un premier lot réel 100 % concentré sur un
    seul concept :
    - **Rotation pondérée** : un concept `weight=2.0` (voir `config/thread_concepts.yaml`, ex.
      piliers Tops/Dramas/Storytelling) repasse deux fois plus souvent qu'un concept
      `weight=1.0` dans l'ordre de tirage — approxime une priorité éditoriale sans tirage
      aléatoire (le croisement reste 100 % déterministe, principe fondateur de T15bis). Les
      concepts sont d'abord triés par poids décroissant (tri stable) avant de construire la
      rotation : sinon, deux concepts qui partagent un même thème (ex. `top_ventes_streams` et
      `record_marquant`, tous deux RECORD_ANECDOTE) verraient le plafond du thème arbitré par
      leur position dans `config/thread_concepts.yaml` plutôt que par leur poids réel — un
      concept à poids fort ajouté en fin de fichier perdrait systématiquement face à un concept
      à poids normal déjà présent plus tôt dans la liste.
    - **Plafond par thème** (~25 % du lot) : empêche qu'un thème unique monopolise un lot
      entier. Si le plafond empêche de remplir le lot faute d'alternative, une passe assouplie
      complète sans plafond plutôt que de laisser un lot anormalement court — mieux vaut un lot
      un peu déséquilibré qu'un lot incomplet."""
    if not groups or not concepts:
        return []

    theme_cap = max(1, round(limit * _THEME_CAP_RATIO))
    # Poids décroissant d'abord (tri stable — égalité de poids préserve l'ordre du fichier).
    ordered_concepts = sorted(concepts, key=lambda c: c.weight, reverse=True)
    # Chaque concept apparaît dans la rotation un nombre de fois proportionnel à son poids.
    rotation: list[ThreadConcept] = []
    for concept in ordered_concepts:
        rotation.extend([concept] * max(1, round(concept.weight)))

    group_cursor: dict[str, int] = dict.fromkeys((c.id for c in concepts), 0)

    def _next_group(concept: ThreadConcept) -> str | None:
        cursor = group_cursor[concept.id]
        while cursor < len(groups):
            group = groups[cursor]
            cursor += 1
            group_cursor[concept.id] = cursor
            if (group, concept.id) not in used_pairs:
                return group
        group_cursor[concept.id] = cursor
        return None

    candidates: list[tuple[str, ThreadConcept]] = []
    theme_counts: dict[str, int] = {}
    exhausted: set[str] = set()
    stalled_rounds = 0
    round_index = 0
    while (
        len(candidates) < limit
        and len(exhausted) < len(ordered_concepts)
        and stalled_rounds <= len(rotation)
    ):
        concept = rotation[round_index % len(rotation)]
        round_index += 1
        if concept.id in exhausted or theme_counts.get(concept.theme.value, 0) >= theme_cap:
            stalled_rounds += 1
            continue
        group = _next_group(concept)
        if group is None:
            exhausted.add(concept.id)
            stalled_rounds += 1
            continue
        candidates.append((group, concept))
        theme_counts[concept.theme.value] = theme_counts.get(concept.theme.value, 0) + 1
        stalled_rounds = 0

    # Passe assouplie : le plafond par thème a pu empêcher de remplir le lot — complète sans
    # plafond plutôt que de laisser un lot anormalement court.
    if len(candidates) < limit:
        chosen = {(group, concept.id) for group, concept in candidates}
        for concept in ordered_concepts:
            while len(candidates) < limit:
                group = _next_group(concept)
                if group is None:
                    break
                if (group, concept.id) not in chosen:
                    candidates.append((group, concept))
                    chosen.add((group, concept.id))

    return candidates[:limit]


def run_thread_replenish(settings: Settings, *, dry_run: bool = False) -> int:
    """Génère un nouveau lot de Topics si le backlog descend sous le seuil configuré (T15bis) —
    croisement déterministe groupe × concept en code (`_candidate_pairs`), un seul appel Gemini
    pour rédiger le titre/premise de chaque paire déjà choisie — jamais pour choisir la paire
    elle-même (voir TODO.md T15bis). Retourne le nombre de topics insérés (0 si le backlog était
    déjà suffisant, ou si aucune paire groupe/concept n'est plus disponible)."""
    conn = storage.init_db(settings.db_path)
    try:
        count = storage.backlog_topic_count(conn)
        if count >= settings.thread_topic_backlog_min:
            logger.info("Backlog de Topics suffisant (%d) — aucun réapprovisionnement.", count)
            return 0

        groups = _load_viral_groups(settings)
        concepts = analyzer.load_thread_concepts(settings.thread_concepts_path)
        used_pairs = storage.used_group_concept_pairs(conn)
        candidate_pairs = _candidate_pairs(
            groups, concepts, used_pairs, limit=settings.thread_ideation_batch_size
        )
        if not candidate_pairs:
            logger.warning(
                "Plus aucune paire groupe/concept disponible — enrichis "
                "config/thread_concepts.yaml ou config/artist_tiers.yaml."
            )
            return 0

        if dry_run:
            for group, concept in candidate_pairs:
                logger.info("[dry-run] proposerait la paire : %s / %s", group, concept.label)
            return 0

        gemini = _gemini(settings)
        result, tokens_in, tokens_out = gemini.ideate_thread_topics(candidate_pairs=candidate_pairs)

        expected_indices = set(range(len(candidate_pairs)))
        received_indices = {item.pair_index for item in result.topics}
        if received_indices != expected_indices:
            logger.warning(
                "Réponse Gemini incohérente pour l'idéation (pair_index attendus %s, reçus "
                "%s) — lot ignoré, réessai au prochain cycle.",
                sorted(expected_indices),
                sorted(received_indices),
            )
            return 0

        writing_by_index = {item.pair_index: item for item in result.topics}
        ideas = [
            ThreadTopicIdea(
                group_name=group,
                theme=concept.theme,
                title=writing_by_index[index].title,
                premise=writing_by_index[index].premise,
                concept_id=concept.id,
            )
            for index, (group, concept) in enumerate(candidate_pairs)
        ]
        ids = storage.insert_topic_ideas(conn, ideas)
        logger.info(
            "Réapprovisionnement : %d nouveaux topics insérés (tokens_in=%d, tokens_out=%d).",
            len(ids),
            tokens_in,
            tokens_out,
        )
        return len(ids)
    finally:
        conn.close()


def run_thread_select(settings: Settings, *, dry_run: bool = False) -> bool:
    """Propose 3 (topic, angle) jamais consommés ensemble, sous forme d'embed + réactions.
    Aucun appel Gemini ici — uniquement de la logique de sélection sur le backlog existant.
    Ignore le cycle si une sélection PENDING existe déjà (évite d'empiler plusieurs pickers)."""
    conn = storage.init_db(settings.db_path)
    try:
        expired = storage.expire_stale_selections(
            conn, ttl_hours=settings.thread_selection_ttl_hours
        )
        if expired:
            logger.info("%d sélection(s) expirée(s) (aucune réaction reçue à temps).", expired)

        if storage.pending_selections(conn):
            logger.info("Une sélection est déjà en attente de réaction — aucune nouvelle proposée.")
            return False

        candidates = storage.select_picker_candidates(conn)
        if len(candidates) < 3:
            logger.warning(
                "Backlog insuffisant pour proposer 3 options diversifiées (%d disponibles) — "
                "cycle ignoré, lancez thread-replenish.",
                len(candidates),
            )
            return False

        if dry_run:
            for topic, angle in candidates:
                logger.info(
                    "[dry-run] proposerait : %s (%s / %s)",
                    topic.title,
                    topic.group_name,
                    angle.value,
                )
            return True

        if not (
            settings.discord_webhook_thread
            and settings.discord_bot_token
            and settings.discord_thread_channel_id
        ):
            logger.warning(
                "discord_webhook_thread / discord_bot_token / discord_thread_channel_id non "
                "configurés — impossible de poster le picker."
            )
            return False

        message_id = notifier.notify_thread_selection(
            candidates,
            url=settings.discord_webhook_thread,
            timeout=settings.request_timeout_seconds,
        )
        discord_reactions.seed_reactions(
            channel_id=settings.discord_thread_channel_id,
            message_id=message_id,
            emojis=_OPTION_EMOJIS,
            bot_token=settings.discord_bot_token,
            timeout=settings.request_timeout_seconds,
        )
        (topic_a, angle_a), (topic_b, angle_b), (topic_c, angle_c) = candidates
        storage.insert_selection(
            conn,
            discord_message_id=message_id,
            option_a=(topic_a.id, angle_a),
            option_b=(topic_b.id, angle_b),
            option_c=(topic_c.id, angle_c),
        )
        storage.touch_offered_topics(conn, [topic.id for topic, _ in candidates])
        logger.info("Picker publié (message %s), 3 options proposées.", message_id)
        return True
    finally:
        conn.close()


@dataclass
class ThreadResolveStats:
    resolved: int = 0
    still_pending: int = 0
    sent: int = 0
    send_failed: int = 0
    generation_failed: int = 0
    quota_exceeded: bool = False


def run_thread_resolve(settings: Settings) -> ThreadResolveStats:
    """Lit les réactions des sélections PENDING ; génère et enregistre le thread dès qu'une
    réaction humaine est détectée (pass 1), puis tente d'envoyer tout thread DRAFT en attente
    (pass 2 — séparé de la génération pour pouvoir réessayer un envoi Discord raté sans
    reconsommer de quota Gemini)."""
    stats = ThreadResolveStats()
    conn = storage.init_db(settings.db_path)
    try:
        if not (settings.discord_bot_token and settings.discord_thread_channel_id):
            return stats

        pending = storage.pending_selections(conn)
        if pending:
            bot_user_id = discord_reactions.get_bot_user_id(
                bot_token=settings.discord_bot_token, timeout=settings.request_timeout_seconds
            )
            gemini = _gemini(settings)

            for selection in pending:
                options = [
                    (selection.option_a_topic_id, selection.option_a_angle),
                    (selection.option_b_topic_id, selection.option_b_angle),
                    (selection.option_c_topic_id, selection.option_c_angle),
                ]
                chosen: tuple[int, ThreadAngle] | None = None
                for emoji, (topic_id, angle) in zip(_OPTION_EMOJIS, options, strict=True):
                    reactor = discord_reactions.get_human_reactor(
                        channel_id=settings.discord_thread_channel_id,
                        message_id=selection.discord_message_id,
                        emoji=emoji,
                        bot_token=settings.discord_bot_token,
                        bot_user_id=bot_user_id,
                        timeout=settings.request_timeout_seconds,
                    )
                    if reactor is not None:
                        chosen = (topic_id, angle)
                        break

                if chosen is None:
                    stats.still_pending += 1
                    continue

                topic_id, angle = chosen
                if angle not in storage.available_angles(conn, topic_id):
                    # Reprise après un crash précédent entre insert_thread et resolve_selection :
                    # le thread existe déjà, on se contente de finaliser l'état de la sélection,
                    # sans rappeler Gemini.
                    storage.resolve_selection(conn, selection.id, topic_id=topic_id, angle=angle)
                    stats.resolved += 1
                    continue

                topic = storage.get_topic(conn, topic_id)
                recent_hooks = storage.recent_hook_labels(conn)
                try:
                    writing, hook_label, tokens_in, tokens_out = gemini.write_thread(
                        topic, angle, recent_hook_labels=recent_hooks
                    )
                except analyzer.QuotaExceededError:
                    logger.warning(
                        "Quota Gemini atteint pendant l'écriture du thread — arrêt du cycle."
                    )
                    stats.quota_exceeded = True
                    break
                except analyzer.AnalysisError as exc:
                    logger.warning(
                        "Échec de génération du thread pour le topic %d : %s", topic_id, exc
                    )
                    stats.generation_failed += 1
                    continue

                storage.insert_thread(
                    conn,
                    selection_id=selection.id,
                    topic_id=topic_id,
                    angle=angle,
                    hook_label=hook_label,
                    writing=writing,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    prompt_version=analyzer.PROMPT_VERSION,
                )
                storage.resolve_selection(conn, selection.id, topic_id=topic_id, angle=angle)
                stats.resolved += 1

        if settings.discord_webhook_thread:
            for thread in storage.pending_threads(conn):
                topic = storage.get_topic(conn, thread.topic_id)
                image_paths = media_library.select_images_for_thread(
                    settings.media_library_path,
                    group_name=topic.group_name,
                    concept_id=topic.concept_id,
                    count=len(thread.tweets),
                )
                extra_image_paths = media_library.select_extra_images(
                    settings.media_library_path,
                    group_name=topic.group_name,
                    concept_id=topic.concept_id,
                    exclude=image_paths,
                    count=_EXTRA_IMAGE_COUNT,
                )
                try:
                    notifier.notify_thread(
                        thread,
                        topic,
                        url=settings.discord_webhook_thread,
                        timeout=settings.request_timeout_seconds,
                        image_paths=image_paths,
                        extra_image_paths=extra_image_paths,
                    )
                    storage.mark_thread_sent(conn, thread.id)
                    stats.sent += 1
                except notifier.NotificationError as exc:
                    logger.warning("Échec d'envoi du thread %d : %s", thread.id, exc)
                    stats.send_failed += 1

        return stats
    finally:
        conn.close()
