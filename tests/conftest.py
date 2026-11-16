from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

import pytest

HOSTILE_STRINGS = ("<script>alert(1)</script>", '"><img src=x onerror=alert(1)>')


@pytest.fixture
def make_feature():
    """Factory for one valid USGS-style feature; override any field with keyword arguments."""

    def _make(
        eq_id: Any = "us1",
        mag: Any = 4.2,
        place: Any = "10 km N of Somewhere, CA",
        lat: Any = 35.0,
        lon: Any = -118.0,
        depth: Any = 7.5,
        time: Any = 1_790_000_000_000,
        updated: Any = None,
    ) -> dict[str, Any]:
        return {
            "type": "Feature",
            "id": eq_id,
            "properties": {"mag": mag, "place": place, "time": time, "updated": updated or time},
            "geometry": {"type": "Point", "coordinates": [lon, lat, depth]},
        }

    return _make


class _ActiveContentFinder(HTMLParser):
    """Collects everything in a page that could run script."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.inline_script_text: list[str] = []
        self.handler_attrs: list[tuple[str, str]] = []
        self.tags: set[str] = set()
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        self.handler_attrs += [(tag, name) for name, _ in attrs if name.startswith("on")]
        if tag == "script":
            self.scripts.append(dict(attrs))
            self._in_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_script_text.append(data)


@pytest.fixture
def assert_inert_page():
    """Assert a generated page has only our local external scripts and no event handlers."""

    def _check(page: str) -> None:
        finder = _ActiveContentFinder()
        finder.feed(page)
        sources = [script.get("src") for script in finder.scripts]
        assert sources == ["plotly.min.js", "geo-assets.js", "figures.js", "app.js"]
        assert finder.inline_script_text == []
        assert finder.handler_attrs == []
        assert not finder.tags & {"img", "iframe", "object", "embed", "form", "base"}

    return _check
