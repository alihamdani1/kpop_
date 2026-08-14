"""Collecte RSS (T4). Une source en échec est journalisée et n'interrompt jamais les autres."""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx
import yaml

from kpop_bot.models import FetchedItem

logger = logging.getLogger(__name__)

_TRACKING_PREFIXES = ("utm_",)
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)

# Certains sites (Yonhap, allkpop) rejettent les User-Agent par défaut des bibliothèques HTTP.
# En-têtes de navigateur standards pour paraître légitime — pas une garantie contre une
# protection plus poussée (challenge JS, fingerprinting), seulement contre un filtre naïf.
# Public (pas de préfixe _) : réutilisé par scraper.py (T17) pour la même raison, sur les mêmes
# sources — inutile de dupliquer une 2e liste d'en-têtes.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class EmptyFeedError(ValueError):
    """La requête a réussi mais n'a produit aucun item exploitable — probablement une page
    de challenge/erreur renvoyée avec un statut 200 plutôt qu'un vrai flux RSS/Atom."""


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    active: bool


def load_sources(path: Path) -> list[Source]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Source(**entry) for entry in data.get("sources", [])]


def canonical_url(raw_url: str) -> str:
    """Retire les paramètres de tracking (utm_*) pour stabiliser l'empreinte de dédup."""
    parsed = urlparse(raw_url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def compute_fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _strip_html(raw: str) -> str:
    return html.unescape(_TAG_RE.sub("", raw)).strip()


def _entry_published_at(entry: feedparser.FeedParserDict) -> dt.datetime:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct is None:
        logger.warning(
            "Aucune date exploitable pour %r — repli sur l'heure courante.", entry.get("link")
        )
        return dt.datetime.now(dt.UTC)
    return dt.datetime(*struct[:6], tzinfo=dt.UTC)


def _first_media_url(entries: list[dict] | None) -> str | None:
    for entry in entries or []:
        url = entry.get("url")
        if url:
            return url
    return None


def _extract_image_url(entry: feedparser.FeedParserDict) -> str | None:
    """Image d'illustration de l'article, pour le visuel social 9:16 (voir social_pipeline.py).
    Essayée dans cet ordre : media:content, media:thumbnail, enclosure (type image/*), premier
    <img> du HTML brut du résumé/contenu (avant le passage par `_strip_html`, appliqué séparément
    pour `raw_summary`). Aucune de ces sources ne lève — None si rien d'exploitable, le pipeline
    de visuels se rabat alors sur la bibliothèque interne (media_library.py)."""
    url = _first_media_url(entry.get("media_content"))
    if url:
        return url
    url = _first_media_url(entry.get("media_thumbnail"))
    if url:
        return url
    for enclosure in entry.get("enclosures", []) or []:
        if str(enclosure.get("type", "")).startswith("image/"):
            enclosure_url = enclosure.get("href") or enclosure.get("url")
            if enclosure_url:
                return enclosure_url

    html_blobs = [entry.get("summary", "")]
    html_blobs.extend(block.get("value", "") for block in entry.get("content") or [])
    for blob in html_blobs:
        match = _IMG_SRC_RE.search(blob or "")
        if match:
            return match.group(1)
    return None


def fetch_source(source: Source, *, timeout: float) -> list[FetchedItem]:
    """Récupère et normalise les items d'une seule source. Lève en cas d'échec réseau ou de
    flux vide — à l'appelant (`fetch_all`) de journaliser et de passer à la source suivante."""
    response = httpx.get(
        source.url, timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS
    )
    response.raise_for_status()
    parsed = feedparser.parse(response.content)

    if not parsed.entries:
        # feedparser ne lève jamais sur un contenu inattendu (page de challenge, HTML
        # générique...) : il retourne juste 0 entrée. On le traite explicitement comme un
        # échec, pour ne jamais activer silencieusement une source qui ne remonte rien.
        raise EmptyFeedError(
            f"Réponse reçue mais aucun item exploitable (bozo={parsed.bozo}) — "
            "probablement pas un flux RSS/Atom valide."
        )

    items: list[FetchedItem] = []
    for entry in parsed.entries:
        url = canonical_url(entry.get("link", ""))
        if not url:
            continue
        raw_summary = _strip_html(entry.get("summary", entry.get("title", "")))
        items.append(
            FetchedItem(
                source=source.name,
                title=_strip_html(entry.get("title", "(sans titre)")),
                url=url,
                published_at=_entry_published_at(entry),
                raw_summary=raw_summary,
                fingerprint=compute_fingerprint(url),
                image_url=_extract_image_url(entry),
            )
        )
    return items


def fetch_all(sources: list[Source], *, timeout: float) -> list[FetchedItem]:
    """Interroge toutes les sources actives. Une source en échec ne bloque pas les autres."""
    items: list[FetchedItem] = []
    for source in sources:
        if not source.active:
            continue
        try:
            source_items = fetch_source(source, timeout=timeout)
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("Échec de collecte pour %s (%s) : %s", source.name, source.url, exc)
            continue
        logger.info("%s : %d item(s) collecté(s).", source.name, len(source_items))
        items.extend(source_items)
    return items
