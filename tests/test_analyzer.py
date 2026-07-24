from __future__ import annotations

import json

import pytest
from google.genai import errors

from kpop_bot import analyzer
from kpop_bot.models import Category, Route

# --- Filet de sécurité mots-clés France : fonction pure, aucun réseau. ---

_KEYWORDS = ["Paris", "France", "Accor Arena", "Stade de France", "Zénith"]


def test_match_simple(make_article):
    article = make_article(title="Groupe X en concert à Paris cet été")
    assert analyzer.matches_france_keywords(article, _KEYWORDS) is True


def test_match_insensible_a_la_casse(make_article):
    article = make_article(title="GROUPE X À PARIS", raw_summary="")
    assert analyzer.matches_france_keywords(article, _KEYWORDS) is True


def test_match_dans_le_resume_aussi(make_article):
    article = make_article(
        title="Groupe X annonce une tournée mondiale",
        raw_summary="La tournée passera notamment par l'Accor Arena.",
    )
    assert analyzer.matches_france_keywords(article, _KEYWORDS) is True


def test_aucun_match_sur_article_sans_lien_france(make_article):
    article = make_article(
        title="Groupe X annonce un comeback",
        raw_summary="Le nouveau single sortira le mois prochain.",
    )
    assert analyzer.matches_france_keywords(article, _KEYWORDS) is False


def test_pas_de_faux_positif_sur_mot_contenant_le_mot_cle(make_article):
    """'Paris' ne doit pas matcher à l'intérieur de 'Parisian' (frontière de mot)."""
    article = make_article(title="A Parisian-style fashion collab", raw_summary="")
    assert analyzer.matches_france_keywords(article, _KEYWORDS) is False


# --- GeminiAnalyzer : appels Gemini simulés, aucun réseau réel. ---


class _FakeUsage:
    prompt_token_count = 100
    candidates_token_count = 42


class _FakeResponse:
    def __init__(self, payload: dict):
        self.text = json.dumps(payload)
        self.usage_metadata = _FakeUsage()


@pytest.fixture
def gemini(monkeypatch):
    instance = analyzer.GeminiAnalyzer(
        api_key="test-key",
        model="gemini-3.6-flash",
        artist_tiers={"tier_1": ["BTS"], "tier_2": ["Groupe X"]},
    )
    return instance


@pytest.fixture
def gemini_with_fallback(monkeypatch):
    instance = analyzer.GeminiAnalyzer(
        api_key="test-key",
        model="gemini-3.1-flash-lite",
        fallback_model="gemini-3.6-flash",
        artist_tiers={"tier_1": ["BTS"], "tier_2": ["Groupe X"]},
    )
    return instance


def test_classify_happy_path(gemini, monkeypatch, make_article):
    payload = {
        "category": "COMEBACK_SORTIE",
        "importance": "MAJEUR",
        "virality": "ELEVE",
        "virality_reason": "Comeback très attendu.",
        "artists": ["Groupe X"],
    }
    monkeypatch.setattr(
        gemini._client.models, "generate_content", lambda **kwargs: _FakeResponse(payload)
    )
    article = make_article()
    result, tokens_in, tokens_out = gemini.classify(article, france_flag=False)
    assert result.category == Category.COMEBACK_SORTIE
    assert tokens_in == 100
    assert tokens_out == 42


def test_classify_filet_france_ecrase_la_categorie_du_modele(gemini, monkeypatch, make_article):
    """Même si le modèle répond autre chose, le filet mots-clés force la catégorie."""
    payload = {
        "category": "COMEBACK_SORTIE",  # l'IA n'a pas vu le lien avec la France
        "importance": "MODERE",
        "virality": "MODERE",
        "virality_reason": "Intérêt correct.",
        "artists": [],
    }
    monkeypatch.setattr(
        gemini._client.models, "generate_content", lambda **kwargs: _FakeResponse(payload)
    )
    article = make_article(title="Groupe X en concert à Paris")
    result, _, _ = gemini.classify(article, france_flag=True)
    assert result.category == Category.CONCERT_EVENEMENT_FRANCE


def test_classify_quota_depasse_leve_quota_exceeded(gemini, monkeypatch, make_article):
    """Sans modèle de secours configuré (fallback_model=None), l'erreur remonte directement —
    comportement inchangé, c'est le filet de sécurité du cycle qui prend le relais."""

    def _raise(**kwargs):
        raise errors.APIError(code=429, response_json={"error": {"message": "quota exceeded"}})

    monkeypatch.setattr(gemini._client.models, "generate_content", _raise)
    with pytest.raises(analyzer.QuotaExceededError):
        gemini.classify(make_article(), france_flag=False)


def test_classify_bascule_sur_le_secours_apres_429(gemini_with_fallback, monkeypatch, make_article):
    calls = []

    def _generate_content(*, model, **kwargs):
        calls.append(model)
        if model == "gemini-3.1-flash-lite":  # modèle principal — en échec
            raise errors.APIError(code=429, response_json={"error": {"message": "quota"}})
        return _FakeResponse(  # modèle de secours — répond normalement
            {
                "category": "COMEBACK_SORTIE",
                "importance": "MAJEUR",
                "virality": "ELEVE",
                "virality_reason": "Test.",
                "artists": [],
            }
        )

    monkeypatch.setattr(gemini_with_fallback._client.models, "generate_content", _generate_content)
    result, _, _ = gemini_with_fallback.classify(make_article(), france_flag=False)

    assert calls == ["gemini-3.1-flash-lite", "gemini-3.6-flash"]  # principal essayé d'abord
    assert result.category == Category.COMEBACK_SORTIE


def test_classify_leve_si_le_secours_echoue_aussi(gemini_with_fallback, monkeypatch, make_article):
    def _raise(**kwargs):
        raise errors.APIError(code=429, response_json={"error": {"message": "quota"}})

    monkeypatch.setattr(gemini_with_fallback._client.models, "generate_content", _raise)
    with pytest.raises(analyzer.QuotaExceededError):
        gemini_with_fallback.classify(make_article(), france_flag=False)


def test_classify_reponse_invalide_leve_analysis_error(gemini, monkeypatch, make_article):
    monkeypatch.setattr(
        gemini._client.models,
        "generate_content",
        lambda **kwargs: _FakeResponse({"category": "PAS_UNE_VRAIE_CATEGORIE"}),
    )
    with pytest.raises(analyzer.AnalysisError):
        gemini.classify(make_article(), france_flag=False)


def test_write_route_a_demande_le_resume_video(gemini, monkeypatch, make_article):
    captured_prompts: list[str] = []

    def _fake_generate(*, model, contents, config):
        captured_prompts.append(config.system_instruction)
        payload = {
            "summary_fr": "Résumé court.",
            "tweet_draft": "Un tweet.",
            "video_summary": "Résumé détaillé pour la vidéo.",
        }
        return _FakeResponse(payload)

    monkeypatch.setattr(gemini._client.models, "generate_content", _fake_generate)
    article = make_article()
    from kpop_bot.models import ClassificationResult, Importance

    classification = ClassificationResult(
        category=Category.CONCERT_EVENEMENT_FRANCE, importance=Importance.MAJEUR, artists=[]
    )
    result, _, _ = gemini.write(article, classification, Route.A)
    assert result.video_summary == "Résumé détaillé pour la vidéo."
    assert "video_summary" in captured_prompts[0]  # l'instruction vidéo est bien injectée


def test_write_route_b_ne_demande_pas_le_resume_video(gemini, monkeypatch, make_article):
    captured_prompts: list[str] = []

    def _fake_generate(*, model, contents, config):
        captured_prompts.append(config.system_instruction)
        return _FakeResponse({"summary_fr": "Résumé court.", "tweet_draft": "Un tweet."})

    monkeypatch.setattr(gemini._client.models, "generate_content", _fake_generate)
    from kpop_bot.models import ClassificationResult, Importance

    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    result, _, _ = gemini.write(make_article(), classification, Route.B)
    assert result.video_summary is None
    assert "video_summary" not in captured_prompts[0]
