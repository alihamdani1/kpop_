"""Diffusion Discord (T6). Deux routes, deux webhooks — pas de salon par catégorie, voir
context.md §4. `BRUIT_INUTILE` n'atteint jamais cette étape (filtré avant, en amont).

Chaque article retenu produit QUATRE messages Discord distincts, pas un seul embed multi-champs :
1. En-tête "INFO {n}" (n = position de l'article dans le cycle en cours).
2. L'embed d'information (titre, score, résumé détaillé le cas échéant).
3. En-tête "Brouillon Tweet".
4. Le message texte brut contenant uniquement le brouillon de tweet.
Les en-têtes sont des éléments fixes (aucun appel IA) qui structurent visuellement le salon.
Sur mobile, la sélection tactile à l'intérieur d'un embed à plusieurs champs déborde souvent
sur le champ voisin. En isolant le tweet dans son propre message, sans rien autour, il n'y a
plus rien sur quoi la sélection puisse mordre.
"""

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


def build_embed(record: ArticleRecord) -> dict:
    """Embed d'information (titre, score, résumé) — SANS le brouillon de tweet, envoyé à
    part par `notify()`. Suppose `route in (A, B)`."""
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
        ]
    else:  # Route.B
        base["fields"] = [_score_field(record)]

    return base


def build_info_header(index: int) -> str:
    """En-tête précédant l'embed d'info. `index` = position de l'article dans le cycle en
    cours (1er article envoyé -> "INFO 1", 2e -> "INFO 2", etc.).

    Syntaxe titre Markdown ("# ") plutôt que du simple gras : rendu identique et fonctionnel
    sur mobile comme sur desktop, tant que c'est un message texte brut (pas un champ d'embed,
    où les titres Markdown ne sont pas rendus — cf. discord/discord-api-docs#7167)."""
    return f"# INFO {index}"


def build_tweet_header() -> str:
    """En-tête précédant le message-tweet. Voir la note de `build_info_header`."""
    return "# Brouillon Tweet"


def _webhook_url_for(record: ArticleRecord, *, url_a: str, url_b: str) -> str:
    if record.route == Route.A:
        return url_a
    if record.route == Route.B:
        return url_b
    raise ValueError(f"Aucun webhook pour la route {record.route!r} — article filtré attendu ici.")


def _post_webhook(webhook_url: str, payload: dict, *, timeout: float) -> None:
    """Envoi générique, avec gestion basique du rate-limit Discord (429 + Retry-After)."""
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


def send_embed(webhook_url: str, embed: dict, *, timeout: float) -> None:
    _post_webhook(webhook_url, {"embeds": [embed]}, timeout=timeout)


def send_message(webhook_url: str, content: str, *, timeout: float) -> None:
    _post_webhook(webhook_url, {"content": content}, timeout=timeout)


def notify(record: ArticleRecord, *, url_a: str, url_b: str, timeout: float, index: int) -> None:
    """Point d'entrée du pipeline : envoie les 4 messages d'un article ANALYZED, dans
    l'ordre, vers le même webhook. `index` numérote l'article au sein du cycle en cours
    (voir `build_info_header`)."""
    webhook_url = _webhook_url_for(record, url_a=url_a, url_b=url_b)
    send_message(webhook_url, build_info_header(index), timeout=timeout)
    send_embed(webhook_url, build_embed(record), timeout=timeout)
    send_message(webhook_url, build_tweet_header(), timeout=timeout)
    send_message(webhook_url, record.tweet_draft, timeout=timeout)


def build_review_message(record: ArticleRecord) -> str:
    """Message unique et allégé pour #info-a-verifier (T13) — pas le format 4-messages
    d'`notify()`, qui n'a de sens que pour du contenu à copier-coller. Réutilise uniquement ce
    que `classify()` a déjà produit (titre original, catégorie, importance) : aucun appel IA
    supplémentaire pour ce filet de sécurité."""
    category = record.category.value if record.category else "?"
    importance = record.importance.value if record.importance else "?"
    return f"**{record.title}**\n{record.source} — classé {category} / {importance}\n{record.url}"


def notify_review(record: ArticleRecord, *, url: str, timeout: float) -> None:
    """Envoie un article filtré (BRUIT_INUTILE) vers #info-a-verifier — un seul message, sans
    embed, pour rester léger vu le volume potentiellement élevé (voir T13)."""
    send_message(url, build_review_message(record), timeout=timeout)
