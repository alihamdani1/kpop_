"""Diffusion Discord (T6). Deux routes, deux webhooks — pas de salon par catégorie, voir
context.md §4. `BRUIT_INUTILE` n'atteint jamais cette étape (filtré avant, en amont)."""

from __future__ import annotations

import logging
import time

import httpx

from kpop_bot.models import VIRALITY_BADGES, ArticleRecord, Route, Virality

logger = logging.getLogger(__name__)

_VIRALITY_COLORS: dict[Virality, int] = {
    Virality.VIRAL: 0xE53935,  # rouge
    Virality.ELEVE: 0xFB8C00,  # orange
    Virality.MODERE: 0xFDD835,  # jaune
    Virality.FAIBLE: 0xB0BEC5,  # gris clair
}

_MAX_RETRIES = 3


class NotificationError(Exception):
    """Échec d'envoi Discord après épuisement des tentatives."""


def _score_field(record: ArticleRecord) -> dict:
    assert record.virality is not None  # garanti pour toute route != IGNORED
    badge = VIRALITY_BADGES[record.virality]
    return {
        "name": "Score de viralité",
        "value": f"{badge} {record.virality.value}",
        "inline": True,
    }


def _tweet_field(record: ArticleRecord) -> dict:
    return {"name": "Brouillon de tweet", "value": f"```{record.tweet_draft}```", "inline": False}


def build_embed(record: ArticleRecord) -> dict:
    """Construit l'embed adapté à la route de l'article. Suppose `route in (A, B)`."""
    color = _VIRALITY_COLORS[record.virality] if record.virality else 0x607D8B
    base = {
        "title": record.title,
        "url": record.url,
        "color": color,
        "footer": {"text": record.source},
        "timestamp": record.published_at.isoformat(),
    }

    if record.route == Route.A:
        base["fields"] = [
            _score_field(record),
            {
                "name": "Résumé détaillé",
                "value": record.video_summary or "(non généré)",
                "inline": False,
            },
            _tweet_field(record),
        ]
    else:  # Route.B
        base["fields"] = [_score_field(record), _tweet_field(record)]

    return base


def _webhook_url_for(record: ArticleRecord, *, url_a: str, url_b: str) -> str:
    if record.route == Route.A:
        return url_a
    if record.route == Route.B:
        return url_b
    raise ValueError(f"Aucun webhook pour la route {record.route!r} — article filtré attendu ici.")


def send_embed(webhook_url: str, embed: dict, *, timeout: float) -> None:
    """Envoie un embed, avec une gestion basique du rate-limit Discord (429 + Retry-After)."""
    payload = {"embeds": [embed]}
    for attempt in range(1, _MAX_RETRIES + 1):
        response = httpx.post(webhook_url, json=payload, timeout=timeout)
        if response.status_code in (200, 204):
            return
        if response.status_code == 429:
            retry_after = float(response.json().get("retry_after", 1.0))
            logger.warning(
                "Rate-limit Discord (tentative %d/%d) — pause %.1fs.",
                attempt,
                _MAX_RETRIES,
                retry_after,
            )
            time.sleep(retry_after)
            continue
        raise NotificationError(
            f"Webhook Discord en échec ({response.status_code}) : {response.text[:300]}"
        )
    raise NotificationError("Webhook Discord toujours rate-limité après plusieurs tentatives.")


def notify(record: ArticleRecord, *, url_a: str, url_b: str, timeout: float) -> None:
    """Point d'entrée du pipeline : construit et envoie l'embed pour un article ANALYZED."""
    webhook_url = _webhook_url_for(record, url_a=url_a, url_b=url_b)
    embed = build_embed(record)
    send_embed(webhook_url, embed, timeout=timeout)
