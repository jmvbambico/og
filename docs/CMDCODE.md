# Command Code (cmdcode) registry row

Everything a future maintainer needs to know about the `cmdcode` row in
`installer/registry.json`. The row was **verified end to end on 2026-09-24**
against a subscribed, authenticated account — see [How it was verified](#how-it-was-verified)
for what was actually exercised and what still has not been.

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

## How it was verified

Verified 2026-09-24 by driving `cmd-acp` over ACP stdio directly (a hand-written
JSON-RPC client: `initialize`, `session/new`, `session/prompt`), against a
subscribed and authenticated account. Two runs, differing only in whether the
shim was in the path:

| Run | `CMD_BIN` | Result |
|---|---|---|
| Control | unset (plain `cmd-acp`) | Agent replied *"I can't complete this — file writes are blocked"*, returned `stopReason: end_turn`, changed **nothing** |
| Treatment | the generated shim, `CMD_MODEL=moonshotai/kimi-k3` | `write_file` tool call ran; the requested file appeared with the requested contents |

The control is the important half. It demonstrates that `--yolo` is
**load-bearing, not a precaution**: without it a Command Code worker reports a
clean `end_turn` having done nothing, which upstream looks exactly like success.
That is the failure mode this whole row is built to avoid.

### Three facts that run produced

1. **`cmd`'s own default model is `deepseek/deepseek-v4-pro`**, read from the
   bridge's `session/new` response (`configOptions` → `model` → `currentValue`).
   An **unpinned** cmdcode worker therefore runs DeepSeek and is silently
   same-vendor with Cline for cross-vendor review. This is why `model.required`
   is `true` and `silent_model_failure` is `true`: an unpinned run is a
   *successful* run on the wrong vendor, not an error.
2. **Command Code writes a `.commandcode/` directory into its cwd** (the
   worktree), the same way freebuff writes `.freebuff/`. It is gitignored;
   without that it turns up in every worker's diff.
3. **`cmd login` is the real command.** `cmd status` tells the user to run
   `cmd auth login`, but there is no `auth` subcommand — `cmd auth --help` falls
   back to the general help, and `cmd help` documents `cmd login`. The row's
   `login` field is correct as written; the CLI's own error message is wrong.

## What is still NOT verified

- **A real dispatch through Omnigent.** The verification above drove the bridge
  directly, not through a live `og` worker session. The first real dispatch is
  still the only proof the harness wiring holds end to end.
- **Quota exhaustion behaviour.** The string `You've reached your 5-hour usage
  limit.` is documented, not observed. The `quota.probe` is `null` and nothing
  has driven the account dry.
- **`--tools-all` and `--skip-onboarding` individually.** Both were present in
  the treatment run, which passed; neither has been shown necessary on its own
  by removing it and watching the run fail.

## Guardrails in the installer

Two failure modes are now refused rather than discovered at launch (added in
v0.10.1 after an independent review):

- **An unresolved `{shim:<name>}` token is a validation ERROR.** Previously a
  typo rendered a path to a file nothing writes, and the launch failed with an
  exec error pointing nowhere near the installer.
- **The substituted shim path is `shlex.quote`d.** Previously a raw path
  containing a space split into two argv entries — `CMD_BIN=/Users/John` plus a
  stray `Smith/...` — breaking the launch for anyone whose home directory
  contains a space. "No shell" prevents variable *expansion*; it does not
  prevent argv *splitting*.

`write_shims` also converges mode as well as content, so a shim that loses its
exec bit is repaired and reported on the next install rather than failing
silently.
