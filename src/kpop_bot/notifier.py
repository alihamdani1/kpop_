"""Diffusion Discord (T6). Route A/B par score de viralité, plus une route dédiée
Concert/Événement France (#concert, optionnelle — voir context.md §4). `BRUIT_INUTILE`
n'atteint jamais cette étape (filtré avant, en amont).

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

import json
import logging
import mimetypes
import time
from pathlib import Path

import httpx

from kpop_bot.models import (
    VIRALITY_BADGES,
    ArticleRecord,
    Route,
    ThreadAngle,
    ThreadRecord,
    ThreadTopicRecord,
    Virality,
)

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
    part par `notify()`. Suppose `route in (A, B, CONCERT)`."""
    color = _VIRALITY_COLORS[record.virality] if record.virality else 0x607D8B
    base = {
        "title": record.title,
        "url": record.url,
        "color": color,
        "footer": {"text": record.source},
        "timestamp": record.published_at.isoformat(),
    }

    if record.route in (Route.A, Route.CONCERT):
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


def _webhook_url_for(
    record: ArticleRecord, *, url_a: str, url_b: str, url_concert: str | None = None
) -> str:
    if record.route == Route.A:
        return url_a
    if record.route == Route.B:
        return url_b
    if record.route == Route.CONCERT:
        # Absent -> repli sur Route A (comportement d'avant l'introduction de #concert),
        # voir settings.discord_webhook_concert.
        return url_concert or url_a
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


def _post_webhook_with_files(
    webhook_url: str, content: str, image_paths: list[Path], *, timeout: float
) -> None:
    """Poste un message texte + une ou plusieurs images jointes dans la même requête (multipart,
    T16/T16bis) — même gestion du rate-limit que `_post_webhook`. Discord exige un encodage
    différent (`data`/`files`) dès qu'un fichier est joint, d'où une fonction dédiée plutôt qu'un
    paramètre optionnel sur `_post_webhook`."""
    payload_json = json.dumps({"content": content}, ensure_ascii=False)
    for attempt in range(1, _MAX_RETRIES + 1):
        opened = [path.open("rb") for path in image_paths]
        try:
            files = [
                (
                    f"files[{index}]",
                    (
                        path.name,
                        handle,
                        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    ),
                )
                for index, (path, handle) in enumerate(zip(image_paths, opened, strict=True))
            ]
            response = httpx.post(
                webhook_url,
                data={"payload_json": payload_json},
                files=files,
                timeout=timeout,
            )
        finally:
            for handle in opened:
                handle.close()
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


def send_message_with_image(
    webhook_url: str, content: str, image_path: Path, *, timeout: float
) -> None:
    _post_webhook_with_files(webhook_url, content, [image_path], timeout=timeout)


def send_message_with_images(
    webhook_url: str, content: str, image_paths: list[Path], *, timeout: float
) -> None:
    _post_webhook_with_files(webhook_url, content, image_paths, timeout=timeout)


def notify(
    record: ArticleRecord,
    *,
    url_a: str,
    url_b: str,
    url_concert: str | None = None,
    timeout: float,
    index: int,
) -> None:
    """Point d'entrée du pipeline : envoie les 4 messages d'un article ANALYZED, dans
    l'ordre, vers le même webhook. `index` numérote l'article au sein du cycle en cours
    (voir `build_info_header`)."""
    webhook_url = _webhook_url_for(record, url_a=url_a, url_b=url_b, url_concert=url_concert)
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


# --- Threads Twitter quotidiens (T15) — picker Discord (webhook + réactions bot) et diffusion
# du thread final. Voir discord_reactions.py pour la lecture/pose des réactions (auth Bot
# distincte du webhook utilisé ici). ---

_THREAD_OPTION_EMOJIS = ["🇦", "🇧", "🇨"]


def build_thread_selection_embed(options: list[tuple[ThreadTopicRecord, ThreadAngle]]) -> dict:
    """Embed du picker quotidien (T15) — exactement 3 options (topic, angle), dans l'ordre
    A/B/C. Aucun appel Gemini à ce stade : uniquement des topics déjà en backlog."""
    fields = [
        {
            "name": f"{emoji} {topic.title}",
            "value": (
                f"**Groupe/portée** : {topic.group_name}\n"
                f"**Thème** : {topic.theme.value}\n"
                f"**Angle** : {angle.value}\n"
                f"{topic.premise}"
            ),
            "inline": False,
        }
        for emoji, (topic, angle) in zip(_THREAD_OPTION_EMOJIS, options, strict=True)
    ]
    return {
        "title": "🧵 Thread du jour — choisis un sujet",
        "description": "Réagis avec 🇦, 🇧 ou 🇨 pour lancer la rédaction du thread choisi.",
        "color": 0x1DA1F2,
        "fields": fields,
    }


def _post_webhook_wait(webhook_url: str, payload: dict, *, timeout: float) -> dict:
    """Variante de `_post_webhook` avec `?wait=true`, pour récupérer le message Discord créé
    (son id) — nécessaire pour le picker T15 dont on doit ensuite lire les réactions dessus.
    Même gestion de rate-limit que `_post_webhook`."""
    separator = "&" if "?" in webhook_url else "?"
    url = f"{webhook_url}{separator}wait=true"
    for attempt in range(1, _MAX_RETRIES + 1):
        response = httpx.post(url, json=payload, timeout=timeout)
        if response.status_code in (200, 201):
            return response.json()
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


def notify_thread_selection(
    options: list[tuple[ThreadTopicRecord, ThreadAngle]], *, url: str, timeout: float
) -> str:
    """Poste l'embed picker et retourne l'id du message Discord créé — à transmettre à
    `discord_reactions.seed_reactions` puis à enregistrer dans `thread_selections`."""
    embed = build_thread_selection_embed(options)
    message = _post_webhook_wait(url, {"embeds": [embed]}, timeout=timeout)
    return str(message["id"])


def build_thread_intro_message(topic: ThreadTopicRecord, angle: ThreadAngle, total: int) -> str:
    return (
        f"# Thread du jour ({total} tweets)\n**{topic.title}** — {topic.group_name} / {angle.value}"
    )


def build_thread_tweet_header(index: int, total: int) -> str:
    """En-tête précédant chaque tweet. Voir la note de `build_info_header` (T6) : élément fixe
    séparé, jamais mélangé au contenu du tweet lui-même."""
    return f"# Tweet {index}/{total}"


def build_thread_extra_images_message(count: int) -> str:
    """Légende du message final regroupant les photos alternatives (T16bis)."""
    return f"# {count} autre(s) photo(s) au choix, si une image d'un tweet ne convient pas"


# --- Visuel social 9:16 (RSS image + tweet -> PNG, voir visual_generator.py/social_pipeline.py)
# — salon Discord privé de prévisualisation avant publication manuelle sur TikTok/Instagram. ---


def build_social_visual_header(index: int) -> str:
    """En-tête précédant le visuel. Voir la note de `build_info_header` (T6)."""
    return f"# Post {index}"


def notify_social_visual(
    record: ArticleRecord, image_path: Path, *, url: str, timeout: float, index: int
) -> None:
    """Envoie le visuel généré (en-tête + tweet en contenu + visuel en pièce jointe) vers le
    salon de prévisualisation. Réutilise `send_message_with_image` telle quelle (T16) — le texte
    du tweet reste le contenu du message, la pièce jointe n'affecte pas sa copie (T16bis)."""
    send_message(url, build_social_visual_header(index), timeout=timeout)
    send_message_with_image(url, record.tweet_draft, image_path, timeout=timeout)


def notify_thread(
    thread: ThreadRecord,
    topic: ThreadTopicRecord,
    *,
    url: str,
    timeout: float,
    image_paths: list[Path] | None = None,
    extra_image_paths: list[Path] | None = None,
) -> None:
    """Diffuse le thread généré (T15) — deux messages par tweet (en-tête, puis le tweet seul),
    jamais fusionnés. Correction (bug initial T15) : un en-tête + bloc de code ``` dans le MÊME
    message que le tweet cassait le copier-coller mobile — le bouton natif « Copier le texte »
    de Discord copie tout le contenu brut du message, en-tête et balises de code compris. En
    isolant le tweet seul dans son propre message, sans rien autour, la copie mobile récupère
    exactement le texte du tweet — même principe déjà validé pour `tweet_draft` en T6.

    `image_paths` (T16, optionnel) : une image par tweet, jointe au message du tweet lui-même
    (pas à l'en-tête) — le texte reste exactement copiable, la pièce jointe ne s'ajoute pas au
    contenu textuel. Liste plus courte que le nombre de tweets, ou absente : dégradation
    gracieuse, les tweets restants partent simplement sans image.

    `extra_image_paths` (T16bis, optionnel) : photos alternatives envoyées dans un dernier
    message, pour permettre de substituer une image sur un tweet précis avant publication
    manuelle sur X — absentes ou vides : ce message n'est simplement pas envoyé."""
    total = len(thread.tweets)
    send_message(url, build_thread_intro_message(topic, thread.angle, total), timeout=timeout)
    for index, tweet in enumerate(thread.tweets, start=1):
        send_message(url, build_thread_tweet_header(index, total), timeout=timeout)
        image_path = (
            image_paths[index - 1] if image_paths and index - 1 < len(image_paths) else None
        )
        if image_path is not None:
            send_message_with_image(url, tweet, image_path, timeout=timeout)
        else:
            send_message(url, tweet, timeout=timeout)

    if extra_image_paths:
        send_message_with_images(
            url,
            build_thread_extra_images_message(len(extra_image_paths)),
            extra_image_paths,
            timeout=timeout,
        )
