from __future__ import annotations

import json

import httpx
import pytest

from quakewatch import sources
from quakewatch.errors import DisallowedURLError, FeedFormatError, FetchError, ResponseTooLargeError

GOOD_URL = sources.FEED_URL.format(window="day")
FEED = {"type": "FeatureCollection", "features": []}


def no_sleep(_seconds: float) -> None:
    pass


@pytest.mark.parametrize(
    "url",
    [
        "http://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
        "https://evil.example.com/all_day.geojson",
        "https://earthquake.usgs.gov.evil.example/all_day.geojson",
        "https://earthquake.usgs.gov@evil.example/all_day.geojson",
        "https://user:pw@earthquake.usgs.gov/all_day.geojson",
        "https://earthquake.usgs.gov:8443/all_day.geojson",
        "https://earthquake.usgs.gov:notaport/all_day.geojson",
        "ftp://earthquake.usgs.gov/all_day.geojson",
        "file:///etc/passwd",
        "//earthquake.usgs.gov/all_day.geojson",
    ],
)
def test_allowlist_rejects(url):
    with pytest.raises(DisallowedURLError):
        sources.check_url(url)


def test_allowlist_accepts_usgs_feed():
    sources.check_url(GOOD_URL)
    sources.check_url("https://earthquake.usgs.gov:443/earthquakes/feed/")


@pytest.mark.parametrize(
    "url", ["https://evil.example.com/feed.json", "http://earthquake.usgs.gov/x"]
)
def test_fetch_refuses_non_allowlisted_url_before_any_request(url):
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    with pytest.raises(DisallowedURLError):
        sources.fetch_feed(url, transport=transport)
    assert calls == []


@pytest.mark.parametrize(
    "location", ["https://evil.example.com/feed.json", "http://earthquake.usgs.gov/feed.json"]
)
def test_redirect_off_allowlist_is_not_followed(location):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": location})

    with pytest.raises(DisallowedURLError):
        sources.fetch_feed(GOOD_URL, transport=httpx.MockTransport(handler), sleep=no_sleep)
    assert requested == [GOOD_URL]


def test_redirect_within_allowlist_is_followed():
    moved = "https://earthquake.usgs.gov/moved.geojson"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOD_URL:
            return httpx.Response(301, headers={"Location": moved})
        return httpx.Response(200, json=FEED)

    assert sources.fetch_feed(GOOD_URL, transport=httpx.MockTransport(handler)) == FEED


def test_streamed_response_over_limit_is_aborted(monkeypatch):
    monkeypatch.setattr(sources, "MAX_RESPONSE_BYTES", 1_000)
    sent = []

    def endless_body():
        while True:
            sent.append(100)
            yield b"x" * 100

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=endless_body()))
    with pytest.raises(ResponseTooLargeError):
        sources.fetch_feed(GOOD_URL, transport=transport, sleep=no_sleep)
    assert sum(sent) <= 1_100  # stopped reading right after the limit


def test_declared_content_length_over_limit_is_aborted(monkeypatch):
    monkeypatch.setattr(sources, "MAX_RESPONSE_BYTES", 1_000)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"Content-Length": "5000000"}, content=iter([b"{}"])
        )
    )
    with pytest.raises(ResponseTooLargeError, match="declared"):
        sources.fetch_feed(GOOD_URL, transport=transport, sleep=no_sleep)


def test_retries_transient_errors_with_backoff():
    statuses = iter([503, 429, 200])
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json=FEED if status == 200 else None)

    feed = sources.fetch_feed(GOOD_URL, transport=httpx.MockTransport(handler), sleep=delays.append)
    assert feed == FEED
    assert delays == [1, 2]


def test_gives_up_after_max_attempts():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("network down", request=request)

    with pytest.raises(FetchError, match="giving up after 3 attempts"):
        sources.fetch_feed(GOOD_URL, transport=httpx.MockTransport(handler), sleep=no_sleep)
    assert len(calls) == sources.MAX_ATTEMPTS


def test_client_errors_are_not_retried():
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(404))
    with pytest.raises(FetchError, match="404"):
        sources.fetch_feed(GOOD_URL, transport=transport, sleep=no_sleep)
    assert len(calls) == 1


def test_sends_descriptive_user_agent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["User-Agent"]
        return httpx.Response(200, json=FEED)

    sources.fetch_usgs("week", transport=httpx.MockTransport(handler))
    assert seen["ua"].startswith("QuakeWatch/")


def test_fetch_usgs_rejects_unknown_window():
    with pytest.raises(ValueError):
        sources.fetch_usgs("year")


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe\x00garbage",
        b'{"type": "FeatureCollection", "features": [NaN]}',
        b'{"type": "FeatureCollection", "features": [Infinity]}',
        b'["a list"]',
        b'{"type": "Feature", "features": []}',
        b'{"type": "FeatureCollection", "features": {"not": "a list"}}',
        b"[" * 100_000 + b"]" * 100_000,
    ],
)
def test_parse_feed_rejects_bad_shapes(body):
    with pytest.raises(FeedFormatError):
        sources.parse_feed(body)


def test_sample_feed_is_deterministic_and_contains_dirty_and_hostile_rows():
    feed = sources.sample_feed()
    assert feed == sources.sample_feed()
    places = [f["properties"]["place"] for f in feed["features"]]
    ids = [f["id"] for f in feed["features"]]
    assert "<script>alert(1)</script>" in places
    assert '"><img src=x onerror=alert(1)>' in places
    assert any(f["properties"]["mag"] is None for f in feed["features"])
    assert any(f["properties"]["mag"] > 10 for f in feed["features"] if f["properties"]["mag"])
    assert len(ids) != len(set(ids))
    json.dumps(feed)  # plain JSON data only
