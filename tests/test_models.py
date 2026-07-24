from __future__ import annotations

import pytest
from pydantic import ValidationError

from kpop_bot.models import (
    Category,
    ClassificationResult,
    Route,
    Virality,
    WritingResult,
    determine_route,
)

# --- determine_route : toutes les combinaisons catégorie x viralité qui comptent. ---


def test_route_bruit_toujours_ignore():
    assert determine_route(Category.BRUIT_INUTILE, None) == Route.IGNORED
    # Même si une viralité était présente par erreur, le bruit reste ignoré.
    assert determine_route(Category.BRUIT_INUTILE, Virality.VIRAL) == Route.IGNORED


def test_route_concert_france_toujours_route_a_quelle_que_soit_la_viralite():
    for virality in (Virality.FAIBLE, Virality.MODERE, Virality.ELEVE, Virality.VIRAL, None):
        assert determine_route(Category.CONCERT_EVENEMENT_FRANCE, virality) == Route.A


@pytest.mark.parametrize("virality", [Virality.VIRAL, Virality.ELEVE])
def test_route_a_sur_forte_viralite_hors_concert_france(virality):
    assert determine_route(Category.SCANDALE_DRAMA, virality) == Route.A
    assert determine_route(Category.COMEBACK_SORTIE, virality) == Route.A


@pytest.mark.parametrize("virality", [Virality.MODERE, Virality.FAIBLE])
def test_route_b_sur_viralite_moderee_ou_faible_hors_concert_france(virality):
    assert determine_route(Category.SCANDALE_DRAMA, virality) == Route.B
    assert determine_route(Category.COMEBACK_SORTIE, virality) == Route.B


# --- ClassificationResult : cohérence nullabilité viralité / catégorie. ---


def test_bruit_inutile_efface_toujours_la_viralite():
    result = ClassificationResult(
        category=Category.BRUIT_INUTILE,
        importance="MINEUR",
        virality=Virality.VIRAL,  # fourni par erreur — doit être effacé
        virality_reason="devrait disparaître",
        artists=[],
    )
    assert result.virality is None
    assert result.virality_reason is None


def test_categorie_retenue_sans_viralite_retombe_sur_faible():
    result = ClassificationResult(
        category=Category.COMEBACK_SORTIE,
        importance="MAJEUR",
        virality=None,  # oublié par le modèle
        artists=["Groupe X"],
    )
    assert result.virality == Virality.FAIBLE
    assert result.virality_reason is not None


def test_categorie_retenue_avec_viralite_fournie_est_preservee():
    result = ClassificationResult(
        category=Category.COMEBACK_SORTIE,
        importance="MAJEUR",
        virality=Virality.VIRAL,
        virality_reason="Comeback très attendu.",
        artists=["Groupe X"],
    )
    assert result.virality == Virality.VIRAL
    assert result.virality_reason == "Comeback très attendu."


# --- WritingResult : le tweet doit rester dans la limite de 280 caractères. ---


def test_tweet_draft_trop_long_est_rejete():
    with pytest.raises(ValidationError):
        WritingResult(summary_fr="Résumé.", tweet_draft="x" * 281)


def test_tweet_draft_exactement_280_est_accepte():
    WritingResult(summary_fr="Résumé.", tweet_draft="x" * 280)


def test_video_summary_optionnel():
    result = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet court.")
    assert result.video_summary is None
