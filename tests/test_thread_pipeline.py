from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import respx

from kpop_bot import analyzer, storage
from kpop_bot.models import (
    ThreadAngle,
    ThreadConcept,
    ThreadTheme,
    ThreadTopicIdea,
    ThreadTopicWriting,
    ThreadWritingResult,
    TopicIdeationResult,
)
from kpop_bot.settings import Settings
from kpop_bot.thread_pipeline import (
    _candidate_pairs,
    _gemini,
    _load_viral_groups,
    run_thread_replenish,
    run_thread_resolve,
    run_thread_select,
)

_BASE = "https://discord.com/api/v10"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Tous les champs optionnels explicités (même à None) pour rester isolé d'un vrai `.env`
    local — même précaution que dans test_pipeline.py (voir sa docstring)."""
    return Settings(
        gemini_api_key="test-key",
        gemini_api_key_2=None,
        discord_webhook_route_a="https://discord.com/api/webhooks/fake/a",
        discord_webhook_route_b="https://discord.com/api/webhooks/fake/b",
        discord_webhook_info_a_verifier=None,
        discord_webhook_tiktok=None,
        discord_bot_token=None,
        discord_thread_channel_id=None,
        discord_webhook_thread=None,
        thread_topic_backlog_min=5,
        thread_ideation_batch_size=3,
        thread_selection_ttl_hours=24.0,
        db_path=tmp_path / "test.db",
    )


def test_gemini_utilise_la_chaine_de_modeles_dediee_aux_threads(settings):
    """T15ter : les threads doivent utiliser thread_gemini_model/*_fallback_model, jamais
    gemini_model/*_fallback_model (réservés au pipeline articles à fort volume)."""
    settings = settings.model_copy(
        update={
            "thread_gemini_model": "gemini-thread-1",
            "thread_gemini_fallback_model": "gemini-thread-2",
            "thread_gemini_second_fallback_model": "gemini-thread-3",
        }
    )
    instance = _gemini(settings)
    assert instance._models == ["gemini-thread-1", "gemini-thread-2", "gemini-thread-3"]


def _idea(**overrides) -> ThreadTopicIdea:
    defaults = dict(
        group_name="Groupe X",
        theme=ThreadTheme.ANALYSE_COMEBACK,
        title="Sujet",
        premise="Promesse.",
    )
    defaults.update(overrides)
    return ThreadTopicIdea(**defaults)


def _seed_topics(settings: Settings, n: int = 4) -> list[int]:
    conn = storage.init_db(settings.db_path)
    themes = list(ThreadTheme)
    ids = storage.insert_topic_ideas(
        conn,
        [
            _idea(group_name=f"Groupe {i}", theme=themes[i % len(themes)], title=f"Sujet {i}")
            for i in range(n)
        ],
    )
    conn.close()
    return ids


def _write_tiny_config(tmp_path: Path) -> tuple[Path, Path]:
    """Config groupes/concepts minimale et contrôlée (T15bis) — pour prédire exactement le
    croisement déterministe produit par `_candidate_pairs`, plutôt que de dépendre de la vraie
    config du projet (qui grossit avec le temps)."""
    tiers_path = tmp_path / "artist_tiers.yaml"
    tiers_path.write_text("tier_1:\n  - Groupe A\n  - Groupe B\n", encoding="utf-8")
    concepts_path = tmp_path / "thread_concepts.yaml"
    concepts_path.write_text(
        "concepts:\n"
        "  - id: concept_1\n"
        "    theme: ANALYSE_COMEBACK\n"
        "    label: Concept 1\n"
        "    brief: Brief 1.\n"
        "  - id: concept_2\n"
        "    theme: RECAP_SCANDALE\n"
        "    label: Concept 2\n"
        "    brief: Brief 2.\n",
        encoding="utf-8",
    )
    return tiers_path, concepts_path


# --- run_thread_replenish ---


def test_run_thread_replenish_no_op_si_backlog_suffisant(settings, monkeypatch):
    settings = settings.model_copy(update={"thread_topic_backlog_min": 2})
    _seed_topics(settings, n=2)

    def _fail_if_called(self, **kwargs):
        pytest.fail("ideate_thread_topics ne doit pas être appelé si le backlog est suffisant")

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "ideate_thread_topics", _fail_if_called)
    assert run_thread_replenish(settings) == 0


def test_run_thread_replenish_dry_run_n_appelle_pas_gemini(settings, monkeypatch):
    settings = settings.model_copy(update={"thread_topic_backlog_min": 5})
    _seed_topics(settings, n=1)

    def _fail_if_called(self, **kwargs):
        pytest.fail("ideate_thread_topics ne doit pas être appelé en dry-run")

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "ideate_thread_topics", _fail_if_called)
    assert run_thread_replenish(settings, dry_run=True) == 0

    conn = storage.init_db(settings.db_path)
    assert storage.backlog_topic_count(conn) == 1  # inchangé, aucune insertion
    conn.close()


def test_run_thread_replenish_genere_un_lot_si_backlog_bas(settings, monkeypatch, tmp_path):
    tiers_path, concepts_path = _write_tiny_config(tmp_path)
    settings = settings.model_copy(
        update={
            "thread_topic_backlog_min": 5,
            "thread_ideation_batch_size": 2,
            "artist_tiers_path": tiers_path,
            "thread_concepts_path": concepts_path,
        }
    )
    _seed_topics(settings, n=1)  # topic historique (concept_id=None) — n'entrave pas le croisement

    # Lot de 2, cap par thème = max(1, round(2*0.25)) = 1 — les 2 concepts (thèmes différents,
    # poids égal) obtiennent chacun 1 place dès la passe normale, sans passe assouplie.
    # 0=(Groupe A, concept_1), 1=(Groupe A, concept_2) — voir _candidate_pairs.
    writing_result = TopicIdeationResult(
        topics=[
            ThreadTopicWriting(pair_index=i, title=f"Titre {i}", premise=f"Promesse {i}.")
            for i in range(2)
        ]
    )
    captured: dict = {}

    def _fake_ideate(self, *, candidate_pairs):
        captured["pairs"] = candidate_pairs
        return writing_result, 50, 20

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "ideate_thread_topics", _fake_ideate)
    inserted = run_thread_replenish(settings)
    assert inserted == 2

    assert [group for group, _ in captured["pairs"]] == ["Groupe A", "Groupe A"]
    assert [concept.id for _, concept in captured["pairs"]] == ["concept_1", "concept_2"]

    conn = storage.init_db(settings.db_path)
    assert storage.backlog_topic_count(conn) == 3  # 1 historique + 2 nouveaux
    rows = {
        row["title"]: row["concept_id"]
        for row in conn.execute("SELECT title, concept_id FROM thread_topics")
    }
    assert rows["Titre 0"] == "concept_1"
    assert rows["Titre 1"] == "concept_2"
    conn.close()


# --- T15quater : rotation pondérée + plafond par thème (fonctions pures, aucun réseau/base) ---


def test_candidate_pairs_priorise_poids_fort_meme_partageant_un_theme():
    """Régression : sans tri par poids, un concept `weight=2.0` listé APRÈS un concept
    `weight=1.0` du même thème dans la config perdait systématiquement le plafond du thème face
    à lui (biais d'ordre de fichier, pas de poids réel) — observé en cherchant à reproduire le
    lot 100% RIVALITE_COMPARAISON vu en conditions réelles."""
    concepts = [
        ThreadConcept(id="normal", theme=ThreadTheme.RECORD_ANECDOTE, label="Normal", brief="B"),
        ThreadConcept(
            id="fort", theme=ThreadTheme.RECORD_ANECDOTE, label="Fort", brief="B", weight=2.0
        ),
    ]
    groups = ["Groupe A", "Groupe B", "Groupe C"]
    pairs = _candidate_pairs(groups, concepts, set(), limit=3)  # cap = round(3*0.25) = 1
    assert pairs[0][1].id == "fort"


def test_candidate_pairs_respecte_le_plafond_par_theme_en_passe_normale():
    """Aucun thème ne dépasse ~25% du lot tant que d'autres thèmes ont de la place — corrige le
    lot réel observé 100% RIVALITE_COMPARAISON."""
    concepts = [
        ThreadConcept(id="c1", theme=ThreadTheme.RECORD_ANECDOTE, label="C1", brief="B"),
        ThreadConcept(id="c2", theme=ThreadTheme.CULTURE_FANS, label="C2", brief="B"),
        ThreadConcept(id="c3", theme=ThreadTheme.MYTHE_VS_REALITE, label="C3", brief="B"),
        ThreadConcept(id="c4", theme=ThreadTheme.CONNEXION_FRANCE, label="C4", brief="B"),
    ]
    groups = [f"Groupe {i}" for i in range(10)]
    pairs = _candidate_pairs(groups, concepts, set(), limit=8)  # cap = round(8*0.25) = 2
    theme_counts: dict[str, int] = {}
    for _, concept in pairs:
        theme_counts[concept.theme.value] = theme_counts.get(concept.theme.value, 0) + 1
    assert all(n <= 2 for n in theme_counts.values())
    assert len(pairs) == 8


def test_candidate_pairs_passe_assouplie_complete_le_lot_si_plafond_bloque():
    """Si le plafond par thème empêche de remplir le lot faute d'alternative (un seul thème
    disponible), une passe assouplie complète sans plafond plutôt que de laisser un lot
    anormalement court."""
    concepts = [ThreadConcept(id="c1", theme=ThreadTheme.RECORD_ANECDOTE, label="C1", brief="B")]
    groups = ["Groupe A", "Groupe B", "Groupe C", "Groupe D"]
    pairs = _candidate_pairs(groups, concepts, set(), limit=4)  # cap = round(4*0.25) = 1
    assert len(pairs) == 4  # dépasse le plafond théorique (1), faute d'alternative


def test_run_thread_replenish_ignore_le_lot_si_pair_index_incoherents(
    settings, monkeypatch, tmp_path
):
    """L'IA ne doit jamais pouvoir dévier des paires imposées — un pair_index hors bornes ou
    incomplet doit faire ignorer tout le lot plutôt que d'insérer un topic mal rattaché."""
    tiers_path, concepts_path = _write_tiny_config(tmp_path)
    settings = settings.model_copy(
        update={
            "thread_topic_backlog_min": 5,
            "thread_ideation_batch_size": 2,
            "artist_tiers_path": tiers_path,
            "thread_concepts_path": concepts_path,
        }
    )
    bad_result = TopicIdeationResult(
        topics=[ThreadTopicWriting(pair_index=99, title="X", premise="Y.")]
    )
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "ideate_thread_topics",
        lambda self, *, candidate_pairs: (bad_result, 10, 5),
    )
    inserted = run_thread_replenish(settings)
    assert inserted == 0
    conn = storage.init_db(settings.db_path)
    assert storage.backlog_topic_count(conn) == 0
    conn.close()


def test_run_thread_replenish_dry_run_journalise_les_paires(
    settings, monkeypatch, tmp_path, caplog
):
    tiers_path, concepts_path = _write_tiny_config(tmp_path)
    settings = settings.model_copy(
        update={
            "thread_topic_backlog_min": 5,
            "artist_tiers_path": tiers_path,
            "thread_concepts_path": concepts_path,
        }
    )

    def _fail_if_called(self, **kwargs):
        pytest.fail("ideate_thread_topics ne doit pas être appelé en dry-run")

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "ideate_thread_topics", _fail_if_called)
    with caplog.at_level("INFO"):
        assert run_thread_replenish(settings, dry_run=True) == 0
    assert any("Groupe A" in record.message for record in caplog.records)


# --- Croisement déterministe (fonctions pures, aucun réseau/base) ---


def test_candidate_pairs_exclut_les_paires_deja_utilisees():
    concepts = [
        ThreadConcept(id="c1", theme=ThreadTheme.ANALYSE_COMEBACK, label="C1", brief="B1"),
        ThreadConcept(id="c2", theme=ThreadTheme.RECAP_SCANDALE, label="C2", brief="B2"),
    ]
    used = {("Groupe A", "c1")}
    pairs = _candidate_pairs(["Groupe A", "Groupe B"], concepts, used, limit=10)
    assert ("Groupe A", concepts[0]) not in pairs
    assert len(pairs) == 3  # (A,c2), (B,c1), (B,c2)


def test_candidate_pairs_respecte_la_limite():
    concepts = [
        ThreadConcept(id=f"c{i}", theme=ThreadTheme.ANALYSE_COMEBACK, label=f"C{i}", brief="B")
        for i in range(5)
    ]
    pairs = _candidate_pairs(["Groupe A"], concepts, set(), limit=2)
    assert len(pairs) == 2


def test_load_viral_groups_deduplique_entre_tiers(settings, tmp_path):
    tiers_path = tmp_path / "artist_tiers.yaml"
    tiers_path.write_text(
        "tier_1:\n  - Groupe A\ntier_2:\n  - Groupe B\n  - Groupe A\n", encoding="utf-8"
    )
    settings = settings.model_copy(update={"artist_tiers_path": tiers_path})
    assert _load_viral_groups(settings) == ["Groupe A", "Groupe B"]


# --- run_thread_select ---


def test_run_thread_select_ignore_si_backlog_insuffisant(settings):
    _seed_topics(settings, n=1)  # moins de 3 disponibles
    assert run_thread_select(settings) is False
    conn = storage.init_db(settings.db_path)
    assert storage.pending_selections(conn) == []
    conn.close()


def test_run_thread_select_ignore_si_selection_deja_pending(settings):
    ids = _seed_topics(settings, n=3)
    conn = storage.init_db(settings.db_path)
    storage.insert_selection(
        conn,
        discord_message_id="msg-existant",
        option_a=(ids[0], ThreadAngle.CONTRARIEN),
        option_b=(ids[1], ThreadAngle.GUIDE_PRATIQUE),
        option_c=(ids[2], ThreadAngle.CAS_ETUDE),
    )
    conn.close()

    assert run_thread_select(settings) is False


@respx.mock
def test_run_thread_select_dry_run_ne_poste_rien_et_ne_modifie_rien(settings):
    _seed_topics(settings, n=3)
    assert run_thread_select(settings, dry_run=True) is True

    conn = storage.init_db(settings.db_path)
    assert storage.pending_selections(conn) == []
    topics = conn.execute("SELECT last_offered_at FROM thread_topics").fetchall()
    assert all(row["last_offered_at"] is None for row in topics)
    conn.close()


def test_run_thread_select_sans_config_discord_retourne_false(settings):
    _seed_topics(settings, n=3)
    assert run_thread_select(settings) is False  # bot_token/webhook/channel_id absents


@respx.mock
def test_run_thread_select_publie_le_picker_et_enregistre_la_selection(settings):
    _seed_topics(settings, n=3)
    settings = settings.model_copy(
        update={
            "discord_webhook_thread": "https://discord.com/api/webhooks/fake/thread",
            "discord_bot_token": "tok",
            "discord_thread_channel_id": "chan-1",
        }
    )
    respx.post(settings.discord_webhook_thread).mock(
        return_value=httpx.Response(200, json={"id": "msg-1", "content": ""})
    )
    for emoji in ["🇦", "🇧", "🇨"]:
        respx.put(
            f"{_BASE}/channels/chan-1/messages/msg-1/reactions/{quote(emoji, safe='')}/@me"
        ).mock(return_value=httpx.Response(204))

    assert run_thread_select(settings) is True

    conn = storage.init_db(settings.db_path)
    [selection] = storage.pending_selections(conn)
    assert selection.discord_message_id == "msg-1"
    topics = conn.execute("SELECT last_offered_at FROM thread_topics").fetchall()
    assert all(row["last_offered_at"] is not None for row in topics)
    conn.close()


# --- run_thread_resolve ---


def test_run_thread_resolve_rien_a_faire_sans_bot_token(settings):
    stats = run_thread_resolve(settings)
    assert stats.resolved == 0
    assert stats.still_pending == 0


def _seed_pending_selection(settings: Settings, ids: list[int]) -> None:
    conn = storage.init_db(settings.db_path)
    storage.insert_selection(
        conn,
        discord_message_id="msg-1",
        option_a=(ids[0], ThreadAngle.CONTRARIEN),
        option_b=(ids[1], ThreadAngle.GUIDE_PRATIQUE),
        option_c=(ids[2], ThreadAngle.CAS_ETUDE),
    )
    conn.close()


def _configured_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "discord_webhook_thread": "https://discord.com/api/webhooks/fake/thread",
            "discord_bot_token": "tok",
            "discord_thread_channel_id": "chan-1",
        }
    )


def _mock_bot_identity() -> None:
    respx.get(f"{_BASE}/users/@me").mock(return_value=httpx.Response(200, json={"id": "bot-42"}))


def _mock_reaction(emoji: str, users: list[dict]) -> None:
    url = f"{_BASE}/channels/chan-1/messages/msg-1/reactions/{quote(emoji, safe='')}"
    respx.get(url).mock(return_value=httpx.Response(200, json=users))


@respx.mock
def test_run_thread_resolve_aucune_reaction_incremente_still_pending(settings):
    ids = _seed_topics(settings, n=3)
    settings = _configured_settings(settings)
    _seed_pending_selection(settings, ids)

    _mock_bot_identity()
    for emoji in ["🇦", "🇧", "🇨"]:
        _mock_reaction(emoji, [{"id": "bot-42"}])

    stats = run_thread_resolve(settings)
    assert stats.still_pending == 1
    assert stats.resolved == 0
    conn = storage.init_db(settings.db_path)
    assert len(storage.pending_selections(conn)) == 1
    conn.close()


@respx.mock
def test_run_thread_resolve_genere_et_envoie_le_thread_choisi(settings, monkeypatch):
    ids = _seed_topics(settings, n=3)
    settings = _configured_settings(settings)
    _seed_pending_selection(settings, ids)

    _mock_bot_identity()
    _mock_reaction("🇦", [{"id": "bot-42"}])  # personne n'a choisi l'option A
    _mock_reaction("🇧", [{"id": "bot-42"}, {"id": "human-7"}])  # option B choisie

    writing = ThreadWritingResult(premise_respectee=True, tweets=[f"Tweet {i}." for i in range(5)])
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "write_thread",
        lambda self, topic, angle, *, recent_hook_labels: (writing, "chiffre_marquant", 40, 15),
    )
    respx.post(settings.discord_webhook_thread).mock(return_value=httpx.Response(204))

    stats = run_thread_resolve(settings)
    assert stats.resolved == 1
    assert stats.sent == 1

    conn = storage.init_db(settings.db_path)
    assert storage.pending_selections(conn) == []
    assert storage.pending_threads(conn) == []
    conn.close()


@respx.mock
def test_run_thread_resolve_joint_une_image_par_tweet(settings, monkeypatch, tmp_path):
    """T16 : l'image jointe à chaque tweet vient de la bibliothèque, résolue à partir du groupe
    du topic choisi (voir media_library.select_images_for_thread)."""
    media_path = tmp_path / "media_library"
    image_path = media_path / "Groupe 1" / "photo.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"fake-bytes")

    ids = _seed_topics(settings, n=3)
    settings = _configured_settings(settings).model_copy(update={"media_library_path": media_path})
    _seed_pending_selection(settings, ids)

    _mock_bot_identity()
    _mock_reaction("🇦", [{"id": "bot-42"}])
    _mock_reaction("🇧", [{"id": "bot-42"}, {"id": "human-7"}])  # option B -> topic "Groupe 1"

    writing = ThreadWritingResult(premise_respectee=True, tweets=[f"Tweet {i}." for i in range(5)])
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "write_thread",
        lambda self, topic, angle, *, recent_hook_labels: (writing, "chiffre_marquant", 10, 5),
    )
    mock = respx.post(settings.discord_webhook_thread).mock(return_value=httpx.Response(204))

    stats = run_thread_resolve(settings)
    assert stats.sent == 1

    tweet_call = mock.calls[2]  # intro, en-tête "Tweet 1/5", puis le 1er tweet avec image jointe
    assert tweet_call.request.headers["content-type"].startswith("multipart/form-data")
    assert b"photo.jpg" in tweet_call.request.content


@respx.mock
def test_run_thread_resolve_reprise_apres_crash_sans_rappeler_gemini(settings, monkeypatch):
    """Le thread a déjà été inséré (crash simulé entre insert_thread et resolve_selection) :
    la reprise doit finaliser la sélection sans regénérer via Gemini."""
    ids = _seed_topics(settings, n=3)
    settings = _configured_settings(settings)
    _seed_pending_selection(settings, ids)

    conn = storage.init_db(settings.db_path)
    writing = ThreadWritingResult(premise_respectee=True, tweets=[f"Tweet {i}." for i in range(5)])
    storage.insert_thread(
        conn,
        selection_id=None,
        topic_id=ids[1],
        angle=ThreadAngle.GUIDE_PRATIQUE,  # même couple que l'option B
        hook_label="chiffre_marquant",
        writing=writing,
        tokens_in=1,
        tokens_out=1,
        prompt_version="v1",
    )
    conn.close()

    _mock_bot_identity()
    _mock_reaction("🇦", [{"id": "bot-42"}])
    _mock_reaction("🇧", [{"id": "bot-42"}, {"id": "human-7"}])

    def _fail_if_called(self, topic, angle, *, recent_hook_labels):
        pytest.fail("write_thread ne doit pas être rappelé après reprise")

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write_thread", _fail_if_called)
    respx.post(settings.discord_webhook_thread).mock(return_value=httpx.Response(204))

    stats = run_thread_resolve(settings)
    assert stats.resolved == 1
    conn = storage.init_db(settings.db_path)
    assert storage.pending_selections(conn) == []
    conn.close()


@respx.mock
def test_run_thread_resolve_quota_exceeded_arrete_le_cycle(settings, monkeypatch):
    ids = _seed_topics(settings, n=3)
    settings = _configured_settings(settings)
    _seed_pending_selection(settings, ids)

    _mock_bot_identity()
    _mock_reaction("🇦", [{"id": "bot-42"}])
    _mock_reaction("🇧", [{"id": "bot-42"}, {"id": "human-7"}])

    def _raise_quota(self, topic, angle, *, recent_hook_labels):
        raise analyzer.QuotaExceededError("quota")

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write_thread", _raise_quota)

    stats = run_thread_resolve(settings)
    assert stats.quota_exceeded is True
    conn = storage.init_db(settings.db_path)
    assert len(storage.pending_selections(conn)) == 1  # pas résolue, reprise au prochain cycle
    conn.close()


@respx.mock
def test_run_thread_resolve_echec_generation_reste_pending_pour_retenter(settings, monkeypatch):
    ids = _seed_topics(settings, n=3)
    settings = _configured_settings(settings)
    _seed_pending_selection(settings, ids)

    _mock_bot_identity()
    _mock_reaction("🇦", [{"id": "bot-42"}])
    _mock_reaction("🇧", [{"id": "bot-42"}, {"id": "human-7"}])

    def _raise_invalid(self, topic, angle, *, recent_hook_labels):
        raise analyzer.AnalysisError("réponse invalide")

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write_thread", _raise_invalid)

    stats = run_thread_resolve(settings)
    assert stats.generation_failed == 1
    conn = storage.init_db(settings.db_path)
    assert len(storage.pending_selections(conn)) == 1
    conn.close()


@respx.mock
def test_run_thread_resolve_retente_l_envoi_d_un_thread_draft_sans_regenerer(settings, monkeypatch):
    """Un thread DRAFT déjà généré (précédent envoi Discord raté) doit être ré-envoyé sans
    aucun nouvel appel Gemini, même en l'absence de sélection PENDING."""
    ids = _seed_topics(settings, n=1)
    settings = _configured_settings(settings)

    conn = storage.init_db(settings.db_path)
    writing = ThreadWritingResult(premise_respectee=True, tweets=[f"Tweet {i}." for i in range(5)])
    storage.insert_thread(
        conn,
        selection_id=None,
        topic_id=ids[0],
        angle=ThreadAngle.CAS_ETUDE,
        hook_label="chiffre_marquant",
        writing=writing,
        tokens_in=1,
        tokens_out=1,
        prompt_version="v1",
    )
    conn.close()

    def _fail_if_called(self, topic, angle, *, recent_hook_labels):
        pytest.fail("write_thread ne doit pas être appelé pour un simple ré-envoi")

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write_thread", _fail_if_called)
    respx.post(settings.discord_webhook_thread).mock(return_value=httpx.Response(204))

    stats = run_thread_resolve(settings)
    assert stats.sent == 1
    conn = storage.init_db(settings.db_path)
    assert storage.pending_threads(conn) == []
    conn.close()


@respx.mock
def test_run_thread_resolve_echec_envoi_garde_le_thread_en_draft(settings):
    ids = _seed_topics(settings, n=1)
    settings = _configured_settings(settings)

    conn = storage.init_db(settings.db_path)
    writing = ThreadWritingResult(premise_respectee=True, tweets=[f"Tweet {i}." for i in range(5)])
    storage.insert_thread(
        conn,
        selection_id=None,
        topic_id=ids[0],
        angle=ThreadAngle.CAS_ETUDE,
        hook_label="chiffre_marquant",
        writing=writing,
        tokens_in=1,
        tokens_out=1,
        prompt_version="v1",
    )
    conn.close()

    respx.post(settings.discord_webhook_thread).mock(
        return_value=httpx.Response(500, text="erreur serveur")
    )

    stats = run_thread_resolve(settings)
    assert stats.send_failed == 1
    conn = storage.init_db(settings.db_path)
    assert len(storage.pending_threads(conn)) == 1
    conn.close()
