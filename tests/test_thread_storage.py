from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from kpop_bot import storage
from kpop_bot.models import (
    SelectionStatus,
    ThreadAngle,
    ThreadStatus,
    ThreadTheme,
    ThreadTopicIdea,
    ThreadWritingResult,
)


@pytest.fixture
def conn(tmp_path: Path):
    connection = storage.init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def _idea(**overrides) -> ThreadTopicIdea:
    defaults = dict(
        group_name="Groupe X",
        theme=ThreadTheme.ANALYSE_COMEBACK,
        title="Titre du sujet",
        premise="La promesse du sujet.",
    )
    defaults.update(overrides)
    return ThreadTopicIdea(**defaults)


def test_insert_topic_ideas_puis_backlog_topic_count(conn):
    ids = storage.insert_topic_ideas(conn, [_idea(), _idea(group_name="Groupe Y")])
    assert len(ids) == 2
    assert storage.backlog_topic_count(conn) == 2


def test_insert_topic_ideas_conserve_concept_id(conn):
    """T15bis : le concept_id (croisement déterministe) doit être persisté et relu."""
    [topic_id] = storage.insert_topic_ideas(conn, [_idea(concept_id="rivalite_historique")])
    assert storage.get_topic(conn, topic_id).concept_id == "rivalite_historique"


def test_insert_topic_ideas_concept_id_absent_reste_none(conn):
    """Rétrocompatibilité : un topic sans concept_id (ancienne idéation libre) reste valide."""
    [topic_id] = storage.insert_topic_ideas(conn, [_idea()])
    assert storage.get_topic(conn, topic_id).concept_id is None


def test_used_group_concept_pairs_ignore_les_topics_sans_concept_id(conn):
    storage.insert_topic_ideas(
        conn,
        [
            _idea(group_name="Groupe A", concept_id="c1"),
            _idea(group_name="Groupe B"),  # historique, concept_id=None
        ],
    )
    assert storage.used_group_concept_pairs(conn) == {("Groupe A", "c1")}


def test_migrate_schema_ajoute_concept_id_sur_thread_topics(tmp_path: Path):
    """Simule une base thread_topics créée avant T15bis (sans concept_id) — la migration doit
    l'ajouter sans erreur, même avec des lignes déjà présentes."""
    path = tmp_path / "legacy_threads.db"
    legacy_conn = sqlite3.connect(path)
    legacy_conn.execute(
        """
        CREATE TABLE thread_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT NOT NULL,
            theme TEXT NOT NULL,
            title TEXT NOT NULL,
            premise TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_offered_at TEXT,
            source TEXT NOT NULL DEFAULT 'ai_ideation'
        )
        """
    )
    legacy_conn.execute(
        "INSERT INTO thread_topics (group_name, theme, title, premise, created_at) "
        "VALUES ('Groupe X', 'ANALYSE_COMEBACK', 'Titre', 'Promesse', '2026-07-24')"
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = storage.init_db(path)  # doit migrer sans écraser la ligne déjà présente
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(thread_topics)").fetchall()}
    assert "concept_id" in columns
    row = conn.execute("SELECT * FROM thread_topics WHERE group_name = 'Groupe X'").fetchone()
    assert row["title"] == "Titre"
    assert row["concept_id"] is None
    conn.close()


def test_available_angles_toutes_disponibles_au_depart(conn):
    [topic_id] = storage.insert_topic_ideas(conn, [_idea()])
    assert set(storage.available_angles(conn, topic_id)) == set(ThreadAngle)


def test_available_angles_diminue_apres_insert_thread(conn):
    [topic_id] = storage.insert_topic_ideas(conn, [_idea()])
    writing = ThreadWritingResult(tweets=[f"Tweet {i}" for i in range(6)])
    storage.insert_thread(
        conn,
        selection_id=None,
        topic_id=topic_id,
        angle=ThreadAngle.CONTRARIEN,
        hook_label="chiffre_marquant",
        writing=writing,
        tokens_in=10,
        tokens_out=5,
        prompt_version="v1",
    )
    remaining = storage.available_angles(conn, topic_id)
    assert ThreadAngle.CONTRARIEN not in remaining
    assert len(remaining) == len(ThreadAngle) - 1


def test_backlog_topic_count_exclut_un_topic_dont_tous_les_angles_sont_consommes(conn):
    [topic_id] = storage.insert_topic_ideas(conn, [_idea()])
    writing = ThreadWritingResult(tweets=[f"Tweet {i}" for i in range(6)])
    for angle in ThreadAngle:
        storage.insert_thread(
            conn,
            selection_id=None,
            topic_id=topic_id,
            angle=angle,
            hook_label="chiffre_marquant",
            writing=writing,
            tokens_in=1,
            tokens_out=1,
            prompt_version="v1",
        )
    assert storage.backlog_topic_count(conn) == 0


def test_threads_unique_topic_angle_leve_integrity_error(conn):
    [topic_id] = storage.insert_topic_ideas(conn, [_idea()])
    writing = ThreadWritingResult(tweets=[f"Tweet {i}" for i in range(6)])
    storage.insert_thread(
        conn,
        selection_id=None,
        topic_id=topic_id,
        angle=ThreadAngle.CONTRARIEN,
        hook_label="chiffre_marquant",
        writing=writing,
        tokens_in=1,
        tokens_out=1,
        prompt_version="v1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        storage.insert_thread(
            conn,
            selection_id=None,
            topic_id=topic_id,
            angle=ThreadAngle.CONTRARIEN,  # même couple (topic, angle) — jamais deux fois
            hook_label="question_qui_pique",
            writing=writing,
            tokens_in=1,
            tokens_out=1,
            prompt_version="v1",
        )


def test_recent_group_theme_pairs_respecte_la_fenetre(conn):
    [topic_id] = storage.insert_topic_ideas(
        conn, [_idea(group_name="Groupe X", theme=ThreadTheme.RECAP_SCANDALE)]
    )
    writing = ThreadWritingResult(tweets=[f"Tweet {i}" for i in range(6)])
    thread_id = storage.insert_thread(
        conn,
        selection_id=None,
        topic_id=topic_id,
        angle=ThreadAngle.CONTRARIEN,
        hook_label="chiffre_marquant",
        writing=writing,
        tokens_in=1,
        tokens_out=1,
        prompt_version="v1",
    )
    assert ("Groupe X", "RECAP_SCANDALE") in storage.recent_group_theme_pairs(conn, days=14)

    # Recule artificiellement la date de création pour simuler un thread ancien.
    old_date = (dt.datetime.now(dt.UTC) - dt.timedelta(days=30)).isoformat()
    conn.execute("UPDATE threads SET created_at = ? WHERE id = ?", (old_date, thread_id))
    conn.commit()
    assert ("Groupe X", "RECAP_SCANDALE") not in storage.recent_group_theme_pairs(conn, days=14)


def test_recent_hook_labels_ordre_et_limite(conn):
    [topic_id] = storage.insert_topic_ideas(conn, [_idea()])
    writing = ThreadWritingResult(tweets=[f"Tweet {i}" for i in range(6)])
    for index, (angle, label) in enumerate(
        zip(
            list(ThreadAngle),
            ["chiffre_marquant", "question_qui_pique", "petite_histoire"],
            strict=False,
        )
    ):
        storage.insert_thread(
            conn,
            selection_id=None,
            topic_id=topic_id,
            angle=angle,
            hook_label=label,
            writing=writing,
            tokens_in=1,
            tokens_out=1,
            prompt_version="v1",
        )
        # Espace les created_at pour un ordre déterministe (insertions trop rapprochées sinon).
        conn.execute(
            "UPDATE threads SET created_at = ? WHERE id = (SELECT MAX(id) FROM threads)",
            ((dt.datetime.now(dt.UTC) + dt.timedelta(seconds=index)).isoformat(),),
        )
        conn.commit()

    labels = storage.recent_hook_labels(conn, limit=2)
    assert labels == ["petite_histoire", "question_qui_pique"]


def test_select_picker_candidates_diversifie_groupe_theme(conn):
    storage.insert_topic_ideas(
        conn,
        [
            _idea(group_name="Groupe A", theme=ThreadTheme.ANALYSE_COMEBACK, title="Sujet 1"),
            _idea(group_name="Groupe A", theme=ThreadTheme.ANALYSE_COMEBACK, title="Sujet 2"),
            _idea(group_name="Groupe B", theme=ThreadTheme.RECAP_SCANDALE, title="Sujet 3"),
            _idea(group_name="Groupe C", theme=ThreadTheme.CULTURE_FANS, title="Sujet 4"),
        ],
    )
    candidates = storage.select_picker_candidates(conn, count=3)
    assert len(candidates) == 3
    pairs = [(topic.group_name, topic.theme.value) for topic, _ in candidates]
    assert len(pairs) == len(set(pairs))  # aucune paire (groupe, thème) répétée dans le lot


def test_select_picker_candidates_exclut_les_couples_recents(conn):
    [topic_id] = storage.insert_topic_ideas(
        conn, [_idea(group_name="Groupe A", theme=ThreadTheme.ANALYSE_COMEBACK)]
    )
    writing = ThreadWritingResult(tweets=[f"Tweet {i}" for i in range(6)])
    storage.insert_thread(
        conn,
        selection_id=None,
        topic_id=topic_id,
        angle=ThreadAngle.CONTRARIEN,
        hook_label="chiffre_marquant",
        writing=writing,
        tokens_in=1,
        tokens_out=1,
        prompt_version="v1",
    )
    storage.insert_topic_ideas(
        conn,
        [
            _idea(
                group_name="Groupe A",
                theme=ThreadTheme.ANALYSE_COMEBACK,
                title="Autre sujet, même paire",
            )
        ],
    )
    candidates = storage.select_picker_candidates(conn, count=3, recent_window_days=14)
    pairs = [(topic.group_name, topic.theme.value) for topic, _ in candidates]
    assert ("Groupe A", "ANALYSE_COMEBACK") not in pairs


def test_touch_offered_topics_met_a_jour_last_offered_at(conn):
    [topic_id] = storage.insert_topic_ideas(conn, [_idea()])
    assert storage.get_topic(conn, topic_id).last_offered_at is None
    storage.touch_offered_topics(conn, [topic_id])
    assert storage.get_topic(conn, topic_id).last_offered_at is not None


def test_selection_cycle_complet(conn):
    ids = storage.insert_topic_ideas(conn, [_idea(title=f"Sujet {i}") for i in range(3)])
    selection_id = storage.insert_selection(
        conn,
        discord_message_id="msg-1",
        option_a=(ids[0], ThreadAngle.CONTRARIEN),
        option_b=(ids[1], ThreadAngle.GUIDE_PRATIQUE),
        option_c=(ids[2], ThreadAngle.CAS_ETUDE),
    )
    [pending] = storage.pending_selections(conn)
    assert pending.status == SelectionStatus.PENDING
    assert pending.discord_message_id == "msg-1"

    storage.resolve_selection(conn, selection_id, topic_id=ids[1], angle=ThreadAngle.GUIDE_PRATIQUE)
    assert storage.pending_selections(conn) == []


def test_expire_stale_selections(conn):
    ids = storage.insert_topic_ideas(conn, [_idea(title=f"Sujet {i}") for i in range(3)])
    storage.insert_selection(
        conn,
        discord_message_id="msg-old",
        option_a=(ids[0], ThreadAngle.CONTRARIEN),
        option_b=(ids[1], ThreadAngle.GUIDE_PRATIQUE),
        option_c=(ids[2], ThreadAngle.CAS_ETUDE),
    )
    old_date = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)).isoformat()
    conn.execute("UPDATE thread_selections SET created_at = ?", (old_date,))
    conn.commit()

    expired = storage.expire_stale_selections(conn, ttl_hours=24)
    assert expired == 1
    assert storage.pending_selections(conn) == []


def test_thread_sent_et_pending_threads(conn):
    [topic_id] = storage.insert_topic_ideas(conn, [_idea()])
    writing = ThreadWritingResult(tweets=[f"Tweet {i}" for i in range(6)])
    thread_id = storage.insert_thread(
        conn,
        selection_id=None,
        topic_id=topic_id,
        angle=ThreadAngle.CONTRARIEN,
        hook_label="chiffre_marquant",
        writing=writing,
        tokens_in=10,
        tokens_out=5,
        prompt_version="v1",
    )
    [draft] = storage.pending_threads(conn)
    assert draft.status == ThreadStatus.DRAFT
    assert draft.tweets == writing.tweets

    storage.mark_thread_sent(conn, thread_id)
    assert storage.pending_threads(conn) == []


def test_mark_thread_failed(conn):
    [topic_id] = storage.insert_topic_ideas(conn, [_idea()])
    writing = ThreadWritingResult(tweets=[f"Tweet {i}" for i in range(6)])
    thread_id = storage.insert_thread(
        conn,
        selection_id=None,
        topic_id=topic_id,
        angle=ThreadAngle.CONTRARIEN,
        hook_label="chiffre_marquant",
        writing=writing,
        tokens_in=1,
        tokens_out=1,
        prompt_version="v1",
    )
    storage.mark_thread_failed(conn, thread_id, "erreur de test")
    assert storage.pending_threads(conn) == []
