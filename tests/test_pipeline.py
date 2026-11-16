"""End-to-end runs of the CLI on offline sources."""

from __future__ import annotations

import sqlite3

import pytest

from quakewatch import dashboard, pipeline
from quakewatch.errors import FetchError

HOSTILE_ID = "x'); DROP TABLE quakes;--"


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "data" / "quakes.db", tmp_path / "site"


def cli(db, out, source="sample", window="day") -> int:
    return pipeline.main(
        ["--source", source, "--window", window, "--db", str(db), "--out", str(out)]
    )


def query(db, sql: str, *params) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchall()


def test_sample_run_end_to_end(paths):
    db, out = paths
    assert cli(db, out) == 0

    assert {p.name for p in out.iterdir()} == dashboard.SITE_FILES  # no database, no logs
    assert query(db, "SELECT COUNT(*) FROM quakes") == [(35,)]
    assert query(db, "SELECT status, total, accepted, rejected FROM runs") == [
        ("success", 39, 35, 4)
    ]
    assert query(db, "SELECT id FROM quakes WHERE id = ?", HOSTILE_ID) == [(HOSTILE_ID,)]


def test_hostile_feed_strings_render_inert(paths, assert_inert_page):
    """The XSS regression test: hostile `place` text from the feed, through the full pipeline."""
    db, out = paths
    assert cli(db, out) == 0
    page = (out / "index.html").read_text(encoding="utf-8")
    figures = (out / "figures.js").read_text(encoding="utf-8")

    assert_inert_page(page)
    assert "<script>alert" not in page
    assert "<img" not in page
    # The M7.2 hostile event is the largest, so it lands in a header tile, escaped.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    for needle in ("<script", "<img", "onerror=alert(1)>"):
        assert needle not in figures.lower()
    assert '\\"&gt;&lt;img src=x onerror=alert(1)&gt;' in figures  # JSON-escaped quote
    # Control and bidi characters were stripped before storage.
    assert query(db, "SELECT place FROM quakes WHERE id = 'qw0033'") == [
        ("12 km N of Test [31m Town , CA",)
    ]


def test_rerun_is_idempotent(paths):
    db, out = paths
    assert cli(db, out) == 0
    assert cli(db, out) == 0
    assert query(db, "SELECT COUNT(*) FROM quakes") == [(35,)]
    assert query(db, "SELECT COUNT(*) FROM runs WHERE status = 'success'") == [(2,)]


def test_too_many_rejections_fail_without_publishing(paths, monkeypatch, make_feature):
    db, out = paths
    features = [make_feature(f"g{i}") for i in range(3)] + [
        make_feature(f"b{i}", mag=99) for i in range(2)
    ]
    monkeypatch.setitem(
        pipeline.SOURCES,
        "sample",
        lambda window: {"type": "FeatureCollection", "features": features},
    )
    assert cli(db, out) == 1
    assert not out.exists()
    assert query(db, "SELECT COUNT(*) FROM quakes") == [(0,)]
    status, error = query(db, "SELECT status, error FROM runs")[0]
    assert status == "failed"
    assert error.startswith("DataQualityError: 2/5 rows rejected (40%)")


def test_empty_feed_fails(paths, monkeypatch):
    db, out = paths
    monkeypatch.setitem(
        pipeline.SOURCES, "sample", lambda window: {"type": "FeatureCollection", "features": []}
    )
    assert cli(db, out) == 1
    assert not out.exists()


def test_fetch_failure_fails_without_publishing(paths, monkeypatch):
    db, out = paths

    def broken(window):
        raise FetchError("giving up after 3 attempts")

    monkeypatch.setitem(pipeline.SOURCES, "usgs", broken)
    assert cli(db, out, source="usgs") == 1
    assert not out.exists()
    assert query(db, "SELECT status FROM runs") == [("failed",)]


def test_database_inside_output_dir_is_refused(tmp_path):
    out = tmp_path / "site"
    assert cli(out / "quakes.db", out) == 1
    assert not out.exists()


def test_unknown_source_is_a_usage_error(paths):
    db, out = paths
    with pytest.raises(SystemExit) as excinfo:
        cli(db, out, source="evil")
    assert excinfo.value.code == 2
