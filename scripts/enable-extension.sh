#!/bin/sh
# Enable the extension now if the running shell knows it already; otherwise
# (fresh install on Wayland) mark it enabled so it loads at the next login.
set -eu
uuid=$1
if gnome-extensions enable "$uuid" 2>/dev/null; then
  echo "enabled $uuid"
  exit 0
fi
python3 - "$uuid" <<'PY'
import ast, subprocess, sys

uuid = sys.argv[1]

def get(key):
    out = subprocess.run(["gsettings", "get", "org.gnome.shell", key],
                         capture_output=True, text=True, check=True).stdout.strip()
    return ast.literal_eval(out.removeprefix("@as "))

def put(key, value):
    subprocess.run(["gsettings", "set", "org.gnome.shell", key, str(value)], check=True)

enabled, disabled = get("enabled-extensions"), get("disabled-extensions")
if uuid not in enabled:
    put("enabled-extensions", enabled + [uuid])
if uuid in disabled:
    put("disabled-extensions", [u for u in disabled if u != uuid])
PY
echo "installed $uuid; it appears after your next login"
