"""Validation of provider output, window labels and usage levels."""

import json
import math
from datetime import datetime

PROVIDER_ERROR_KINDS = ("auth", "missing", "unavailable", "other")
LEVELS = ("ok", "warn", "critical")


class ValidationError(Exception):
    pass


def _finite(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValidationError(f"number out of range: {text}")
    return value


def _no_constant(name):
    raise ValidationError(f"{name} is not a valid number")


def parse_json(text):
    """json.loads, but NaN and Infinity are errors (the GNOME side can't parse them)."""
    return json.loads(text, parse_float=_finite, parse_constant=_no_constant)


def derive_label(minutes):
    if not minutes:
        return None
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def parse_time(value):
    """Parse an ISO 8601 timestamp that must carry a time zone."""
    if not isinstance(value, str):
        raise ValidationError(f"resets_at must be an ISO 8601 string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"resets_at is not ISO 8601: {value!r}") from None
    if parsed.tzinfo is None:
        raise ValidationError(f"resets_at has no time zone: {value!r}")
    return parsed


def _window(raw, where, need_id=False):
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} must be an object")
    used = raw.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or used < 0:
        raise ValidationError(f"{where}.used_percent must be a number >= 0")
    minutes = raw.get("window_minutes")
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0):
        raise ValidationError(f"{where}.window_minutes must be a positive integer")
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        raise ValidationError(f"{where}.label must be a string")
    resets_at = raw.get("resets_at")
    window = {
        "label": label or derive_label(minutes),
        "used_percent": used,
        "resets_at": parse_time(resets_at).isoformat() if resets_at is not None else None,
        "window_minutes": minutes,
    }
    if need_id:
        if not isinstance(raw.get("id"), str) or not raw["id"]:
            raise ValidationError(f"{where}.id must be a non-empty string")
        window = {"id": raw["id"], **window}
    return window


def validate_output(raw):
    """Normalise a provider's `fetch` output.

    Returns ("data", data) or ("error", error). Raises ValidationError.
    """
    if not isinstance(raw, dict):
        raise ValidationError("output must be a JSON object")
    if "error" in raw:
        error = raw["error"]
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            raise ValidationError("error must be an object with a message")
        kind = error.get("kind")
        return "error", {
            "kind": kind if kind in PROVIDER_ERROR_KINDS else "other",
            "message": error["message"],
        }
    data = {
        "short": _window(raw["short"], "short") if raw.get("short") is not None else None,
        "long": _window(raw["long"], "long") if raw.get("long") is not None else None,
    }
    if data["short"] is None and data["long"] is None:
        raise ValidationError("output needs at least one of short, long")
    windows = raw.get("windows")
    windows = [] if windows is None else windows
    if not isinstance(windows, list):
        raise ValidationError("windows must be a list")
    data["windows"] = [_window(w, f"windows[{i}]", need_id=True) for i, w in enumerate(windows)]
    extra = raw.get("extra")
    extra = {} if extra is None else extra
    if not isinstance(extra, dict):
        raise ValidationError("extra must be an object")
    data["extra"] = extra
    return "data", data


def level_of(used, thresholds):
    if used >= thresholds["critical"]:
        return "critical"
    if used >= thresholds["warn"]:
        return "warn"
    return "ok"


def worst(levels):
    """Worst of ok/warn/critical; None for an empty input."""
    levels = list(levels)
    return max(levels, key=LEVELS.index) if levels else None


def all_windows(data):
    return [w for w in (data["short"], data["long"]) if w] + data["windows"]
