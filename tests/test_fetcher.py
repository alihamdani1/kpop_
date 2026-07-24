from __future__ import annotations

from pathlib import Path

import pytest

from kpop_bot.fetcher import canonical_url, compute_fingerprint, load_sources


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
