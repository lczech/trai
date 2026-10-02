# Writing a provider

A provider is a directory named after its id:

```
~/.config/trai/providers/<id>/
  provider.toml
  fetch
```

trai looks in `~/.config/trai/providers/` first, then in its built-in providers. A user provider with the same id as a built-in one replaces it.

## provider.toml

```toml
name = "My LLM"       # required
color = "#7c6cf0"     # ring colour, default "#888888"
timeout = 30          # seconds, default 30
```

## fetch

Any executable (mind the shebang and `chmod +x`). It runs in `$HOME` with no input and must print one JSON object to stdout.

On success:

```json
{
  "short": { "used_percent": 42, "resets_at": "2026-10-02T01:20:00+00:00", "window_minutes": 300 },
  "long":  { "used_percent": 68, "resets_at": "2026-10-03T09:00:00+00:00", "window_minutes": 10080 },
  "windows": [
    { "id": "opus", "label": "Opus 7d", "used_percent": 12, "resets_at": "2026-10-03T09:00:00+00:00", "window_minutes": 10080 }
  ],
  "extra": { "plan": "pro" }
}
```

- `short` (e.g. the 5-hour window) and `long` (e.g. the weekly window) are each optional, but at least one is required. Which one gets the outer ring is up to the config.
- A window needs only `used_percent`, from 0 to 100, always *used*. `NaN` or `Infinity` anywhere make the whole output invalid.
  - `resets_at` is ISO 8601 with a time zone.
  - `label` defaults to a value derived from `window_minutes` (300 → `5h`).
- `windows` is optional and holds other limits, such as per-model caps. Each entry needs an `id`. They are checked against the thresholds but not drawn as rings.
- `extra` is optional and free-form. `plan` and `cli_version` are shown in the popup.

On a failure you can recognise, exit 0 and print:

```json
{ "error": { "kind": "auth", "message": "Not logged in. Run `my-llm login`." } }
```

`kind` is one of:
- `auth`: the user must act.
- `missing`: the CLI is not installed. Hidden unless the provider is listed explicitly in the config.
- `unavailable`: network or server.
- `other`

A non-zero exit, invalid JSON, or exceeding the timeout also counts as a failure. trai keeps showing the last good data, dimmed.

## Example

```sh
#!/bin/sh
used=$(my-llm usage --percent) || {
  echo '{"error": {"kind": "unavailable", "message": "my-llm usage failed"}}'
  exit 0
}
echo "{\"long\": {\"used_percent\": $used, \"window_minutes\": 1440}}"
```

## Testing

```sh
trai providers                  # is it discovered, and is the manifest valid?
trai status --refresh <id>      # run it now
```

The built-in providers in `trai/providers/` are complete examples in Python.
