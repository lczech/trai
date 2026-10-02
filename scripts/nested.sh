#!/bin/sh
# Run a nested GNOME Shell with this checkout's extension, isolated from your
# real session's settings. Your trai config is shared.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
uuid=trai@lczech.github.io
tmp=$(mktemp -d)

# Session services (e.g. ibus-daemon) outlive the nested shell and spin once
# its bus is gone, so stop every process that carries this session's env.
cleanup() {
  for env in /proc/[0-9]*/environ; do
    { tr '\0' '\n' < "$env"; } 2>/dev/null | grep -qxF "XDG_DATA_HOME=$tmp/data" &&
      kill "$(basename "$(dirname "$env")")" 2>/dev/null
  done
  rm -rf "$tmp"
}
trap cleanup EXIT INT TERM

ext="$tmp/data/gnome-shell/extensions/$uuid"
mkdir -p "$ext" "$tmp/config"
cp -r "$repo/gnome-extension/." "$ext/"
glib-compile-schemas "$ext/schemas"
ln -s "${XDG_CONFIG_HOME:-$HOME/.config}/trai" "$tmp/config/trai"

mode=
[ -e /usr/share/gnome-shell/modes/ubuntu.json ] && mode=--mode=ubuntu
export XDG_DATA_HOME="$tmp/data" XDG_CONFIG_HOME="$tmp/config"
export MUTTER_DEBUG_DUMMY_MODE_SPECS="${MUTTER_DEBUG_DUMMY_MODE_SPECS:-1600x900}"
dbus-run-session -- sh -c "
  gsettings set org.gnome.shell enabled-extensions \"['$uuid']\"
  gsettings set org.gnome.shell welcome-dialog-last-shown-version '999'
  exec gnome-shell --nested --wayland $mode"
