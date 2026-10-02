"""Provider discovery and execution."""

import json
import os
import signal
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import config
from .model import ValidationError, parse_json, validate_output

BUILTIN_DIR = Path(__file__).parent / "providers"
DEFAULT_COLOR = "#888888"
DEFAULT_TIMEOUT = 30


@dataclass
class Provider:
    id: str
    path: Path
    source: str  # "built-in" | "user"
    name: str = ""
    color: str = DEFAULT_COLOR
    timeout: float = DEFAULT_TIMEOUT
    problem: str | None = None

    @property
    def fetch(self):
        return self.path / "fetch"


def user_dir():
    return config.config_dir() / "providers"


def _load(path, source):
    provider = Provider(id=path.name, path=path, source=source, name=path.name)
    try:
        with open(path / "provider.toml", "rb") as f:
            manifest = tomllib.load(f)
    except FileNotFoundError:
        provider.problem = "provider.toml missing"
        return provider
    except (OSError, tomllib.TOMLDecodeError) as e:
        provider.problem = f"provider.toml: {e}"
        return provider

    name, color, timeout = manifest.get("name"), manifest.get("color", DEFAULT_COLOR), manifest.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(name, str) or not name:
        provider.problem = "provider.toml: name is required"
    elif not isinstance(color, str) or not config.HEX.match(color):
        provider.problem = 'provider.toml: color must be "#rrggbb"'
    elif isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        provider.problem = "provider.toml: timeout must be a positive number"
    elif not os.access(provider.fetch, os.X_OK):
        provider.problem = "fetch missing or not executable"
    else:
        provider.name, provider.color, provider.timeout = name, color, timeout
    return provider


def discover():
    """Return {id: Provider}, sorted by id; user providers replace built-in ones."""
    found = {}
    for source, base in (("built-in", BUILTIN_DIR), ("user", user_dir())):
        if base.is_dir():
            for path in base.iterdir():
                if path.is_dir() and not path.name.startswith((".", "_")):
                    found[path.name] = _load(path, source)
    return dict(sorted(found.items()))


def run(provider):
    """Run a provider's fetch. Returns ("data", data) or ("error", error)."""
    if provider.problem:
        return "error", {"kind": "crash", "message": f"invalid provider: {provider.problem}"}
    try:
        # Own session, so a timeout also kills the CLIs the fetch started.
        proc = subprocess.Popen(
            [str(provider.fetch)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path.home(),
            start_new_session=True,
        )
    except OSError as e:
        return "error", {"kind": "crash", "message": str(e)}
    try:
        stdout, stderr = proc.communicate(timeout=provider.timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            # A process that left the group still holds the pipes; stop waiting for it.
            proc.stdout.close()
            proc.stderr.close()
            proc.wait()
        return "error", {"kind": "timeout", "message": f"no response within {provider.timeout:g}s"}

    if proc.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
        return "error", {"kind": "crash", "message": " ".join(tail) or f"fetch exited with {proc.returncode}"}
    try:
        return validate_output(parse_json(stdout))
    except json.JSONDecodeError:
        return "error", {"kind": "crash", "message": "fetch printed invalid JSON"}
    except ValidationError as e:
        return "error", {"kind": "crash", "message": f"invalid output: {e}"}
