from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest
import respx

from kpop_bot.discord_reactions import (
    DiscordBotError,
    get_bot_user_id,
    get_human_reactor,
    seed_reactions,
)

_BASE = "https://discord.com/api/v10"


@respx.mock
def test_get_bot_user_id_lit_l_identite_du_bot():
    respx.get(f"{_BASE}/users/@me").mock(return_value=httpx.Response(200, json={"id": "bot-42"}))
    assert get_bot_user_id(bot_token="tok", timeout=5.0) == "bot-42"
    assert respx.calls[0].request.headers["Authorization"] == "Bot tok"


@respx.mock
def test_get_bot_user_id_echec_leve_discord_bot_error():
    respx.get(f"{_BASE}/users/@me").mock(return_value=httpx.Response(401, text="unauthorized"))
    with pytest.raises(DiscordBotError):
        get_bot_user_id(bot_token="tok", timeout=5.0)


@respx.mock
def test_seed_reactions_pose_une_reaction_par_emoji():
    emojis = ["🇦", "🇧", "🇨"]
    routes = [
        respx.put(
            f"{_BASE}/channels/chan-1/messages/msg-1/reactions/{quote(emoji, safe='')}/@me"
        ).mock(return_value=httpx.Response(204))
        for emoji in emojis
    ]
    seed_reactions(
        channel_id="chan-1", message_id="msg-1", emojis=emojis, bot_token="tok", timeout=5.0
    )
    assert all(route.called for route in routes)


@respx.mock
def test_seed_reactions_echec_leve_discord_bot_error():
    respx.put(f"{_BASE}/channels/chan-1/messages/msg-1/reactions/{quote('🇦', safe='')}/@me").mock(
        return_value=httpx.Response(403, text="forbidden")
    )
    with pytest.raises(DiscordBotError):
        seed_reactions(
            channel_id="chan-1", message_id="msg-1", emojis=["🇦"], bot_token="tok", timeout=5.0
        )


@respx.mock
def test_get_human_reactor_ignore_la_reaction_du_bot():
    url = f"{_BASE}/channels/chan-1/messages/msg-1/reactions/{quote('🇦', safe='')}"
    respx.get(url).mock(return_value=httpx.Response(200, json=[{"id": "bot-42"}]))
    reactor = get_human_reactor(
        channel_id="chan-1",
        message_id="msg-1",
        emoji="🇦",
        bot_token="tok",
        bot_user_id="bot-42",
        timeout=5.0,
    )
    assert reactor is None


@respx.mock
def test_get_human_reactor_detecte_un_vote_humain():
    url = f"{_BASE}/channels/chan-1/messages/msg-1/reactions/{quote('🇦', safe='')}"
    respx.get(url).mock(
        return_value=httpx.Response(200, json=[{"id": "bot-42"}, {"id": "human-7"}])
    )
    reactor = get_human_reactor(
        channel_id="chan-1",
        message_id="msg-1",
        emoji="🇦",
        bot_token="tok",
        bot_user_id="bot-42",
        timeout=5.0,
    )
    assert reactor == "human-7"


@respx.mock
def test_get_human_reactor_echec_leve_discord_bot_error():
    url = f"{_BASE}/channels/chan-1/messages/msg-1/reactions/{quote('🇦', safe='')}"
    respx.get(url).mock(return_value=httpx.Response(500, text="erreur serveur"))
    with pytest.raises(DiscordBotError):
        get_human_reactor(
            channel_id="chan-1",
            message_id="msg-1",
            emoji="🇦",
            bot_token="tok",
            bot_user_id="bot-42",
            timeout=5.0,
        )
