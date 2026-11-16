from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from quakewatch import store
from quakewatch.errors import FetchError
from quakewatch.transform import QualityReport, transform

HOSTILE_ID = "x'); DROP TABLE quakes;--"


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "data" / "quakes.db")
    yield connection
    connection.close()


def events_from(*features):
    events, _ = transform(features)
    return events


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM quakes").fetchone()[0]


def tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_upsert_is_idempotent(conn, make_feature):
    events = events_from(make_feature("a"), make_feature("b"))
    store.upsert(conn, events)
    store.upsert(conn, events)
    assert count(conn) == 2


def test_revised_magnitude_updates_row(conn, make_feature):
    store.upsert(conn, events_from(make_feature("a", mag=4.0, updated=1_790_000_000_000)))
    store.upsert(conn, events_from(make_feature("a", mag=4.3, updated=1_790_000_100_000)))
    assert conn.execute("SELECT mag FROM quakes WHERE id = ?", ("a",)).fetchall() == [(4.3,)]


def test_stale_copy_does_not_overwrite_newer_revision(conn, make_feature):
    store.upsert(conn, events_from(make_feature("a", mag=4.3, updated=1_790_000_100_000)))
    store.upsert(conn, events_from(make_feature("a", mag=4.0, updated=1_790_000_000_000)))
    assert conn.execute("SELECT mag FROM quakes WHERE id = ?", ("a",)).fetchone() == (4.3,)


def test_sql_injection_in_id_is_stored_as_plain_text(conn, make_feature):
    store.upsert(conn, events_from(make_feature("safe1")))
    store.upsert(conn, events_from(make_feature(HOSTILE_ID), make_feature("safe2")))
    assert {"quakes", "runs"} <= tables(conn)
    assert count(conn) == 3
    stored = conn.execute("SELECT id FROM quakes WHERE id = ?", (HOSTILE_ID,)).fetchone()
    assert stored == (HOSTILE_ID,)


def test_time_index_exists(conn):
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(quakes)")}
    assert "idx_quakes_time" in indexes


def test_untrusted_schema_is_disabled(conn):
    assert conn.execute("PRAGMA trusted_schema").fetchone() == (0,)


def test_missing_depth_is_stored_as_null(conn, make_feature):
    feature = make_feature("a")
    feature["geometry"]["coordinates"] = [-118.0, 35.0]
    store.upsert(conn, events_from(feature))
    assert conn.execute("SELECT depth_km FROM quakes").fetchone() == (None,)


def test_load_events_round_trip(conn, make_feature):
    store.upsert(conn, events_from(make_feature("b", time=2_000), make_feature("a", time=1_000)))
    loaded = store.load_events(conn)
    assert loaded["id"].tolist() == ["a", "b"]  # ordered by time
    assert list(loaded.columns)[:3] == ["id", "time_ms", "updated_ms"]


def test_record_run_logs_success_and_failure(conn):
    report = QualityReport(total=10, accepted=9)
    report.reasons["missing_field"] = 1
    started = datetime(2026, 11, 15, tzinfo=UTC)
    store.record_run(conn, started, "sample", "day", report)
    store.record_run(conn, started, "usgs", "week", QualityReport(), error=FetchError("timeout"))
    rows = conn.execute(
        "SELECT source, feed_window, status, total, accepted, rejected, reasons, error FROM runs"
    ).fetchall()
    assert rows[0][:6] == ("sample", "day", "success", 10, 9, 1)
    assert json.loads(rows[0][6]) == {"missing_field": 1}
    assert rows[1][2] == "failed"
    assert rows[1][7] == "FetchError: timeout"
