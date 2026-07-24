from __future__ import annotations

import httpx
import pytest
import respx

from kpop_bot.models import Route
from kpop_bot.notifier import NotificationError, build_embed, notify, send_embed

_WEBHOOK_URL = "https://discord.com/api/webhooks/fake/route-a"

# --- Construction des embeds. ---


def test_embed_route_a_contient_le_resume_detaille(make_article):
    article = make_article(route=Route.A, video_summary="Résumé détaillé pour la vidéo.")
    embed = build_embed(article)
    field_names = [f["name"] for f in embed["fields"]]
    assert "Résumé détaillé" in field_names
    assert "Brouillon de tweet" in field_names
    assert "Score de viralité" in field_names


def test_embed_route_b_ne_contient_pas_le_resume_detaille(make_article):
    article = make_article(route=Route.B, video_summary=None)
    embed = build_embed(article)
    field_names = [f["name"] for f in embed["fields"]]
    assert "Résumé détaillé" not in field_names
    assert "Brouillon de tweet" in field_names
    assert "Score de viralité" in field_names


def test_embed_tweet_est_dans_un_bloc_de_code(make_article):
    article = make_article(route=Route.B, tweet_draft="Un tweet prêt à copier-coller.")
    embed = build_embed(article)
    tweet_field = next(f for f in embed["fields"] if f["name"] == "Brouillon de tweet")
    assert tweet_field["value"] == "```Un tweet prêt à copier-coller.```"


# --- Envoi HTTP (respx, aucun réseau réel). ---


@respx.mock
def test_send_embed_succes():
    route = respx.post(_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    send_embed(_WEBHOOK_URL, {"title": "test"}, timeout=5.0)
    assert route.called


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
def test_notify_route_vers_le_bon_webhook(make_article):
    url_a = "https://discord.com/api/webhooks/fake/a"
    url_b = "https://discord.com/api/webhooks/fake/b"
    mock_a = respx.post(url_a).mock(return_value=httpx.Response(204))
    mock_b = respx.post(url_b).mock(return_value=httpx.Response(204))

    article = make_article(route=Route.A)
    notify(article, url_a=url_a, url_b=url_b, timeout=5.0)

    assert mock_a.called
    assert not mock_b.called
