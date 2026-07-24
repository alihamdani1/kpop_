from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from kpop_bot import storage
from kpop_bot.models import (
    Category,
    ClassificationResult,
    FetchedItem,
    Importance,
    Route,
    Virality,
    WritingResult,
)
from kpop_bot.pipeline import resend_sent
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
