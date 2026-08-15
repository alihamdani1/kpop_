from __future__ import annotations

import base64
from pathlib import Path

import httpx
import respx

from kpop_bot import visual_generator

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "social_post.html"


def test_font_size_class_texte_court():
    assert visual_generator._font_size_class("a" * 20) == "text-lg"


def test_font_size_class_texte_moyen():
    assert visual_generator._font_size_class("a" * 55) == "text-md"


def test_font_size_class_texte_long():
    assert visual_generator._font_size_class("a" * 100) == "text-sm"


def _build_html(
    *,
    image_bytes: bytes = b"x",
    headline: str = "Un titre de test",
    key_points: list[str] | None = None,
) -> str:
    return visual_generator.build_html(
        _TEMPLATE_PATH,
        image_bytes=image_bytes,
        headline=headline,
        key_points=key_points if key_points is not None else [],
        category_label="RELEASE",
        formatted_date="14 août 2026",
    )


def test_build_html_integre_image_et_titre():
    html = _build_html(image_bytes=b"fake-image-bytes", headline="Un titre de test")
    encoded = base64.b64encode(b"fake-image-bytes").decode("ascii")
    assert f"data:image/jpeg;base64,{encoded}" in html
    assert "Un titre de test" in html
    assert 'class="headline text-lg"' in html


def test_build_html_integre_categorie_et_date():
    html = _build_html()
    assert "RELEASE" in html
    assert "14 août 2026" in html


def test_build_html_integre_les_points_cles_si_presents():
    html = _build_html(key_points=["Premier point.", "Deuxième point."])
    assert "Premier point." in html
    assert "Deuxième point." in html
    assert '<ul class="key-points">' in html


def test_build_html_omet_la_liste_de_points_cles_si_vide():
    html = _build_html(key_points=[])
    assert '<ul class="key-points">' not in html


def test_build_html_echappe_le_html_dans_le_texte():
    """Le texte affiché n'est jamais du HTML de confiance — un '<' ou '&' littéral ne doit pas
    casser la mise en page ni être interprété comme balise."""
    html = _build_html(headline="Une percée <script> & compagnie")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_build_html_choisit_la_classe_selon_la_longueur():
    html = _build_html(headline="a" * 100)
    assert 'class="headline text-sm"' in html


# --- download_image (respx, aucun réseau réel). ---

_IMAGE_URL = "https://example.com/photo.jpg"


@respx.mock
def test_download_image_succes():
    respx.get(_IMAGE_URL).mock(
        return_value=httpx.Response(200, content=b"bytes", headers={"content-type": "image/jpeg"})
    )
    assert visual_generator.download_image(_IMAGE_URL, timeout=5.0) == b"bytes"


@respx.mock
def test_download_image_none_si_type_de_contenu_non_image():
    respx.get(_IMAGE_URL).mock(
        return_value=httpx.Response(
            200, content=b"<html></html>", headers={"content-type": "text/html"}
        )
    )
    assert visual_generator.download_image(_IMAGE_URL, timeout=5.0) is None


@respx.mock
def test_download_image_none_si_statut_erreur():
    respx.get(_IMAGE_URL).mock(return_value=httpx.Response(404))
    assert visual_generator.download_image(_IMAGE_URL, timeout=5.0) is None


@respx.mock
def test_download_image_none_si_trop_volumineuse():
    big_content = b"x" * (visual_generator._MAX_IMAGE_BYTES + 1)
    respx.get(_IMAGE_URL).mock(
        return_value=httpx.Response(
            200, content=big_content, headers={"content-type": "image/jpeg"}
        )
    )
    assert visual_generator.download_image(_IMAGE_URL, timeout=5.0) is None


@respx.mock
def test_download_image_none_si_erreur_reseau():
    respx.get(_IMAGE_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert visual_generator.download_image(_IMAGE_URL, timeout=5.0) is None


def test_template_existe_et_est_lisible():
    assert _TEMPLATE_PATH.exists()
    assert _TEMPLATE_PATH.read_text(encoding="utf-8")
