"""Scraping best-effort de la page article (T18) — vient compléter l'extrait RSS, souvent trop
court pour porter une information précise (ex. le nom de l'idole n'apparaît que dans le corps
de l'article, jamais dans le titre ni l'extrait — voir TODO.md T18). Utilisé par `pipeline.py`
pour enrichir `classify()`/`write()`/`write_social_visual()`, et pour trouver une image
principale plus fiable que l'extraction RSS existante (`fetcher._extract_image_url`).

Best-effort partout : `fetch_article_page` ne lève jamais. Un site injoignable, une page sans
texte exploitable, ou un HTML inattendu retombent simplement sur `None` — l'appelant continue
alors avec le seul extrait RSS, comme avant l'introduction de ce module."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from kpop_bot.fetcher import BROWSER_HEADERS

logger = logging.getLogger(__name__)

# Borne la taille de texte envoyée aux prompts IA — un coût token maîtrisé plutôt qu'un article
# entier, largement suffisant pour lever l'ambiguïté d'un extrait RSS trop vague.
_MAX_TEXT_CHARS = 4000

# Retirées avant extraction du texte : navigation, pieds de page, scripts... jamais du contenu
# éditorial de l'article lui-même.
_NOISE_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form")


@dataclass(frozen=True)
class ScrapedArticle:
    text: str
    main_image_url: str | None
    extra_image_urls: list[str]


def _extract_main_image(soup: BeautifulSoup, base_url: str) -> str | None:
    """`og:image`/`twitter:image` : quasi universel sur les sites d'actu, bien plus fiable que
    deviner une balise <img> au hasard dans la page."""
    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
        tag = soup.find("meta", attrs=attrs)
        content = tag.get("content") if tag else None
        if content:
            return urljoin(base_url, content)
    return None


def _article_root(soup: BeautifulSoup) -> BeautifulSoup:
    """Le conteneur le plus probable du corps de l'article — repli sur la page entière si
    aucune balise sémantique n'est trouvée (mieux qu'un texte vide)."""
    return soup.find("article") or soup.find("main") or soup


def _extract_body_text(root: BeautifulSoup) -> str:
    for tag in root.find_all(_NOISE_TAGS):
        tag.decompose()
    paragraphs = [p.get_text(" ", strip=True) for p in root.find_all("p")]
    text = " ".join(p for p in paragraphs if p)
    return text[:_MAX_TEXT_CHARS]


def _extract_extra_images(root: BeautifulSoup, base_url: str, *, exclude: str | None) -> list[str]:
    urls: list[str] = []
    for img in root.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        resolved = urljoin(base_url, src)
        if resolved == exclude or resolved in urls:
            continue
        urls.append(resolved)
    return urls


def fetch_article_page(url: str, *, timeout: float) -> ScrapedArticle | None:
    """Récupère et parse la page d'un article. `None` si la page n'apporte rien d'exploitable
    (échec réseau, HTML sans paragraphe identifiable...) — ne lève jamais, voir docstring du
    module."""
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        root = _article_root(soup)
        text = _extract_body_text(root)
        if not text:
            return None
        main_image = _extract_main_image(soup, url)
        extra_images = _extract_extra_images(root, url, exclude=main_image)
        return ScrapedArticle(text=text, main_image_url=main_image, extra_image_urls=extra_images)
    except Exception as exc:  # best-effort — un article sans page exploitable n'est jamais fatal
        logger.info("Scraping de page ignoré pour %s : %s", url, exc)
        return None


__all__ = ["ScrapedArticle", "fetch_article_page"]
