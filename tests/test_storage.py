from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from kpop_bot import storage
from kpop_bot.models import (
    ArticleStatus,
    Category,
    ClassificationResult,
    FetchedItem,
    Importance,
    Route,
    Virality,
    WritingResult,
)


@pytest.fixture
def conn(tmp_path: Path):
    connection = storage.init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def _item(**overrides) -> FetchedItem:
    defaults = dict(
        source="Soompi",
        title="Groupe X annonce un comeback",
        url="https://www.soompi.com/article/123",
        published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
        raw_summary="Un résumé.",
        fingerprint="fp-123",
    )
    defaults.update(overrides)
    return FetchedItem(**defaults)


def test_insert_puis_fingerprint_exists(conn):
    assert storage.fingerprint_exists(conn, "fp-123") is False
    storage.insert_new_article(conn, _item())
    assert storage.fingerprint_exists(conn, "fp-123") is True


def test_insertion_double_est_evitee_par_l_appelant(conn):
    """La couche storage ne dédoublonne pas elle-même — c'est fingerprint_exists() qui
    protège l'appelant, comme dans pipeline.py."""
    storage.insert_new_article(conn, _item())
    with pytest.raises(sqlite3.IntegrityError):
        storage.insert_new_article(conn, _item())


def test_second_run_ne_reinsere_rien(conn):
    items = [_item(fingerprint="a"), _item(fingerprint="b")]
    new_count = 0
    for item in items:
        if not storage.fingerprint_exists(conn, item.fingerprint):
            storage.insert_new_article(conn, item)
            new_count += 1
    assert new_count == 2

    # Deuxième "cycle" avec les mêmes items : aucune insertion.
    new_count_second_pass = 0
    for item in items:
        if not storage.fingerprint_exists(conn, item.fingerprint):
            storage.insert_new_article(conn, item)
            new_count_second_pass += 1
    assert new_count_second_pass == 0


def test_save_analysis_route_ignored_devient_filtered(conn):
    article_id = storage.insert_new_article(conn, _item())
    classification = ClassificationResult(
        category=Category.BRUIT_INUTILE, importance=Importance.MINEUR, artists=[]
    )
    storage.save_analysis(conn, article_id, classification, Route.IGNORED, False, None, 10, 0, "v1")
    [record] = storage.pending(conn, ArticleStatus.FILTERED)
    assert record.category == Category.BRUIT_INUTILE
    assert record.virality is None
    assert record.tweet_draft is None


def test_save_analysis_route_a_devient_analyzed_avec_ecriture(conn):
    article_id = storage.insert_new_article(conn, _item())
    classification = ClassificationResult(
        category=Category.CONCERT_EVENEMENT_FRANCE,
        importance=Importance.MAJEUR,
        virality=Virality.ELEVE,
        virality_reason="Concert en France.",
        artists=["Groupe X"],
    )
    writing = WritingResult(
        summary_fr="Résumé court.",
        tweet_draft="Un concert arrive en France ! 🎤 #KPop #GroupeX",
        video_summary="Résumé détaillé pour la vidéo.",
    )
    storage.save_analysis(conn, article_id, classification, Route.A, True, writing, 120, 60, "v1")
    [record] = storage.pending(conn, ArticleStatus.ANALYZED)
    assert record.route == Route.A
    assert record.france_override is True
    assert record.video_summary == "Résumé détaillé pour la vidéo."
    assert record.tokens_in == 120
    assert record.artists == ["Groupe X"]


def test_mark_failed_puis_reset_failed_to_new(conn):
    article_id = storage.insert_new_article(conn, _item())
    storage.mark_failed(conn, article_id, "erreur de test")
    [failed] = storage.pending(conn, ArticleStatus.FAILED)
    assert failed.error == "erreur de test"

    reset_count = storage.reset_failed_to_new(conn)
    assert reset_count == 1
    [reset] = storage.pending(conn, ArticleStatus.NEW)
    assert reset.error is None


def test_mark_sent(conn):
    article_id = storage.insert_new_article(conn, _item())
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet.")
    storage.save_analysis(conn, article_id, classification, Route.B, False, writing, 10, 5, "v1")
    storage.mark_sent(conn, article_id)
    assert storage.pending(conn, ArticleStatus.ANALYZED) == []
    [sent] = storage.pending(conn, ArticleStatus.SENT)
    assert sent.sent_at is not None


def test_mark_filtered_sent(conn):
    article_id = storage.insert_new_article(conn, _item())
    classification = ClassificationResult(
        category=Category.BRUIT_INUTILE, importance=Importance.MINEUR, artists=[]
    )
    storage.save_analysis(conn, article_id, classification, Route.IGNORED, False, None, 10, 0, "v1")
    storage.mark_filtered_sent(conn, article_id)
    assert storage.pending(conn, ArticleStatus.FILTERED) == []
    [reviewed] = storage.pending(conn, ArticleStatus.FILTERED_SENT)
    assert reviewed.sent_at is not None
    assert reviewed.category == Category.BRUIT_INUTILE


def test_pending_respecte_la_limite(conn):
    for i in range(5):
        storage.insert_new_article(conn, _item(fingerprint=f"fp-{i}", url=f"https://x/{i}"))
    assert len(storage.pending(conn, ArticleStatus.NEW, limit=3)) == 3
    assert len(storage.pending(conn, ArticleStatus.NEW)) == 5
