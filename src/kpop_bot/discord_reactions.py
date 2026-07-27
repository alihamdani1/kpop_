"""Client REST minimal pour le Bot Discord de T15 — lecture/ajout de réactions uniquement.

Auth différente du reste du pipeline (Bot token, pas un webhook) et responsabilité différente
(lecture, pas seulement envoi) : séparé de `notifier.py` pour cette raison. Pas de connexion
Gateway, pas de nouvelle dépendance — de simples appels REST ponctuels via `httpx`, comme le
reste du pipeline. L'envoi des messages (embed picker, thread final) reste 100 % webhook, géré
par `notifier.py` — ce module ne fait qu'ajouter/lire des réactions sur des messages déjà postés.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://discord.com/api/v10"
_MAX_RETRIES = 3


class DiscordBotError(Exception):
    """Échec d'un appel REST authentifié par Bot token, après épuisement des tentatives."""


def _headers(bot_token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {bot_token}"}


def _request_with_retry(
    method: str, url: str, *, headers: dict[str, str], timeout: float
) -> httpx.Response:
    """Gestion du rate-limit Discord (429 + Retry-After), même logique que
    `notifier._post_webhook`. Les endpoints de réactions sont particulièrement stricts côté
    Discord (bien plus que les webhooks) — un 429 y est attendu en usage normal (3 réactions
    posées coup sur coup), pas seulement en cas de rafale anormale. Observé en conditions
    réelles lors du premier déploiement de T15 : `seed_reactions` faisait échouer tout le cycle
    dès le 2e appel PUT, faute de retry."""
    for attempt in range(1, _MAX_RETRIES + 1):
        response = httpx.request(method, url, headers=headers, timeout=timeout)
        if response.status_code in (200, 204):
            return response
        if response.status_code == 429:
            retry_after = float(response.json().get("retry_after", 1.0))
            logger.warning(
                "Rate-limit Discord (bot, tentative %d/%d) — pause %.2fs.",
                attempt,
                _MAX_RETRIES,
                retry_after,
            )
            time.sleep(retry_after)
            continue
        raise DiscordBotError(
            f"Échec de la requête Discord ({response.status_code}) : {response.text[:300]}"
        )
    raise DiscordBotError("Requête Discord toujours rate-limitée après plusieurs tentatives.")


def get_bot_user_id(*, bot_token: str, timeout: float) -> str:
    """Id du bot lui-même — sert à filtrer sa propre réaction (posée par `seed_reactions`) lors
    de la lecture des réactions humaines. Récupéré à chaque cycle plutôt que configuré à la
    main : un appel REST de plus est négligeable, et évite une étape manuelle en plus pour
    l'utilisateur."""
    response = _request_with_retry(
        "GET", f"{_API_BASE}/users/@me", headers=_headers(bot_token), timeout=timeout
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
        _request_with_retry("PUT", url, headers=_headers(bot_token), timeout=timeout)


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
    response = _request_with_retry("GET", url, headers=_headers(bot_token), timeout=timeout)
    for user in response.json():
        if user.get("id") != bot_user_id:
            return str(user["id"])
    return None
