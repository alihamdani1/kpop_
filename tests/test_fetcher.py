from __future__ import annotations

from pathlib import Path

import feedparser
import httpx
import pytest
import respx

from kpop_bot.fetcher import (
    EmptyFeedError,
    Source,
    canonical_url,
    compute_fingerprint,
    fetch_all,
    fetch_source,
    load_sources,
)

_VALID_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test Feed</title>
<item>
  <title>Un article de test</title>
  <link>https://example.com/article-1</link>
  <description>Un résumé.</description>
  <pubDate>Fri, 24 Jul 2026 12:00:00 GMT</pubDate>
</item>
</channel></rss>
"""


def test_canonical_url_strips_utm_params():
    raw = "https://www.soompi.com/article/123?utm_source=newsletter&utm_medium=email&ref=abc"
    result = canonical_url(raw)
    assert "utm_source" not in result
    assert "utm_medium" not in result
    assert "ref=abc" in result  # seuls les paramètres utm_* sont retirés


def test_canonical_url_sans_query_string_est_inchangee():
    raw = "https://www.soompi.com/article/123"
    assert canonical_url(raw) == raw


def test_compute_fingerprint_est_deterministe():
    url = "https://www.soompi.com/article/123"
    assert compute_fingerprint(url) == compute_fingerprint(url)


def test_compute_fingerprint_differe_selon_url():
    fp1 = compute_fingerprint("https://www.soompi.com/article/123")
    fp2 = compute_fingerprint("https://www.soompi.com/article/456")
    assert fp1 != fp2


def test_load_sources(tmp_path: Path):
    config = tmp_path / "sources.yaml"
    config.write_text(
        """
sources:
  - name: Soompi
    url: https://www.soompi.com/feed
    active: true
  - name: Yonhap Entertainment
    url: https://example.com/yonhap-rss
    active: false
""",
        encoding="utf-8",
    )
    sources = load_sources(config)
    assert len(sources) == 2
    assert sources[0].name == "Soompi"
    assert sources[0].active is True
    assert sources[1].active is False


def test_load_sources_fichier_vide(tmp_path: Path):
    config = tmp_path / "sources.yaml"
    config.write_text("", encoding="utf-8")
    assert load_sources(config) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<p>Texte avec <b>balises</b>.</p>", "Texte avec balises."),
        ("Sans balises.", "Sans balises."),
        ("Entit&eacute; HTML", "Entité HTML"),
    ],
)
def test_strip_html(raw: str, expected: str):
    from kpop_bot.fetcher import _strip_html

    assert _strip_html(raw) == expected


# --- Extraction de l'image RSS (visuel social 9:16). ---


def _entry_with_extra_xml(extra: str) -> feedparser.FeedParserDict:
    xml = f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Test</title>
<item>
  <title>Article de test</title>
  <link>https://example.com/article</link>
  <description>Un résumé.</description>
  {extra}
</item>
</channel></rss>
"""
    return feedparser.parse(xml).entries[0]


def test_extract_image_url_media_content():
    from kpop_bot.fetcher import _extract_image_url

    entry = _entry_with_extra_xml(
        '<media:content url="https://example.com/media-content.jpg" medium="image"/>'
    )
    assert _extract_image_url(entry) == "https://example.com/media-content.jpg"


def test_extract_image_url_media_thumbnail_si_pas_de_media_content():
    from kpop_bot.fetcher import _extract_image_url

    entry = _entry_with_extra_xml('<media:thumbnail url="https://example.com/media-thumb.jpg"/>')
    assert _extract_image_url(entry) == "https://example.com/media-thumb.jpg"


def test_extract_image_url_enclosure_image():
    from kpop_bot.fetcher import _extract_image_url

    entry = _entry_with_extra_xml(
        '<enclosure url="https://example.com/enclosure.jpg" type="image/jpeg" length="123"/>'
    )
    assert _extract_image_url(entry) == "https://example.com/enclosure.jpg"


def test_extract_image_url_ignore_enclosure_non_image():
    from kpop_bot.fetcher import _extract_image_url

    entry = _entry_with_extra_xml(
        '<enclosure url="https://example.com/podcast.mp3" type="audio/mpeg" length="123"/>'
    )
    assert _extract_image_url(entry) is None


def test_extract_image_url_repli_sur_img_dans_le_html_du_resume():
    from kpop_bot.fetcher import _extract_image_url

    xml = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item>
  <title>Article de test</title>
  <link>https://example.com/article</link>
  <description><![CDATA[<p>Texte <img src="https://example.com/inline.jpg" alt="">
  </p>]]></description>
</item>
</channel></rss>
"""
    entry = feedparser.parse(xml).entries[0]
    assert _extract_image_url(entry) == "https://example.com/inline.jpg"


def test_extract_image_url_none_si_rien_trouve():
    from kpop_bot.fetcher import _extract_image_url

    entry = _entry_with_extra_xml("")
    assert _extract_image_url(entry) is None


@respx.mock
def test_fetch_source_alimente_image_url_sur_fetched_item():
    xml = """<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Test</title>
<item>
  <title>Article avec image</title>
  <link>https://example.com/article-image</link>
  <description>Un résumé.</description>
  <pubDate>Fri, 24 Jul 2026 12:00:00 GMT</pubDate>
  <media:content url="https://example.com/photo.jpg" medium="image"/>
</item>
</channel></rss>
"""
    respx.get(_SOURCE.url).mock(return_value=httpx.Response(200, text=xml))
    items = fetch_source(_SOURCE, timeout=5.0)
    assert items[0].image_url == "https://example.com/photo.jpg"


# --- Requêtes HTTP (respx, aucun réseau réel). ---

_SOURCE = Source(name="Test", url="https://example.com/feed", active=True)


@respx.mock
def test_fetch_source_envoie_un_user_agent_de_navigateur():
    route = respx.get(_SOURCE.url).mock(return_value=httpx.Response(200, text=_VALID_RSS))
    items = fetch_source(_SOURCE, timeout=5.0)

    assert len(items) == 1
    assert items[0].title == "Un article de test"
    sent_headers = route.calls[0].request.headers
    assert "python-httpx" not in sent_headers["user-agent"].lower()
    assert "chrome" in sent_headers["user-agent"].lower()


@respx.mock
def test_fetch_source_leve_empty_feed_error_sur_reponse_non_rss():
    """Un 200 OK contenant une page de challenge/erreur (pas du XML) ne doit jamais être
    traité comme un succès silencieux — voir la discussion sur le 403 Yonhap/allkpop."""
    respx.get(_SOURCE.url).mock(
        return_value=httpx.Response(200, text="<html><body>Access denied</body></html>")
    )
    with pytest.raises(EmptyFeedError):
        fetch_source(_SOURCE, timeout=5.0)


@respx.mock
def test_fetch_all_ignore_une_source_en_echec_et_continue():
    ok_source = Source(name="OK", url="https://example.com/ok-feed", active=True)
    broken_source = Source(name="Cassé", url="https://example.com/broken-feed", active=True)
    inactive_source = Source(name="Inactif", url="https://example.com/inactive", active=False)

    respx.get(ok_source.url).mock(return_value=httpx.Response(200, text=_VALID_RSS))
    respx.get(broken_source.url).mock(return_value=httpx.Response(403))

    items = fetch_all([ok_source, broken_source, inactive_source], timeout=5.0)

    assert len(items) == 1
    assert items[0].source == "OK"
