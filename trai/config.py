"""Loading and validating config.toml."""

import copy
import os
import re
import tomllib
from pathlib import Path

DEFAULTS = {
    "providers": None,
    "refresh_seconds": 180,
    "path": [],
    "show": "used",
    "thresholds": {"warn": 75, "critical": 90},
    "display": {
        "show_name": False,
        "show_labels": False,
        "divider": True,
        "name_color": "theme",
        "label_color": "theme-dim",
        "value_color": "theme",
        "warn_color": "#e5a50a",
        "critical_color": "#e01b24",
        "notify_warn": False,
        "notify_critical": True,
        "outer_ring": "short",
        "panel_box": "right",
        "panel_index": 0,
        "ring": {
            "outer_diameter": 24,
            "outer_width": 3,
            "inner_diameter": 15,
            "inner_width": 3,
        },
    },
    "colors": {},
}

TEMPLATE = """\
# trai config. Every line shows its default; uncomment to change.

# Enabled providers, in panel order. Without this list, all discovered
# providers are used alphabetically, and those whose CLI is missing are hidden.
# providers = ["claude", "codex"]

# refresh_seconds = 180

# Directories to search for provider CLIs before PATH, e.g. when the desktop
# session's PATH lacks what your shell adds in ~/.bashrc.
# path = ["~/.local/npm/bin"]

# What rings and numbers show: "used" or "remaining".
# show = "used"

# [thresholds]  # on used percent, regardless of `show`
# warn = 75
# critical = 90

# [display]
# show_name = false
# show_labels = false
# divider = true
# Colours: "theme", "theme-dim", "provider" or "#rrggbb".
# name_color = "theme"
# label_color = "theme-dim"
# value_color = "theme"
# warn_color = "#e5a50a"
# critical_color = "#e01b24"
# notify_warn = false
# notify_critical = true
# Which window gets the bigger, outer ring: "short" (e.g. 5h) or "long" (e.g. weekly).
# outer_ring = "short"
# Top bar area ("left", "center", "right") and position within it (0 = leftmost).
# panel_box = "right"
# panel_index = 0

# [display.ring]  # pixels; single-window providers use the outer values
# outer_diameter = 24
# outer_width = 3
# inner_diameter = 15
# inner_width = 3

# [colors]  # ring colour per provider id, "#rrggbb"
# codex = "#10a37f"
"""

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
TEXT_COLORS = ("theme", "theme-dim", "provider")


class ConfigError(Exception):
    def __init__(self, message, partial=None):
        super().__init__(message)
        self.partial = partial  # effective config with only the broken keys at their defaults


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "trai"


def config_path():
    return config_dir() / "config.toml"


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check(ok, message):
    if not ok:
        raise ConfigError(message)


def _validate_value(name, value):
    """Return the value for the dotted key `name`, or raise ConfigError."""
    if name == "providers":
        _check(isinstance(value, list) and all(isinstance(i, str) for i in value),
               "providers must be a list of provider ids")
        _check(len(set(value)) == len(value), "providers contains duplicates")
    elif name == "refresh_seconds":
        _check(_is_number(value) and value >= 10, "refresh_seconds must be a number >= 10")
    elif name == "path":
        _check(isinstance(value, list) and all(isinstance(d, str) and d for d in value),
               "path must be a list of directories")
    elif name == "show":
        _check(value in ("used", "remaining"), 'show must be "used" or "remaining"')
    elif name.startswith("thresholds."):
        _check(_is_number(value) and 0 <= value <= 100, f"{name} must be a number from 0 to 100")
    elif name.startswith("display.ring."):
        _check(_is_number(value) and value > 0, f"{name} must be a positive number")
    elif name == "display.outer_ring":
        _check(value in ("short", "long"), 'display.outer_ring must be "short" or "long"')
    elif name == "display.panel_box":
        _check(value in ("left", "center", "right"), 'display.panel_box must be "left", "center" or "right"')
    elif name == "display.panel_index":
        _check(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
               "display.panel_index must be an integer >= 0")
    elif name.endswith("_color"):
        _check(isinstance(value, str) and (value in TEXT_COLORS or bool(HEX.match(value))),
               f'{name} must be "theme", "theme-dim", "provider" or "#rrggbb"')
    elif name.startswith("colors."):
        _check(isinstance(value, str) and bool(HEX.match(value)), f'{name} must be "#rrggbb"')
    else:
        _check(isinstance(value, bool), f"{name} must be true or false")
    return value


def _merge(raw, defaults, prefix, errors):
    """Defaults overlaid with every valid value from raw; problems go to errors."""
    out = copy.deepcopy(defaults)
    for key, value in raw.items():
        name = prefix + key
        if key not in defaults:
            errors.append(f"unknown key {name}")
        elif isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                errors.append(f"[{name}] must be a table")
            elif name == "colors":  # free-form keys: provider ids
                out[key] = _merge(value, dict.fromkeys(value), "colors.", errors)
                out[key] = {k: v for k, v in out[key].items() if v is not None}
            else:
                out[key] = _merge(value, defaults[key], name + ".", errors)
        else:
            try:
                out[key] = _validate_value(name, value)
            except ConfigError as e:
                errors.append(str(e))
    return out


def _validate(raw):
    """Return (config, errors); invalid keys keep their defaults."""
    errors = []
    cfg = _merge(raw, DEFAULTS, "", errors)
    if cfg["thresholds"]["warn"] > cfg["thresholds"]["critical"]:
        errors.append("thresholds.warn must not exceed thresholds.critical")
        cfg["thresholds"] = copy.deepcopy(DEFAULTS["thresholds"])
    return cfg, errors


def load(path=None):
    """Return the effective config. Raises ConfigError, which carries the partial config."""
    path = Path(path) if path else config_path()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}", copy.deepcopy(DEFAULTS)) from None
    cfg, errors = _validate(raw)
    if errors:
        raise ConfigError(f"{path}: {'; '.join(errors)}", cfg)
    return cfg


def defaults():
    return copy.deepcopy(DEFAULTS)
