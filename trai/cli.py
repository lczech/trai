"""Command line interface."""

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import __version__, cache, config
from . import provider as provider_mod
from .model import all_windows, level_of, worst

EXIT_OK, EXIT_WARN, EXIT_CRITICAL, EXIT_UNKNOWN, EXIT_USAGE = 0, 1, 2, 3, 4
# Most urgent first: an agent must see "critical" even if another provider is unknown.
EXIT_PRIORITY = (EXIT_CRITICAL, EXIT_UNKNOWN, EXIT_WARN, EXIT_OK)
EXIT_FOR_LEVEL = {"ok": EXIT_OK, "warn": EXIT_WARN, "critical": EXIT_CRITICAL, "unknown": EXIT_UNKNOWN}

CHECK_HELP = """\
Print one line per provider and exit with the most urgent level:
  0 ok        all windows below the warn threshold
  1 warn      a window is at or above warn
  2 critical  a window is at or above critical
  3 unknown   no usable data: none yet, an internal error, or a failed refresh with data
              older than 2 x refresh_seconds (or than --max-age, if given)
  4 usage or config error
Critical wins over unknown, unknown over warn. Percentages are always "used".
Windows include per-model limits, not just the 5h/weekly ones.
"(as of …)" marks data older than refresh_seconds."""


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"trai: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def build_parser():
    parser = Parser(prog="trai", description="LLM subscription usage at a glance.")
    parser.add_argument("--version", action="version", version=f"trai {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    def add_freshness(p):
        group = p.add_mutually_exclusive_group()
        group.add_argument("--max-age", type=float, metavar="S",
                           help="max age of cached data in seconds (default: refresh_seconds)")
        group.add_argument("--refresh", action="store_true", help="always fetch fresh data (= --max-age 0)")

    status = sub.add_parser("status", help="show usage")
    status.add_argument("ids", nargs="*", metavar="ID", help="provider ids (default: all active)")
    fmt = status.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="machine-readable data")
    fmt.add_argument("--gui", action="store_true",
                     help="data plus display settings, for GUI frontends (opens nothing)")
    add_freshness(status)

    check = sub.add_parser("check", help="one-line verdict per provider, with exit code",
                           description=CHECK_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    check.add_argument("ids", nargs="*", metavar="ID", help="provider ids (default: all active)")
    check.add_argument("--warn", type=float, metavar="N", help="override the warn threshold")
    check.add_argument("--critical", type=float, metavar="N", help="override the critical threshold")
    add_freshness(check)

    sub.add_parser("providers", help="list discovered providers")

    cfg = sub.add_parser("config", help="config file helpers")
    cfg_sub = cfg.add_subparsers(dest="action", required=True, parser_class=Parser)
    init = cfg_sub.add_parser("init", help="write a commented config template")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    cfg_sub.add_parser("path", help="print the config file path")
    return parser


# --- formatting -------------------------------------------------------------

def iso(at):
    return datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds") if at else None


def format_age(seconds):
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def format_reset(resets_at, now):
    if not resets_at:
        return None
    when = datetime.fromisoformat(resets_at)
    delta = when.timestamp() - now
    if delta <= 0:
        return "reset due"
    if delta < 86400:
        hours, minutes = int(delta // 3600), int(delta % 3600 // 60)
        if not hours:
            return f"resets in {max(minutes, 1)}m"
        return f"resets in {hours}h {minutes}m" if minutes else f"resets in {hours}h"
    return "resets " + when.astimezone().strftime("%a %H:%M")


def format_percent(value):
    return f"{value:.0f}%"


# --- selection and reports --------------------------------------------------

def select(cfg, discovered, ids):
    """Return (providers, explicit, unknown_ids_in_config)."""
    if ids:
        unknown = [i for i in ids if i not in discovered]
        if unknown:
            raise UsageError(f"unknown provider{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}")
        return [discovered[i] for i in ids], True, []
    if cfg["providers"] is not None:
        unknown = [i for i in cfg["providers"] if i not in discovered]
        return [discovered[i] for i in cfg["providers"] if i in discovered], True, unknown
    return list(discovered.values()), False, []


def is_missing(entry):
    return cache.is_failed(entry) and entry["error"]["kind"] == "missing"


def fetch_all(providers, max_age):
    if not providers:
        return []
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        return list(pool.map(lambda p: cache.get(p, max_age), providers))


def report(prov, entry, cfg, now):
    success = entry["success"]
    failed = cache.is_failed(entry)
    data = success["data"] if success else None
    out = {
        "id": prov.id,
        "name": prov.name,
        "color": cfg["colors"].get(prov.id, prov.color),
        "level": "unknown",
        "stale": failed or bool(success and now - success["at"] > 2 * cfg["refresh_seconds"]),
        "fetched_at": iso(success["at"]) if success else None,
        "error": None,
        "short": None,
        "long": None,
        "windows": [],
        "extra": {},
    }
    if failed:
        error = entry["error"]
        out["error"] = {"kind": error["kind"], "message": error["message"], "at": iso(error["at"])}
    if data:
        def annotate(window):
            return {**window, "level": level_of(window["used_percent"], cfg["thresholds"])} if window else None
        out["short"], out["long"] = annotate(data["short"]), annotate(data["long"])
        out["windows"] = [annotate(w) for w in data["windows"]]
        out["extra"] = data["extra"]
        out["level"] = worst(w["level"] for w in all_windows(out))
    return out


def gather(cfg, ids, max_age, now):
    """Return (reports, config_problem) for the selected providers."""
    discovered = provider_mod.discover()
    providers, explicit, unknown = select(cfg, discovered, ids)
    entries = fetch_all(providers, max_age)
    reports = [report(p, e, cfg, now) for p, e in zip(providers, entries) if explicit or not is_missing(e)]
    problem = f"unknown provider in config: {', '.join(unknown)}" if unknown else None
    return reports, problem


# --- commands ---------------------------------------------------------------

def default_max_age(args, cfg):
    if args.refresh:
        return 0
    if args.max_age is not None:
        return args.max_age
    # The GUI polls every refresh_seconds; a full-interval max age would skip
    # every other refresh, because the previous one finished slightly later.
    return cfg["refresh_seconds"] / 2 if getattr(args, "gui", False) else cfg["refresh_seconds"]


def window_text(window, show, with_label=True):
    used = window["used_percent"]
    value = format_percent(100 - used if show == "remaining" else used)
    label = window["label"] or window.get("id") or ""
    return f"{label} {value}" if with_label and label else value


def cmd_status(args, cfg, now):
    reports, problem = gather(cfg, args.ids, default_max_age(args, cfg), now)
    if problem:
        raise config.ConfigError(problem)
    if args.json:
        print(json.dumps({"schema": 1, "providers": reports}, indent=2))
        return EXIT_OK

    suffix = "left" if cfg["show"] == "remaining" else "used"
    for i, r in enumerate(reports):
        if i:
            print()
        head = r["name"]
        plan = r["extra"].get("plan")
        if plan:
            head += f" ({plan})"
        if r["fetched_at"]:
            head += f" · updated {format_age(now - datetime.fromisoformat(r['fetched_at']).timestamp())}"
        print(head)
        rows = all_windows(r) if r["level"] != "unknown" else []
        width = max((len(w["label"] or w.get("id") or "") for w in rows), default=0)
        for w in rows:
            used = w["used_percent"]
            value = format_percent(100 - used if cfg["show"] == "remaining" else used)
            parts = [f"  {(w['label'] or w.get('id') or ''):<{width}}  {value:>4} {suffix}"]
            reset = format_reset(w["resets_at"], now)
            if reset:
                parts.append(reset)
            if w["level"] != "ok":
                parts.append(w["level"].upper())
            print("   ".join(parts))
        if r["error"]:
            print(f"  ! refresh failed ({r['error']['kind']}): {r['error']['message']}")
    if not reports:
        print("no active providers")
    return EXIT_OK


def cmd_gui(args, cfg, config_error, now):
    try:
        reports, problem = gather(cfg, [], default_max_age(args, cfg), now)
    except Exception as e:  # the GUI must always get JSON
        reports, problem = [], f"trai failed: {e}"
    print(json.dumps({
        "gui_schema": 1,
        "config": {key: cfg[key] for key in ("refresh_seconds", "show", "thresholds", "display")},
        "config_error": config_error or problem,
        "providers": reports,
    }))
    return EXIT_OK


def cmd_check(args, cfg, now):
    thresholds = dict(cfg["thresholds"])
    if args.warn is not None:
        thresholds["warn"] = args.warn
    if args.critical is not None:
        thresholds["critical"] = args.critical
    if thresholds["warn"] > thresholds["critical"]:
        raise UsageError("warn threshold must not exceed critical threshold")
    cfg = {**cfg, "thresholds": thresholds}
    max_age = default_max_age(args, cfg)
    # Data that could not be refreshed still counts until it is stale, so that a
    # caller who can't refresh (e.g. a sandboxed agent) gets a level between two
    # refreshes done by someone else. An explicit max age is taken literally.
    explicit = args.refresh or args.max_age is not None
    usable_age = max_age if explicit else 2 * cfg["refresh_seconds"]

    reports, problem = gather(cfg, args.ids, max_age, now)
    if problem:
        raise config.ConfigError(problem)
    if not reports:
        print("no active providers")
        return EXIT_UNKNOWN

    codes = []
    for r in reports:
        fetched = datetime.fromisoformat(r["fetched_at"]).timestamp() if r["fetched_at"] else None
        age = now - fetched if fetched is not None else None
        too_old = age is not None and r["error"] and age > usable_age
        level = "unknown" if too_old else r["level"]
        codes.append(EXIT_FOR_LEVEL[level])

        shown = [w for w in (r["short"], r["long"]) if w]
        shown += [w for w in r["windows"] if w["level"] != "ok"]
        parts = []
        for w in shown:
            text = window_text(w, "used")
            reset = format_reset(w["resets_at"], now)
            parts.append(f"{text} ({reset})" if reset else text)
        notes = []
        if age is not None and age > cfg["refresh_seconds"]:
            notes.append(f"as of {format_age(age)}")
        if r["error"]:
            notes.append(f"refresh failed ({r['error']['kind']}): {r['error']['message']}")
        line = " · ".join([f"{r['id']}: {level.upper()}"] + parts)
        if notes:
            line += ("  (" + "; ".join(notes) + ")") if parts else " · " + "; ".join(notes)
        print(line)
    return next(code for code in EXIT_PRIORITY if code in codes)


def cmd_providers(cfg, now):
    discovered = provider_mod.discover()
    rows = [("ID", "NAME", "SOURCE", "ACTIVE", "STATUS")]
    for pid, prov in discovered.items():
        entry = cache.load(pid)
        if cfg["providers"] is not None:
            active = pid in cfg["providers"]
        else:
            active = not is_missing(entry)
        if prov.problem:
            status = f"invalid: {prov.problem}"
        elif cache.is_failed(entry):
            status = f"{entry['error']['kind']} ({format_age(now - entry['error']['at'])})"
        elif entry["success"]:
            status = f"ok ({format_age(now - entry['success']['at'])})"
        else:
            status = "never fetched"
        rows.append((pid, prov.name, prov.source, "yes" if active else "no", status))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)) + "  " + row[4])
    return EXIT_OK


def cmd_config(args):
    path = config.config_path()
    if args.action == "path":
        print(path)
        return EXIT_OK
    if path.exists() and not args.force:
        raise UsageError(f"{path} exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.TEMPLATE)
    print(f"wrote {path}")
    return EXIT_OK


def apply_path(cfg):
    """Prepend the configured directories to PATH, which providers inherit."""
    if cfg["path"]:
        dirs = [os.path.expanduser(d) for d in cfg["path"]]
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])


def main(argv=None):
    args = build_parser().parse_args(argv)
    now = time.time()
    try:
        if args.command == "config":
            return cmd_config(args)
        try:
            cfg, config_error = config.load(), None
        except config.ConfigError as e:
            if not (args.command == "status" and args.gui):
                raise
            cfg, config_error = e.partial, str(e)
        apply_path(cfg)
        if args.command == "status":
            return cmd_gui(args, cfg, config_error, now) if args.gui else cmd_status(args, cfg, now)
        if args.command == "check":
            return cmd_check(args, cfg, now)
        return cmd_providers(cfg, now)
    except (UsageError, config.ConfigError) as e:
        print(f"trai: error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except Exception:  # Python's default exit code 1 would read as "warn"
        traceback.print_exc()
        print("trai: internal error", file=sys.stderr)
        return EXIT_UNKNOWN


def run():
    sys.exit(main())
