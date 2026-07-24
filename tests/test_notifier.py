from __future__ import annotations

import json

import httpx
import pytest
import respx

from kpop_bot.models import Route
from kpop_bot.notifier import (
    NotificationError,
    build_embed,
    build_info_header,
    build_tweet_header,
    notify,
    send_embed,
    send_message,
)

_WEBHOOK_URL = "https://discord.com/api/webhooks/fake/route-a"

# --- Construction de l'embed d'information (le tweet n'y est plus — voir notify()). ---


def test_embed_route_a_contient_le_resume_detaille_mais_pas_le_tweet(make_article):
    article = make_article(route=Route.A, video_summary="Résumé détaillé pour la vidéo.")
    embed = build_embed(article)
    field_names = [f["name"] for f in embed["fields"]]
    assert "Résumé détaillé" in field_names
    assert "Score de viralité" in field_names
    assert "Brouillon de tweet" not in field_names  # envoyé dans un message à part


def test_embed_route_b_ne_contient_ni_resume_ni_tweet(make_article):
    article = make_article(route=Route.B, video_summary=None)
    embed = build_embed(article)
    field_names = [f["name"] for f in embed["fields"]]
    assert "Résumé détaillé" not in field_names
    assert "Score de viralité" in field_names
    assert "Brouillon de tweet" not in field_names


# --- En-têtes (éléments fixes, sans appel IA). ---


def test_build_info_header_numerote_l_article():
    assert build_info_header(1) == "# INFO 1"
    assert build_info_header(3) == "# INFO 3"


def test_build_tweet_header_est_fixe():
    assert build_tweet_header() == "# Brouillon Tweet"


# --- Envoi HTTP (respx, aucun réseau réel). ---


@respx.mock
def test_send_embed_succes():
    route = respx.post(_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    send_embed(_WEBHOOK_URL, {"title": "test"}, timeout=5.0)
    assert route.called


@respx.mock
def test_send_message_envoie_le_contenu_brut():
    route = respx.post(_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    send_message(_WEBHOOK_URL, "Un tweet prêt à copier-coller.", timeout=5.0)
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body == {"content": "Un tweet prêt à copier-coller."}


@respx.mock
def test_send_embed_retente_apres_rate_limit(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    route = respx.post(_WEBHOOK_URL).mock(
        side_effect=[
            httpx.Response(429, json={"retry_after": 0.01}),
            httpx.Response(204),
        ]
    )
    send_embed(_WEBHOOK_URL, {"title": "test"}, timeout=5.0)
    assert route.call_count == 2


@respx.mock
def test_send_embed_echec_persistant_leve_notification_error():
    respx.post(_WEBHOOK_URL).mock(return_value=httpx.Response(500, text="erreur serveur"))
    with pytest.raises(NotificationError):
        send_embed(_WEBHOOK_URL, {"title": "test"}, timeout=5.0)


@respx.mock
def test_notify_envoie_quatre_messages_dans_l_ordre_vers_le_bon_webhook(make_article):
    """notify() doit produire 4 requêtes, dans l'ordre : en-tête INFO, embed, en-tête tweet,
    message-tweet — jamais fusionnées (voir docstring de notifier.py pour le pourquoi mobile)."""
    url_a = "https://discord.com/api/webhooks/fake/a"
    url_b = "https://discord.com/api/webhooks/fake/b"
    mock_a = respx.post(url_a).mock(return_value=httpx.Response(204))
    mock_b = respx.post(url_b).mock(return_value=httpx.Response(204))

    article = make_article(route=Route.A, tweet_draft="Tweet isolé, rien autour.")
    notify(article, url_a=url_a, url_b=url_b, timeout=5.0, index=2)

    assert mock_a.call_count == 4
    assert not mock_b.called

    bodies = [json.loads(call.request.content) for call in mock_a.calls]
    assert bodies[0] == {"content": "# INFO 2"}
    assert "embeds" in bodies[1]
    assert bodies[2] == {"content": "# Brouillon Tweet"}
    assert bodies[3] == {"content": "Tweet isolé, rien autour."}
