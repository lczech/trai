#!/bin/sh
# Regenerate docs/screenshots/ in a headless GNOME Shell (>= 46), isolated
# from your real session. Claude/Codex show your real current usage; the other
# providers are fakes from ./providers.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out="$repo/docs/screenshots"
uuid=trai@lczech.github.io
tmp=$(mktemp -d)
session=

stop_shell() {
  [ -n "$session" ] && kill -- "-$session" 2>/dev/null || true
  session=
  sleep 1
  # Session services (e.g. ibus-daemon) outlive the shell and spin once its
  # bus is gone, so stop every process that carries this run's environment.
  for env in /proc/[0-9]*/environ; do
    { tr '\0' '\n' < "$env"; } 2>/dev/null | grep -qxF "XDG_DATA_HOME=$tmp/data" &&
      kill -9 "$(basename "$(dirname "$env")")" 2>/dev/null || true
  done
}
trap 'stop_shell; rm -rf "$tmp"' EXIT INT TERM

ext="$tmp/data/gnome-shell/extensions"
mkdir -p "$ext" "$tmp/config/trai" "$tmp/cache" "$tmp/io" "$out"
cp -r "$repo/gnome-extension" "$ext/$uuid"
glib-compile-schemas "$ext/$uuid/schemas"
cp -r "$here/helper" "$ext/trai-screenshots@local"
cp -r "$here/providers" "$tmp/config/trai/providers"
# The extension runs this checkout's CLI, not the installed one.
printf '#!/bin/sh\nexec /usr/bin/python3 -c "import sys; sys.path.insert(0, \\"%s\\"); from trai.cli import run; run()" "$@"\n' \
  "$repo" > "$tmp/trai"
chmod +x "$tmp/trai"

# Run JS in the shell (see helper/extension.js); `t` holds the helpers.
js() {
  rm -f "$tmp/io/result"
  printf '%s' "$1" > "$tmp/io/cmd.tmp" && mv "$tmp/io/cmd.tmp" "$tmp/io/cmd.js"
  for _ in $(seq 150); do
    if [ -e "$tmp/io/result" ]; then
      result=$(cat "$tmp/io/result")
      case $result in ERROR*) echo "js failed: $result" >&2; exit 1 ;; esac
      printf '%s\n' "$result"
      return
    fi
    sleep 0.2
  done
  echo "js timed out; log: $tmp/shell.log" >&2
  exit 1
}

# Wait until the extension has finished a refresh and shows $1 pills.
wait_pills() {
  for _ in $(seq 60); do
    [ "$(js "return t.ext() && !t.ext()._running && t.pills().length === $1")" = true ] && return
    sleep 0.5
  done
  echo "extension did not show $1 pills; log: $tmp/shell.log" >&2
  exit 1
}

start_shell() {  # SCALE WIDTHxHEIGHT
  stop_shell
  mode=
  [ -e /usr/share/gnome-shell/modes/ubuntu.json ] && mode=--mode=ubuntu
  XDG_DATA_HOME="$tmp/data" XDG_CONFIG_HOME="$tmp/config" XDG_CACHE_HOME="$tmp/cache" \
  TRAI_SHOTS_DIR="$tmp/io" GSETTINGS_SCHEMA_DIR="$ext/$uuid/schemas" \
  setsid dbus-run-session -- sh -c "
    gsettings set org.gnome.shell enabled-extensions \"['$uuid', 'trai-screenshots@local']\"
    gsettings set org.gnome.shell welcome-dialog-last-shown-version '999'
    gsettings set org.gnome.desktop.interface scaling-factor $1
    gsettings set org.gnome.desktop.interface color-scheme prefer-dark
    gsettings set org.gnome.shell.extensions.trai trai-path '$tmp/trai'
    exec gnome-shell --headless --virtual-monitor $2 --wayland --wayland-display trai-shots-$$ $mode
  " > "$tmp/shell.log" 2>&1 < /dev/null &
  session=$!
  [ "$(js 'return "ready"')" = ready ] || { echo "shell did not start; log: $tmp/shell.log" >&2; exit 1; }
  js 't.Main.overview.hide()' > /dev/null
}

set_config() {  # TOML; then refresh and wait for N pills
  printf '%s\n' "$1" > "$tmp/config/trai/config.toml"
  js 't.ext().refresh(true)' > /dev/null
  sleep 0.5
  wait_pills "$2"
}

panel_shot() {  # NAME: the trai pills plus the system area to their right
  js "const [x] = t.root().get_transformed_position();
      return t.shot('$out/$1.png', x - 8, 0, global.stage.width, t.Main.panel.height);" > /dev/null
}

menu_shot() {  # NAME PILL_INDEX: click the pill, shoot its open menu
  js "t.click(t.pills()[$2])" > /dev/null
  sleep 1
  js "const pill = t.pills()[$2];
      if (!pill.menu.isOpen) throw new Error('menu did not open');
      const [x, y] = pill.menu.actor.get_transformed_position();
      const [w, h] = pill.menu.actor.get_transformed_size();
      const done = t.shot('$out/$1.png', x - 20, 0, w + 40, y + h + 20);
      return done.then(path => { pill.menu.close(false); t.park(); return path; });"
  sleep 0.5
}

start_shell 2 3200x1800
set_config 'providers = ["claude", "codex"]' 2
panel_shot default-2x
menu_shot menu-2x 0
set_config 'providers = ["claude", "codex"]
[display]
show_name = true
show_labels = true' 2
panel_shot default-names-2x
set_config 'providers = ["claude", "codex", "hot", "single", "broken"]' 5
panel_shot states-2x
menu_shot menu-error-2x 4
set_config 'providers = ["claude", "codex", "hot"]' 3
menu_shot menu-warn-2x 2
set_config 'providers = ["claude", "codex", "hot"]
[display]
show_name = true
show_labels = true' 3
panel_shot names-2x

start_shell 1 1600x900
set_config 'providers = ["claude", "codex"]' 2
panel_shot panel-1x
echo "screenshots written to $out"
