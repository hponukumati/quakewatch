"""Render the static dashboard: index.html plus local JavaScript files.

Security model:
* Every value in the page goes through a Jinja2 template with autoescape=True.
* Feed strings bound for Plotly go through escape_text(), the one choke point for chart text,
  because Plotly parses hover text as markup.
* There is no inline <script>. Figure data lives in figures.js and render code in app.js, so the
  Content-Security-Policy can be script-src 'self'. Plotly and the map outlines are served from
  the same origin, so the page makes no third-party requests at runtime.
"""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from jinja2 import Environment, PackageLoader
from plotly.offline import get_plotlyjs
from plotly.utils import PlotlyJSONEncoder

from quakewatch.transform import QualityReport

# GitHub Pages cannot send headers, so the template carries this as a <meta> tag (tested).
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'none'; base-uri 'none'; form-action 'none'"
)
SITE_FILES = frozenset({"index.html", "plotly.min.js", "geo-assets.js", "figures.js", "app.js"})
RECENT_DAYS = 30
TIME_FORMAT = "%Y-%m-%d %H:%M UTC"
# SHA-256 of assets/world_110m.json, downloaded once from https://cdn.plot.ly/un/world_110m.json
WORLD_TOPOJSON_SHA256 = "e1bf51740ad28396265e52123ea7315d692f112664ab2cb0f1ea76a96fe1bb0a"

# Colors from a validated single-hue sequential ramp (light to dark = small to large).
SURFACE, TEXT, MUTED, BAR = "#fcfcfb", "#0b0b0b", "#52514e", "#2a78d6"
MAG_SCALE = [[0.0, "#86b6ef"], [0.35, "#3987e5"], [0.7, "#1c5cab"], [1.0, "#0d366b"]]

_env = Environment(loader=PackageLoader("quakewatch", "templates"), autoescape=True)
_PACKAGE = resources.files("quakewatch")


def escape_text(values: Iterable[Any]) -> list[str]:
    """HTML-escape values bound for Plotly. Every feed string in a chart passes through here.

    Plotly parses hover text as markup and decodes &amp; &lt; &gt; but shows &quot; literally.
    Chart text never lands in an attribute and no tag can form once < is escaped, so quotes
    are left as they are (quote=False). The page template escapes quotes too.
    """
    return [html.escape(str(value), quote=False) for value in values]


def render(
    events: pd.DataFrame,
    report: QualityReport | None,
    out_dir: Path,
    *,
    generated_at: datetime | None = None,
) -> list[Path]:
    """Write the site into out_dir and return the files written."""
    recent = _recent(events)
    figures = {} if events.empty else {
        "daily": _daily_figure(events),
        "magnitudes": _magnitude_figure(recent),
        "map": _map_figure(recent),
    }  # fmt: skip
    page = _env.get_template("index.html.j2").render(
        stats=_summary(events, recent),
        report=report,
        recent_days=RECENT_DAYS,
        generated_at=(generated_at or datetime.now(UTC)).strftime(TIME_FORMAT),
    )
    figures_json = json.dumps(figures, cls=PlotlyJSONEncoder)
    files = {
        "index.html": page,
        "plotly.min.js": get_plotlyjs(),
        "geo-assets.js": _geo_assets_js(),
        "figures.js": f"window.QUAKEWATCH_FIGURES = {figures_json};\n",
        "app.js": _PACKAGE.joinpath("static", "app.js").read_text("utf-8"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8")
    return [out_dir / name for name in files]


def _times(events: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(events["time_ms"], unit="ms", utc=True)


def _recent(events: pd.DataFrame) -> pd.DataFrame:
    """Events from the RECENT_DAYS before the newest one (relative, so output is reproducible)."""
    if events.empty:
        return events
    times = _times(events)
    return events[times >= times.max() - pd.Timedelta(days=RECENT_DAYS)]


def _summary(events: pd.DataFrame, recent: pd.DataFrame) -> dict[str, Any]:
    """Header numbers. Strings stay raw here: the template escapes them."""
    if events.empty:
        return {"total": 0}
    times = _times(events)
    largest = recent.loc[recent["mag"].idxmax()]
    regions = recent.groupby("region").size().sort_values(ascending=False, kind="stable")
    return {
        "total": len(events),
        "first": times.min().strftime("%Y-%m-%d"),
        "last": times.max().strftime("%Y-%m-%d"),
        "recent": len(recent),
        "mean_mag": float(recent["mag"].mean()),
        "largest_mag": float(largest["mag"]),
        "largest_place": largest["place"],
        "largest_time": pd.to_datetime(largest["time_ms"], unit="ms", utc=True).strftime(
            TIME_FORMAT
        ),
        "top_region": regions.index[0],
        "top_region_count": int(regions.iloc[0]),
    }


def _layout(title: str, **extra: Any) -> dict[str, Any]:
    return {
        "title": {"text": title, "x": 0.01, "font": {"size": 15, "color": TEXT}},
        "paper_bgcolor": SURFACE,
        "plot_bgcolor": SURFACE,
        "font": {"family": "system-ui, sans-serif", "color": MUTED},
        "margin": {"l": 48, "r": 16, "t": 48, "b": 40},
        "height": 360,
        **extra,
    }


def _axis(title: str = "", **extra: Any) -> dict[str, Any]:
    return {"title": title, "gridcolor": "#ecebe8", "zeroline": False, **extra}


def _daily_figure(events: pd.DataFrame) -> go.Figure:
    counts = _times(events).dt.strftime("%Y-%m-%d").value_counts().sort_index()
    bars = go.Bar(
        x=counts.index.tolist(),
        y=counts.tolist(),
        marker={"color": BAR, "cornerradius": 4},
        hovertemplate="%{x|%b %d, %Y}: %{y} events<extra></extra>",
    )
    return go.Figure(
        bars,
        _layout(
            "Events per day (UTC)",
            bargap=0.15,
            xaxis=_axis(type="date"),
            yaxis=_axis("events"),
        ),
    )


def _magnitude_figure(recent: pd.DataFrame) -> go.Figure:
    histogram = go.Histogram(
        x=recent["mag"].tolist(),
        xbins={"size": 0.5},
        marker={"color": BAR, "cornerradius": 4},
        hovertemplate="M%{x}: %{y} events<extra></extra>",
    )
    return go.Figure(
        histogram,
        _layout(
            f"Magnitude distribution, last {RECENT_DAYS} days",
            bargap=0.08,
            xaxis=_axis("magnitude"),
            yaxis=_axis("events"),
        ),
    )


def _map_figure(recent: pd.DataFrame) -> go.Figure:
    mags = recent["mag"]
    depth = recent["depth_km"].map(lambda d: "unknown" if pd.isna(d) else f"{d:.1f} km")
    customdata = list(
        zip(
            escape_text(mags.map("M{:.1f}".format)),
            escape_text(recent["place"]),
            escape_text(_times(recent).dt.strftime(TIME_FORMAT)),
            escape_text(depth),
            strict=True,
        )
    )
    points = go.Scattergeo(
        lon=recent["longitude"].tolist(),
        lat=recent["latitude"].tolist(),
        customdata=customdata,
        hovertemplate=(
            "<b>%{customdata[0]}</b> %{customdata[1]}<br>%{customdata[2]}"
            "<br>depth %{customdata[3]}<extra></extra>"
        ),
        marker={
            "size": (mags.clip(lower=0) * 3 + 6).tolist(),
            "color": mags.tolist(),
            "colorscale": MAG_SCALE,
            "colorbar": {"title": {"text": "magnitude"}, "thickness": 12},
            "line": {"color": SURFACE, "width": 1},
            "opacity": 0.85,
        },
    )
    figure = go.Figure(
        points, _layout(f"Earthquakes, last {RECENT_DAYS} days (size and color = magnitude)")
    )
    figure.update_layout(height=520, margin={"l": 0, "r": 0, "t": 48, "b": 0})
    figure.update_geos(
        projection_type="natural earth",
        fitbounds=False,  # plotly.js 4 defaults to "locations", which errors for scattergeo
        resolution=110,  # must match the vendored world_110m topojson
        showland=True,
        landcolor="#ecebe8",
        showocean=True,
        oceancolor=SURFACE,
        showcountries=True,
        countrycolor="#d4d3cf",
        coastlinecolor="#bdbcb7",
        bgcolor=SURFACE,
    )
    return figure


def _geo_assets_js() -> str:
    """Pre-load the map outlines so Plotly never fetches them from its CDN."""
    raw = _PACKAGE.joinpath("assets", "world_110m.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != WORLD_TOPOJSON_SHA256:
        raise RuntimeError("assets/world_110m.json does not match its pinned SHA-256")
    topojson = json.dumps(json.loads(raw), separators=(",", ":"))  # re-serialized: data only
    return (
        "window.PlotlyGeoAssets = window.PlotlyGeoAssets || { topojson: {} };\n"
        f"window.PlotlyGeoAssets.topojson.world_110m = {topojson};\n"
    )
