"""SQLite persistence.

Every statement is a constant string with ? or :name placeholders; no SQL is built from data.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from quakewatch.transform import COLUMNS, QualityReport

SCHEMA = """
CREATE TABLE IF NOT EXISTS quakes (
    id          TEXT PRIMARY KEY,
    time_ms     INTEGER NOT NULL,
    updated_ms  INTEGER NOT NULL,
    mag         REAL NOT NULL,
    place       TEXT NOT NULL,
    region      TEXT NOT NULL,
    mag_class   TEXT NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    depth_km    REAL
);
CREATE INDEX IF NOT EXISTS idx_quakes_time ON quakes (time_ms);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    source      TEXT NOT NULL,
    feed_window TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    total       INTEGER NOT NULL,
    accepted    INTEGER NOT NULL,
    rejected    INTEGER NOT NULL,
    reasons     TEXT NOT NULL,  -- JSON object: drop reason -> count
    error       TEXT
);
"""

# Revisions (same id, newer `updated`) replace the stored row; stale copies are ignored.
UPSERT = """
INSERT INTO quakes (id, time_ms, updated_ms, mag, place, region, mag_class,
                    latitude, longitude, depth_km)
VALUES (:id, :time_ms, :updated_ms, :mag, :place, :region, :mag_class,
        :latitude, :longitude, :depth_km)
ON CONFLICT (id) DO UPDATE SET
    time_ms = excluded.time_ms, updated_ms = excluded.updated_ms, mag = excluded.mag,
    place = excluded.place, region = excluded.region, mag_class = excluded.mag_class,
    latitude = excluded.latitude, longitude = excluded.longitude, depth_km = excluded.depth_km
WHERE excluded.updated_ms >= quakes.updated_ms
"""

INSERT_RUN = """
INSERT INTO runs (started_at, finished_at, source, feed_window, status,
                  total, accepted, rejected, reasons, error)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SELECT_EVENTS = """
SELECT id, time_ms, updated_ms, mag, place, region, mag_class, latitude, longitude, depth_km
FROM quakes ORDER BY time_ms
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open or create the database.

    The file may come back from a CI cache, so it is treated as untrusted: with
    trusted_schema OFF, views and triggers inside it cannot call functions with side effects.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA trusted_schema = OFF")
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, events: pd.DataFrame) -> int:
    """Insert or update events in one transaction. Re-running with the same data is a no-op."""
    frame = events[COLUMNS]
    rows = frame.astype(object).where(frame.notna(), None).to_dict("records")
    with conn:
        conn.executemany(UPSERT, rows)
    return len(rows)


def record_run(
    conn: sqlite3.Connection,
    started_at: datetime,
    source: str,
    window: str,
    report: QualityReport,
    error: BaseException | None = None,
) -> None:
    """Append one row to the runs log."""
    message = None if error is None else f"{type(error).__name__}: {error}"[:500]
    with conn:
        conn.execute(
            INSERT_RUN,
            (
                started_at.isoformat(),
                datetime.now(UTC).isoformat(),
                source,
                window,
                "failed" if error else "success",
                report.total,
                report.accepted,
                report.rejected,
                json.dumps(report.to_dict()["reasons"]),
                message,
            ),
        )


def load_events(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(SELECT_EVENTS, conn)
