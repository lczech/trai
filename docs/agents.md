# trai for coding agents

Paste this into your `CLAUDE.md` or `AGENTS.md`, and adjust the provider id and the rules to taste:

```markdown
## Usage limits

`trai check <provider>` reports how much of the subscription usage limits is used
(`claude` for Claude Code, `codex` for Codex). Run it before starting a large step,
such as a new task, a long refactor or a big test run, and act on its exit code:

- 0: carry on.
- 1 (warn): finish what you are doing, but don't start large new work.
- 2 (critical): finish the current step cleanly, leave the work in a consistent state,
  write a short status note (what is done, what is next, when the limit resets as shown
  by trai), and stop.
- Anything else: usage can't be determined. Treat it like 2.

Run plain `trai check <provider>`, without `--max-age` or `--refresh`. If the output
says "as of …" or "refresh failed", the data is old: don't work around that.
```

Notes:

- `trai check --help` explains everything, so agents can look it up themselves.
- Agents can be more careful than your panel without changing the config, e.g. `trai check claude --critical 85`.
- Results are cached for `refresh_seconds` (default 3 minutes), so frequent checks are cheap.

## Sandboxed and unattended agents

A sandboxed agent usually can't refresh: the cache and the CLIs' own state (`~/.claude.json`, `~/.codex`) are read-only to it. Don't open those up; refresh from outside instead. `trai check` then reports the cached data until it is stale (2 × `refresh_seconds`), and unknown after that.

The panel refreshes only while you're logged in and the screen is unlocked. For runs while you're away, install the timer (see the README):

```sh
make install-timer INTERVAL=30
```

It ticks every `INTERVAL` seconds and fetches whenever the cache is older than `refresh_seconds`. So the data an agent sees is at most about `refresh_seconds` + `INTERVAL` old; `refresh_seconds = 120` suits active agents. Each fetch starts the `claude` and `codex` CLIs, which takes a second or two of CPU. Fetching much more often than every 2 minutes risks being rate-limited by the provider. Check the timer with `systemctl --user list-timers trai-refresh.timer`.
