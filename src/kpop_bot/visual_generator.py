"""Génération du visuel social 9:16 (image d'article + tweet -> PNG), pour diffusion via
`social_pipeline.py` sur un salon Discord privé de prévisualisation (relecture avant publication
manuelle sur TikTok/Instagram — même principe human-in-the-loop que le reste du projet).

Rendu HTML/CSS (`templates/social_post.html`, Jinja2) via Chromium headless (Playwright) — choisi
plutôt qu'une composition raster (Pillow) pour obtenir gratuitement, via CSS (`object-fit: cover`,
flexbox), un rendu responsive qui ne déforme jamais l'image source ni ne fait déborder un texte de
longueur variable."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import jinja2
from playwright.sync_api import Browser, sync_playwright

_MAX_IMAGE_BYTES = 10 * 1024 * 1024

_VIEWPORT_WIDTH = 1080
_VIEWPORT_HEIGHT = 1920  # 9:16 — format vertical standard TikTok/Reels/Stories.

# Paliers de taille de police par longueur de texte, calculés en Python plutôt qu'en CSS pur
# (clamp() seul ne suffirait pas à garantir qu'un tweet proche de la limite de 260 caractères ne
# déborde jamais du bloc texte).
_FONT_SIZE_TIERS: list[tuple[int, str]] = [
    (100, "text-lg"),
    (180, "text-md"),
]
_FONT_SIZE_DEFAULT = "text-sm"


def _font_size_class(text: str) -> str:
    for max_length, css_class in _FONT_SIZE_TIERS:
        if len(text) <= max_length:
            return css_class
    return _FONT_SIZE_DEFAULT


def _image_data_uri(image_bytes: bytes) -> str:
    """Encode l'image en `data:` URI, embarquée directement dans le HTML — pas de fichier
    temporaire pour l'image source, chaque rendu reste autonome. Le type MIME exact importe peu
    au navigateur pour l'affichage ; JPEG en repli couvre le cas le plus courant en photo presse."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def build_html(
    template_path: Path,
    *,
    image_bytes: bytes,
    tweet_text: str,
    category_label: str,
    formatted_date: str,
) -> str:
    """Rendu Jinja2 pur (aucun navigateur) — testable indépendamment de Playwright. Le contenu
    (catégorie, date) est décidé côté social_pipeline.py — ce module ignore tout de la sémantique
    d'un article, il ne fait que gabarier des chaînes déjà prêtes."""
    env = jinja2.Environment(autoescape=True)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    return template.render(
        image_data_uri=_image_data_uri(image_bytes),
        tweet_text=tweet_text,
        font_size_class=_font_size_class(tweet_text),
        category_label=category_label,
        formatted_date=formatted_date,
    )


class SocialVisualRenderer:
    """Wrapper Playwright — un seul navigateur Chromium lancé pour tout un batch d'articles (le
    coût de lancement d'un navigateur par article serait disproportionné vu le volume).

    Usage :
        with SocialVisualRenderer(template_path) as renderer:
            for article in articles:
                png_bytes = renderer.render(
                    image_bytes=..., tweet_text=..., category_label=..., formatted_date=...
                )
    """

    def __init__(self, template_path: Path) -> None:
        self._template_path = template_path
        self._playwright = None
        self._browser: Browser | None = None

    def __enter__(self) -> SocialVisualRenderer:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def render(
        self, *, image_bytes: bytes, tweet_text: str, category_label: str, formatted_date: str
    ) -> bytes:
        assert self._browser is not None, "SocialVisualRenderer doit être utilisé via `with`."
        html = build_html(
            self._template_path,
            image_bytes=image_bytes,
            tweet_text=tweet_text,
            category_label=category_label,
            formatted_date=formatted_date,
        )
        page = self._browser.new_page(
            viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT}
        )
        try:
            page.set_content(html, wait_until="load")
            return page.screenshot()
        finally:
            page.close()


def download_image(url: str, *, timeout: float) -> bytes | None:
    """Télécharge l'image RSS de l'article. None sur tout échec (réseau, statut, type de
    contenu, taille) — déclenche le repli media_library côté social_pipeline.py, jamais une
    exception qui interromprait le run."""
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    if not response.headers.get("content-type", "").startswith("image/"):
        return None
    if len(response.content) > _MAX_IMAGE_BYTES:
        return None
    return response.content
