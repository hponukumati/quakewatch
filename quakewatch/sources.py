"""Feed sources: the live USGS feed and a deterministic offline sample.

All upstream data is untrusted. The live fetch only talks to an HTTPS allowlist (including
on redirects), uses timeouts and bounded retries, aborts above a size limit, and checks the
top-level JSON shape before anything else sees it. Response bodies are never logged.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from quakewatch import __version__
from quakewatch.errors import DisallowedURLError, FeedFormatError, FetchError, ResponseTooLargeError

log = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset({"earthquake.usgs.gov"})
FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_{window}.geojson"
WINDOWS = ("day", "week", "month")
USER_AGENT = f"QuakeWatch/{__version__} (+https://github.com/hponukumati/quakewatch)"
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
TIMEOUT = httpx.Timeout(10.0, read=30.0)
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class _RetryableStatusError(FetchError):
    """A transient HTTP status that is worth another attempt."""


def check_url(url: str) -> None:
    """Raise DisallowedURLError unless url is https:// on an allowlisted host and port 443."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:  # non-numeric or out-of-range port
        port = -1
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
    ):
        raise DisallowedURLError(f"refusing to fetch non-allowlisted URL {url!r}")


def _check_request(request: httpx.Request) -> None:
    # httpx runs request hooks for every request it sends, including each redirect hop,
    # so a redirect off the allowlist is refused before it is followed.
    check_url(str(request.url))


def fetch_usgs(
    window: str = "day",
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch the USGS 'all earthquakes' summary feed for window (day, week or month)."""
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}, got {window!r}")
    return fetch_feed(FEED_URL.format(window=window), transport=transport, sleep=sleep)


def fetch_feed(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch url with retries and exponential backoff, then parse and shape-check it."""
    check_url(url)
    client = httpx.Client(
        transport=transport,
        timeout=TIMEOUT,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"},
        event_hooks={"request": [_check_request]},
    )
    with client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return parse_feed(_download(client, url))
            except (httpx.TransportError, _RetryableStatusError) as exc:
                if attempt == MAX_ATTEMPTS:
                    raise FetchError(f"giving up after {attempt} attempts: {exc!r}") from exc
                delay = 2 ** (attempt - 1)
                log.warning("fetch attempt %d failed (%r); retrying in %ds", attempt, exc, delay)
                sleep(delay)
    raise AssertionError("unreachable")  # the loop always returns or raises


def _download(client: httpx.Client, url: str) -> bytes:
    """GET url as a stream and abort as soon as the body passes MAX_RESPONSE_BYTES."""
    with client.stream("GET", url) as response:
        if response.status_code in RETRY_STATUSES:
            raise _RetryableStatusError(f"HTTP {response.status_code}")
        if response.status_code != httpx.codes.OK:
            raise FetchError(f"unexpected HTTP status {response.status_code}")
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise ResponseTooLargeError(f"declared size {declared} bytes is over the limit")
        body = bytearray()
        # iter_bytes() yields decompressed data, so a gzip bomb hits the same limit.
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError(f"response passed {MAX_RESPONSE_BYTES} bytes")
    log.info("fetched %d bytes from %s", len(body), url)
    return bytes(body)


def _reject_constant(name: str) -> None:
    raise FeedFormatError(f"feed contains the non-standard JSON constant {name}")


def parse_feed(body: bytes) -> dict[str, Any]:
    """Decode JSON (plain json.loads: no eval, no pickle) and require a FeatureCollection."""
    try:
        payload = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:  # bad JSON, bad UTF-8, or absurd nesting
        raise FeedFormatError("feed is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise FeedFormatError("feed is not a GeoJSON FeatureCollection")
    if not isinstance(payload.get("features"), list):
        raise FeedFormatError("feed has no 'features' list")
    return payload


# --- Offline sample -------------------------------------------------------------------------

SAMPLE_START = datetime(2026, 10, 15, tzinfo=UTC)

# (id, place, magnitude, latitude, longitude, depth_km): realistic, valid events.
_SAMPLE_EVENTS = [
    ("qw0001", "10 km NE of Ridgecrest, CA", 2.1, 35.70, -117.55, 8.2),
    ("qw0002", "5 km SSW of Anza, CA", 1.3, 33.51, -116.69, 12.4),
    ("qw0003", "6 km NNW of The Geysers, CA", 0.9, 38.83, -122.79, 2.1),
    ("qw0004", "60 km W of Ferndale, CA", 3.4, 40.55, -124.95, 18.0),
    ("qw0005", "45 km SW of Adak, Alaska", 3.9, 51.55, -177.11, 40.3),
    ("qw0006", "120 km S of Sand Point, Alaska", 4.6, 54.25, -160.40, 30.1),
    ("qw0007", "18 km NW of Talkeetna, Alaska", 1.8, 62.44, -150.35, 60.0),
    ("qw0008", "8 km W of Volcano, Hawaii", 2.6, 19.43, -155.31, 1.2),
    ("qw0009", "3 km E of Pāhala, Hawaii", 2.2, 19.20, -155.45, 31.5),
    ("qw0010", "28 km SE of Mina, Nevada", 1.6, 38.20, -117.90, 6.8),
    ("qw0011", "6 km W of Perry, Oklahoma", 2.3, 36.29, -97.36, 5.0),
    ("qw0012", "9 km S of Indios, Puerto Rico", 3.1, 17.91, -66.82, 11.0),
    ("qw0013", "Fiji region", 4.9, -17.80, -178.40, 560.0),
    ("qw0014", "south of the Kermadec Islands", 5.1, -32.10, -178.00, 33.0),
    ("qw0015", "135 km NE of Neiafu, Tonga", 4.7, -17.70, -173.10, 70.0),
    ("qw0016", "40 km S of Kokopo, Papua New Guinea", 5.3, -4.70, 152.20, 45.0),
    ("qw0017", "Kepulauan Talaud, Indonesia", 4.5, 4.10, 126.80, 50.0),
    ("qw0018", "70 km N of Tobelo, Indonesia", 4.8, 2.35, 128.05, 120.0),
    ("qw0019", "15 km E of Hualien City, Taiwan", 4.4, 23.98, 121.75, 20.0),
    ("qw0020", "30 km NNE of Iwaki, Japan", 4.2, 37.30, 141.05, 50.0),
    ("qw0021", "near the east coast of Honshu, Japan", 5.0, 38.90, 142.60, 35.0),
    ("qw0022", "12 km NW of Ovalle, Chile", 3.8, -30.52, -71.30, 60.0),
    ("qw0023", "90 km WSW of Iquique, Chile", 4.3, -20.60, -70.90, 25.0),
    ("qw0024", "Guerrero, Mexico", 4.1, 17.20, -100.10, 40.0),
    ("qw0025", "Crete, Greece", 3.7, 35.20, 25.50, 15.0),
    ("qw0026", "18 km N of Malatya, Turkey", 4.0, 38.51, 38.32, 10.0),
    ("qw0027", "Reykjanes Ridge", 3.6, 58.90, -32.00, 10.0),
    ("qw0028", "Central Mid-Atlantic Ridge", 4.6, 0.90, -29.50, 10.0),
    ("qw0029", "25 km SE of Pahala, Hawaii", -0.4, 19.05, -155.30, 3.0),
    ("qw0030", "Northern Sumatra, Indonesia", 6.4, 3.10, 95.90, 30.0),
]

# Valid rows with hostile text: they must be stored and rendered as inert text.
_HOSTILE_EVENTS = [
    ("qw0031", "<script>alert(1)</script>", 7.2, -20.50, -175.40, 35.0),
    ("qw0032", '"><img src=x onerror=alert(1)>', 5.6, 38.30, 142.40, 25.0),
    ("qw0033", "12 km N of Test\x00\x1b[31m Town" + chr(0x202E) + ", CA", 3.2, 36.0, -120.0, 5.0),
    ("qw0034", "A" * 500 + ", Overflow", 1.5, 10.00, 10.00, 10.0),
    ("x'); DROP TABLE quakes;--", "8 km S of Injection, Nevada", 2.4, 38.50, -118.00, 7.0),
]


def _feature(
    eq_id: str, place: str, mag: float | None, lat: float, lon: float, depth: float, hours: int
) -> dict[str, Any]:
    millis = int((SAMPLE_START + timedelta(hours=hours)).timestamp() * 1000)
    return {
        "type": "Feature",
        "id": eq_id,
        "properties": {"mag": mag, "place": place, "time": millis, "updated": millis},
        "geometry": {"type": "Point", "coordinates": [lon, lat, depth]},
    }


def sample_feed(window: str = "month") -> dict[str, Any]:
    """A deterministic offline feed with clean, hostile and dirty rows. window is ignored."""
    valid = _SAMPLE_EVENTS + _HOSTILE_EVENTS
    features = [_feature(*row, hours=3 + 19 * i) for i, row in enumerate(valid)]
    features += [
        _feature("dirty01", "5 km E of Nowhere, CA", None, 35.0, -118.0, 3.0, hours=200),
        _feature("dirty02", "Impossible, CA", 12.5, 35.0, -118.0, 3.0, hours=210),
        _feature("dirty03", "Off the map", 3.0, 95.0, 0.0, 10.0, hours=220),
        _feature(*_SAMPLE_EVENTS[0], hours=3),  # duplicate id
    ]
    return {"type": "FeatureCollection", "metadata": {"title": "sample"}, "features": features}
