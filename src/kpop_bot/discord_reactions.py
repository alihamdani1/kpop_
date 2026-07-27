"""Client REST minimal pour le Bot Discord de T15 — lecture/ajout de réactions uniquement.

Auth différente du reste du pipeline (Bot token, pas un webhook) et responsabilité différente
(lecture, pas seulement envoi) : séparé de `notifier.py` pour cette raison. Pas de connexion
Gateway, pas de nouvelle dépendance — de simples appels REST ponctuels via `httpx`, comme le
reste du pipeline. L'envoi des messages (embed picker, thread final) reste 100 % webhook, géré
par `notifier.py` — ce module ne fait qu'ajouter/lire des réactions sur des messages déjà postés.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://discord.com/api/v10"


class DiscordBotError(Exception):
    """Échec d'un appel REST authentifié par Bot token."""


def _headers(bot_token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {bot_token}"}


def get_bot_user_id(*, bot_token: str, timeout: float) -> str:
    """Id du bot lui-même — sert à filtrer sa propre réaction (posée par `seed_reactions`) lors
    de la lecture des réactions humaines. Récupéré à chaque cycle plutôt que configuré à la
    main : un appel REST de plus est négligeable, et évite une étape manuelle en plus pour
    l'utilisateur."""
    response = httpx.get(f"{_API_BASE}/users/@me", headers=_headers(bot_token), timeout=timeout)
    if response.status_code != 200:
        raise DiscordBotError(
            f"Échec de lecture de l'identité du bot ({response.status_code}) : "
            f"{response.text[:300]}"
        )
    return str(response.json()["id"])


def seed_reactions(
    *, channel_id: str, message_id: str, emojis: list[str], bot_token: str, timeout: float
) -> None:
    """Ajoute les réactions du bot sur son propre message (embed picker) — l'humain n'a plus
    qu'à cliquer la même émoji pour voter, pas besoin de la saisir lui-même."""
    for emoji in emojis:
        encoded = quote(emoji, safe="")
        url = f"{_API_BASE}/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
        response = httpx.put(url, headers=_headers(bot_token), timeout=timeout)
        if response.status_code not in (200, 204):
            raise DiscordBotError(
                f"Échec d'ajout de la réaction {emoji} ({response.status_code}) : "
                f"{response.text[:300]}"
            )


def get_human_reactor(
    *,
    channel_id: str,
    message_id: str,
    emoji: str,
    bot_token: str,
    bot_user_id: str,
    timeout: float,
) -> str | None:
    """Id du premier utilisateur humain ayant réagi avec `emoji` sur ce message, ou `None` si
    seul le bot a réagi (personne n'a encore voté)."""
    encoded = quote(emoji, safe="")
    url = f"{_API_BASE}/channels/{channel_id}/messages/{message_id}/reactions/{encoded}"
    response = httpx.get(url, headers=_headers(bot_token), timeout=timeout)
    if response.status_code != 200:
        raise DiscordBotError(
            f"Échec de lecture des réactions {emoji} ({response.status_code}) : "
            f"{response.text[:300]}"
        )
    for user in response.json():
        if user.get("id") != bot_user_id:
            return str(user["id"])
    return None
