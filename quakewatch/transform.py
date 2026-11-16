"""Validate raw GeoJSON features into a clean DataFrame plus a quality report.

Strings are cleaned here (control characters stripped, length capped) but not HTML-escaped:
escaping belongs to the output format and happens in dashboard.py.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from quakewatch.errors import DataQualityError

MAG_MIN, MAG_MAX = -2.0, 10.0
MAX_REJECT_RATIO = 0.20
MAX_ID_LEN, MAX_PLACE_LEN = 64, 200
MAX_TIME_MS = 4_102_444_800_000  # 2100-01-01 UTC: later is corrupt (and overflows pandas)
COLUMNS = [
    "id", "time_ms", "updated_ms", "mag", "place", "region",
    "mag_class", "latitude", "longitude", "depth_km",
]  # fmt: skip
# (exclusive upper bound, label), the USGS / Michigan Tech magnitude classes.
MAG_CLASSES = (
    (3.0, "micro"), (4.0, "minor"), (5.0, "light"),
    (6.0, "moderate"), (7.0, "strong"), (8.0, "major"),
)  # fmt: skip

# C0/C1 control characters plus Unicode bidi marks, overrides and isolates (text spoofing).
# Listed as code points so this source file contains no bidi characters itself.
_UNSAFE_CODEPOINTS = (
    *range(0x00, 0x20), *range(0x7F, 0xA0), 0x200E, 0x200F,
    *range(0x202A, 0x202F), *range(0x2066, 0x206A),
)  # fmt: skip
_TO_SPACE = dict.fromkeys(_UNSAFE_CODEPOINTS, " ")
_DISTANCE_PREFIX = re.compile(r"^\d+(?:\.\d+)?\s*km\s+[NSEW]{1,3}\s+of\s+", re.IGNORECASE)


@dataclass
class QualityReport:
    total: int = 0
    accepted: int = 0
    reasons: Counter[str] = field(default_factory=Counter)

    @property
    def rejected(self) -> int:
        return self.total - self.accepted

    @property
    def reject_ratio(self) -> float:
        return self.rejected / self.total if self.total else 0.0

    def check(self, max_ratio: float = MAX_REJECT_RATIO) -> None:
        """Raise DataQualityError if nothing survived or too much was rejected."""
        if self.accepted == 0:
            raise DataQualityError(
                f"no valid events: {self.total} features, all rejected or the feed is empty"
            )
        if self.reject_ratio > max_ratio:
            raise DataQualityError(
                f"{self.rejected}/{self.total} rows rejected ({self.reject_ratio:.0%}), over the "
                f"{max_ratio:.0%} limit; the upstream format may have changed"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "reasons": dict(sorted(self.reasons.items())),
        }


def clean_text(value: str, max_len: int) -> str:
    """Strip control and bidi characters, collapse whitespace and cap the length."""
    return " ".join(value.translate(_TO_SPACE).split())[:max_len].rstrip()


def derive_region(place: str) -> str:
    """'12 km NE of Ridgecrest, CA' -> 'CA'; '8 km W of Volcano' -> 'Volcano'."""
    region = place.rsplit(",", 1)[1] if "," in place else _DISTANCE_PREFIX.sub("", place)
    return region.strip() or "Unknown"


def magnitude_class(mag: float) -> str:
    return next((label for upper, label in MAG_CLASSES if mag < upper), "great")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _parse(feature: Any) -> dict[str, Any] | str:
    """Return a clean row, or the reason this feature is dropped."""
    if not isinstance(feature, dict):
        return "malformed_feature"
    props, geometry = feature.get("properties"), feature.get("geometry")
    if not isinstance(props, dict) or not isinstance(geometry, dict):
        return "malformed_feature"
    coords = geometry.get("coordinates")
    coords = coords if isinstance(coords, list) else []
    raw = {
        "id": feature.get("id"),
        "time": props.get("time"),
        "mag": props.get("mag"),
        "place": props.get("place"),
        "longitude": coords[0] if len(coords) > 0 else None,
        "latitude": coords[1] if len(coords) > 1 else None,
    }
    if any(value is None for value in raw.values()):
        return "missing_field"
    if not (isinstance(raw["id"], str) and isinstance(raw["place"], str)) or not all(
        _is_number(raw[key]) for key in ("time", "mag", "longitude", "latitude")
    ):
        return "invalid_type"
    eq_id, place = clean_text(raw["id"], MAX_ID_LEN), clean_text(raw["place"], MAX_PLACE_LEN)
    if not eq_id or not place:
        return "missing_field"
    if not MAG_MIN <= raw["mag"] <= MAG_MAX:
        return "magnitude_out_of_range"
    if not -90 <= raw["latitude"] <= 90:
        return "latitude_out_of_range"
    if not -180 <= raw["longitude"] <= 180:
        return "longitude_out_of_range"
    if not 0 <= raw["time"] <= MAX_TIME_MS:
        return "time_out_of_range"
    updated = props.get("updated")
    if not (_is_number(updated) and 0 <= updated <= MAX_TIME_MS):
        updated = raw["time"]
    depth = coords[2] if len(coords) > 2 and _is_number(coords[2]) else None
    return {
        "id": eq_id,
        "time_ms": int(raw["time"]),
        "updated_ms": int(updated),
        "mag": float(raw["mag"]),
        "place": place,
        "region": derive_region(place),
        "mag_class": magnitude_class(raw["mag"]),
        "latitude": float(raw["latitude"]),
        "longitude": float(raw["longitude"]),
        "depth_km": None if depth is None else float(depth),
    }


def transform(features: Iterable[Any]) -> tuple[pd.DataFrame, QualityReport]:
    """Validate features. Duplicate ids keep the first valid occurrence."""
    report = QualityReport()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for feature in features:
        report.total += 1
        row = _parse(feature)
        if isinstance(row, dict) and row["id"] in seen:
            row = "duplicate_id"
        if isinstance(row, str):
            report.reasons[row] += 1
            continue
        seen.add(row["id"])
        rows.append(row)
    report.accepted = len(rows)
    return pd.DataFrame(rows, columns=COLUMNS), report
