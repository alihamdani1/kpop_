from __future__ import annotations

import pytest
from pydantic import ValidationError

from kpop_bot.models import (
    TWEET_TAG_LABELS,
    Category,
    ClassificationResult,
    Importance,
    Route,
    SocialVisualContent,
    TweetTag,
    Virality,
    WritingResult,
    determine_route,
    determine_tweet_tag,
)

# --- determine_route : toutes les combinaisons catégorie x viralité qui comptent. ---


def test_route_bruit_toujours_ignore():
    assert determine_route(Category.BRUIT_INUTILE, None) == Route.IGNORED
    # Même si une viralité était présente par erreur, le bruit reste ignoré.
    assert determine_route(Category.BRUIT_INUTILE, Virality.VIRAL) == Route.IGNORED


def test_route_concert_france_toujours_route_concert_quelle_que_soit_la_viralite():
    for virality in (Virality.FAIBLE, Virality.MODERE, Virality.ELEVE, Virality.VIRAL, None):
        assert determine_route(Category.CONCERT_EVENEMENT_FRANCE, virality) == Route.CONCERT


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


# --- WritingResult : le tweet IA doit rester à 260 caractères max (place laissée au tag,
# ajouté séparément par le pipeline — voir determine_tweet_tag ci-dessous). ---


def test_tweet_draft_trop_long_est_rejete():
    with pytest.raises(ValidationError):
        WritingResult(summary_fr="Résumé.", tweet_draft="x" * 261)


def test_tweet_draft_exactement_260_est_accepte():
    WritingResult(summary_fr="Résumé.", tweet_draft="x" * 260)


def test_video_summary_optionnel():
    result = WritingResult(summary_fr="Résumé.", tweet_draft="Tweet court.")
    assert result.video_summary is None


# --- determine_tweet_tag : dérivé de category/importance/virality, jamais de l'IA. ---


def test_tag_flash_prime_sur_tout():
    categories = (
        Category.SCANDALE_DRAMA,
        Category.COMEBACK_SORTIE,
        Category.CONCERT_EVENEMENT_FRANCE,
    )
    for category in categories:
        assert determine_tweet_tag(category, Importance.MAJEUR, Virality.VIRAL) == TweetTag.FLASH
        assert determine_tweet_tag(category, Importance.MAJEUR, Virality.ELEVE) == TweetTag.FLASH


@pytest.mark.parametrize("virality", [Virality.MODERE, Virality.FAIBLE, None])
def test_tag_gossip_sur_scandale_hors_flash(virality):
    result = determine_tweet_tag(Category.SCANDALE_DRAMA, Importance.MAJEUR, virality)
    assert result == TweetTag.GOSSIP
    result2 = determine_tweet_tag(Category.SCANDALE_DRAMA, Importance.MODERE, Virality.VIRAL)
    assert result2 == TweetTag.GOSSIP


def test_tag_france_sur_concert_hors_flash():
    result = determine_tweet_tag(
        Category.CONCERT_EVENEMENT_FRANCE, Importance.MODERE, Virality.FAIBLE
    )
    assert result == TweetTag.FRANCE


def test_tag_release_specifique_a_comeback_sortie():
    assert (
        determine_tweet_tag(Category.COMEBACK_SORTIE, Importance.MINEUR, Virality.FAIBLE)
        == TweetTag.RELEASE
    )


def test_tag_info_en_repli_generique_hors_comeback():
    # BRUIT_INUTILE n'atteint jamais ce point en pratique (route IGNORED avant), mais
    # determine_tweet_tag() reste une fonction pure testable isolément — ce test vérifie
    # que le repli générique (INFO) est bien distinct du cas spécifique COMEBACK_SORTIE
    # (RELEASE), et non confondu avec lui comme avant l'ajout de ce tag.
    assert determine_tweet_tag(Category.BRUIT_INUTILE, Importance.MINEUR, None) == TweetTag.INFO


def test_tous_les_tags_ont_un_libelle():
    for tag in TweetTag:
        assert tag in TWEET_TAG_LABELS
        assert TWEET_TAG_LABELS[tag].strip()  # non vide


# --- SocialVisualContent (remplace InstagramNewsPost) : titre + points clés du visuel 9:16,
# rédigés ensemble par un 3e appel dédié pour toute route retenue. ---

_VALID_SOCIAL_VISUAL = dict(
    headline_fr="Le groupe confirme un comeback surprise",
    key_points_fr=["Un extrait a été dévoilé ce matin.", "Aucune fuite avant l'annonce."],
)


def test_social_visual_content_avec_2_points_cles_est_accepte():
    content = SocialVisualContent(**_VALID_SOCIAL_VISUAL)
    assert len(content.key_points_fr) == 2


def test_social_visual_content_avec_3_points_cles_est_accepte():
    content = SocialVisualContent(
        **{**_VALID_SOCIAL_VISUAL, "key_points_fr": ["Point 1.", "Point 2.", "Point 3."]}
    )
    assert len(content.key_points_fr) == 3


def test_social_visual_content_avec_1_seul_point_cle_est_rejete():
    with pytest.raises(ValidationError):
        SocialVisualContent(**{**_VALID_SOCIAL_VISUAL, "key_points_fr": ["Un seul point."]})


def test_social_visual_content_avec_4_points_cles_est_rejete():
    with pytest.raises(ValidationError):
        SocialVisualContent(
            **{**_VALID_SOCIAL_VISUAL, "key_points_fr": ["P1.", "P2.", "P3.", "P4."]}
        )


def test_social_visual_content_headline_trop_longue_est_rejetee():
    with pytest.raises(ValidationError):
        SocialVisualContent(**{**_VALID_SOCIAL_VISUAL, "headline_fr": "x" * 111})


def test_social_visual_content_headline_exactement_110_est_acceptee():
    SocialVisualContent(**{**_VALID_SOCIAL_VISUAL, "headline_fr": "x" * 110})


def test_social_visual_content_point_cle_trop_long_est_rejete():
    with pytest.raises(ValidationError):
        SocialVisualContent(**{**_VALID_SOCIAL_VISUAL, "key_points_fr": ["x" * 141, "Point 2."]})
