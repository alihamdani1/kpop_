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


def fetch_source(source: Source, *, timeout: float) -> list[FetchedItem]:
    """Récupère et normalise les items d'une seule source. Lève en cas d'échec réseau —
    à l'appelant (`fetch_all`) de journaliser et de passer à la source suivante."""
    response = httpx.get(source.url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)

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
