from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from kpop_bot import social_pipeline, storage
from kpop_bot.models import (
    Category,
    ClassificationResult,
    FetchedItem,
    Importance,
    Route,
    SocialVisualContent,
    Virality,
    WritingResult,
)
from kpop_bot.settings import Settings

_WEBHOOK = "https://discord.com/api/webhooks/fake/social"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Tous les champs optionnels explicités (même à None) — voir test_pipeline.py pour la
    raison (isolation d'un vrai `.env` local)."""
    return Settings(
        gemini_api_key="test-key",
        gemini_api_key_2=None,
        discord_webhook_route_a="https://discord.com/api/webhooks/fake/a",
        discord_webhook_route_b="https://discord.com/api/webhooks/fake/b",
        discord_webhook_social=None,
        db_path=tmp_path / "test.db",
        media_library_path=tmp_path / "media_library",
        social_visual_template_path=Path(__file__).resolve().parent.parent
        / "templates"
        / "social_post.html",
        social_visual_instagram_template_path=Path(__file__).resolve().parent.parent
        / "templates"
        / "social_post_instagram.html",
    )


def _seed_sent_article(
    settings: Settings,
    *,
    image_url: str | None = None,
    artists: list[str] | None = None,
    headline_fr: str = "Un titre de test",
    key_points_fr: list[str] | None = None,
) -> int:
    """Un seul article SENT par test — un fingerprint fixe suffit, chaque test utilise sa propre
    base (`settings.db_path` sous `tmp_path`)."""
    conn = storage.init_db(settings.db_path)
    article_id = storage.insert_new_article(
        conn,
        FetchedItem(
            source="Soompi",
            title="Article de test",
            url="https://www.soompi.com/article/test",
            published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
            raw_summary="Résumé brut.",
            fingerprint="fp-social-visual-test",
            image_url=image_url,
        ),
    )
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE,
        importance=Importance.MODERE,
        virality=Virality.MODERE,
        virality_reason="Test.",
        artists=artists or [],
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Un tweet de test.")
    social_visual = SocialVisualContent(
        headline_fr=headline_fr, key_points_fr=key_points_fr or ["Point 1.", "Point 2."]
    )
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
        social_visual=social_visual,
    )
    storage.mark_sent(conn, article_id)
    conn.close()
    return article_id


class _FakeRenderer:
    """Remplace SocialVisualRenderer (Playwright réel) — pas de navigateur dans les tests.
    `render()` est appelé deux fois par article (gabarit TikTok puis Instagram, voir
    social_pipeline._TIKTOK_DIMENSIONS/_INSTAGRAM_DIMENSIONS) — `calls` garde trace des deux."""

    instances: list[_FakeRenderer] = []

    def __init__(self) -> None:
        self.calls: list[tuple[Path, int, int, bytes, str, list[str], str, str]] = []
        self.raise_on_render = False
        _FakeRenderer.instances.append(self)

    def __enter__(self) -> _FakeRenderer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def render(
        self,
        *,
        template_path: Path,
        width: int,
        height: int,
        image_bytes: bytes,
        headline: str,
        key_points: list[str],
        category_label: str,
        formatted_date: str,
    ) -> bytes:
        if self.raise_on_render:
            raise RuntimeError("échec de rendu simulé")
        self.calls.append(
            (
                template_path,
                width,
                height,
                image_bytes,
                headline,
                key_points,
                category_label,
                formatted_date,
            )
        )
        return b"fake-png-bytes"


@pytest.fixture(autouse=True)
def _reset_fake_renderer():
    _FakeRenderer.instances = []
    yield
    _FakeRenderer.instances = []


def test_run_social_visuals_no_op_si_webhook_absent(settings, monkeypatch):
    _seed_sent_article(settings, image_url="https://example.com/photo.jpg")
    monkeypatch.setattr(social_pipeline.visual_generator, "SocialVisualRenderer", _FakeRenderer)

    stats = social_pipeline.run_social_visuals(settings)

    assert stats.candidates == 0
    assert stats.sent == 0
    assert _FakeRenderer.instances == []


@respx.mock
def test_run_social_visuals_envoie_avec_image_rss(settings, monkeypatch):
    settings = settings.model_copy(update={"discord_webhook_social": _WEBHOOK})
    _seed_sent_article(settings, image_url="https://example.com/photo.jpg")
    monkeypatch.setattr(social_pipeline.visual_generator, "SocialVisualRenderer", _FakeRenderer)
    monkeypatch.setattr(
        social_pipeline.visual_generator, "download_image", lambda url, *, timeout: b"rss-bytes"
    )
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(204))

    stats = social_pipeline.run_social_visuals(settings)

    assert stats.candidates == 1
    assert stats.sent == 1
    assert stats.skipped_no_image == 0
    [renderer] = _FakeRenderer.instances
    assert [call[:3] for call in renderer.calls] == [
        (settings.social_visual_template_path, 1080, 1920),
        (settings.social_visual_instagram_template_path, 1080, 1350),
    ]
    assert all(
        call[3:]
        == (
            b"rss-bytes",
            "Un titre de test",
            ["Point 1.", "Point 2."],
            "RELEASE",
            "24 juillet 2026",
        )
        for call in renderer.calls
    )

    conn = storage.init_db(settings.db_path)
    assert storage.pending_social_visuals(conn, limit=10) == []
    conn.close()


@respx.mock
def test_run_social_visuals_repli_media_library_si_pas_image_rss(settings, monkeypatch, tmp_path):
    settings = settings.model_copy(update={"discord_webhook_social": _WEBHOOK})
    _seed_sent_article(settings, image_url=None, artists=["Groupe X"])
    fallback_image = tmp_path / "fallback.jpg"
    fallback_image.write_bytes(b"media-library-bytes")

    monkeypatch.setattr(social_pipeline.visual_generator, "SocialVisualRenderer", _FakeRenderer)
    monkeypatch.setattr(
        social_pipeline.media_library,
        "select_image_for_article",
        lambda path, *, artists: fallback_image,
    )
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(204))

    stats = social_pipeline.run_social_visuals(settings)

    assert stats.sent == 1
    [renderer] = _FakeRenderer.instances
    assert len(renderer.calls) == 2  # gabarit TikTok puis gabarit Instagram
    assert all(
        call[3:]
        == (
            b"media-library-bytes",
            "Un titre de test",
            ["Point 1.", "Point 2."],
            "RELEASE",
            "24 juillet 2026",
        )
        for call in renderer.calls
    )


def test_run_social_visuals_skip_si_aucune_image_disponible(settings, monkeypatch):
    settings = settings.model_copy(update={"discord_webhook_social": _WEBHOOK})
    _seed_sent_article(settings, image_url=None)
    monkeypatch.setattr(social_pipeline.visual_generator, "SocialVisualRenderer", _FakeRenderer)
    monkeypatch.setattr(
        social_pipeline.media_library, "select_image_for_article", lambda path, *, artists: None
    )

    stats = social_pipeline.run_social_visuals(settings)

    assert stats.skipped_no_image == 1
    assert stats.sent == 0
    [renderer] = _FakeRenderer.instances  # instancié pour le batch, mais .render() jamais appelé
    assert renderer.calls == []

    conn = storage.init_db(settings.db_path)
    assert len(storage.pending_social_visuals(conn, limit=10)) == 1  # repris au prochain run
    conn.close()


def test_run_social_visuals_echec_de_rendu_non_bloquant(settings, monkeypatch):
    settings = settings.model_copy(update={"discord_webhook_social": _WEBHOOK})
    _seed_sent_article(settings, image_url="https://example.com/photo.jpg")

    def _fake_renderer_factory() -> _FakeRenderer:
        instance = _FakeRenderer()
        instance.raise_on_render = True
        return instance

    monkeypatch.setattr(
        social_pipeline.visual_generator, "SocialVisualRenderer", _fake_renderer_factory
    )
    monkeypatch.setattr(
        social_pipeline.visual_generator, "download_image", lambda url, *, timeout: b"rss-bytes"
    )

    stats = social_pipeline.run_social_visuals(settings)

    assert stats.render_failed == 1
    assert stats.sent == 0
    conn = storage.init_db(settings.db_path)
    assert len(storage.pending_social_visuals(conn, limit=10)) == 1  # repris au prochain run
    conn.close()


@respx.mock
def test_run_social_visuals_echec_envoi_discord_non_bloquant(settings, monkeypatch):
    settings = settings.model_copy(update={"discord_webhook_social": _WEBHOOK})
    _seed_sent_article(settings, image_url="https://example.com/photo.jpg")
    monkeypatch.setattr(social_pipeline.visual_generator, "SocialVisualRenderer", _FakeRenderer)
    monkeypatch.setattr(
        social_pipeline.visual_generator, "download_image", lambda url, *, timeout: b"rss-bytes"
    )
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(500))

    stats = social_pipeline.run_social_visuals(settings)

    assert stats.send_failed == 1
    assert stats.sent == 0
    conn = storage.init_db(settings.db_path)
    assert len(storage.pending_social_visuals(conn, limit=10)) == 1
    conn.close()


def test_run_social_visuals_dry_run_ne_declenche_rien(settings, monkeypatch):
    settings = settings.model_copy(update={"discord_webhook_social": _WEBHOOK})
    _seed_sent_article(settings, image_url="https://example.com/photo.jpg")
    monkeypatch.setattr(social_pipeline.visual_generator, "SocialVisualRenderer", _FakeRenderer)

    stats = social_pipeline.run_social_visuals(settings, dry_run=True)

    assert stats.candidates == 1
    assert stats.sent == 0
    assert _FakeRenderer.instances == []


def test_run_social_visuals_respecte_social_visual_batch_limit(settings):
    settings = settings.model_copy(
        update={"discord_webhook_social": _WEBHOOK, "social_visual_batch_limit": 1}
    )
    conn = storage.init_db(settings.db_path)
    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    writing = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet.")
    for i in range(2):
        article_id = storage.insert_new_article(
            conn,
            FetchedItem(
                source="Soompi",
                title=f"Article {i}",
                url=f"https://x/{i}",
                published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
                raw_summary="résumé",
                fingerprint=f"fp-limit-{i}",
            ),
        )
        storage.save_analysis(
            conn, article_id, classification, Route.B, False, writing, 10, 5, "v1"
        )
        storage.mark_sent(conn, article_id)
    conn.close()

    stats = social_pipeline.run_social_visuals(settings, dry_run=True)

    assert stats.candidates == 1


# --- _resolve_image_bytes en isolation. ---


def test_resolve_image_bytes_utilise_rss_en_priorite(settings, monkeypatch):
    monkeypatch.setattr(
        social_pipeline.visual_generator, "download_image", lambda url, *, timeout: b"rss-bytes"
    )
    article = _make_record(image_url="https://example.com/photo.jpg", artists=["Groupe X"])
    assert social_pipeline._resolve_image_bytes(article, settings) == b"rss-bytes"


def test_resolve_image_bytes_repli_media_library_si_telechargement_echoue(
    settings, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        social_pipeline.visual_generator, "download_image", lambda url, *, timeout: None
    )
    fallback_image = tmp_path / "fallback.jpg"
    fallback_image.write_bytes(b"media-library-bytes")
    monkeypatch.setattr(
        social_pipeline.media_library,
        "select_image_for_article",
        lambda path, *, artists: fallback_image,
    )
    article = _make_record(image_url="https://example.com/photo.jpg", artists=["Groupe X"])
    assert social_pipeline._resolve_image_bytes(article, settings) == b"media-library-bytes"


def test_resolve_image_bytes_none_si_rien_de_disponible(settings, monkeypatch):
    monkeypatch.setattr(
        social_pipeline.media_library, "select_image_for_article", lambda path, *, artists: None
    )
    article = _make_record(image_url=None, artists=[])
    assert social_pipeline._resolve_image_bytes(article, settings) is None


def _make_record(
    *,
    image_url: str | None,
    artists: list[str],
    category: Category | None = None,
    importance: Importance | None = None,
    virality: Virality | None = None,
    tweet_draft: str = "Tweet.",
    headline_fr: str | None = None,
    key_points_fr: list[str] | None = None,
    summary_fr: str | None = None,
    published_at: dt.datetime | None = None,
):
    from kpop_bot.models import ArticleRecord, ArticleStatus

    now = published_at or dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
    return ArticleRecord(
        id=1,
        fingerprint="fp",
        source="Soompi",
        title="Titre",
        url="https://x",
        published_at=now,
        raw_summary="résumé",
        status=ArticleStatus.SENT,
        route=Route.B,
        category=category,
        importance=importance,
        virality=virality,
        tweet_draft=tweet_draft,
        headline_fr=headline_fr,
        key_points_fr=key_points_fr or [],
        summary_fr=summary_fr,
        artists=artists,
        image_url=image_url,
        created_at=now,
    )


# --- _format_date_fr / _category_label en isolation. ---


def test_format_date_fr_sans_heure():
    moment = dt.datetime(2026, 8, 14, 10, 7, tzinfo=dt.UTC)
    assert social_pipeline._format_date_fr(moment) == "14 août 2026"


# --- _fallback_headline / _headline_text / _key_points en isolation. ---


def test_fallback_headline_tronque_a_14_mots():
    summary = " ".join(f"mot{i}" for i in range(20))
    headline = social_pipeline._fallback_headline(summary)
    assert headline == " ".join(f"mot{i}" for i in range(14))


def test_fallback_headline_retire_la_ponctuation_finale():
    assert social_pipeline._fallback_headline("Un résumé court.") == "Un résumé court"


def test_headline_text_utilise_headline_fr_si_present():
    article = _make_record(
        image_url=None, artists=[], headline_fr="Une accroche dédiée", summary_fr="Résumé complet."
    )
    assert social_pipeline._headline_text(article) == "Une accroche dédiée"


def test_headline_text_repli_sur_summary_fr_si_headline_fr_absent():
    """Articles migrés avant l'introduction de headline_fr (colonne NULL) — voir
    storage._MIGRATED_COLUMNS."""
    article = _make_record(
        image_url=None, artists=[], headline_fr=None, summary_fr="Résumé complet."
    )
    assert social_pipeline._headline_text(article) == "Résumé complet"


def test_fallback_key_points_decoupe_summary_fr_en_phrases():
    points = social_pipeline._fallback_key_points("Première phrase. Deuxième phrase.")
    assert points == ["Première phrase.", "Deuxième phrase."]


def test_fallback_key_points_liste_vide_si_summary_fr_absent():
    assert social_pipeline._fallback_key_points("") == []


def test_fallback_key_points_plafonne_a_3():
    summary = ". ".join(f"Phrase {i}" for i in range(5)) + "."
    assert len(social_pipeline._fallback_key_points(summary)) == 3


def test_key_points_utilise_key_points_fr_si_present():
    """Rédigés ensemble avec headline_fr par SocialVisualContent — préférés au découpage
    mécanique de summary_fr, qui peut se répéter avec le titre (voir cahier des charges)."""
    article = _make_record(
        image_url=None,
        artists=[],
        key_points_fr=["Un vrai point clé.", "Un autre, distinct du titre."],
        summary_fr="Un résumé qui ne devrait pas être utilisé ici.",
    )
    assert social_pipeline._key_points(article) == [
        "Un vrai point clé.",
        "Un autre, distinct du titre.",
    ]


def test_key_points_repli_sur_summary_fr_si_key_points_fr_absent():
    """Articles migrés avant l'introduction de key_points_fr (colonne '[]' en base), ou échec
    du 3e appel Gemini — voir run_social_visuals."""
    article = _make_record(
        image_url=None, artists=[], key_points_fr=None, summary_fr="Première phrase. Deuxième."
    )
    assert social_pipeline._key_points(article) == ["Première phrase.", "Deuxième."]


def test_category_label_derive_depuis_classification():
    """Même fonction pure que le tag préfixé au tweet Twitter (T5quater) — cohérence garantie
    entre les deux, aucun appel IA supplémentaire."""
    article = _make_record(
        image_url=None,
        artists=[],
        category=Category.SCANDALE_DRAMA,
        importance=Importance.MODERE,
        virality=Virality.MODERE,
    )
    assert social_pipeline._category_label(article) == "GOSSIP"


def test_category_label_repli_info_si_classification_absente():
    """Ne devrait jamais arriver pour un article SENT (category/importance toujours fixés par
    save_analysis) — filet défensif plutôt qu'une exception."""
    article = _make_record(image_url=None, artists=[])
    assert social_pipeline._category_label(article) == "INFO"
