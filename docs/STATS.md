# og stats — per-agent quota/capacity reporter

`og stats` answers one question before a dispatch: **which coder has room to
run?** It builds the lineup from `og-install.json` (orchestrator, coders by
priority, reviewer last), finds each agent's quota probe in its registry row's
`quota` block, runs the probes concurrently, and prints one row per **agent
instance**. A harness installed twice gets two rows and two independent
records: the first keeps the registry id (`opencode`), later ones get a
pin-derived id (`opencode@cmdcode`, or `opencode-2` without a provider pin).

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
| agent     | agent instance id (`opencode`, `opencode@cmdcode`, …) |
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
- **assumed** — the binding quota family/pool was identified from the agent's
  pinned model, not measured (kilo pinned to the free router: the paid credit
  balance does not bind it, and the free tier exposes no queryable remaining,
  so `remaining` is `-`).

## Probes (one per agent)

| Probe | Agent | How |
|-------|-------|-----|
| anthropic-oauth | claude | Keychain/file OAuth token → Anthropic usage endpoint (5h + 7d windows; 5h binds) |
| codex-wham | codex | `~/.codex/auth.json` (or opencode's copy) → wham usage (primary binds) |
| antigravity | agy | `antigravity-accounts.json` cachedQuota, worst family binds |
| kilo-profile | kilo | `kilo profile --json` balance; a `…/free` pin does not draw on the paid balance, so the balance is reported but does not bind |
| cursor-dashboard | cursor | token from `state.vscdb` → current-period usage |
| deepseek-balance | cline | `GET user/balance`, needs `$DEEPSEEK_API_KEY` |
| launch-budget | freebuff | last balance freebuff itself printed (chat.db transcripts + newest runner logs); `unknown` when nothing reported since the daily reset — og never subtracts a per-launch cost. The observation is taken only from freebuff worker sessions' own error output, and anything else — including og's own reports — is ignored by design |
| (none) | opencode, kiro, cmdcode | known limit, not measurable: `og stats` shows the registry `quota.note`; `og start` runs the row's `quota.liveness` probe instead |

## Marks

`--mark <id> dry [--until ISO8601|+2h|+30m|+1d] [--reason ...]` forces an
agent **instance** `dry` until the expiry (**default: +1h**); `--clear <id>`
removes it. Use the id the table prints: marking `opencode@cmdcode` leaves the
other OpenCode instance untouched.
An expired mark is ignored and pruned on the next state write. The
orchestrator treats a marked agent as unavailable and dispatches around it —
use marks to hold quota back for a planned run. A probe result of `dry`
(measured exhaustion) behaves the same way without any mark.

The default used to be "no expiry", which is how one `Rate limit exceeded`
mark kept an agent out of the roster for a week: nothing ever overrode it for
a quota model with no queryable balance. Every mark now has a deadline, and
`og start` refreshes the evidence (below).

## Startup probe (`og start` / `og restart`)

Before any session opens, `og start` probes every agent in the installed
lineup once, concurrently, with a per-agent timeout (20s for liveness, the
probe's own 8s otherwise). The probe can never abort or hang a start; its
exit status is ignored.

- **Measurable quota** (`quota.probe` in the registry): run exactly as
  `og stats` runs it.
- **No queryable quota** (`probe: null`), or an `assumed` reading such as
  Kilo's free router: run a **liveness** probe — one tiny real request through
  the agent's own CLI on the model the install pinned. The command shape comes
  from the registry row's `quota.liveness.argv` (`{model}` is replaced by the
  pin in `og-install.json`, falling back to the registry's `model.prefer`);
  nothing user-specific is hardcoded.
- **A liveness probe never bills.** It runs only when the exact pin appears in
  the registry row's `quota.liveness.free_models` allow-list — ids the registry
  and docs document as free tier (OpenCode Zen's free pins, Kilo's
  `kilo/kilo-auto/free`). An id's shape is never evidence: a paid model can be
  named `…-free`. An unlisted pin (Command Code's credits, Kiro's `auto`)
  reports `skipped: model not verified free` and the mark is left alone.
- The request runs with stdin closed and its own session/process group, in a
  fresh empty temporary directory, never the caller's cwd. On timeout the
  whole group gets SIGTERM, then SIGKILL after a short grace, so a grandchild
  the CLI spawned cannot survive. No action-enabling flags (`--yolo`,
  `--tools-all`) are passed, and the prompt forbids tools.
- A known quota string from `quota.liveness.failure_patterns` means `dry`, but
  ONLY when the request failed (non-zero exit, or no reply). A successful
  reply that merely mentions a quota phrase is `ok`. The reset clause supports
  `2d`, `1d 3h`, `2h 41m`, `45m` and `30s`; a zero or unparsable reset falls
  back to +1h.
- A missing binary reports `unavailable`, an auth error or timeout reports
  `error`, and a paid pin reports `skipped`, with the first error line or the
  reason. None of them is `dry`: a broken or unaffordable probe is not evidence
  of exhaustion, and none writes or clears a mark. They persist as `unknown` in
  `og-quota.json` (the record schema has no error/skip state), so
  `og stats --no-probe` renders them as `unknown`, not as a capacity claim.
- An `ok` result clears any existing dry mark and records the probe time.

One compact line per agent is printed, e.g.:

```
  opencode     ok           ok (reply received)
  cmdcode      dry          dry: You've reached your 5-hour usage limit. Resets in 2h 41m (until 10-13 14:12)
```

Skip the probe with `og start --no-probe` or `OG_START_PROBE=0` (offline or
metered use). `og restart` runs it too, because restart is stop + start.

## Files and overrides

- State: `$OMNIGENT_HOME/og-quota.json` (last-good records + marks, versioned,
  written atomically). Writers serialise on a sidecar `og-quota.json.lock`
  (`flock`): `og stats --mark`/`--clear`, `og stats`'s own probe save, and the
  start probe each lock around load→merge→save, and probe saves merge per
  agent — so a mark set while a probe ran is not lost. A 429 or probe failure
  reuses the last-good record if it is under 30 minutes old, flagged as
  `<probe> (cached)`.
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
