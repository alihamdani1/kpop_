from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
import respx

from kpop_bot.models import (
    Category,
    Importance,
    Route,
    ThreadAngle,
    ThreadRecord,
    ThreadStatus,
    ThreadTheme,
    ThreadTopicRecord,
    Virality,
)
from kpop_bot.notifier import (
    NotificationError,
    build_embed,
    build_info_header,
    build_review_message,
    build_thread_intro_message,
    build_thread_selection_embed,
    build_thread_tweet_header,
    build_tiktok_embed,
    build_tiktok_script_header,
    build_tiktok_script_message,
    build_tweet_header,
    notify,
    notify_review,
    notify_thread,
    notify_thread_selection,
    notify_tiktok,
    send_embed,
    send_message,
    send_message_with_image,
    send_message_with_images,
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


# --- Threads Twitter quotidiens (T15) : picker (webhook + réactions bot) et diffusion finale. ---


def _topic(**overrides) -> ThreadTopicRecord:
    defaults = dict(
        id=1,
        group_name="Groupe X",
        theme=ThreadTheme.ANALYSE_COMEBACK,
        title="Titre du sujet",
        premise="La promesse du sujet.",
        created_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
        last_offered_at=None,
        source="ai_ideation",
    )
    defaults.update(overrides)
    return ThreadTopicRecord(**defaults)


def _thread(**overrides) -> ThreadRecord:
    defaults = dict(
        id=1,
        selection_id=1,
        topic_id=1,
        angle=ThreadAngle.CONTRARIEN,
        hook_label="chiffre_marquant",
        tweets=["Premier tweet.", "Deuxième tweet.", "Troisième tweet."],
        status=ThreadStatus.DRAFT,
        tokens_in=10,
        tokens_out=5,
        prompt_version="v1",
        created_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
        sent_at=None,
        error=None,
    )
    defaults.update(overrides)
    return ThreadRecord(**defaults)


def test_build_thread_selection_embed_contient_les_3_options():
    options = [
        (_topic(title="Sujet A"), ThreadAngle.CONTRARIEN),
        (_topic(title="Sujet B"), ThreadAngle.GUIDE_PRATIQUE),
        (_topic(title="Sujet C"), ThreadAngle.CAS_ETUDE),
    ]
    embed = build_thread_selection_embed(options)
    assert len(embed["fields"]) == 3
    assert embed["fields"][0]["name"] == "🇦 Sujet A"
    assert embed["fields"][1]["name"] == "🇧 Sujet B"
    assert embed["fields"][2]["name"] == "🇨 Sujet C"
    assert "CONTRARIEN" in embed["fields"][0]["value"]


@respx.mock
def test_notify_thread_selection_retourne_l_id_du_message():
    url = "https://discord.com/api/webhooks/fake/thread"
    respx.post(url).mock(return_value=httpx.Response(200, json={"id": "1234567890", "content": ""}))
    options = [
        (_topic(title="Sujet A"), ThreadAngle.CONTRARIEN),
        (_topic(title="Sujet B"), ThreadAngle.GUIDE_PRATIQUE),
        (_topic(title="Sujet C"), ThreadAngle.CAS_ETUDE),
    ]
    message_id = notify_thread_selection(options, url=url, timeout=5.0)
    assert message_id == "1234567890"
    assert "wait=true" in str(respx.calls[0].request.url)


def test_build_thread_intro_message_annonce_le_nombre_de_tweets():
    message = build_thread_intro_message(_topic(title="Sujet A"), ThreadAngle.STORYTELLING, 6)
    assert "6 tweets" in message
    assert "Sujet A" in message
    assert "STORYTELLING" in message


def test_build_thread_tweet_header_est_un_element_fixe_distinct_du_contenu():
    assert build_thread_tweet_header(2, 6) == "# Tweet 2/6"


@respx.mock
def test_notify_thread_isole_chaque_tweet_dans_son_propre_message():
    """Régression : un en-tête + bloc de code ``` dans LE MÊME message que le tweet cassait le
    copier-coller mobile (le bouton natif « Copier le texte » de Discord copie tout le contenu
    brut du message). Le tweet doit être seul dans son message, rien autour — même principe que
    tweet_draft en T6."""
    url = "https://discord.com/api/webhooks/fake/thread"
    mock = respx.post(url).mock(return_value=httpx.Response(204))
    thread = _thread(tweets=["Un.", "Deux.", "Trois."])
    notify_thread(thread, _topic(), url=url, timeout=5.0)

    assert mock.call_count == 7  # 1 intro + (en-tête + tweet) * 3
    bodies = [json.loads(call.request.content) for call in mock.calls]
    assert "3 tweets" in bodies[0]["content"]
    assert bodies[1]["content"] == "# Tweet 1/3"
    assert bodies[2]["content"] == "Un."  # le tweet seul, aucun bloc de code, aucun en-tête
    assert bodies[3]["content"] == "# Tweet 2/3"
    assert bodies[4]["content"] == "Deux."
    assert bodies[5]["content"] == "# Tweet 3/3"
    assert bodies[6]["content"] == "Trois."


# --- Images d'illustration (T16) : une image par tweet, jointe au message du tweet lui-même. ---


@respx.mock
def test_send_message_with_image_envoie_en_multipart(tmp_path):
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"fake-bytes")
    url = "https://discord.com/api/webhooks/fake/thread"
    route = respx.post(url).mock(return_value=httpx.Response(204))

    send_message_with_image(url, "Contenu du tweet.", image_path, timeout=5.0)

    assert route.called
    request = route.calls[0].request
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b"Contenu du tweet." in request.content
    assert b"photo.jpg" in request.content
    assert b"fake-bytes" in request.content


@respx.mock
def test_notify_thread_joint_une_image_par_tweet_quand_disponible(tmp_path):
    url = "https://discord.com/api/webhooks/fake/thread"
    mock = respx.post(url).mock(return_value=httpx.Response(204))
    image1 = tmp_path / "1.jpg"
    image1.write_bytes(b"img1")
    image2 = tmp_path / "2.jpg"
    image2.write_bytes(b"img2")

    thread = _thread(tweets=["Un.", "Deux."])
    notify_thread(thread, _topic(), url=url, timeout=5.0, image_paths=[image1, image2])

    assert mock.call_count == 5  # intro + (en-tête + tweet) * 2
    assert mock.calls[2].request.headers["content-type"].startswith("multipart/form-data")
    assert b"Un." in mock.calls[2].request.content
    assert b"1.jpg" in mock.calls[2].request.content
    assert mock.calls[4].request.headers["content-type"].startswith("multipart/form-data")
    assert b"Deux." in mock.calls[4].request.content
    assert b"2.jpg" in mock.calls[4].request.content


@respx.mock
def test_notify_thread_sans_images_reste_inchange():
    """Non-régression : sans image_paths (comportement d'avant T16), aucun message en
    multipart — uniquement du JSON pur, comme avant."""
    url = "https://discord.com/api/webhooks/fake/thread"
    mock = respx.post(url).mock(return_value=httpx.Response(204))
    thread = _thread(tweets=["Un.", "Deux."])
    notify_thread(thread, _topic(), url=url, timeout=5.0)

    assert mock.call_count == 5
    for call in mock.calls:
        assert call.request.headers["content-type"] == "application/json"


@respx.mock
def test_notify_thread_moins_d_images_que_de_tweets_degrade_gracieusement(tmp_path):
    url = "https://discord.com/api/webhooks/fake/thread"
    mock = respx.post(url).mock(return_value=httpx.Response(204))
    image1 = tmp_path / "1.jpg"
    image1.write_bytes(b"img1")

    thread = _thread(tweets=["Un.", "Deux."])
    notify_thread(thread, _topic(), url=url, timeout=5.0, image_paths=[image1])

    assert mock.calls[2].request.headers["content-type"].startswith("multipart/form-data")
    assert mock.calls[4].request.headers["content-type"] == "application/json"


# --- Photos alternatives en fin de thread (T16bis). ---


@respx.mock
def test_send_message_with_images_envoie_plusieurs_pieces_jointes(tmp_path):
    img1 = tmp_path / "1.jpg"
    img1.write_bytes(b"bytes-1")
    img2 = tmp_path / "2.jpg"
    img2.write_bytes(b"bytes-2")
    url = "https://discord.com/api/webhooks/fake/thread"
    route = respx.post(url).mock(return_value=httpx.Response(204))

    send_message_with_images(url, "Légende.", [img1, img2], timeout=5.0)

    assert route.called
    body = route.calls[0].request.content
    assert b"Legende." in body or b"L\xc3\xa9gende." in body
    assert b"1.jpg" in body
    assert b"2.jpg" in body
    assert b"bytes-1" in body
    assert b"bytes-2" in body


@respx.mock
def test_notify_thread_envoie_un_dernier_message_avec_les_photos_alternatives(tmp_path):
    url = "https://discord.com/api/webhooks/fake/thread"
    mock = respx.post(url).mock(return_value=httpx.Response(204))
    extra1 = tmp_path / "extra1.jpg"
    extra1.write_bytes(b"e1")
    extra2 = tmp_path / "extra2.jpg"
    extra2.write_bytes(b"e2")

    thread = _thread(tweets=["Un."])
    notify_thread(thread, _topic(), url=url, timeout=5.0, extra_image_paths=[extra1, extra2])

    assert mock.call_count == 4  # intro + en-tête + tweet + message final des photos alternatives
    last_call = mock.calls[3]
    assert last_call.request.headers["content-type"].startswith("multipart/form-data")
    assert b"extra1.jpg" in last_call.request.content
    assert b"extra2.jpg" in last_call.request.content


@respx.mock
def test_notify_thread_sans_photos_alternatives_n_envoie_pas_de_message_final():
    url = "https://discord.com/api/webhooks/fake/thread"
    mock = respx.post(url).mock(return_value=httpx.Response(204))
    thread = _thread(tweets=["Un."])
    notify_thread(thread, _topic(), url=url, timeout=5.0, extra_image_paths=[])

    assert mock.call_count == 3  # intro + en-tête + tweet, rien de plus
