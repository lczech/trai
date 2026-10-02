"""Per-provider result cache, shared between all trai processes."""

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import provider as provider_mod

# Bump when the stored data changes shape; older entries are then refetched.
VERSION = 2
# How much longer than a provider's timeout to wait for another process's refresh.
LOCK_GRACE = 10


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "trai"


def _empty():
    return {"success": None, "error": None}


def load(provider_id):
    """{"success": {"data", "at"} | None, "error": {"kind", "message", "at"} | None}"""
    try:
        with open(cache_dir() / f"{provider_id}.json") as f:
            entry = json.load(f)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(entry, dict) or entry.get("version") != VERSION:
        return _empty()
    return {"success": entry.get("success"), "error": entry.get("error")}


def _save(provider_id, entry):
    path = cache_dir() / f"{provider_id}.json"
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump({**entry, "version": VERSION}, f)
    os.replace(tmp, path)


@contextmanager
def _lock(provider_id, timeout):
    """Yield True once the lock is held, or False after `timeout` seconds."""
    cache_dir().mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(cache_dir() / f"{provider_id}.lock", "w") as f:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
        yield True


def last_attempt(entry):
    times = [part["at"] for part in (entry["success"], entry["error"]) if part]
    return max(times) if times else None


def is_failed(entry):
    """True if the most recent refresh failed."""
    error, success = entry["error"], entry["success"]
    return bool(error) and (not success or error["at"] >= success["at"])


def get(provider, max_age):
    """Cached entry if a refresh was attempted within max_age seconds, else refresh.

    Concurrent callers share one refresh: whoever waits on the lock reuses the
    result of the refresh that held it. If the lock stays taken (e.g. a stopped
    process holds it), the current cache is returned as is.
    """
    cutoff = time.time() - max_age
    entry = load(provider.id)
    if (last_attempt(entry) or -1) >= cutoff:
        return entry
    with _lock(provider.id, provider.timeout + LOCK_GRACE) as locked:
        entry = load(provider.id)
        if not locked:
            message = "another trai process has been refreshing too long"
            return {**entry, "error": {"kind": "timeout", "message": message, "at": time.time()}}
        if (last_attempt(entry) or -1) >= cutoff:
            return entry
        kind, payload = provider_mod.run(provider)
        now = time.time()
        if kind == "data":
            entry = {"success": {"data": payload, "at": now}, "error": None}
        else:
            entry["error"] = {**payload, "at": now}
        _save(provider.id, entry)
    return entry
