# Command Code (cmdcode) registry row

Everything a future maintainer needs to know about the `cmdcode` row in
`installer/registry.json`. It is **unverified**: nothing has been exercised end
to end yet, because the account is not subscribed and `cmd status` currently
reports not authenticated. Treat every claim here as researched-but-untested.

---

## What Command Code is

[Command Code](https://commandcode.ai) (`cmd`) is a coding agent CLI (a
Windsurf-style fork lineage) with a free tier, a GOAT tier for its bundled
model, and paid tiers up to "Max 20x". It reaches Omnigent here through a
**third-party ACP bridge plus a generated shim script**, not a native harness.

## Why the bridge + shim route, not a fork or a native harness

`cmd-acp` (npm, v0.2.0, ~9 commits, last published 2026-08-06, itself a fork of
`soycanopa/cmd-acp`) bridges ACP to `cmd -p`, spawning **one `cmd -p` process
per prompt**. There is no native Omnigent harness for `cmd`, and forking the
bridge is the thing we are trying to *avoid* maintaining. The bridge already
honours the `CMD_BIN` env var to pick the binary it spawns (`dist/index.js`,
`resolveCmdBinary`) and passes the parent env through to it, so rather than
patching the fork, og points `CMD_BIN` at a generated shim.

Why the shim is needed at all: the bridge exposes model, reasoning effort and
permission mode **only** through the ACP method `session/set_config_option`,
which Omnigent never sends (the `acp_command` docstring in `og_install.py`
traces the wire). The session config therefore starts **empty** — no `--model`
and, critically, **no `--yolo`**. Headless runs still go through Command Code's
permission engine, so without `--yolo` a worker returns `end_turn` having
changed nothing: the "completed with no output" signature. The shim injects the
flags the bridge never will.

## The shim

The registry row's `shim` block causes the installer to generate a script named
`cmd-og` at `CMD_BIN`. Byte-identical contents:

```sh
#!/bin/sh
exec cmd "$@" ${CMD_MODEL:+--model "$CMD_MODEL"} --yolo --tools-all --skip-onboarding
```

JSON-escaped in the registry, the `"$@"` and `"$CMD_MODEL"` quotes become
`\"`; the parsed value round-trips byte-identical to the shell above.

What each flag is for:

| Piece | Purpose |
|---|---|
| `exec cmd "$@"` | Replace the shell with `cmd`, forwarding the ACP session args. |
| `${CMD_MODEL:+--model "$CMD_MODEL"}` | When og exports a pinned model as `CMD_MODEL`, pass it as `--model`. Empty when unpinned, in which case `cmd` runs its own default model — a **successful** run, not an error, which is why `silent_model_failure` is `true`. |
| `--yolo` | Bypass Command Code's permission engine so a headless worker can actually change files. Without it the worker "completes" having changed nothing. |
| `--tools-all` | Headless runs otherwise withhold some tools; this re-exposes them. |
| `--skip-onboarding` | Command Code otherwise runs a taste-onboarding step on first launch. |

The alternative to the shim — settings `permissions.defaultMode = yolo` in
`~/.commandcode/settings.json` — applies globally to the user's **interactive**
cmd too and cannot carry `--tools-all`. The shim keeps it per-worker. Unlike
freebuff/blink there is no long-lived process to reap: each prompt is one
`cmd -p` that exits.

## Vendor-collision table vs the current roster

`vendor` must follow the pinned model (this is the freebuff rule, applied the
same way). The default pin `moonshotai/kimi-k3` is chosen because **no other
roster member uses the moonshot vendor**. Pinning something else changes the
cross-vendor review outcome:

| `cmdcode` pin | Vendor implied | Same-vendor with roster member |
|---|---|---|
| `moonshotai/kimi-k3` (default) | moonshot | *(none — safe)* |
| `deepseek/*` | deepseek | **Cline** |
| `z-ai/*` | z-ai | **Freebuff** |
| `meta/muse-spark-*` | meta | **OpenCode's** pin |
| `claude-*` | anthropic | the **orchestrator** (Claude Code) |
| `gpt-*` / `codex` / `o1`+ | openai | an **OpenAI** reviewer |

Id form matters: open-weight models are provider-prefixed
(`moonshotai/…`, `deepseek/…`, `z-ai/…`) but Anthropic and OpenAI ids are
**bare** (`claude-sonnet-5`, `gpt-5.5`).

## Verify before trusting

The row is `unverified: true`; nothing has been exercised end to end. Before
flipping it:

- [ ] `cmd login` in a shell actually authenticates (see the login note below).
- [ ] A real dispatch produces a **real commit** in scope with gates green —
      an empty transcript means the pin or shim is wrong, not that it refused.
- [ ] With `CMD_MODEL` set to the preferred pin, `cmd` runs *that* model
      (`--model` reached it through the shim).
- [ ] `--yolo` and `--tools-all` both reached `cmd` (the worker changed files
      without any permission card).
- [ ] The first `cmd -p` boot that omits `--skip-onboarding` would hang/fail;
      confirm onboarding was skipped on a fresh install.
- [ ] The quota-exhaustion string `You've reached your 5-hour usage limit.`
      appears in real transcript output when dry.

### Open discrepancy: the login command

`cmd --help` documents `cmd login`, but `cmd status` tells the user to run
`cmd auth login`. The registry row currently records `cmd login`. Whichever is
real must be confirmed (on an authenticated machine) before the row's `login`
field is trusted; this is exactly the sort of thing that only the first real
login settles.