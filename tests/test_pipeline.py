from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from kpop_bot import analyzer, storage
from kpop_bot.models import (
    TWEET_TAG_LABELS,
    ArticleStatus,
    Category,
    ClassificationResult,
    FetchedItem,
    Importance,
    Route,
    TweetTag,
    Virality,
    WritingResult,
)
from kpop_bot.pipeline import resend_sent, run_cycle
from kpop_bot.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        gemini_api_key="test-key",
        discord_webhook_route_a="https://discord.com/api/webhooks/fake/a",
        discord_webhook_route_b="https://discord.com/api/webhooks/fake/b",
        db_path=tmp_path / "test.db",
    )


def _seed_sent_article(settings: Settings, *, route: Route) -> None:
    conn = storage.init_db(settings.db_path)
    article_id = storage.insert_new_article(
        conn,
        FetchedItem(
            source="Soompi",
            title="Article de test",
            url="https://www.soompi.com/article/test",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="Résumé brut.",
            fingerprint=f"fp-{route.value}",
        ),
    )
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE,
        importance=Importance.MODERE,
        virality=Virality.MODERE,
        virality_reason="Test.",
        artists=["Groupe X"],
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Un tweet de test.")
    storage.save_analysis(conn, article_id, classification, route, False, writing, 10, 5, "v1")
    storage.mark_sent(conn, article_id)
    conn.close()


@respx.mock
def test_resend_sent_renvoie_sans_appeler_gemini(settings):
    _seed_sent_article(settings, route=Route.B)
    mock_b = respx.post(settings.discord_webhook_route_b).mock(return_value=httpx.Response(204))

    count = resend_sent(settings)

    assert count == 1
    assert mock_b.call_count == 4  # en-tête INFO + embed + en-tête tweet + message-tweet


def test_resend_sent_sans_articles_envoyes_ne_fait_rien(settings):
    count = resend_sent(settings)
    assert count == 0


@respx.mock
def test_run_cycle_prefixe_le_tag_derive_sur_le_tweet_envoye(settings, tmp_path, monkeypatch):
    """Le tag ([RELEASE]/[GOSSIP]/...) est ajouté par le pipeline lui-même (determine_tweet_tag),
    jamais demandé à l'IA — seul run_cycle() exerce ce chemin en conditions réelles, donc c'est
    le seul endroit où ce comportement peut être vérifié bout en bout."""
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")
    artist_tiers_path = tmp_path / "artist_tiers.yaml"
    artist_tiers_path.write_text("{}\n", encoding="utf-8")
    settings = settings.model_copy(
        update={"sources_path": sources_path, "artist_tiers_path": artist_tiers_path}
    )

    conn = storage.init_db(settings.db_path)
    storage.insert_new_article(
        conn,
        FetchedItem(
            source="Soompi",
            title="Groupe X annonce un comeback",
            url="https://www.soompi.com/article/comeback",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="Nouveau single à venir.",
            fingerprint="fp-tag-integration",
        ),
    )
    conn.close()

    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE,
        importance=Importance.MODERE,
        virality=Virality.MODERE,
        virality_reason="Test.",
        artists=["Groupe X"],
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Un tweet de test.")
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "classify",
        lambda self, item, *, france_flag, milestone_flag=False: (classification, 10, 5),
    )
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "write",
        lambda self, item, classification, route: (writing, 3, 2),
    )
    respx.post(settings.discord_webhook_route_b).mock(return_value=httpx.Response(204))

    stats = run_cycle(settings, limit=10, dry_run=False)

    assert stats.sent == 1
    conn = storage.init_db(settings.db_path)
    record = storage.pending(conn, ArticleStatus.SENT)[0]
    conn.close()
    # COMEBACK_SORTIE + MODERE/MODERE -> ni FLASH ni GOSSIP ni FRANCE -> repli RELEASE.
    assert record.tweet_draft == f"{TWEET_TAG_LABELS[TweetTag.RELEASE]}\n\nUn tweet de test."


# --- T13 : filet record/palier viral + canal #info-a-verifier. ---


@respx.mock
def test_run_cycle_calcule_et_transmet_le_flag_milestone(settings, tmp_path, monkeypatch):
    """pipeline.py doit calculer matches_viral_milestone() et le transmettre à classify() —
    la logique de correction elle-même (override si l'IA maintient BRUIT_INUTILE) est testée
    séparément dans test_analyzer.py."""
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")
    artist_tiers_path = tmp_path / "artist_tiers.yaml"
    artist_tiers_path.write_text("tier_1:\n  - BLACKPINK\n", encoding="utf-8")
    settings = settings.model_copy(
        update={"sources_path": sources_path, "artist_tiers_path": artist_tiers_path}
    )

    conn = storage.init_db(settings.db_path)
    storage.insert_new_article(
        conn,
        FetchedItem(
            source="Soompi",
            title="BLACKPINK's MV Becomes 1st K-Pop Group MV Ever To Hit 2.4 Billion Views",
            url="https://www.soompi.com/article/blackpink-record",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="",
            fingerprint="fp-milestone",
        ),
    )
    conn.close()

    captured_flags: dict[str, bool] = {}

    def _fake_classify(self, item, *, france_flag, milestone_flag=False):
        captured_flags["milestone_flag"] = milestone_flag
        classification = ClassificationResult(
            category=Category.COMEBACK_SORTIE,
            importance=Importance.MAJEUR,
            virality=Virality.ELEVE,
            virality_reason="Test.",
            artists=["BLACKPINK"],
        )
        return classification, 10, 5

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "classify", _fake_classify)
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "write",
        lambda self, item, classification, route: (
            WritingResult(summary_fr="Résumé.", tweet_draft="Un tweet."),
            3,
            2,
        ),
    )
    respx.post(settings.discord_webhook_route_a).mock(return_value=httpx.Response(204))

    run_cycle(settings, limit=10, dry_run=False)

    assert captured_flags["milestone_flag"] is True


@respx.mock
def test_run_cycle_envoie_les_articles_filtres_vers_info_a_verifier(
    settings, tmp_path, monkeypatch
):
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")
    artist_tiers_path = tmp_path / "artist_tiers.yaml"
    artist_tiers_path.write_text("{}\n", encoding="utf-8")
    review_url = "https://discord.com/api/webhooks/fake/verif"
    settings = settings.model_copy(
        update={
            "sources_path": sources_path,
            "artist_tiers_path": artist_tiers_path,
            "discord_webhook_info_a_verifier": review_url,
        }
    )

    conn = storage.init_db(settings.db_path)
    storage.insert_new_article(
        conn,
        FetchedItem(
            source="Yonhap Culture",
            title="Ranking putaclic sans intérêt",
            url="https://en.yna.co.kr/article/bruit",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="",
            fingerprint="fp-bruit",
        ),
    )
    conn.close()

    classification = ClassificationResult(
        category=Category.BRUIT_INUTILE, importance=Importance.MINEUR, artists=[]
    )
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "classify",
        lambda self, item, *, france_flag, milestone_flag=False: (classification, 10, 5),
    )
    review_mock = respx.post(review_url).mock(return_value=httpx.Response(204))

    stats = run_cycle(settings, limit=10, dry_run=False)

    assert stats.filtered == 1
    assert stats.filtered_reviewed == 1
    assert review_mock.call_count == 1
    conn = storage.init_db(settings.db_path)
    [record] = storage.pending(conn, ArticleStatus.FILTERED_SENT)
    conn.close()
    assert record.category == Category.BRUIT_INUTILE


@respx.mock
def test_run_cycle_sans_webhook_verif_laisse_les_articles_filtres_intacts(
    settings, tmp_path, monkeypatch
):
    """Comportement par défaut (webhook non configuré) inchangé : les articles BRUIT_INUTILE
    restent simplement FILTERED, jamais envoyés nulle part — voir T13. Aucune route respx
    enregistrée : le test échoue si un appel HTTP inattendu était tenté. `discord_webhook_
    info_a_verifier` forcé à None explicitement (et pas juste laissé par défaut) pour rester
    isolé d'un vrai `.env` local qui en définirait un."""
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")
    artist_tiers_path = tmp_path / "artist_tiers.yaml"
    artist_tiers_path.write_text("{}\n", encoding="utf-8")
    settings = settings.model_copy(
        update={
            "sources_path": sources_path,
            "artist_tiers_path": artist_tiers_path,
            "discord_webhook_info_a_verifier": None,
        }
    )

    conn = storage.init_db(settings.db_path)
    storage.insert_new_article(
        conn,
        FetchedItem(
            source="Yonhap Culture",
            title="Ranking putaclic sans intérêt",
            url="https://en.yna.co.kr/article/bruit2",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="",
            fingerprint="fp-bruit2",
        ),
    )
    conn.close()

    classification = ClassificationResult(
        category=Category.BRUIT_INUTILE, importance=Importance.MINEUR, artists=[]
    )
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "classify",
        lambda self, item, *, france_flag, milestone_flag=False: (classification, 10, 5),
    )

    stats = run_cycle(settings, limit=10, dry_run=False)

    assert stats.filtered == 1
    assert stats.filtered_reviewed == 0
    conn = storage.init_db(settings.db_path)
    [record] = storage.pending(conn, ArticleStatus.FILTERED)
    conn.close()
    assert record.category == Category.BRUIT_INUTILE
