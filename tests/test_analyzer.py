from __future__ import annotations

import json

import pytest
from google.genai import errors

from kpop_bot import analyzer
from kpop_bot.models import Category, Route, Virality

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


# --- Filet de sécurité record/palier viral (T13) : mots-clés + artiste connu, aucun réseau. ---

_MILESTONE_KEYWORDS = ["billion views", "million views", "billion streams", "record high"]
_ARTIST_TIERS = {"tier_1": ["BTS", "BLACKPINK"], "tier_2": ["Groupe X"]}


def test_milestone_match_mot_cle_et_artiste_connu(make_article):
    article = make_article(
        title="BLACKPINK's MV Becomes 1st K-Pop Group MV Ever To Hit 2.4 Billion Views",
        raw_summary="",
    )
    assert analyzer.matches_viral_milestone(article, _MILESTONE_KEYWORDS, _ARTIST_TIERS) is True


def test_milestone_aucun_match_sans_mot_cle(make_article):
    article = make_article(title="BLACKPINK annonce une tournée mondiale", raw_summary="")
    assert analyzer.matches_viral_milestone(article, _MILESTONE_KEYWORDS, _ARTIST_TIERS) is False


def test_milestone_aucun_match_mot_cle_sans_artiste_connu(make_article):
    """Un mot-clé de record seul ne suffit pas — évite les faux positifs hors-sujet (ex. un
    record de fréquentation d'un musée, sans lien avec un artiste K-pop répertorié)."""
    article = make_article(
        title="Local museum reports record high attendance this year", raw_summary=""
    )
    assert analyzer.matches_viral_milestone(article, _MILESTONE_KEYWORDS, _ARTIST_TIERS) is False


def test_milestone_match_dans_le_resume_aussi(make_article):
    article = make_article(
        title="Grosse annonce pour Groupe X",
        raw_summary="Leur dernier single vient de dépasser le milliard de vues (billion views).",
    )
    assert analyzer.matches_viral_milestone(article, _MILESTONE_KEYWORDS, _ARTIST_TIERS) is True


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
        api_keys=["test-key"],
        models=["gemini-3.6-flash"],
        artist_tiers={"tier_1": ["BTS"], "tier_2": ["Groupe X"]},
        min_seconds_between_calls=0,  # throttling réel désactivé pour des tests instantanés
    )
    return instance


@pytest.fixture
def gemini_with_fallback(monkeypatch):
    instance = analyzer.GeminiAnalyzer(
        api_keys=["test-key"],
        models=["gemini-3.1-flash-lite", "gemini-3.6-flash"],
        artist_tiers={"tier_1": ["BTS"], "tier_2": ["Groupe X"]},
        min_seconds_between_calls=0,
    )
    return instance


@pytest.fixture
def gemini_with_two_keys(monkeypatch):
    instance = analyzer.GeminiAnalyzer(
        api_keys=["test-key-1", "test-key-2"],
        models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"],
        artist_tiers={"tier_1": ["BTS"], "tier_2": ["Groupe X"]},
        min_seconds_between_calls=0,
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
        gemini._clients[0].models, "generate_content", lambda **kwargs: _FakeResponse(payload)
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
        gemini._clients[0].models, "generate_content", lambda **kwargs: _FakeResponse(payload)
    )
    article = make_article(title="Groupe X en concert à Paris")
    result, _, _ = gemini.classify(article, france_flag=True)
    assert result.category == Category.CONCERT_EVENEMENT_FRANCE


def test_classify_note_milestone_injectee_dans_le_prompt(gemini, monkeypatch, make_article):
    captured_prompts: list[str] = []

    def _fake_generate(*, model, contents, config):
        captured_prompts.append(config.system_instruction)
        return _FakeResponse(
            {
                "category": "COMEBACK_SORTIE",
                "importance": "MAJEUR",
                "virality": "ELEVE",
                "virality_reason": "Record notable.",
                "artists": ["BLACKPINK"],
            }
        )

    monkeypatch.setattr(gemini._clients[0].models, "generate_content", _fake_generate)
    article = make_article(title="BLACKPINK's MV hits 2.4 billion views")
    gemini.classify(article, france_flag=False, milestone_flag=True)
    assert "BRUIT_INUTILE" in captured_prompts[0]  # la note système rappelle bien la règle


def test_classify_filet_milestone_force_comeback_si_ia_maintient_bruit(
    gemini, monkeypatch, make_article
):
    """Dernier recours : l'IA a ignoré la note système et répond quand même BRUIT_INUTILE —
    le filet corrige quand même, avec une viralité par défaut explicable."""
    payload = {"category": "BRUIT_INUTILE", "importance": "MINEUR", "artists": ["BLACKPINK"]}
    monkeypatch.setattr(
        gemini._clients[0].models, "generate_content", lambda **kwargs: _FakeResponse(payload)
    )
    article = make_article(title="BLACKPINK's MV hits 2.4 billion views")
    result, _, _ = gemini.classify(article, france_flag=False, milestone_flag=True)
    assert result.category == Category.COMEBACK_SORTIE
    assert result.virality == Virality.ELEVE
    assert result.virality_reason is not None
    assert "record" in result.virality_reason.lower()


def test_classify_sans_flag_milestone_bruit_inutile_reste_inchange(
    gemini, monkeypatch, make_article
):
    """Régression : sans détection préalable (mot-clé + artiste connu), aucun forçage — le
    comportement d'avant T13 reste intact."""
    payload = {"category": "BRUIT_INUTILE", "importance": "MINEUR", "artists": []}
    monkeypatch.setattr(
        gemini._clients[0].models, "generate_content", lambda **kwargs: _FakeResponse(payload)
    )
    result, _, _ = gemini.classify(make_article(), france_flag=False, milestone_flag=False)
    assert result.category == Category.BRUIT_INUTILE


def test_classify_filet_milestone_n_ecrase_pas_une_categorie_deja_correcte(
    gemini, monkeypatch, make_article
):
    """Si l'IA a déjà suivi la note système, le filet ne doit rien recalculer par-dessus son
    jugement (viralité en particulier — ne pas remplacer une estimation réelle par le repli)."""
    payload = {
        "category": "COMEBACK_SORTIE",
        "importance": "MAJEUR",
        "virality": "VIRAL",
        "virality_reason": "Jugement du modèle, pas du filet.",
        "artists": ["BLACKPINK"],
    }
    monkeypatch.setattr(
        gemini._clients[0].models, "generate_content", lambda **kwargs: _FakeResponse(payload)
    )
    result, _, _ = gemini.classify(make_article(), france_flag=False, milestone_flag=True)
    assert result.virality == Virality.VIRAL  # pas écrasé par le repli ELEVE du filet
    assert result.virality_reason == "Jugement du modèle, pas du filet."


def test_classify_quota_depasse_leve_quota_exceeded(gemini, monkeypatch, make_article):
    """Sans modèle de secours configuré (chaîne à un seul modèle), l'erreur remonte
    directement — comportement inchangé, c'est le filet de sécurité du cycle qui prend le
    relais."""

    def _raise(**kwargs):
        raise errors.APIError(code=429, response_json={"error": {"message": "quota exceeded"}})

    monkeypatch.setattr(gemini._clients[0].models, "generate_content", _raise)
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

    monkeypatch.setattr(
        gemini_with_fallback._clients[0].models, "generate_content", _generate_content
    )
    result, _, _ = gemini_with_fallback.classify(make_article(), france_flag=False)

    assert calls == ["gemini-3.1-flash-lite", "gemini-3.6-flash"]  # principal essayé d'abord
    assert result.category == Category.COMEBACK_SORTIE


def test_classify_leve_si_le_secours_echoue_aussi(gemini_with_fallback, monkeypatch, make_article):
    def _raise(**kwargs):
        raise errors.APIError(code=429, response_json={"error": {"message": "quota"}})

    monkeypatch.setattr(gemini_with_fallback._clients[0].models, "generate_content", _raise)
    with pytest.raises(analyzer.QuotaExceededError):
        gemini_with_fallback.classify(make_article(), france_flag=False)


def test_classify_bascule_sur_la_2e_cle_api_apres_429_sur_toute_la_chaine(
    gemini_with_two_keys, monkeypatch, make_article
):
    """Les 3 modèles échouent en 429 sur la 1re clé -> repli sur la 2e clé, à partir du
    modèle principal de la chaîne."""
    calls = []

    def _generate_content_key_1(*, model, **kwargs):
        calls.append(("clé-1", model))
        raise errors.APIError(code=429, response_json={"error": {"message": "quota"}})

    def _generate_content_key_2(*, model, **kwargs):
        calls.append(("clé-2", model))
        if model != "gemini-3.5-flash-lite":  # seul le modèle principal répond, sur la 2e clé
            raise errors.APIError(code=429, response_json={"error": {"message": "quota"}})
        return _FakeResponse(
            {
                "category": "COMEBACK_SORTIE",
                "importance": "MAJEUR",
                "virality": "ELEVE",
                "virality_reason": "Test.",
                "artists": [],
            }
        )

    monkeypatch.setattr(
        gemini_with_two_keys._clients[0].models, "generate_content", _generate_content_key_1
    )
    monkeypatch.setattr(
        gemini_with_two_keys._clients[1].models, "generate_content", _generate_content_key_2
    )
    result, _, _ = gemini_with_two_keys.classify(make_article(), france_flag=False)

    assert calls == [
        ("clé-1", "gemini-3.5-flash-lite"),
        ("clé-1", "gemini-3.1-flash-lite"),
        ("clé-1", "gemini-2.5-flash-lite"),
        ("clé-2", "gemini-3.5-flash-lite"),  # repli sur la 2e clé, chaîne de modèles reprise à zéro
    ]
    assert result.category == Category.COMEBACK_SORTIE


def test_classify_leve_si_les_deux_cles_epuisent_toute_la_chaine(
    gemini_with_two_keys, monkeypatch, make_article
):
    def _raise(**kwargs):
        raise errors.APIError(code=429, response_json={"error": {"message": "quota"}})

    monkeypatch.setattr(gemini_with_two_keys._clients[0].models, "generate_content", _raise)
    monkeypatch.setattr(gemini_with_two_keys._clients[1].models, "generate_content", _raise)
    with pytest.raises(analyzer.QuotaExceededError):
        gemini_with_two_keys.classify(make_article(), france_flag=False)


def test_classify_reponse_invalide_leve_analysis_error(gemini, monkeypatch, make_article):
    monkeypatch.setattr(
        gemini._clients[0].models,
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

    monkeypatch.setattr(gemini._clients[0].models, "generate_content", _fake_generate)
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

    monkeypatch.setattr(gemini._clients[0].models, "generate_content", _fake_generate)
    from kpop_bot.models import ClassificationResult, Importance

    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE, importance=Importance.MODERE, artists=[]
    )
    result, _, _ = gemini.write(make_article(), classification, Route.B)
    assert result.video_summary is None
    assert "video_summary" not in captured_prompts[0]


@pytest.mark.parametrize(
    "category",
    [Category.COMEBACK_SORTIE, Category.SCANDALE_DRAMA, Category.CONCERT_EVENEMENT_FRANCE],
)
def test_write_injecte_la_consigne_d_engagement_propre_a_la_categorie(
    gemini, monkeypatch, make_article, category
):
    """Le tag (déterministe) n'est jamais demandé à l'IA — seule la consigne de fin de tweet,
    qui varie naturellement par catégorie, est injectée dans le prompt."""
    captured_prompts: list[str] = []

    def _fake_generate(*, model, contents, config):
        captured_prompts.append(config.system_instruction)
        return _FakeResponse({"summary_fr": "Résumé court.", "tweet_draft": "Un tweet."})

    monkeypatch.setattr(gemini._clients[0].models, "generate_content", _fake_generate)
    from kpop_bot.models import ClassificationResult, Importance

    classification = ClassificationResult(
        category=category, importance=Importance.MODERE, artists=[]
    )
    gemini.write(make_article(), classification, Route.B)
    assert analyzer._ENGAGEMENT_HOOKS[category] in captured_prompts[0]
    assert "[FLASH]" not in captured_prompts[0]  # le tag n'est jamais demandé à l'IA


# --- write_tiktok_script (T14) : prompt système dédié, séparé de celui du tweet. ---


def test_write_tiktok_script_utilise_un_prompt_dedie_et_renvoie_le_schema(
    gemini, monkeypatch, make_article
):
    captured_prompts: list[str] = []

    def _fake_generate(*, model, contents, config):
        captured_prompts.append(config.system_instruction)
        return _FakeResponse(
            {
                "hook": "Un record vient de tomber !",
                "on_screen_texte": "RECORD BATTU",
                "script_body": "Le groupe X vient de franchir un nouveau palier de vues...",
                "closing_hook": "Vous vous y attendiez ?",
                "visual_ideas": ["Zoom sur le compteur de vues", "Archives du clip"],
                "caption_seo": {
                    "legende": "Un nouveau record vient de tomber",
                    "hashtags": ["#kpop", "#kpopnews"],
                },
            }
        )

    monkeypatch.setattr(gemini._clients[0].models, "generate_content", _fake_generate)
    from kpop_bot.models import ClassificationResult, Importance

    classification = ClassificationResult(
        category=Category.COMEBACK_SORTIE,
        importance=Importance.MAJEUR,
        virality=Virality.ELEVE,
        virality_reason="Test.",
        artists=["Groupe X"],
    )
    result, tokens_in, tokens_out = gemini.write_tiktok_script(make_article(), classification)

    assert result.hook == "Un record vient de tomber !"
    assert result.on_screen_texte == "RECORD BATTU"
    assert result.visual_ideas == ["Zoom sur le compteur de vues", "Archives du clip"]
    assert result.caption_seo.hashtags == ["#kpop", "#kpopnews"]
    assert tokens_in == 100
    assert tokens_out == 42
    # Prompt bien distinct de celui du tweet — règle "aucun emoji" propre au script TikTok.
    assert "aucun emoji" in captured_prompts[0].lower()
    assert "COMEBACK_SORTIE" in captured_prompts[0]
    assert "ELEVE" in captured_prompts[0]


def test_write_tiktok_script_gere_une_viralite_absente(gemini, monkeypatch, make_article):
    """En pratique, une ClassificationResult valide a toujours une viralité non-nulle hors
    BRUIT_INUTILE (voir le validator dans models.py) — mais le formatage du prompt doit rester
    sûr si ce champ était un jour None, sans lever d'exception."""
    captured_prompts: list[str] = []

    def _fake_generate(*, model, contents, config):
        captured_prompts.append(config.system_instruction)
        return _FakeResponse(
            {
                "hook": "Accroche.",
                "on_screen_texte": "TEXTE",
                "script_body": "Corps.",
                "closing_hook": "Chute.",
                "visual_ideas": ["Idée 1"],
                "caption_seo": {"legende": "Légende.", "hashtags": ["#kpop"]},
            }
        )

    monkeypatch.setattr(gemini._clients[0].models, "generate_content", _fake_generate)
    from kpop_bot.models import ClassificationResult, Importance

    classification = ClassificationResult(
        category=Category.CONCERT_EVENEMENT_FRANCE, importance=Importance.MAJEUR, artists=[]
    ).model_copy(update={"virality": None})  # contourne le validator, cas normalement inatteignable
    gemini.write_tiktok_script(make_article(), classification)
    assert "N/A" in captured_prompts[0]


# --- Throttling RPM : horloge simulée, aucune vraie attente. ---


def test_throttle_espace_les_appels_selon_min_interval(monkeypatch):
    instance = analyzer.GeminiAnalyzer(
        api_keys=["test-key"],
        models=["gemini-3.5-flash-lite"],
        artist_tiers={},
        min_seconds_between_calls=4.5,
    )

    fake_now = [100.0]  # horloge simulée, avance manuellement
    sleeps: list[float] = []

    monkeypatch.setattr(analyzer.time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr(analyzer.time, "sleep", lambda seconds: sleeps.append(seconds))

    instance._throttle()  # premier appel : rien à attendre
    assert sleeps == []

    fake_now[0] += 1.0  # seulement 1s écoulée depuis le premier appel
    instance._throttle()
    assert sleeps == [pytest.approx(3.5)]  # 4.5 - 1.0

    sleeps.clear()
    fake_now[0] += 10.0  # largement plus que l'intervalle minimum
    instance._throttle()
    assert sleeps == []  # aucune attente nécessaire


def test_throttle_desactive_si_min_interval_nul(monkeypatch):
    instance = analyzer.GeminiAnalyzer(
        api_keys=["test-key"],
        models=["gemini-3.5-flash-lite"],
        artist_tiers={},
        min_seconds_between_calls=0,
    )
    monkeypatch.setattr(analyzer.time, "sleep", lambda _s: pytest.fail("ne doit jamais dormir"))
    instance._throttle()
    instance._throttle()  # même appelé deux fois de suite, sans délai
