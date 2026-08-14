from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from kpop_bot import analyzer, scraper, storage
from kpop_bot.models import (
    TWEET_TAG_LABELS,
    ArticleStatus,
    Category,
    ClassificationResult,
    FetchedItem,
    Importance,
    InstagramNewsPost,
    Route,
    TweetTag,
    Virality,
    WritingResult,
)
from kpop_bot.pipeline import _instagram_news_api_keys, resend_sent, run_cycle
from kpop_bot.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Tous les champs optionnels sont explicités (même à None) pour rester isolé d'un vrai
    `.env` local qui les définirait — sinon un test peut silencieusement déclencher un vrai
    appel réseau (voir T14 : `DISCORD_WEBHOOK_INSTAGRAM_NEWS` dans `.env` a fait échouer un
    test qui ne s'y attendait pas, faute de cette isolation explicite)."""
    return Settings(
        gemini_api_key="test-key",
        gemini_api_key_2=None,
        discord_webhook_route_a="https://discord.com/api/webhooks/fake/a",
        discord_webhook_route_b="https://discord.com/api/webhooks/fake/b",
        discord_webhook_concert=None,
        discord_webhook_info_a_verifier=None,
        discord_webhook_instagram_news=None,
        db_path=tmp_path / "test.db",
    )


@pytest.fixture(autouse=True)
def _no_scraping(monkeypatch):
    """Neutralise le scraping de page (T18) par défaut dans tous ces tests — même principe que
    l'isolation des webhooks ci-dessus : un test ne doit jamais déclencher un vrai appel
    réseau. Un test dédié à l'intégration du scraping le réactive explicitement (monkeypatch
    local, qui prime sur celui-ci)."""
    monkeypatch.setattr(scraper, "fetch_article_page", lambda url, *, timeout: None)


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
        lambda self, item, *, france_flag, milestone_flag=False, page_text=None: (
            classification,
            10,
            5,
        ),
    )
    monkeypatch.setattr(
        analyzer.GeminiAnalyzer,
        "write",
        lambda self, item, classification, route, *, page_text=None: (writing, 3, 2),
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

    def _fake_classify(self, item, *, france_flag, milestone_flag=False, page_text=None):
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
        lambda self, item, classification, route, *, page_text=None: (
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
        lambda self, item, *, france_flag, milestone_flag=False, page_text=None: (
            classification,
            10,
            5,
        ),
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
        lambda self, item, *, france_flag, milestone_flag=False, page_text=None: (
            classification,
            10,
            5,
        ),
    )

    stats = run_cycle(settings, limit=10, dry_run=False)

    assert stats.filtered == 1
    assert stats.filtered_reviewed == 0
    conn = storage.init_db(settings.db_path)
    [record] = storage.pending(conn, ArticleStatus.FILTERED)
    conn.close()
    assert record.category == Category.BRUIT_INUTILE


# --- T18 (remplace T14) : post Instagram dédié (Route A/CONCERT), clé 2 en priorité, salon
# séparé. ---


def test_instagram_news_api_keys_inverse_l_ordre_si_2e_cle_presente(settings):
    s = settings.model_copy(update={"gemini_api_key": "clé-1", "gemini_api_key_2": "clé-2"})
    assert _instagram_news_api_keys(s) == ["clé-2", "clé-1"]


def test_instagram_news_api_keys_sans_2e_cle_ne_contient_que_la_premiere(settings):
    s = settings.model_copy(update={"gemini_api_key": "clé-1", "gemini_api_key_2": None})
    assert _instagram_news_api_keys(s) == ["clé-1"]


def _seed_two_articles(settings: Settings) -> None:
    conn = storage.init_db(settings.db_path)
    storage.insert_new_article(
        conn,
        FetchedItem(
            source="Soompi",
            title="Concert à Paris",
            url="https://www.soompi.com/article/route-a",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="",
            fingerprint="fp-route-a",
        ),
    )
    storage.insert_new_article(
        conn,
        FetchedItem(
            source="Soompi",
            title="Petite actu",
            url="https://www.soompi.com/article/route-b",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="",
            fingerprint="fp-route-b",
        ),
    )
    conn.close()


def _fake_classify_route_a_ou_b(self, item, *, france_flag, milestone_flag=False, page_text=None):
    if item.url.endswith("route-a"):
        classification = ClassificationResult(
            category=Category.CONCERT_EVENEMENT_FRANCE,
            importance=Importance.MAJEUR,
            virality=Virality.ELEVE,
            virality_reason="Test.",
            artists=[],
        )
    else:
        classification = ClassificationResult(
            category=Category.COMEBACK_SORTIE,
            importance=Importance.MODERE,
            virality=Virality.FAIBLE,
            virality_reason="Test.",
            artists=[],
        )
    return classification, 10, 5


def _fake_write(self, item, classification, route, *, page_text=None):
    return WritingResult(summary_fr="Résumé.", tweet_draft="Un tweet."), 3, 2


@respx.mock
def test_run_cycle_genere_le_post_instagram_uniquement_pour_route_a(
    settings, tmp_path, monkeypatch
):
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")
    artist_tiers_path = tmp_path / "artist_tiers.yaml"
    artist_tiers_path.write_text("{}\n", encoding="utf-8")
    instagram_url = "https://discord.com/api/webhooks/fake/instagram"
    settings = settings.model_copy(
        update={
            "sources_path": sources_path,
            "artist_tiers_path": artist_tiers_path,
            "discord_webhook_instagram_news": instagram_url,
        }
    )
    _seed_two_articles(settings)

    instagram_calls: list[str] = []

    def _fake_write_instagram_news(self, item, classification, *, page_text=None):
        instagram_calls.append(item.url)
        return (
            InstagramNewsPost(
                hook="H.",
                paragraph_context="C.",
                paragraph_detail="D.",
                engagement_question="Q ?",
                hashtags=["#kpop", "#comeback"],
            ),
            5,
            3,
        )

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "classify", _fake_classify_route_a_ou_b)
    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write", _fake_write)
    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write_instagram_news", _fake_write_instagram_news)
    respx.post(settings.discord_webhook_route_a).mock(return_value=httpx.Response(204))
    respx.post(settings.discord_webhook_route_b).mock(return_value=httpx.Response(204))
    respx.post(instagram_url).mock(return_value=httpx.Response(204))

    stats = run_cycle(settings, limit=10, dry_run=False)

    assert instagram_calls == ["https://www.soompi.com/article/route-a"]
    assert stats.instagram_news_generated == 1
    assert stats.instagram_news_sent == 1


@respx.mock
def test_run_cycle_echec_generation_instagram_news_n_empeche_pas_l_envoi_principal(
    settings, tmp_path, monkeypatch
):
    """Un post Instagram raté est un bonus perdu, pas une raison de marquer l'article FAILED
    ni d'arrêter le cycle — même principe qu'en T14."""
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")
    artist_tiers_path = tmp_path / "artist_tiers.yaml"
    artist_tiers_path.write_text("{}\n", encoding="utf-8")
    instagram_url = "https://discord.com/api/webhooks/fake/instagram"
    settings = settings.model_copy(
        update={
            "sources_path": sources_path,
            "artist_tiers_path": artist_tiers_path,
            "discord_webhook_instagram_news": instagram_url,
        }
    )
    conn = storage.init_db(settings.db_path)
    storage.insert_new_article(
        conn,
        FetchedItem(
            source="Soompi",
            title="Concert à Paris",
            url="https://www.soompi.com/article/route-a",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="",
            fingerprint="fp-route-a",
        ),
    )
    conn.close()

    def _raise_analysis_error(self, item, classification, *, page_text=None):
        raise analyzer.AnalysisError("boom")

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "classify", _fake_classify_route_a_ou_b)
    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write", _fake_write)
    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write_instagram_news", _raise_analysis_error)
    respx.post(settings.discord_webhook_route_a).mock(return_value=httpx.Response(204))

    stats = run_cycle(settings, limit=10, dry_run=False)

    assert stats.instagram_news_generation_failed == 1
    assert stats.analysis_failed == 0  # l'article lui-même n'est pas en échec
    assert stats.sent == 1  # envoyé normalement malgré l'échec du post Instagram


@respx.mock
def test_run_cycle_sans_webhook_instagram_news_ne_genere_ni_n_envoie_rien(
    settings, tmp_path, monkeypatch
):
    """Comportement par défaut (webhook non configuré) inchangé : aucun 3e appel Gemini,
    aucun envoi vers un salon Instagram — même principe qu'en T14."""
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []\n", encoding="utf-8")
    artist_tiers_path = tmp_path / "artist_tiers.yaml"
    artist_tiers_path.write_text("{}\n", encoding="utf-8")
    settings = settings.model_copy(
        update={
            "sources_path": sources_path,
            "artist_tiers_path": artist_tiers_path,
            "discord_webhook_instagram_news": None,
        }
    )
    _seed_two_articles(settings)

    def _fail_if_called(self, item, classification, *, page_text=None):
        pytest.fail("write_instagram_news ne doit jamais être appelé sans webhook configuré")

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "classify", _fake_classify_route_a_ou_b)
    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write", _fake_write)
    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write_instagram_news", _fail_if_called)
    respx.post(settings.discord_webhook_route_a).mock(return_value=httpx.Response(204))
    respx.post(settings.discord_webhook_route_b).mock(return_value=httpx.Response(204))

    stats = run_cycle(settings, limit=10, dry_run=False)

    assert stats.instagram_news_generated == 0
    assert stats.instagram_news_sent == 0


# --- T18 : scraping best-effort de la page article, avant classify(). ---


@respx.mock
def test_run_cycle_transmet_le_texte_scrape_a_classify_et_write(settings, tmp_path, monkeypatch):
    """Le texte scrapé (T18) doit être transmis tel quel aux deux appels — c'est ce qui permet
    de nommer précisément un artiste absent du titre/extrait RSS."""
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
            title="Petite actu",
            url="https://www.soompi.com/article/scrape",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="",
            fingerprint="fp-scrape",
        ),
    )
    conn.close()

    monkeypatch.setattr(
        scraper,
        "fetch_article_page",
        lambda url, *, timeout: scraper.ScrapedArticle(
            text="Le membre Jane du groupe X a confirmé son retour en solo.",
            main_image_url="https://www.soompi.com/main.jpg",
            extra_image_urls=["https://www.soompi.com/extra.jpg"],
        ),
    )

    captured: dict[str, str | None] = {}

    def _fake_classify(self, item, *, france_flag, milestone_flag=False, page_text=None):
        captured["classify_page_text"] = page_text
        return (
            ClassificationResult(
                category=Category.COMEBACK_SORTIE,
                importance=Importance.MODERE,
                virality=Virality.FAIBLE,
                virality_reason="Test.",
                artists=["Groupe X"],
            ),
            10,
            5,
        )

    def _fake_write(self, item, classification, route, *, page_text=None):
        captured["write_page_text"] = page_text
        return WritingResult(summary_fr="Résumé.", tweet_draft="Un tweet."), 3, 2

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "classify", _fake_classify)
    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write", _fake_write)
    respx.post(settings.discord_webhook_route_b).mock(return_value=httpx.Response(204))

    stats = run_cycle(settings, limit=10, dry_run=False)

    assert stats.scraped_pages == 1
    expected_text = "Le membre Jane du groupe X a confirmé son retour en solo."
    assert captured["classify_page_text"] == expected_text
    assert captured["write_page_text"] == expected_text
    conn = storage.init_db(settings.db_path)
    [record] = storage.pending(conn, ArticleStatus.SENT)
    conn.close()
    assert record.image_url == "https://www.soompi.com/main.jpg"
    assert record.extra_image_urls == ["https://www.soompi.com/extra.jpg"]


@respx.mock
def test_run_cycle_sans_page_exploitable_continue_avec_le_seul_extrait_rss(
    settings, tmp_path, monkeypatch
):
    """Le scraping est best-effort — `None` (page injoignable ou sans texte exploitable) ne
    doit jamais bloquer le cycle, seulement laisser page_text/image_url absents."""
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
            title="Petite actu",
            url="https://www.soompi.com/article/no-scrape",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="",
            fingerprint="fp-no-scrape",
        ),
    )
    conn.close()
    # La fixture _no_scraping (autouse) renvoie déjà None — pas de monkeypatch supplémentaire.

    monkeypatch.setattr(analyzer.GeminiAnalyzer, "classify", _fake_classify_route_a_ou_b)
    monkeypatch.setattr(analyzer.GeminiAnalyzer, "write", _fake_write)
    respx.post(settings.discord_webhook_route_b).mock(return_value=httpx.Response(204))

    stats = run_cycle(settings, limit=10, dry_run=False)

    assert stats.scraped_pages == 0
    assert stats.sent == 1
