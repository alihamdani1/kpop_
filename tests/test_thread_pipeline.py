from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import respx

from kpop_bot import analyzer, storage
from kpop_bot.models import ThreadAngle, ThreadTheme, ThreadTopicIdea, ThreadWritingResult
from kpop_bot.settings import Settings
from kpop_bot.thread_pipeline import run_thread_replenish, run_thread_resolve, run_thread_select

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


def test_run_thread_replenish_genere_un_lot_si_backlog_bas(settings, monkeypatch):
    from kpop_bot.models import TopicIdeationResult

    settings = settings.model_copy(update={"thread_topic_backlog_min": 5})
    _seed_topics(settings, n=1)

    result = TopicIdeationResult(
        topics=[_idea(title="Nouveau sujet 1"), _idea(title="Nouveau sujet 2")]
    )
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "ideate_thread_topics",
        lambda self, *, batch_size, excluded_pairs: (result, 50, 20),
    )
    inserted = run_thread_replenish(settings)
    assert inserted == 2

    conn = storage.init_db(settings.db_path)
    assert storage.backlog_topic_count(conn) == 3  # 1 déjà présent + 2 nouveaux
    conn.close()


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

    writing = ThreadWritingResult(tweets=[f"Tweet {i}." for i in range(5)])
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
def test_run_thread_resolve_reprise_apres_crash_sans_rappeler_gemini(settings, monkeypatch):
    """Le thread a déjà été inséré (crash simulé entre insert_thread et resolve_selection) :
    la reprise doit finaliser la sélection sans regénérer via Gemini."""
    ids = _seed_topics(settings, n=3)
    settings = _configured_settings(settings)
    _seed_pending_selection(settings, ids)

    conn = storage.init_db(settings.db_path)
    writing = ThreadWritingResult(tweets=[f"Tweet {i}." for i in range(5)])
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
    writing = ThreadWritingResult(tweets=[f"Tweet {i}." for i in range(5)])
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
    writing = ThreadWritingResult(tweets=[f"Tweet {i}." for i in range(5)])
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
