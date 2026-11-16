"""Command-line pipeline: fetch -> validate -> store -> render.

Any failure exits non-zero before the site is written, so CI never publishes a broken or
empty dashboard.
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

from quakewatch import dashboard, store
from quakewatch.errors import QuakeWatchError, UnsafePathError
from quakewatch.sources import WINDOWS, fetch_usgs, sample_feed
from quakewatch.transform import QualityReport, transform

log = logging.getLogger("quakewatch")

SOURCES = {"usgs": fetch_usgs, "sample": sample_feed}


def run(source: str, window: str, db_path: Path, out_dir: Path) -> QualityReport:
    """Run the pipeline once and log the attempt in the runs table.

    Raises QuakeWatchError (after logging the failed run) if anything goes wrong.
    """
    if db_path.resolve().is_relative_to(out_dir.resolve()):
        raise UnsafePathError(f"--db {db_path} is inside --out {out_dir}: it would be published")
    started_at = datetime.now(UTC)
    report = QualityReport()
    conn = store.connect(db_path)
    try:
        events, report = transform(SOURCES[source](window)["features"])
        log.info("quality report: %s", report.to_dict())
        report.check()
        store.upsert(conn, events)
        dashboard.render(store.load_events(conn), report, out_dir)
    except Exception as exc:
        store.record_run(conn, started_at, source, window, report, error=exc)
        raise
    else:
        store.record_run(conn, started_at, source, window, report)
    finally:
        conn.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m quakewatch",
        description="Fetch USGS earthquakes, validate them, store them in SQLite "
        "and render a static dashboard.",
    )
    parser.add_argument("--source", choices=sorted(SOURCES), default="usgs")
    parser.add_argument("--window", choices=WINDOWS, default="day", help="USGS feed window")
    parser.add_argument("--db", type=Path, default=Path("data/quakes.db"), help="SQLite file")
    parser.add_argument("--out", type=Path, default=Path("site"), help="site output directory")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        report = run(args.source, args.window, args.db, args.out)
    except QuakeWatchError as exc:
        log.error("run failed: %s", exc)
        return 1
    log.info("stored %d valid events; site written to %s", report.accepted, args.out)
    return 0
