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
- 3 (unknown): usage could not be checked. Continue carefully and check again later.
```

Notes:

- `trai check --help` explains everything, so agents can look it up themselves.
- Agents can be more careful than your panel without changing the config, e.g. `trai check claude --critical 85`.
- Results are cached for `refresh_seconds` (default 3 minutes), so frequent checks are cheap. `--refresh` forces fresh data.
