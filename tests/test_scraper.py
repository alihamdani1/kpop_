from __future__ import annotations

import httpx
import respx

from kpop_bot import scraper

_URL = "https://www.soompi.com/article/123"

_FULL_PAGE = """
<html>
<head>
    <meta property="og:image" content="/images/main.jpg">
</head>
<body>
    <nav><p>Menu non pertinent</p></nav>
    <article>
        <p>Le membre Jane du groupe X a confirmé son retour en solo.</p>
        <p>La sortie est prévue pour le mois prochain, selon l'agence.</p>
        <img src="/images/inline1.jpg">
        <img src="/images/inline2.jpg">
    </article>
    <footer><p>Pied de page non pertinent</p></footer>
</body>
</html>
"""


@respx.mock
def test_fetch_article_page_extrait_le_texte_et_l_image_principale():
    respx.get(_URL).mock(return_value=httpx.Response(200, text=_FULL_PAGE))
    result = scraper.fetch_article_page(_URL, timeout=5.0)
    assert result is not None
    assert "Jane" in result.text
    assert result.main_image_url == "https://www.soompi.com/images/main.jpg"


@respx.mock
def test_fetch_article_page_ignore_la_navigation_et_le_pied_de_page():
    respx.get(_URL).mock(return_value=httpx.Response(200, text=_FULL_PAGE))
    result = scraper.fetch_article_page(_URL, timeout=5.0)
    assert result is not None
    assert "Menu non pertinent" not in result.text
    assert "Pied de page non pertinent" not in result.text


@respx.mock
def test_fetch_article_page_collecte_les_images_additionnelles_sans_l_image_principale():
    respx.get(_URL).mock(return_value=httpx.Response(200, text=_FULL_PAGE))
    result = scraper.fetch_article_page(_URL, timeout=5.0)
    assert result is not None
    assert result.main_image_url not in result.extra_image_urls
    assert "https://www.soompi.com/images/inline1.jpg" in result.extra_image_urls
    assert "https://www.soompi.com/images/inline2.jpg" in result.extra_image_urls


@respx.mock
def test_fetch_article_page_repli_sur_twitter_image_si_pas_d_og_image():
    html = """
    <html><head><meta name="twitter:image" content="https://cdn.example.com/tw.jpg"></head>
    <body><article><p>Un paragraphe suffisamment long pour être retenu.</p></article></body>
    </html>
    """
    respx.get(_URL).mock(return_value=httpx.Response(200, text=html))
    result = scraper.fetch_article_page(_URL, timeout=5.0)
    assert result is not None
    assert result.main_image_url == "https://cdn.example.com/tw.jpg"


@respx.mock
def test_fetch_article_page_sans_paragraphe_renvoie_none():
    html = "<html><body><div>Rien d'exploitable ici, aucune balise p.</div></body></html>"
    respx.get(_URL).mock(return_value=httpx.Response(200, text=html))
    assert scraper.fetch_article_page(_URL, timeout=5.0) is None


@respx.mock
def test_fetch_article_page_erreur_http_renvoie_none_sans_lever():
    respx.get(_URL).mock(return_value=httpx.Response(500))
    assert scraper.fetch_article_page(_URL, timeout=5.0) is None


@respx.mock
def test_fetch_article_page_erreur_reseau_renvoie_none_sans_lever():
    respx.get(_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert scraper.fetch_article_page(_URL, timeout=5.0) is None


@respx.mock
def test_fetch_article_page_tronque_le_texte_trop_long():
    long_paragraph = "mot " * 2000  # bien au-delà de _MAX_TEXT_CHARS
    html = f"<html><body><article><p>{long_paragraph}</p></article></body></html>"
    respx.get(_URL).mock(return_value=httpx.Response(200, text=html))
    result = scraper.fetch_article_page(_URL, timeout=5.0)
    assert result is not None
    assert len(result.text) == scraper._MAX_TEXT_CHARS
