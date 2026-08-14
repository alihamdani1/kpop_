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
    InstagramNewsPost,
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


def test_save_analysis_avec_post_instagram(conn):
    article_id = storage.insert_new_article(conn, _item())
    classification = ClassificationResult(
        category=Category.CONCERT_EVENEMENT_FRANCE,
        importance=Importance.MAJEUR,
        virality=Virality.ELEVE,
        virality_reason="Concert en France.",
        artists=["Groupe X"],
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet.")
    instagram_news = InstagramNewsPost(
        hook="Accroche.",
        paragraph_context="Contexte.",
        paragraph_detail="Détail.",
        engagement_question="Une question ?",
        hashtags=["#kpop", "#comeback"],
    )
    storage.save_analysis(
        conn,
        article_id,
        classification,
        Route.A,
        True,
        writing,
        120,
        60,
        "v1",
        instagram_news=instagram_news,
    )
    [record] = storage.pending(conn, ArticleStatus.ANALYZED)
    assert record.instagram_hook == "Accroche."
    assert record.instagram_paragraph_context == "Contexte."
    assert record.instagram_paragraph_detail == "Détail."
    assert record.instagram_engagement_question == "Une question ?"
    assert record.instagram_hashtags == ["#kpop", "#comeback"]


def test_save_analysis_sans_post_instagram_laisse_les_champs_vides(conn):
    article_id = storage.insert_new_article(conn, _item())
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet.")
    storage.save_analysis(conn, article_id, classification, Route.B, False, writing, 10, 5, "v1")
    [record] = storage.pending(conn, ArticleStatus.ANALYZED)
    assert record.instagram_hook is None
    assert record.instagram_paragraph_context is None
    assert record.instagram_paragraph_detail is None
    assert record.instagram_hashtags == []


def test_save_analysis_avec_image_scrapee_met_a_jour_image_url(conn):
    """Le scraping (T18) doit pouvoir remplacer l'image RSS par une meilleure trouvée sur la
    page article, et enregistrer les images additionnelles trouvées en bonus."""
    article_id = storage.insert_new_article(conn, _item())
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet.")
    storage.save_analysis(
        conn,
        article_id,
        classification,
        Route.B,
        False,
        writing,
        10,
        5,
        "v1",
        image_url="https://www.soompi.com/main.jpg",
        extra_image_urls=["https://www.soompi.com/extra1.jpg"],
    )
    [record] = storage.pending(conn, ArticleStatus.ANALYZED)
    assert record.image_url == "https://www.soompi.com/main.jpg"
    assert record.extra_image_urls == ["https://www.soompi.com/extra1.jpg"]


def test_save_analysis_sans_image_scrapee_conserve_l_image_existante(conn):
    """`image_url=None` (aucune page exploitable) ne doit jamais écraser l'image déjà connue
    depuis la collecte RSS — voir COALESCE dans save_analysis."""
    article_id = storage.insert_new_article(conn, _item(image_url="https://www.soompi.com/rss.jpg"))
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet.")
    storage.save_analysis(conn, article_id, classification, Route.B, False, writing, 10, 5, "v1")
    [record] = storage.pending(conn, ArticleStatus.ANALYZED)
    assert record.image_url == "https://www.soompi.com/rss.jpg"


def test_migrate_schema_ajoute_les_colonnes_manquantes(tmp_path: Path):
    """Simule une base créée avant T14 (sans les colonnes instagram_*/extra_image_urls) — la
    migration doit les ajouter sans erreur, sur une vraie base déjà peuplée (comme
    data/kpop.db en production)."""
    path = tmp_path / "legacy.db"
    legacy_conn = sqlite3.connect(path)
    legacy_conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            published_at TEXT NOT NULL,
            raw_summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'NEW',
            created_at TEXT NOT NULL
        )
        """
    )
    legacy_conn.execute(
        "INSERT INTO articles (fingerprint, source, title, url, published_at, raw_summary, "
        "created_at) VALUES ('fp-legacy', 'Soompi', 'Titre', 'https://x', '2026-07-24', "
        "'résumé', '2026-07-24')"
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = storage.init_db(path)  # doit migrer sans écraser la ligne déjà présente
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    assert {
        "instagram_hook",
        "instagram_paragraph_context",
        "instagram_paragraph_detail",
        "instagram_engagement_question",
        "instagram_hashtags",
        "extra_image_urls",
    } <= columns
    row = conn.execute("SELECT * FROM articles WHERE fingerprint = 'fp-legacy'").fetchone()
    assert row["title"] == "Titre"
    assert row["instagram_hook"] is None
    conn.close()


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


# --- Visuel social 9:16 : migration + backfill, pending_social_visuals, mark_social_visual_sent.


def test_migrate_social_visual_columns_backfill_les_articles_deja_sent(tmp_path: Path):
    """data/kpop.db a de vrais articles déjà SENT en production — sans backfill, l'ajout de
    social_visual_sent_at les laisserait tous à NULL et le premier run de social_pipeline
    inonderait Discord de visuels rétroactifs. Simule une base pré-migration avec un article déjà
    SENT et un autre encore NEW."""
    path = tmp_path / "legacy.db"
    legacy_conn = sqlite3.connect(path)
    legacy_conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            published_at TEXT NOT NULL,
            raw_summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'NEW',
            created_at TEXT NOT NULL,
            sent_at TEXT
        )
        """
    )
    legacy_conn.execute(
        "INSERT INTO articles (fingerprint, source, title, url, published_at, raw_summary, "
        "status, created_at, sent_at) VALUES ('fp-sent', 'Soompi', 'Titre envoyé', 'https://x', "
        "'2026-07-24', 'résumé', 'SENT', '2026-07-24', '2026-07-24T12:00:00+00:00')"
    )
    legacy_conn.execute(
        "INSERT INTO articles (fingerprint, source, title, url, published_at, raw_summary, "
        "status, created_at, sent_at) VALUES ('fp-new', 'Soompi', 'Titre nouveau', 'https://y', "
        "'2026-07-24', 'résumé', 'NEW', '2026-07-24', NULL)"
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = storage.init_db(path)
    sent_row = conn.execute(
        "SELECT social_visual_sent_at, sent_at FROM articles WHERE fingerprint = 'fp-sent'"
    ).fetchone()
    assert sent_row["social_visual_sent_at"] == sent_row["sent_at"]
    new_row = conn.execute(
        "SELECT social_visual_sent_at FROM articles WHERE fingerprint = 'fp-new'"
    ).fetchone()
    assert new_row["social_visual_sent_at"] is None
    conn.close()


def test_migrate_social_visual_columns_est_un_no_op_si_deja_migre(conn):
    """Un 2e appel à init_db() (donc à la migration) ne doit pas re-backfiller ni écraser un
    social_visual_sent_at déjà positionné par un run précédent de social_pipeline."""
    article_id = storage.insert_new_article(conn, _item())
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet.")
    storage.save_analysis(conn, article_id, classification, Route.B, False, writing, 10, 5, "v1")
    storage.mark_sent(conn, article_id)
    storage.mark_social_visual_sent(conn, article_id)
    [before] = storage.pending(conn, ArticleStatus.SENT)

    storage._migrate_schema(conn)  # simule un 2e init_db() sur la même base

    [after] = storage.pending(conn, ArticleStatus.SENT)
    assert after.social_visual_sent_at == before.social_visual_sent_at


def test_pending_social_visuals_exclut_les_articles_deja_traites(conn):
    id_a = storage.insert_new_article(conn, _item(fingerprint="fp-a", url="https://x/a"))
    id_b = storage.insert_new_article(conn, _item(fingerprint="fp-b", url="https://x/b"))
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet.")
    for article_id in (id_a, id_b):
        storage.save_analysis(
            conn, article_id, classification, Route.B, False, writing, 10, 5, "v1"
        )
        storage.mark_sent(conn, article_id)
    storage.mark_social_visual_sent(conn, id_a)

    [remaining] = storage.pending_social_visuals(conn, limit=10)
    assert remaining.id == id_b


def test_pending_social_visuals_ignore_les_articles_non_sent(conn):
    storage.insert_new_article(conn, _item())  # reste NEW
    assert storage.pending_social_visuals(conn, limit=10) == []


def test_pending_social_visuals_respecte_la_limite(conn):
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet.")
    for i in range(5):
        article_id = storage.insert_new_article(
            conn, _item(fingerprint=f"fp-{i}", url=f"https://x/{i}")
        )
        storage.save_analysis(
            conn, article_id, classification, Route.B, False, writing, 10, 5, "v1"
        )
        storage.mark_sent(conn, article_id)
    assert len(storage.pending_social_visuals(conn, limit=3)) == 3


def test_pending_respecte_la_limite(conn):
    for i in range(5):
        storage.insert_new_article(conn, _item(fingerprint=f"fp-{i}", url=f"https://x/{i}"))
    assert len(storage.pending(conn, ArticleStatus.NEW, limit=3)) == 3
    assert len(storage.pending(conn, ArticleStatus.NEW)) == 5
