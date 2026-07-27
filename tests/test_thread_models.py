from __future__ import annotations

import pytest
from pydantic import ValidationError

from kpop_bot.models import ThreadTheme, ThreadTopicIdea, ThreadWritingResult, TopicIdeationResult


def _tweets(n: int, *, length: int = 50) -> list[str]:
    return [f"Tweet {i} " + "x" * length for i in range(n)]


def test_thread_writing_result_accepte_entre_5_et_8_tweets():
    result = ThreadWritingResult(tweets=_tweets(6))
    assert len(result.tweets) == 6


@pytest.mark.parametrize("count", [1, 4, 9, 12])
def test_thread_writing_result_rejette_un_nombre_de_tweets_hors_bornes(count):
    with pytest.raises(ValidationError):
        ThreadWritingResult(tweets=_tweets(count))


def test_thread_writing_result_rejette_un_tweet_trop_long():
    tweets = _tweets(6)
    tweets[2] = "x" * 261
    with pytest.raises(ValidationError):
        ThreadWritingResult(tweets=tweets)


def test_thread_writing_result_accepte_un_tweet_a_la_limite():
    tweets = _tweets(5)
    tweets[0] = "x" * 260
    result = ThreadWritingResult(tweets=tweets)
    assert len(result.tweets[0]) == 260


def test_topic_ideation_result_regroupe_plusieurs_idees():
    ideas = [
        ThreadTopicIdea(
            group_name="Groupe X",
            theme=ThreadTheme.ANALYSE_COMEBACK,
            title="Titre",
            premise="Promesse.",
        )
        for _ in range(3)
    ]
    result = TopicIdeationResult(topics=ideas)
    assert len(result.topics) == 3
