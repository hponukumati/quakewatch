from __future__ import annotations

import pytest

from quakewatch.errors import DataQualityError
from quakewatch.sources import sample_feed
from quakewatch.transform import (
    MAX_ID_LEN,
    MAX_PLACE_LEN,
    QualityReport,
    clean_text,
    derive_region,
    magnitude_class,
    transform,
)


def reasons_for(features):
    _, report = transform(features)
    return dict(report.reasons)


def test_sample_feed_quality_report():
    events, report = transform(sample_feed()["features"])
    assert (report.total, report.accepted, report.rejected) == (39, 35, 4)
    assert dict(report.reasons) == {
        "missing_field": 1,
        "magnitude_out_of_range": 1,
        "latitude_out_of_range": 1,
        "duplicate_id": 1,
    }
    assert len(events) == 35
    assert events["id"].is_unique
    report.check()  # about 10% rejected: under the limit


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"mag": None}, "missing_field"),
        ({"place": None}, "missing_field"),
        ({"eq_id": None}, "missing_field"),
        ({"place": "\x00\x01‮"}, "missing_field"),
        ({"mag": "4.5"}, "invalid_type"),
        ({"mag": True}, "invalid_type"),
        ({"mag": float("nan")}, "invalid_type"),
        ({"lat": float("inf")}, "invalid_type"),
        ({"eq_id": 12345}, "invalid_type"),
        ({"place": ["list"]}, "invalid_type"),
        ({"mag": 10.01}, "magnitude_out_of_range"),
        ({"mag": -2.5}, "magnitude_out_of_range"),
        ({"lat": 90.5}, "latitude_out_of_range"),
        ({"lon": -180.5}, "longitude_out_of_range"),
        ({"time": -1}, "time_out_of_range"),
        ({"time": 10**20}, "time_out_of_range"),
    ],
)
def test_each_drop_reason(make_feature, overrides, reason):
    assert reasons_for([make_feature(**overrides)]) == {reason: 1}


@pytest.mark.parametrize(
    "feature",
    [
        "not a dict",
        None,
        {"id": "a", "properties": [], "geometry": {"coordinates": [0, 0]}},
        {"id": "a", "properties": {"mag": 1}, "geometry": None},
    ],
)
def test_malformed_features(feature):
    assert reasons_for([feature]) == {"malformed_feature": 1}


def test_missing_coordinates(make_feature):
    feature = make_feature()
    feature["geometry"]["coordinates"] = [10.0]
    assert reasons_for([feature]) == {"missing_field": 1}


def test_magnitude_bounds_are_inclusive(make_feature):
    events, report = transform([make_feature("a", mag=-2.0), make_feature("b", mag=10.0)])
    assert report.accepted == 2


def test_duplicate_ids_keep_the_first(make_feature):
    events, report = transform([make_feature("a", mag=3.0), make_feature("a", mag=5.0)])
    assert dict(report.reasons) == {"duplicate_id": 1}
    assert events["mag"].tolist() == [3.0]


def test_strings_are_cleaned_and_capped(make_feature):
    feature = make_feature(eq_id="x" * 500, place="A\x00B\x1b[31m‮C  D" + "z" * 500)
    events, _ = transform([feature])
    row = events.iloc[0]
    assert len(row["id"]) == MAX_ID_LEN
    assert len(row["place"]) == MAX_PLACE_LEN
    assert row["place"].startswith("A B [31m C D")
    assert not any(ord(ch) < 32 or 0x202A <= ord(ch) <= 0x202E for ch in row["place"])


def test_clean_text_keeps_unicode_letters():
    assert clean_text("3 km E of Pāhala,\tHawaii\n", 200) == "3 km E of Pāhala, Hawaii"


def test_missing_depth_and_updated_are_tolerated(make_feature):
    feature = make_feature(time=1_790_000_000_000)
    feature["geometry"]["coordinates"] = [-118.0, 35.0]
    feature["properties"]["updated"] = "garbage"
    events, _ = transform([feature])
    assert events.iloc[0]["updated_ms"] == 1_790_000_000_000
    assert events.iloc[0]["depth_km"] is None


@pytest.mark.parametrize(
    ("place", "region"),
    [
        ("12 km NE of Ridgecrest, CA", "CA"),
        ("near the east coast of Honshu, Japan", "Japan"),
        ("8 km W of Volcano", "Volcano"),
        ("Fiji region", "Fiji region"),
        ("10 km N of ,", "Unknown"),
    ],
)
def test_derive_region(place, region):
    assert derive_region(place) == region


@pytest.mark.parametrize(
    ("mag", "label"),
    [(-1.0, "micro"), (2.99, "micro"), (3.0, "minor"), (4.5, "light"), (5.0, "moderate"),
     (6.9, "strong"), (7.0, "major"), (8.0, "great"), (10.0, "great")],
)  # fmt: skip
def test_magnitude_class(mag, label):
    assert magnitude_class(mag) == label


def test_derived_columns_present(make_feature):
    events, _ = transform([make_feature(mag=6.1, place="5 km S of Town, Chile")])
    assert events.iloc[0][["region", "mag_class"]].tolist() == ["Chile", "strong"]


# --- Rejection threshold ------------------------------------------------------------------


def batch(make_feature, good: int, bad: int):
    return [make_feature(f"g{i}") for i in range(good)] + [
        make_feature(f"b{i}", mag=None) for i in range(bad)
    ]


def test_exactly_20_percent_rejected_passes(make_feature):
    _, report = transform(batch(make_feature, good=8, bad=2))
    assert report.reject_ratio == pytest.approx(0.2)
    report.check()


def test_over_20_percent_rejected_fails(make_feature):
    _, report = transform(batch(make_feature, good=7, bad=3))
    with pytest.raises(DataQualityError, match="30%"):
        report.check()


def test_all_rejected_fails(make_feature):
    _, report = transform(batch(make_feature, good=0, bad=5))
    with pytest.raises(DataQualityError, match="no valid events"):
        report.check()


def test_empty_feed_fails():
    events, report = transform([])
    assert events.empty
    with pytest.raises(DataQualityError, match="no valid events"):
        report.check()


def test_report_to_dict_is_sorted():
    report = QualityReport(total=3, accepted=1)
    report.reasons.update({"b": 1, "a": 1})
    expected = {"total": 3, "accepted": 1, "rejected": 2, "reasons": {"a": 1, "b": 1}}
    assert report.to_dict() == expected
