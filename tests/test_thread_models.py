from __future__ import annotations

import pytest
from pydantic import ValidationError

from kpop_bot.models import (
    ThreadConcept,
    ThreadTheme,
    ThreadTopicIdea,
    ThreadTopicWriting,
    ThreadWritingResult,
    TopicIdeationResult,
)


def _tweets(n: int, *, length: int = 50) -> list[str]:
    return [f"Tweet {i} " + "x" * length for i in range(n)]


def test_thread_writing_result_accepte_entre_5_et_8_tweets():
    result = ThreadWritingResult(premise_respectee=True, tweets=_tweets(6))
    assert len(result.tweets) == 6


@pytest.mark.parametrize("count", [1, 4, 9, 12])
def test_thread_writing_result_rejette_un_nombre_de_tweets_hors_bornes(count):
    with pytest.raises(ValidationError):
        ThreadWritingResult(premise_respectee=True, tweets=_tweets(count))


def test_thread_writing_result_rejette_un_tweet_trop_long():
    tweets = _tweets(6)
    tweets[2] = "x" * 261
    with pytest.raises(ValidationError):
        ThreadWritingResult(premise_respectee=True, tweets=tweets)


def test_thread_writing_result_accepte_un_tweet_a_la_limite():
    tweets = _tweets(5)
    tweets[0] = "x" * 260
    result = ThreadWritingResult(premise_respectee=True, tweets=tweets)
    assert len(result.tweets[0]) == 260


def test_thread_writing_result_rejette_si_premise_non_respectee():
    """T15ter : seule vérification demandée à l'IA qui n'est pas mécaniquement vérifiable par
    du code — une réponse honnête 'False' doit faire rejeter le thread plutôt que le diffuser."""
    with pytest.raises(ValidationError, match="premise"):
        ThreadWritingResult(premise_respectee=False, tweets=_tweets(6))


def test_thread_writing_result_rejette_un_hashtag_hors_dernier_tweet():
    """T15ter : validator strict (pas une auto-déclaration du modèle) — un hashtag dans le
    corps doit faire rejeter tout le thread."""
    tweets = _tweets(6)
    tweets[2] = "Un tweet avec #unhashtag glissé dedans."
    with pytest.raises(ValidationError, match="hashtag"):
        ThreadWritingResult(premise_respectee=True, tweets=tweets)


def test_thread_writing_result_accepte_un_hashtag_sur_le_dernier_tweet():
    tweets = _tweets(6)
    tweets[-1] = "Dernier tweet avec #kpop autorisé ici."
    result = ThreadWritingResult(premise_respectee=True, tweets=tweets)
    assert "#kpop" in result.tweets[-1]


def test_topic_ideation_result_regroupe_plusieurs_redactions():
    """Depuis T15bis, l'IA ne rédige plus que titre/premise pour des paires déjà choisies par
    le code — elle ne renvoie plus group_name/theme (voir ThreadTopicWriting)."""
    writings = [
        ThreadTopicWriting(pair_index=i, title="Titre", premise="Promesse.") for i in range(3)
    ]
    result = TopicIdeationResult(topics=writings)
    assert len(result.topics) == 3


def test_thread_topic_idea_concept_id_optionnel_pour_retrocompatibilite():
    """Les topics historiques (avant T15bis, idéation libre) n'ont pas de concept_id — doit
    rester constructible sans ce champ."""
    idea = ThreadTopicIdea(
        group_name="Groupe X",
        theme=ThreadTheme.ANALYSE_COMEBACK,
        title="Titre",
        premise="Promesse.",
    )
    assert idea.concept_id is None


def test_thread_concept_construction():
    concept = ThreadConcept(
        id="rivalite_historique",
        theme=ThreadTheme.RIVALITE_COMPARAISON,
        label="Rivalité historique",
        brief="Compare deux groupes sur un aspect précis.",
    )
    assert concept.id == "rivalite_historique"
