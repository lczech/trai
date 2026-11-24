"""Generate a self-contained HTML dashboard from retained readings."""

import colorsys
import hashlib
import html
import json
import math
import os
import re
import shlex
import stat
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import cache, history
from . import provider as provider_mod

RANGES = {"24h": 24 * 60 * 60, "7d": 7 * 24 * 60 * 60, "30d": 30 * 24 * 60 * 60}
DEFAULT_RANGE = "24h"
WIDTH, HEIGHT = 1200, 300
LEFT, RIGHT, TOP, BOTTOM = 64, 24, 20, 42
PLOT_WIDTH, PLOT_HEIGHT = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
SERIES_ORDER = {"short": 0, "long": 1}
DASHES = {"short": "10 6", "long": ""}
EXTRA_DASHES = ("2 5", "10 4 2 4", "14 5 2 5 2 5", "5 4")
RESET_JITTER_SECONDS = 60


class PlotError(Exception):
    pass


@dataclass(frozen=True)
class Point:
    at: float
    used: float
    resets_at: str | None
    label: str | None


@dataclass
class Readings:
    series: dict
    ignored: int = 0
    unreadable_files: int = 0


def _anchor(provider_id):
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", provider_id).strip("-") or "provider"
    # The suffix prevents two ids that normalise to the same slug from colliding.
    return f"provider-{slug}-{hashlib.sha256(provider_id.encode()).hexdigest()[:8]}"


def _parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no time zone")
    return parsed.timestamp()


def _parse_window(raw, need_id=False):
    if not isinstance(raw, dict):
        raise ValueError("window is not an object")
    used = raw.get("used_percent")
    if (isinstance(used, bool) or not isinstance(used, (int, float))
            or not math.isfinite(used) or used < 0):
        raise ValueError("invalid used_percent")
    resets_at = raw.get("resets_at")
    label = raw.get("label")
    if resets_at is not None and not isinstance(resets_at, str):
        raise ValueError("invalid resets_at")
    if resets_at is not None:
        _parse_time(resets_at)
    if label is not None and not isinstance(label, str):
        raise ValueError("invalid label")
    if need_id and (not isinstance(raw.get("id"), str) or not raw["id"]):
        raise ValueError("invalid window id")
    return float(used), resets_at, label


def _read_record(raw, earliest, latest, series):
    if not isinstance(raw, dict) or type(raw.get("schema")) is not int or raw["schema"] != history.SCHEMA:
        raise ValueError("unsupported schema")
    provider_id = raw.get("provider")
    if not isinstance(provider_id, str) or not provider_id:
        raise ValueError("invalid provider")
    at = _parse_time(raw.get("recorded_at"))
    if not earliest <= at <= latest:
        return
    additions = []
    for key in ("short", "long"):
        window = raw.get(key)
        if window is not None:
            used, resets_at, label = _parse_window(window)
            additions.append((key, Point(at, used, resets_at, label)))
    windows = raw.get("windows")
    if not isinstance(windows, list):
        raise ValueError("windows is not a list")
    for window in windows:
        used, resets_at, label = _parse_window(window, need_id=True)
        additions.append((f"extra:{window['id']}", Point(at, used, resets_at, label)))
    for key, point in additions:
        series[provider_id][key].append(point)


def read_history(now):
    """Read schema-1 records needed by the longest plot range."""
    series = defaultdict(lambda: defaultdict(list))
    ignored = unreadable = 0
    directory = history.history_dir()
    try:
        paths = sorted(directory.iterdir())
    except FileNotFoundError:
        return Readings({})
    except OSError:
        return Readings({}, unreadable_files=1)
    earliest = now - RANGES["30d"]
    for path in paths:
        if not history.DAILY_FILE.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            continue
        try:
            with open(path, errors="replace") as source:
                for line in source:
                    if not line.strip():
                        ignored += 1
                        continue
                    try:
                        _read_record(json.loads(line), earliest, now, series)
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        ignored += 1
        except OSError:
            unreadable += 1
    out = {}
    for provider_id, provider_series in series.items():
        out[provider_id] = {}
        for key, points in provider_series.items():
            out[provider_id][key] = sorted(points, key=lambda point: point.at)
    return Readings(out, ignored, unreadable)


def _current_provider_ids(cfg, discovered):
    if cfg["providers"] is not None:
        return list(cfg["providers"])
    ids = []
    for provider_id in discovered:
        entry = cache.load(provider_id)
        missing = cache.is_failed(entry) and entry["error"]["kind"] == "missing"
        if not missing:
            ids.append(provider_id)
    return ids


def _providers(cfg, readings):
    discovered = provider_mod.discover()
    current = _current_provider_ids(cfg, discovered)
    historical = sorted(set(readings.series) - set(current))
    providers = []
    for provider_id in current + historical:
        provider = discovered.get(provider_id)
        providers.append({
            "id": provider_id,
            "name": provider.name if provider else provider_id,
            "color": cfg["colors"].get(provider_id, provider.color if provider else "#888888"),
        })
    return providers


def _documents_dir():
    """Return XDG_DOCUMENTS_DIR without evaluating the user-dirs file as shell."""
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        lines = (config_home / "user-dirs.dirs").read_text().splitlines()
    except OSError:
        return home / "Documents"
    for line in lines:
        if not line.startswith("XDG_DOCUMENTS_DIR="):
            continue
        try:
            words = shlex.split(line.partition("=")[2])
        except ValueError:
            break
        if len(words) == 1:
            value = words[0].replace("${HOME}", str(home)).replace("$HOME", str(home))
            path = Path(value)
            if path.is_absolute():
                return path
        break
    return home / "Documents"


def _plot_dir():
    base = _documents_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
        info = base.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(f"unsafe directory: {base}")
    except OSError as error:
        raise PlotError(f"cannot use Documents directory: {error}") from None
    return base


def _remove_legacy_plot(documents):
    legacy_dir = documents / "trai"
    legacy = legacy_dir / "usage-history.html"
    try:
        if legacy.is_file() and not legacy.is_symlink():
            legacy.unlink()
        legacy_dir.rmdir()  # succeeds only if trai left the directory empty
    except OSError:
        pass


def _display_value(point, show):
    return 100 - point.used if show == "remaining" else point.used


def _segments(points, start, end, max_gap):
    selected = [point for point in points if start <= point.at <= end]
    if not selected:
        return []
    out, segment = [], [selected[0]]
    for point in selected[1:]:
        previous = segment[-1]
        reset = (previous.resets_at is not None and point.resets_at is not None
                 and abs(_parse_time(previous.resets_at) - _parse_time(point.resets_at))
                 > RESET_JITTER_SECONDS)
        if reset or point.at - previous.at > max_gap:
            out.append(segment)
            segment = [point]
        else:
            segment.append(point)
    out.append(segment)
    return out


def _downsample(points, show, limit=PLOT_WIDTH * 2):
    """Keep chronological min/max representatives from time buckets."""
    if len(points) <= limit:
        return points
    buckets = max(1, limit // 2)
    start, span = points[0].at, points[-1].at - points[0].at
    if span <= 0:
        return [points[0], points[-1]]
    grouped = defaultdict(list)
    for point in points:
        index = min(int((point.at - start) / span * buckets), buckets - 1)
        grouped[index].append(point)
    sampled = [points[0]]
    for index in sorted(grouped):
        bucket = grouped[index]
        low = min(bucket, key=lambda point: _display_value(point, show))
        high = max(bucket, key=lambda point: _display_value(point, show))
        for point in sorted({low, high}, key=lambda item: item.at):
            if point != sampled[-1]:
                sampled.append(point)
    if points[-1] != sampled[-1]:
        sampled.append(points[-1])
    return sampled


def _shade(color, index):
    if index == 0:
        return color
    red, green, blue = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    adjustment = (0.12, -0.12, 0.22, -0.22)[(index - 1) % 4]
    lightness = min(0.82, max(0.18, lightness + adjustment))
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def _series_style(key, extra_index, color):
    if key in DASHES:
        return color, DASHES[key]
    return _shade(color, extra_index), EXTRA_DASHES[extra_index % len(EXTRA_DASHES)]


def _number(value):
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _age(seconds):
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def _label_for(key, points):
    for point in reversed(points):
        if point.label:
            return point.label
    return key.removeprefix("extra:")


def _path_for(segments, start, end, show):
    commands, singletons, overflow = [], [], []
    state = None
    for segment in segments:
        sampled = _downsample(segment, show)
        for index, point in enumerate(sampled):
            value = _display_value(point, show)
            x = LEFT + (point.at - start) / (end - start) * PLOT_WIDTH
            y = TOP + (100 - min(100, max(0, value))) / 100 * PLOT_HEIGHT
            commands.append(f"{'M' if index == 0 else 'L'}{x:.2f},{y:.2f}")
        if len(sampled) == 1:
            singletons.append((LEFT + (sampled[0].at - start) / (end - start) * PLOT_WIDTH,
                               _display_value(sampled[0], show)))
        for point in segment:
            value = _display_value(point, show)
            current = "above" if value > 100 else "below" if value < 0 else None
            if current and current != state:
                x = LEFT + (point.at - start) / (end - start) * PLOT_WIDTH
                overflow.append((x, current))
            state = current
        state = None
    return " ".join(commands), singletons, overflow


def _swatch(color, dash):
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<svg class="swatch" viewBox="0 0 34 8" aria-hidden="true">'
            f'<path d="M1 4H33" stroke="{color}" stroke-width="3"{dash_attr}/></svg>')


def _time_ticks(start, end, range_name):
    fmt = "%H:%M" if range_name == "24h" else "%a %d" if range_name == "7d" else "%d %b"
    ticks = []
    for index in range(7):
        at = start + (end - start) * index / 6
        x = LEFT + PLOT_WIDTH * index / 6
        label = datetime.fromtimestamp(at).astimezone().strftime(fmt)
        ticks.append((x, label))
    return ticks


def _chart(provider, provider_series, range_name, start, end, cfg, now):
    show = cfg["show"]
    max_gap = 2 * cfg["refresh_seconds"]
    present = []
    keys = sorted(provider_series, key=lambda key: (SERIES_ORDER.get(key, 2), key))
    extra_indices = {key: index for index, key in enumerate(key for key in keys if key.startswith("extra:"))}
    for key in keys:
        points = [point for point in provider_series[key] if start <= point.at <= end]
        if not points:
            continue
        color, dash = _series_style(key, extra_indices.get(key, 0), provider["color"])
        present.append((key, points, color, dash))

    anchor = f"{_anchor(provider['id'])}-{range_name}"
    heading = html.escape(provider["name"])
    provider_id = html.escape(provider["id"])
    if not present:
        return (f'<article class="provider" id="{anchor}"><h2>{heading}</h2>'
                f'<p class="provider-id">{provider_id}</p>'
                '<div class="empty">No readings in this range.</div></article>')

    svg = [f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
           f'aria-label="{heading} usage history">']
    for value in (0, 25, 50, 75, 100):
        y = TOP + (100 - value) / 100 * PLOT_HEIGHT
        svg.append(f'<line class="grid" x1="{LEFT}" y1="{y:.2f}" x2="{WIDTH - RIGHT}" y2="{y:.2f}"/>')
        svg.append(f'<text class="tick y-tick" x="{LEFT - 10}" y="{y + 4:.2f}">{value}</text>')
    for x, label in _time_ticks(start, end, range_name):
        svg.append(f'<line class="grid vertical" x1="{x:.2f}" y1="{TOP}" x2="{x:.2f}" y2="{HEIGHT - BOTTOM}"/>')
        svg.append(f'<text class="tick x-tick" x="{x:.2f}" y="{HEIGHT - 14}">{html.escape(label)}</text>')
    axis = "% remaining" if show == "remaining" else "% used"
    svg.append(f'<text class="axis-label" transform="translate(16 {TOP + PLOT_HEIGHT / 2}) rotate(-90)">{axis}</text>')

    legend = []
    for key, points, color, dash in present:
        segments = _segments(provider_series[key], start, end, max_gap)
        path, singletons, overflow = _path_for(segments, start, end, show)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        svg.append(f'<path class="series-halo" d="{path}"{dash_attr}/>')
        svg.append(f'<path class="series" d="{path}" stroke="{color}"{dash_attr}/>')
        for x, value in singletons:
            y = TOP + (100 - min(100, max(0, value))) / 100 * PLOT_HEIGHT
            svg.append(f'<circle class="point-halo" cx="{x:.2f}" cy="{y:.2f}" r="5"/>')
            svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{color}"/>')
        for x, direction in overflow:
            if direction == "above":
                points_attr = f"{x - 5:.2f},{TOP + 8} {x + 5:.2f},{TOP + 8} {x:.2f},{TOP}"
            else:
                bottom = TOP + PLOT_HEIGHT
                points_attr = f"{x - 5:.2f},{bottom - 8} {x + 5:.2f},{bottom - 8} {x:.2f},{bottom}"
            svg.append(f'<polygon points="{points_attr}" fill="{color}"/>')
        latest = points[-1]
        value = _display_value(latest, show)
        legend.append(f'<li>{_swatch(color, dash)}<span>{html.escape(_label_for(key, points))}</span>'
                      f'<strong>{_number(value)}% {show}</strong><small>{_age(max(0, now - latest.at))}</small></li>')
    svg.append("</svg>")

    earliest = min(point.at for points in provider_series.values() for point in points)
    coverage = f"Earliest retained reading: {_age(max(0, now - earliest))}."
    return (f'<article class="provider" id="{anchor}"><h2>{heading}</h2>'
            f'<p class="provider-id">{provider_id}</p><ul class="legend">{"".join(legend)}</ul>'
            f'{"".join(svg)}<p class="coverage">{coverage}</p></article>')


def _warning(readings):
    parts = []
    if readings.ignored:
        noun = "record was" if readings.ignored == 1 else "records were"
        parts.append(f"{readings.ignored} unreadable or unsupported history {noun} ignored")
    if readings.unreadable_files:
        noun = "file could" if readings.unreadable_files == 1 else "files could"
        parts.append(f"{readings.unreadable_files} history {noun} not be read")
    return "; ".join(parts)


def _document(cfg, readings, providers, initial_range, now):
    generated = datetime.fromtimestamp(now).astimezone()
    generated_text = generated.strftime("%d %b %Y, %H:%M %Z")
    nav = []
    for provider in providers:
        anchor = _anchor(provider["id"])
        nav.append(f'<a href="#{anchor}-{initial_range}" data-provider="{anchor}">{html.escape(provider["name"])}</a>')
    views = []
    for range_name, seconds in RANGES.items():
        charts = []
        for provider in providers:
            charts.append(_chart(provider, readings.series.get(provider["id"], {}), range_name,
                                 now - seconds, now, cfg, now))
        if not charts:
            charts.append('<div class="empty">No history recorded yet.</div>')
        hidden = "" if range_name == initial_range else " hidden"
        views.append(f'<main class="range-view" data-range="{range_name}"{hidden}>{"".join(charts)}</main>')
    warning = _warning(readings)
    warning_html = f'<p class="warning">⚠ {html.escape(warning)}.</p>' if warning else ""
    show = html.escape(cfg["show"])
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>trai usage history</title>
<style>
:root {{ color-scheme: light dark; --bg: #fff; --fg: #202020; --dim: #666; --grid: #ddd; --panel: #f7f7f7; --warning: #8a4b00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #1e1e1e; --fg: #eee; --dim: #aaa; --grid: #444; --panel: #282828; --warning: #ffb44d; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--fg); font: 15px system-ui, sans-serif; }}
header {{ position: sticky; top: 0; z-index: 2; padding: 16px max(20px, calc((100% - 1260px) / 2)); background: color-mix(in srgb, var(--bg) 94%, transparent); border-bottom: 1px solid var(--grid); backdrop-filter: blur(8px); }}
h1 {{ display: inline; margin: 0 18px 0 0; font-size: 1.25rem; }}
.generated {{ color: var(--dim); }}
.controls {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; }}
button, nav a {{ border: 1px solid var(--grid); border-radius: 6px; padding: 5px 10px; color: var(--fg); background: var(--panel); font: inherit; text-decoration: none; cursor: pointer; }}
button[aria-pressed="true"] {{ border-color: #3584e4; box-shadow: inset 0 0 0 1px #3584e4; }}
nav {{ display: flex; flex-wrap: wrap; gap: 6px; margin-left: 12px; }}
.warning {{ max-width: 1260px; margin: 16px auto; padding: 10px 14px; color: var(--warning); border: 1px solid currentColor; border-radius: 6px; }}
.range-view {{ max-width: 1260px; margin: 0 auto; padding: 8px 20px 40px; }}
[hidden] {{ display: none !important; }}
.provider {{ scroll-margin-top: 130px; margin-top: 28px; padding: 18px; background: var(--panel); border: 1px solid var(--grid); border-radius: 10px; }}
h2 {{ display: inline; margin: 0; font-size: 1.15rem; }}
.provider-id {{ display: inline; margin-left: 8px; color: var(--dim); }}
.chart {{ display: block; width: 100%; min-height: 220px; margin-top: 12px; overflow: visible; }}
.grid {{ stroke: var(--grid); stroke-width: 1; }} .grid.vertical {{ stroke-dasharray: 2 5; }}
.tick {{ fill: var(--dim); font-size: 12px; }} .y-tick {{ text-anchor: end; }} .x-tick {{ text-anchor: middle; }}
.axis-label {{ fill: var(--dim); font-size: 12px; text-anchor: middle; }}
.series-halo {{ fill: none; stroke: var(--bg); stroke-width: 7; stroke-linejoin: round; stroke-linecap: round; }}
.series {{ fill: none; stroke-width: 3; stroke-linejoin: round; stroke-linecap: round; }}
.point-halo {{ fill: var(--bg); }}
.legend {{ display: flex; flex-wrap: wrap; gap: 8px 20px; margin: 12px 0 0; padding: 0; list-style: none; }}
.legend li {{ display: grid; grid-template-columns: 34px auto auto; align-items: center; column-gap: 7px; }}
.legend strong {{ font-variant-numeric: tabular-nums; }} .legend small {{ grid-column: 2 / 4; color: var(--dim); }}
.swatch {{ width: 34px; height: 8px; }}
.coverage {{ margin: 0; color: var(--dim); font-size: .9rem; }}
.empty {{ margin-top: 14px; min-height: 120px; display: grid; place-items: center; color: var(--dim); border: 1px dashed var(--grid); border-radius: 6px; }}
@media (max-width: 640px) {{ header {{ position: static; }} nav {{ margin: 6px 0 0; width: 100%; }} .provider {{ padding: 10px; }} .chart {{ min-height: 180px; }} }}
</style>
</head>
<body data-initial-range="{initial_range}">
<header><h1>trai usage history</h1><span class="generated">Generated {html.escape(generated_text)} · showing % {show}</span>
<div class="controls"><div class="ranges" role="group" aria-label="Time range">
<button data-range="24h" aria-pressed="{'true' if initial_range == '24h' else 'false'}">24h</button>
<button data-range="7d" aria-pressed="{'true' if initial_range == '7d' else 'false'}">7d</button>
<button data-range="30d" aria-pressed="{'true' if initial_range == '30d' else 'false'}">30d</button>
</div><nav>{''.join(nav)}</nav></div></header>
{warning_html}{''.join(views)}
<script>
let selectedRange = document.body.dataset.initialRange;
function selectRange(range) {{
  selectedRange = range;
  document.querySelectorAll('.range-view').forEach(view => view.hidden = view.dataset.range !== range);
  document.querySelectorAll('button[data-range]').forEach(button => button.setAttribute('aria-pressed', button.dataset.range === range));
}}
document.querySelectorAll('button[data-range]').forEach(button => button.addEventListener('click', () => selectRange(button.dataset.range)));
document.querySelectorAll('nav a').forEach(link => link.addEventListener('click', event => {{
  event.preventDefault();
  const target = document.getElementById(`${{link.dataset.provider}}-${{selectedRange}}`);
  if (target) {{ history.replaceState(null, '', `#${{target.id}}`); target.scrollIntoView(); }}
}}));
</script>
</body>
</html>'''


def generate(cfg, focus=None, initial_range=DEFAULT_RANGE, now=None):
    """Generate the dashboard and return a browser-compatible file URI."""
    if initial_range not in RANGES:
        raise PlotError(f"unknown range: {initial_range}")
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    readings = read_history(now)
    providers = _providers(cfg, readings)
    ids = {provider["id"] for provider in providers}
    if focus is not None and focus not in ids:
        raise PlotError(f"unknown provider: {focus}")
    document = _document(cfg, readings, providers, initial_range, now)
    directory = _plot_dir()
    path = directory / "trai-usage-history.html"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".trai-usage-history.", delete=False) as output:
            temporary = Path(output.name)
            output.write(document)
        os.replace(temporary, path)
        _remove_legacy_plot(directory)
    except OSError as error:
        try:
            if temporary:
                temporary.unlink()
        except OSError:
            pass
        raise PlotError(f"cannot write plot: {error}") from None
    uri = path.as_uri()
    if focus is not None:
        uri += f"#{quote(f'{_anchor(focus)}-{initial_range}', safe='-_')}"
    return uri
