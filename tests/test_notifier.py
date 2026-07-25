from __future__ import annotations

import json

import httpx
import pytest
import respx

from kpop_bot.models import Category, Importance, Route, Virality
from kpop_bot.notifier import (
    NotificationError,
    build_embed,
    build_info_header,
    build_review_message,
    build_tiktok_embed,
    build_tiktok_script_header,
    build_tiktok_script_message,
    build_tweet_header,
    notify,
    notify_review,
    notify_tiktok,
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


# --- Canal #info-a-verifier (T13) : message unique et allégé pour le bruit filtré. ---


def test_build_review_message_contient_titre_source_categorie_lien(make_article):
    article = make_article(
        title="Article bruit",
        source="Koreaboo",
        category=Category.BRUIT_INUTILE,
        importance=Importance.MINEUR,
        url="https://www.koreaboo.com/article/bruit",
    )
    message = build_review_message(article)
    assert "Article bruit" in message
    assert "Koreaboo" in message
    assert "BRUIT_INUTILE" in message
    assert "MINEUR" in message
    assert "https://www.koreaboo.com/article/bruit" in message


@respx.mock
def test_notify_review_envoie_un_seul_message(make_article):
    url = "https://discord.com/api/webhooks/fake/verif"
    route = respx.post(url).mock(return_value=httpx.Response(204))
    article = make_article(category=Category.BRUIT_INUTILE, importance=Importance.MINEUR)
    notify_review(article, url=url, timeout=5.0)
    assert route.call_count == 1


# --- Salon dédié aux scripts TikTok (T14) : Route A uniquement, en plus de #actus-videos. ---


def test_build_tiktok_embed_contient_score_resume_et_idees(make_article):
    article = make_article(
        route=Route.A,
        virality=Virality.ELEVE,
        video_summary="Résumé détaillé déjà généré pour la vidéo.",
        tiktok_visual_ideas=["Zoom sur le clip", "Texte à l'écran avec le chiffre clé"],
    )
    embed = build_tiktok_embed(article)
    field_names = [f["name"] for f in embed["fields"]]
    assert "Score de viralité" in field_names
    assert "Résumé détaillé" in field_names
    assert "💡 Idées de montage" in field_names
    ideas_field = next(f for f in embed["fields"] if f["name"] == "💡 Idées de montage")
    assert "Zoom sur le clip" in ideas_field["value"]
    assert "Texte à l'écran avec le chiffre clé" in ideas_field["value"]


def test_build_tiktok_script_header_est_fixe():
    assert build_tiktok_script_header() == "# Script TikTok"


def test_build_tiktok_script_message_contient_tous_les_champs(make_article):
    article = make_article(
        tiktok_hook="Un record vient de tomber !",
        tiktok_on_screen_texte="RECORD BATTU",
        tiktok_script_body="Le groupe X a franchi un nouveau palier de vues.",
        tiktok_closing_hook="Vous vous y attendiez ?",
        tiktok_caption_legende="Un record vient de tomber",
        tiktok_caption_hashtags=["#kpop", "#kpopnews"],
    )
    message = build_tiktok_script_message(article)
    assert "Un record vient de tomber !" in message
    assert "RECORD BATTU" in message
    assert "Le groupe X a franchi un nouveau palier de vues." in message
    assert "Vous vous y attendiez ?" in message
    assert "Un record vient de tomber" in message
    assert "#kpop #kpopnews" in message


@respx.mock
def test_notify_tiktok_envoie_quatre_messages_dans_l_ordre(make_article):
    url = "https://discord.com/api/webhooks/fake/tiktok"
    mock = respx.post(url).mock(return_value=httpx.Response(204))
    article = make_article(
        route=Route.A,
        virality=Virality.ELEVE,
        tiktok_hook="Accroche.",
        tiktok_on_screen_texte="TEXTE.",
        tiktok_script_body="Corps.",
        tiktok_closing_hook="Chute.",
        tiktok_visual_ideas=["Idée 1"],
        tiktok_caption_legende="Légende.",
        tiktok_caption_hashtags=["#kpop"],
    )
    notify_tiktok(article, url=url, timeout=5.0, index=1)

    assert mock.call_count == 4
    bodies = [json.loads(call.request.content) for call in mock.calls]
    assert bodies[0] == {"content": "# INFO 1"}
    assert "embeds" in bodies[1]
    assert bodies[2] == {"content": "# Script TikTok"}
    assert "Accroche." in bodies[3]["content"]
