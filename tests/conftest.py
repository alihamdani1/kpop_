from __future__ import annotations

import datetime as dt

import pytest

from kpop_bot.models import ArticleRecord, ArticleStatus, Category, Importance, Route, Virality


@pytest.fixture
def make_article():
    """Fabrique un ArticleRecord minimal, avec surcharges au besoin."""

    def _make(**overrides) -> ArticleRecord:
        now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.UTC)
        defaults = dict(
            id=1,
            fingerprint="abc123",
            source="Soompi",
            title="Groupe X annonce un comeback",
            url="https://www.soompi.com/article/123",
            published_at=now,
            raw_summary="Le groupe X a annoncé son retour avec un nouveau single.",
            status=ArticleStatus.ANALYZED,
            category=Category.COMEBACK_SORTIE,
            importance=Importance.MODERE,
            virality=Virality.MODERE,
            virality_reason="Comeback attendu par la fanbase.",
            route=Route.B,
            france_override=False,
            summary_fr="Le groupe X revient avec un nouveau single. Sortie le mois prochain.",
            video_summary=None,
            tweet_draft="Le groupe X annonce son comeback avec un single ! 🎶 #KPop #GroupeX",
            artists=["Groupe X"],
            tokens_in=100,
            tokens_out=50,
            prompt_version="v1",
            created_at=now,
            sent_at=None,
            error=None,
        )
        defaults.update(overrides)
        return ArticleRecord(**defaults)

    return _make
