# og stats — per-agent quota/capacity reporter

`og stats` answers one question before a dispatch: **which coder has room to
run?** It builds the lineup from `og-install.json` (orchestrator, coders by
priority, reviewer last), finds each agent's quota probe in its registry row's
`quota` block, runs the probes concurrently, and prints one row per agent.

```bash
og stats                        # installed lineup, live probes
og stats --all                  # every registry agent, not just installed
og stats --agent opencode        # one agent only
og stats --json                 # machine-readable merged view
og stats --no-probe             # stored records only, no network (offline)
og stats --mark kilo dry --until +2h --reason "saving for the big run"
og stats --clear kilo
```

## Columns

| Column    | Meaning |
|-----------|---------|
| role      | orchestrator / coder / reviewer (from the install state) |
| agent     | agent id |
| state     | `ok`, `dry`, or `unknown` |
| remaining | quota left, e.g. `60/100%`, `12.5 credits`, `0 Freebucks` |
| reset     | next window reset, local time with a relative hint (`in 45m`) |
| tier      | how the number was obtained (below) |
| source    | which probe produced it, `mark`, or `<probe> (cached)` |
| age       | how old the record is |
| detail    | one-line human note, never a secret |

`--json` prints `{"version": 1, "checked_at": ISO, "agents": {id: record +
"role" + "priority"}}`. Record fields: `state`, `tier`, `remaining`, `limit`,
`unit`, `windows[{name, used_percent, reset_at}]`, `reset_at`, `source`,
`checked_at`, `detail`.

## Tiers

- **measured** — read from the vendor (OAuth usage endpoint, CLI, dashboard),
  or from the vendor tool's own printed output (freebuff launch-budget: the
  balance freebuff last reported in a transcript or runner log).
- **unknown** — no credential, no endpoint answer, no reported balance since
  the daily reset, no budget configured.

## Probes (one per agent)

| Probe | Agent | How |
|-------|-------|-----|
| anthropic-oauth | claude | Keychain/file OAuth token → Anthropic usage endpoint (5h + 7d windows; 5h binds) |
| codex-wham | codex | `~/.codex/auth.json` (or opencode's copy) → wham usage (primary binds) |
| antigravity | agy | `antigravity-accounts.json` cachedQuota, worst family binds |
| kilo-profile | kilo | `kilo profile --json` balance |
| cursor-dashboard | cursor | token from `state.vscdb` → current-period usage |
| deepseek-balance | cline | `GET user/balance`, needs `$DEEPSEEK_API_KEY` |
| launch-budget | freebuff | last balance freebuff itself printed (chat.db transcripts + newest runner logs); `unknown` when nothing reported since the daily reset — og never subtracts a per-launch cost |
| (none) | opencode, kiro | known limit, not measurable: row shows the registry `quota.note` |

## Marks

`--mark <id> dry [--until ISO8601|+2h|+30m|+1d] [--reason ...]` forces an
agent `dry` until the expiry (default: no expiry); `--clear <id>` removes it.
An expired mark is ignored and pruned on the next state write. The
orchestrator treats a marked agent as unavailable and dispatches around it —
use marks to hold quota back for a planned run. A probe result of `dry`
(measured exhaustion) behaves the same way without any mark.

## Files and overrides

- State: `$OMNIGENT_HOME/og-quota.json` (last-good records + marks, versioned,
  written atomically). A 429 or probe failure reuses the last-good record if
  it is under 30 minutes old, flagged as `<probe> (cached)`.
- `OG_REGISTRY=/path/to/registry.json` points at an alternate catalog
  (default: the repo's `installer/registry.json`).

## Privacy

Read-only GETs (one dashboard POST for cursor); never prints or logs token
values; never refreshes OAuth tokens — a refresh would rotate the user's real
session. Missing credentials report `unknown`, never an error.

## Limitations

- OAuth-gated probes only work where the user is logged in (same machine).
- 5h/primary windows bind by design; a nearly-spent weekly window is visible
  in `detail` but does not flip `state` on its own.
- Cached records are at most 30 minutes old; anything older reports `unknown`.
- The freebuff balance is observed, not computed: freebuff bills per hour at a
  per-model rate and launches from anywhere, so counting launches and
  subtracting a flat cost produced confident wrong numbers. A balance older
  than the last local midnight reads as `unknown` (the pool resets daily).
