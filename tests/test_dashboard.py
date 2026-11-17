from __future__ import annotations

import re
from datetime import UTC, datetime

import pandas as pd
import pytest

from quakewatch import dashboard
from quakewatch.transform import COLUMNS, QualityReport, transform

GENERATED_AT = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
HOSTILE_STRINGS = ("<script>alert(1)</script>", '"><img src=x onerror=alert(1)>')


def read(out_dir, name: str) -> str:
    return (out_dir / name).read_text(encoding="utf-8")


def meta_content(page: str, attr: str, value: str) -> str:
    match = re.search(rf'<meta {attr}="{value}" content="([^"]*)">', page)
    assert match, f"missing <meta {attr}={value!r}>"
    return match.group(1)


@pytest.fixture
def empty_site(tmp_path):
    dashboard.render(pd.DataFrame(columns=COLUMNS), None, tmp_path, generated_at=GENERATED_AT)
    return tmp_path


def test_empty_dashboard_renders(empty_site, assert_inert_page):
    page = read(empty_site, "index.html")
    assert {p.name for p in empty_site.iterdir()} == dashboard.SITE_FILES
    assert "No earthquakes recorded yet" in page
    assert 'id="chart-' not in page
    assert read(empty_site, "figures.js") == "window.QUAKEWATCH_FIGURES = {};\n"
    assert_inert_page(page)


def test_page_has_strict_csp_and_no_referrer(empty_site):
    page = read(empty_site, "index.html")
    assert meta_content(page, "http-equiv", "Content-Security-Policy") == dashboard.CSP
    assert meta_content(page, "name", "referrer") == "no-referrer"
    assert page.index("Content-Security-Policy") < page.index("<script")


def test_page_makes_no_third_party_requests(empty_site):
    page = read(empty_site, "index.html")
    # The only absolute URL is the USGS attribution link, which is never fetched.
    assert re.findall(r"(?:src|href)=\"(https?:[^\"]+)\"", page) == [
        "https://earthquake.usgs.gov/earthquakes/feed/"
    ]
    assert "<link" not in page
    assert "fonts.googleapis" not in page


def test_plotly_is_bundled_locally(empty_site):
    assert read(empty_site, "plotly.min.js").startswith("/**")
    assert "Plotly" in read(empty_site, "plotly.min.js")[:2000]


def test_geo_assets_preload_world_topojson(empty_site):
    js = read(empty_site, "geo-assets.js")
    assert js.startswith("window.PlotlyGeoAssets = window.PlotlyGeoAssets || ")
    assert "window.PlotlyGeoAssets.topojson.world_110m = {" in js
    assert "<" not in js


def test_tampered_topojson_is_refused(monkeypatch):
    monkeypatch.setattr(dashboard, "WORLD_TOPOJSON_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="SHA-256"):
        dashboard._geo_assets_js()


def test_hostile_strings_are_escaped_everywhere(tmp_path, make_feature, assert_inert_page):
    features = [
        make_feature(f"id{i}", mag=6.5 - i, place=f"10 km N of {text}, {text}")
        for i, text in enumerate(HOSTILE_STRINGS)
    ]
    events, report = transform(features)
    report.reasons["<b>not-a-real-reason</b>"] = 1  # even our own labels are escaped
    dashboard.render(events, report, tmp_path, generated_at=GENERATED_AT)

    page = read(tmp_path, "index.html")
    assert_inert_page(page)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page  # header tiles (place and region)
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "&lt;b&gt;not-a-real-reason&lt;/b&gt;" in page

    figures = read(tmp_path, "figures.js")
    assert "<script" not in figures.lower()
    assert "<img" not in figures.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in figures  # Plotly hover text


def test_chart_titles_fit_a_phone_screen(make_feature):
    # At 15px a title takes about 7px per character, and a phone-width chart is about 340px.
    events, _ = transform([make_feature()])
    figures = (dashboard._daily_figure, dashboard._magnitude_figure, dashboard._map_figure)
    assert all(len(make(events).layout.title.text) <= 40 for make in figures)


def test_escape_text_is_the_chart_choke_point():
    assert dashboard.escape_text(['<a href="x">', "R&D", 4.5]) == [
        '&lt;a href="x"&gt;',
        "R&amp;D",
        "4.5",
    ]


def test_header_stats_use_recent_window(tmp_path, make_feature):
    day = 86_400_000
    old = make_feature("old", mag=9.0, place="Old, Chile", time=1_790_000_000_000)
    new = make_feature("new", mag=3.0, place="New, Japan", time=1_790_000_000_000 + 60 * day)
    events, _ = transform([old, new])
    dashboard.render(events, QualityReport(total=2, accepted=2), tmp_path)
    page = read(tmp_path, "index.html")
    assert "M3.0" in page  # the M9.0 event is outside the 30-day window
    assert ">2</span>" in page  # but it still counts towards events stored
