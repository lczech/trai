"""Bounded JSON Lines history of successful provider readings."""

import fcntl
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = 1
WARNING_INTERVAL = 24 * 60 * 60
DAILY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


def history_dir():
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state_home / "trai" / "readings"


def _timestamp(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def _line(provider_id, data, at):
    record = {
        "schema": SCHEMA,
        "provider": provider_id,
        "recorded_at": _timestamp(at),
        "short": data["short"],
        "long": data["long"],
        "windows": data["windows"],
    }
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _append(provider_id, data, at):
    directory = history_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    day = datetime.fromtimestamp(at, timezone.utc).date().isoformat()
    path = directory / f"{day}.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        # O_APPEND makes placement atomic. flock also keeps a rare short write
        # from interleaving with another trai record while it is completed.
        fcntl.flock(fd, fcntl.LOCK_EX)
        remaining = memoryview(_line(provider_id, data, at))
        while remaining:
            written = os.write(fd, remaining)
            if not written:
                raise OSError("history append wrote no data")
            remaining = remaining[written:]
    finally:
        os.close(fd)


def _prune(at, retention_days):
    cutoff = datetime.fromtimestamp(at, timezone.utc).date() - timedelta(days=retention_days - 1)
    for path in history_dir().iterdir():
        match = DAILY_FILE.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        try:
            file_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _append_and_prune(provider_id, data, at, retention_days):
    """Serialize appends with pruning so an active daily file cannot be removed."""
    directory = history_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(directory / ".history.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _append(provider_id, data, at)
        try:
            _prune(at, retention_days)
        except OSError as error:
            return error
    return None


def _emit_warning(message, warning_dir, now):
    """Warn at most once per interval, or every time if throttling cannot persist."""
    text = f"trai: warning: {message}"
    try:
        warning_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(warning_dir / "history-warning.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            marker = warning_dir / "history-warning"
            try:
                last = float(marker.read_text())
            except (OSError, ValueError):
                last = None
            if last is not None and 0 <= now - last < WARNING_INTERVAL:
                return
            try:
                marker.write_text(str(now))
            except OSError:
                print(text, file=sys.stderr)
                return
            print(text, file=sys.stderr)
    except OSError:
        print(text, file=sys.stderr)


def record(provider_id, data, at, retention_days, warning_dir):
    """Record one successful fetch without allowing history errors to break it."""
    if retention_days == 0:
        return
    try:
        prune_error = _append_and_prune(provider_id, data, at, retention_days)
    except OSError as error:
        _emit_warning(f"cannot record history: {error}", warning_dir, at)
        return
    if prune_error:
        _emit_warning(f"cannot prune history: {prune_error}", warning_dir, at)
