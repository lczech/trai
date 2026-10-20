# trai design

trai shows LLM subscription usage (e.g. 5h and weekly limits) in the GNOME top bar and as a CLI for humans and coding agents.

Two parts, plus an optional timer:

- **`trai`**: Python CLI (stdlib only, Python ≥ 3.11). Runs providers, caches results, reads the config.
- **GNOME extension**: draws one pill per provider. Gets everything from `trai status --gui`.
- **Timer** (`make install-timer`): a systemd user timer that runs `trai status` every `INTERVAL` seconds, so the cache stays fresh for callers that can't refresh (sandboxed agents) while the extension is off (screen locked).

## Providers

A provider is a directory `<id>/` containing:

- `provider.toml`
  ```toml
  name = "Claude"       # required
  color = "#d97757"     # ring colour, default "#888888"
  timeout = 30          # seconds, default 30
  ```
- `fetch`: an executable in any language, run with cwd `$HOME`. Prints one JSON object to stdout.

The id is the directory name. Search order is `~/.config/trai/providers/`, then the built-in providers. A user directory replaces a built-in one with the same id completely.

### `fetch` output on success

```json
{
  "short":   { "label": "5h", "used_percent": 42, "resets_at": "2026-10-02T01:20:00+00:00", "window_minutes": 300 },
  "long":    { "label": "7d", "used_percent": 68, "resets_at": "2026-10-03T09:00:00+00:00", "window_minutes": 10080 },
  "windows": [ { "id": "seven_day_opus", "label": "Opus 7d", "used_percent": 12, "resets_at": "…", "window_minutes": 10080 } ],
  "extra":   { "plan": "pro" }
}
```

- `short` and `long` (by window length, e.g. 5h and 7d) are each optional, but at least one is required. Providers say which window is which; `display.outer_ring` decides which one gets the outer ring.
- Window fields:
  - `used_percent` is required: a number from 0 to 100, always *used*. `NaN` and `Infinity` are rejected anywhere in the output.
  - `resets_at` is ISO 8601 with a time zone.
  - `label` defaults to a value derived from `window_minutes` (300 → "5h", 10080 → "7d", 1440 → "1d").
  - Items in `windows` also require an `id`.
- `windows` holds additional limits, such as per-model caps. They are checked against the thresholds but not drawn as rings.
- `extra` is free-form and passed through as is. Conventional keys:
  - `plan` (string)
  - `overage`: `{enabled, used, limit, currency}`
  - `cli_version` (string): version of the CLI the provider asked, shown in the popup.

### `fetch` output on a failure it recognises

The provider exits with 0 and prints:

```json
{ "error": { "kind": "auth", "message": "Claude CLI not logged in. Run `claude`." } }
```

`kind` is one of:

- `auth`: the user must act.
- `missing`: the CLI is not installed.
- `unavailable`: network or server problem.
- `other`

trai adds kinds of its own:

- `timeout`
- `crash`: non-zero exit, invalid JSON, output that fails validation, or a broken provider directory. The message is the tail of stderr, or the reason.
- `cache`: the cache can't be written, so this caller can't refresh (see [Cache](#cache)).

trai never retries; the next refresh is the retry.

## Cache

Each provider has `$XDG_CACHE_HOME/trai/<id>.json`: `{"version", "success": {"data", "at"}, "error": {"kind", "message", "at"}}`. An error never overwrites the last success. Entries with another `version` are ignored and refetched.

Refreshes take a per-provider `flock` on `<id>.lock`.

- A caller gets the cache immediately if the last refresh attempt (success or failure) is younger than `--max-age`. The default max age is `refresh_seconds`, and `refresh_seconds / 2` for `--gui`, so the extension's own timer never skips a refresh.
- Otherwise the caller refreshes. If a refresh is already running, it waits for that one instead of starting a second.
- `--refresh` is the same as `--max-age 0`.
- A caller waits for the lock at most the provider's timeout plus 10s. After that it gets the current cache with a `timeout` error, so a stuck process (e.g. a paused terminal) can't freeze everyone.
- On timeout, the fetch's process group is killed; a child that escaped the group is abandoned after 2s instead of being waited for.
- If the cache can't be written (e.g. a sandboxed agent with a read-only home), the caller gets the current cache with a `cache` error and fetches nothing. Such callers rely on someone else refreshing: the panel or the timer.

Data is **stale** if the last refresh failed, or if it's older than 2 × `refresh_seconds`.

## Config

`$XDG_CONFIG_HOME/trai/config.toml`. trai never writes it automatically. A missing file or key means the default. `trai config init` writes a template with every line commented out.

Each key is validated on its own. An invalid key is an error (exit 4 on the CLI), but `--gui` keeps every valid key, falls back to the default only for the broken ones, and reports them as `config_error`.

```toml
providers = ["claude", "codex"]   # enabled + order
refresh_seconds = 180
path = []                         # dirs searched for provider CLIs before PATH, ~ expanded
show = "used"                     # "used" | "remaining"; affects rings and numbers

[thresholds]                      # on used percent, regardless of `show`
warn = 75
critical = 90

[display]
show_name = false
show_labels = false
divider = true
name_color = "theme"              # "theme" | "theme-dim" | "provider" | "#rrggbb"
label_color = "theme-dim"
value_color = "theme"
warn_color = "#e5a50a"            # replaces value_color only
critical_color = "#e01b24"
notify_warn = false
notify_critical = true
outer_ring = "short"              # "short" | "long": which window gets the outer ring
panel_box = "right"               # "left" | "center" | "right"
panel_index = 0                   # 0 = leftmost within that box

[display.ring]
outer_diameter = 24
outer_width = 3
inner_diameter = 15
inner_width = 3                   # single-window providers use the outer values

[colors]                          # ring colour per provider id
codex = "#10a37f"
```

If `providers` is missing: all discovered providers are used in alphabetical order, and providers whose latest result is `missing` are hidden. If `providers` is set: exactly those, in that order, always shown.

## CLI

```
trai status [ID…] [--json | --gui] [--max-age S | --refresh]
trai check  [ID…] [--warn N] [--critical N] [--max-age S | --refresh]
trai providers                 # id, source (built-in/user), active, last status
trai config init | path
```

**Levels:** each window is `ok`, `warn` or `critical`, based on used percent against the thresholds. A provider's level is the worst of all its windows, including `windows`. A provider with no usable data is `unknown`.

**`trai check`** prints one line per provider, e.g.:

```
claude: WARN · 5h 82% (resets in 1h 12m) · 7d 61% (resets Fri 10:00)
```

Its exit code is the most urgent level across the providers (critical before unknown before warn):

| Exit code | Meaning |
|---|---|
| 0 | ok |
| 1 | warn |
| 2 | critical |
| 3 | unknown (no data, an internal error, or a failed refresh with data older than the usable age) |
| 4 | usage or config error |

argparse's default exit code 2 must be overridden, since 2 means critical. Likewise, an internal error exits 3, not Python's default 1, which would read as warn.

After a failed refresh, data counts until it is stale: the **usable age** is 2 × `refresh_seconds`. So a caller that can't refresh itself (e.g. a sandboxed agent) still gets a level between two refreshes by the panel or the timer. With an explicit `--max-age` or `--refresh`, the usable age is that max age.

Data older than `refresh_seconds` gets a note, whatever the reason (failed refresh, large `--max-age`), so a reading never looks fresher than it is:

```
codex: OK · 5h 32% (resets in 2h 19m) · 7d 5% (resets Wed 10:55)  (as of 3 min ago; refresh failed (cache): …)
```

**`--json`** returns data only:

```json
{
  "schema": 1,
  "providers": [{
    "id": "claude", "name": "Claude", "level": "warn", "stale": false,
    "fetched_at": "…", "error": null,
    "short": { "…": "…", "level": "warn" }, "long": { "…": "…" }, "windows": [], "extra": {}
  }]
}
```

The rule for `schema`: adding fields keeps the number, renaming or removing fields bumps it.

**`--gui`** returns the same provider list (only the displayed providers), plus:

- `gui_schema`
- the resolved config: `refresh_seconds`, `show`, `thresholds`, `display`, and each provider's colour
- `config_error`: null, or a message

It only produces output; it doesn't open anything.

## GNOME extension

- UUID `trai@lczech.github.io`. GNOME Shell 46–50, ESM.
- **Finding trai:** `~/.local/bin/trai`, then `PATH`. A GSettings key `trai-path` (no prefs UI) overrides both. If not found, the pill shows ⚠ and the message.
- **Refresh:** runs `trai status --gui` asynchronously (`Gio.Subprocess`) every `refresh_seconds`, starting at 180 until the first response. Config changes apply on the next refresh. A watchdog kills a run that takes over 120s.
- **trai failing** (not found, crash, invalid output, watchdog): the pills keep the last data, dimmed with ⚠, and the popup shows the error. Without earlier data, a single grey "trai ⚠" pill.
- **Layout:**
  - One container in the panel box `panel_box` at `panel_index` holds all pills in config order.
  - Each pill is its own button with its own popup.
  - No permanent background, just the native hover highlight.
- **Pill:** rings, then `[name] [label] value │ [label] value`, short first. Optional parts follow the config.
- **Rings:**
  - Concentric. `outer_ring` decides which window is outside (default `short`); a single window gets one ring with the outer geometry.
  - Track: provider colour at ~25% opacity. Fill: from 12 o'clock, clockwise, round caps, capped at 100%, provider colour.
  - With `show = "remaining"`, the fill is 100 − used.
- **States:**
  - Stale or error: everything dimmed to ~50%, ⚠ after the numbers.
  - An entry in `windows` at warn or critical: a small dot in that colour at the top right of the rings.
- **Popup:**
  - Error message, if any.
  - One line per window (short, long, `windows`): label, value, reset time.
  - `extra.plan` and `extra.cli_version`, if present.
  - "Updated N min ago".
  - "Refresh now", which runs `--gui --refresh`.
- **Notifications:** remember the last level of each window in memory. Notify when it rises (to warn if `notify_warn`, to critical if `notify_critical`). The memory is lost when the extension is disabled, which GNOME does on every screen lock, so a window that is still critical notifies again after unlocking. A failing notification is logged and doesn't affect the pills.

## Built-in providers

Each `fetch` separates fetching from a pure `parse(response) -> output` function. `parse` tries the known response formats newest first, so users on older CLI versions keep working; each format has a recorded fixture in `tests/fixtures/`. Both report the CLI's `--version` as `extra.cli_version`.

**claude**

- Finding the binary, in order:
  1. `PATH`
  2. `~/.local/bin/claude`
  3. `~/.claude/local/claude`
  4. the newest `~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude`
- Call: `claude --print --verbose --input-format stream-json --output-format stream-json --no-session-persistence --strict-mcp-config --setting-sources ""`. The last three skip session files, MCP servers, hooks and plugins; if the CLI doesn't know them ("unknown option"), it retries without. Stdin is `{"type":"control_request","request_id":"1","request":{"subtype":"get_usage"}}`. Read the `control_response`. No model is called (`total_cost_usd: 0`). Like any Claude Code start, it rewrites `~/.claude.json`.
- Mapping, newest format first:
  1. `rate_limits.limits[]`: kind `session` → short, `weekly_all` → long, any other kind → `windows` (id `<kind>_<scope>`, label e.g. "Opus 7d"). `scope` is a string in older CLIs and an object (`{"model": {"display_name": "Fable", …}, "surface": …}`) in newer ones; the scope name joins the display names.
  2. Fallback if that yields neither: `five_hour` → short, `seven_day` → long, non-null `seven_day_*` → `windows`.
  - `subscription_type` → `extra.plan`, `extra_usage` → `extra.overage`.
- `rate_limits_available: false` → `other` ("no subscription rate limits, API key auth?").

**codex**

- Finding the binary: `PATH`, `~/.local/bin`, `/usr/local/bin`.
- Call: `codex app-server`, JSON-RPC over stdio: `initialize`, then `account/rateLimits/read`. Retry once after 0.5s on "authentication required". The pipe is read unbuffered, since lines in a read-ahead buffer are invisible to `select()`. An account query, no model is called.
- Mapping:
  - `primary` → short, `secondary` → long. Labels come from `windowDurationMins`; `resetsAt` (epoch) is converted to ISO.
  - Other entries of `rateLimitsByLimitId` → `windows`.
  - `planType` → `extra.plan`, `credits` → `extra.overage`, `rateLimitResetCredits.availableCount` → `extra.reset_credits`.

## Repo layout

```
trai/                 Python package: cli, config, providers, cache, model
trai/providers/       claude/, codex/
gnome-extension/      metadata.json, extension.js, stylesheet.css, schemas/
tests/                unittest; tests/providers/ (fake: ok, short-only, error-auth, missing, crash, hang, garbage, scripted)
tests/fixtures/       recorded, anonymised claude and codex responses
docs/                 design.md, providers.md, agents.md
scripts/              nested.sh (make dev), enable-extension.sh, screenshots/ (make screenshots)
Makefile              install, uninstall, test, dev, screenshots
```

`make install` copies the package to `~/.local/share/trai/`, puts a launcher at `~/.local/bin/trai`, and installs and enables the extension. `pyproject.toml` allows `pipx` or `uv tool install` as an alternative.

`docs/agents.md` is a snippet to paste into CLAUDE.md or AGENTS.md:

- Run `trai check <id>` before large steps.
- On 1, don't start large work.
- On 2, finish the current step cleanly, leave the work consistent, write a status note with the reset time, and stop.

It must never mention committing.

## Not in v1

AppIndicator or KDE frontend, multiple accounts per provider, burn-rate projection, `trai wait`, config file watching, extensions.gnome.org.

License: MIT. The README stays short and credits the projects in `sources/` as prior art (ideas only, no code).
