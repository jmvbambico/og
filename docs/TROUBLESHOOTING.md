# Troubleshooting

Every entry here is a failure that actually happened, what it looked like, and
what it turned out to be. The symptom is rarely where the cause is.

---

## "Native Claude terminal failed to start"

**Log:** `~/.omnigent/logs/runner/runner-*.log`

```
RuntimeError: tmux launch failed (rc=1): command too long
```

**Cause.** `argv`-delivery harnesses pass the whole system prompt inline via
`--append-system-prompt`, and Omnigent shell-quotes the entire argv into ONE
tmux command string. tmux refuses a command string past **~16,320 bytes**.
Non-prompt args cost ~3 KB, leaving ~13.3 KB for the prompt.

**Fix.** Shrink the prompt, or switch to a `per_turn` orchestrator (OpenCode),
which has no such ceiling.

```bash
# measure what tmux will actually receive
python3 -c "
import yaml, shlex, sys
p = yaml.safe_load(open(sys.argv[1]))['prompt']
print(len(p), 'raw |', len(shlex.quote(p)), 'shell-quoted')" \
  ~/.omnigent/agents/dev-lead/config.yaml
```

Quoted ≠ raw: each apostrophe expands to 5 bytes, so `do not` is *cheaper* than
`don't`. New guidance belongs in a skill file, which loads from disk and costs
the command line nothing.

Find your machine's exact limit:

```bash
for n in 12000 14000 16000 18000; do
  P=$(python3 -c "print('x'*$n)")
  tmux -L probe new-session -d -s t "echo $P" 2>&1 && \
    { echo "$n OK"; tmux -L probe kill-session -t t; } || echo "$n FAIL"
done; tmux -L probe kill-server 2>/dev/null
```

---

## A worker dies instantly with `Model not found: <orchestrator's model>`

```
ProviderModelNotFoundError: Model not found: claude-opus-5[1m]/.
```

**Cause.** The worker had no *effective* model pin, so it inherited the
orchestrator's model id, which its harness cannot resolve.

Two distinct ways a pin is ineffective:

1. **Wrong key.** The parser reads `executor.model` only. `executor.config` is
   a free-form dict, so a `model:` under it is accepted with no validation
   error and silently ignored.

   ```yaml
   executor:
     type: omnigent
     model: opencode/mimo-v2.5-free   # ✅ read
     config:
       harness: opencode-native
       model: opencode/mimo-v2.5-free # ❌ accepted, ignored
   ```

2. **Overridden at dispatch.** The runner resolves
   `launch_config.model_override or <spec model>` — in that order — so any
   `args.model` on `sys_session_send` replaces the pin. The orchestrator should
   not pass `args.model` at all.

**Check which it is:**

```python
from omnigent.spec import parse
from omnigent.runtime.workflow import _resolve_spec_model
spec = parse("~/.omnigent/agents/dev-lead")
[(s.name, _resolve_spec_model(s)) for s in spec.sub_agents]   # None = dead pin
```

---

## A worker returns "completed" but did nothing

Transcript contains only your prompt. No assistant turn, no tool calls,
worktree untouched. No error anywhere.

**Cause A — the model switch was accepted but unusable.** ACP agents (Cline,
likely Kilo) accept a `session/set_config_option` model switch they cannot
serve instead of rejecting it. The only evidence is an INFO line in the runner
log naming the model it switched to:

```bash
grep "model set to" ~/.omnigent/logs/runner/*.log
# acp[Cline] model set to claude-opus-5[1m]   ← pin leaked; should be deepseek/...
```

This never reaches the UI. Where OpenCode raises a loud
`ProviderModelNotFoundError`, an ACP agent just goes quiet.

**Cause B — the harness never delivered the prompt.** Harnesses with
`prompt_delivery: none` (Antigravity, Kiro, Cursor, Goose, Hermes, Gemini)
never receive the sub-agent spec prompt at all. The worker sees only
`args.input`, so any rule you wrote in its spec — scope limits, which gates to
run, commit locally — was never sent. Inline those rules into the dispatch.

Either way: **do not re-send the same task.** It is a misconfiguration, not a
refusal.

---

## Post-merge branch cleanup is blocked and never prompts

```
Blocked by the blast-radius policy. (irreversible: 'git push origin --delete feature/x')
```

**Cause.** Omnigent's builtin blast-radius policy classifies *every* remote
branch deletion as irreversible and returns DENY. That tier ignores
`gate_pushes: false`, and DENY is not approvable, so no prompt ever appears.
Bundling the deletion with benign cleanup makes it worse — the whole compound
command dies on the one offending statement.

**Fix.** Use `omnigent_local_policies.blast_radius_with_branch_cleanup`, which
this repo installs. It allows deleting a *non-protected* branch under a cleanup
prefix and leaves force-push, `--mirror`, `--prune`, protected refs and the
`rm -rf` tiers exactly as they were.

---

## Every message is `[Denied by policy: Denied by policy (policy evaluation error).]`

The server starts, then denies **everything** — chat, terminal startup. The
server log says:

```
Input policy evaluation failed ...: No module named 'omnigent_local_policies'
```

**Cause.** `~/.omnigent/config.yaml` names `omnigent_local_policies` in
`policy_modules`, but the interpreter omnigent runs under cannot import it —
the `.pth` that puts `~/.omnigent/policies` on its `sys.path` is missing from
*that* interpreter's site-packages. Omnigent does not degrade here: every
evaluation raises, and a raise is a deny.

og ≤ 0.5.1 wrote the `.pth` into the site-packages of the interpreter running
the *installer* (`install.sh` picks the system `python3` when it has PyYAML),
not omnigent's own venv. On Linux those are root-owned, the write was skipped
with a `warn(...)` that scrolled past, and the install finished "successfully"
into this state.

**Fix.** Update og and re-apply: `og update` (or `./install.sh` from a pulled
checkout). The installer now resolves the interpreter behind the `omnigent`
entry point (uv tool / pipx / Homebrew), writes the `.pth` into *its*
site-packages, imports the module under it, and checks both handlers are
registered — and it **fails the install** with the remediation if any step
does not hold. By hand, if you are stuck on an older og:

```bash
PY="$(head -1 "$(readlink -f "$(command -v omnigent)")" | sed 's/^#!//')"   # omnigent's python
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo "$HOME/.omnigent/policies" > "$SITE/omnigent-local-policies.pth"
"$PY" -c 'import omnigent_local_policies as m; print(m.__file__)'          # must print the path
og stop && og start
```

---

## A policy is configured but never fires

**Cause.** Being importable is not the same as being registered. Local handlers
reach the interpreter via a `.pth` file, but Omnigent's registry only loads
`BUILTIN_POLICY_MODULES` plus whatever **`policy_modules`** in
`~/.omnigent/config.yaml` names. Without that key the module imports fine and
the policy simply never runs.

```python
from omnigent.policies.registry import load_registry, is_registered_handler
load_registry(extra_modules=["omnigent_local_policies"])
is_registered_handler("omnigent_local_policies.merge_gate")   # must be True
```

Confirm it is actually deciding things — a gate that never appears in session
history is not enforcing anything:

```python
import sqlite3, re
c = sqlite3.connect("file:~/.omnigent/chat.db?mode=ro", uri=True)
[d for (d,) in c.execute("select cast(data as text) from conversation_items")
 if d and re.search("Auto-merge into|protected branch", d)]
```

---

## `error: agent bundle not found at ~/.omnigent/agents/dev-lead`

You named the orchestrator something else during install (`og-install.json`
has `"agent_name": "your-name"`, and `~/.omnigent/agents/your-name/` exists),
but `og start` still looks for `dev-lead`.

**Cause.** `bin/og` computed `AGENT_NAME="${OG_AGENT:-dev-lead}"` *before*
sourcing `og.env` — the file that actually sets `OG_AGENT` to your configured
name. The default always won because the override hadn't been read yet.

**Fix.** Fixed in this repo's `bin/og` (`OG_AGENT` is read before `AGENT_NAME`
is resolved now). If you're hitting this, update your installed copy:

```bash
cp /path/to/og/bin/og "$(command -v og)"
```

---

## Server crashes on startup with a YAML `ScannerError` on `model: * ...`

```
yaml.scanner.ScannerError: while scanning an alias
  in "<unicode string>", line 13, column 10:
      model: * auto                 1.00x cre ...
```

**Cause.** A coder's `model` in `og-install.json` was captured from the
vendor CLI's `--list-models` output verbatim. Most vendors print one bare id
per line; some (kiro-cli observed) print a formatted table row instead — a
`*` marker for the active model, then whitespace-padded columns. The whole
row got stored as the "model id," and its leading `*` is YAML alias syntax,
which blows up the moment it's templated unquoted into `config.yaml`.

**Fix.** Fixed in `installer/og_install.py`: `list_models()` now takes only
the first column of each line, and `model_block()` quotes the value via
`json.dumps` regardless, so no future vendor output can do this again. If
you're already stuck with a corrupted `og-install.json`:

```bash
# edit the bad "model" value for the affected coder, e.g. to "auto"
python3 installer/og_install.py --plan ~/.omnigent/og-install.json --dry-run
python3 installer/og_install.py --plan ~/.omnigent/og-install.json   # regenerate for real
```

(`--plan` needs a Python with PyYAML — Omnigent's own venv has it, e.g.
`$(brew --prefix omnigent)/libexec/bin/python` on macOS/Homebrew.)

---

## `og start` dies with `[Errno 48] address already in use`

```
ERROR ... [Errno 48] error while attempting to bind on address ('0.0.0.0', 6767): address already in use
error: the omnigent server exited during startup (see log above)
```

**Cause.** Something else on the machine is already listening on `OG_PORT`
(default 6767) — it does not have to be a previous `og`/omnigent process.

**Fix.** `og start` now checks the port before launching and names the
process holding it:

```
error: port 6767 is already in use by jmanager- (pid 24800).
    Set OG_PORT=<free port> in ~/.omnigent/og.env, or stop that process first.
```

Find a free port and set it in `~/.omnigent/og.env` (`OG_PORT=...`) and, to
keep `og-install.json` consistent for the next `./install.sh` reconfigure,
its `"port"` field too. `./install.sh` itself now checks port availability
when it asks and suggests a free one if the default is taken.

---

## `og login` fails with `security: command not found`

```
/home/you/.local/bin/og: line 1081: security: command not found
error: could not write to the Keychain
```

**Cause.** Older `og` builds stored credentials only in the macOS Keychain via
the `security` tool, which does not exist on Linux or WSL.

**Fix.** Update (`og update`, or re-run `./install.sh` from a fresh checkout).
Current `bin/og` picks a store per platform — macOS Keychain, Linux Secret
Service via `secret-tool`, or `~/.omnigent/og-credentials` (mode `0600`) on
WSL and headless Linux — and `og status` reports which one holds your
credentials. Force one with `OG_CRED_BACKEND=keychain|secret-tool|file` in
`~/.omnigent/og.env` if the detection picks wrong (for example a Linux desktop
whose keyring is locked: set `file`).

---

## First run: `no stored credentials (run: og login)`

```
no stored credentials (run: og login)

    The server and tunnel are up, but no session could be minted.
    If you have not stored credentials yet:

      og login
```

**Cause.** This is expected on a genuinely first install — no admin account
exists on the server yet, and `og`'s own CLI session (backed by the stored
credentials — see `og login` in the README) is separate from creating that
account. Omnigent's server normally auto-opens a
browser to the create-admin form for you, but only when bound to loopback;
`og` always binds the LAN/tunnel address instead, so that auto-open never
fires, and older `og` builds just printed this message and stopped.

**Fix.** Current `bin/og` handles this itself: when minting a session fails,
it checks `GET /v1/info`'s `needs_setup`, opens the browser to the create-admin
form, and prompts you in the terminal for the same username + password so it
can store them — then continues straight through to attaching
the host daemon. If you're on an older `og` (or running non-interactively,
where there's no terminal to prompt), do it manually:

```bash
og login     # same username + password you set in the browser
og attach
```

---

## OpenCode: a provider I logged into is missing from the installer's model list

You ran `opencode auth login` (Anthropic, Google, OpenAI, DeepSeek, …), but
`./install.sh` shows only `opencode/…` ids — or `opencode models` itself never
prints that provider.

**Cause.** The installer lists exactly what `opencode models` prints, and
OpenCode builds that list from `~/.local/share/opencode/auth.json` by two
different rules:

- an **API key** login (`type: api` — DeepSeek, OpenRouter, Zen, …) becomes a
  provider by itself;
- an **OAuth session** login (`type: oauth` — a Claude, Google, or ChatGPT
  subscription) becomes a provider **only through an auth plugin** for that
  vendor. OpenAI (ChatGPT/Codex) and GitHub Copilot ship built in; Anthropic and
  Google do not.

So a subscription login sits in `auth.json` and contributes nothing until its
plugin is loaded. `opencode auth list` still shows it as a credential, which is
what makes this look like an installer bug.

**What the installer does now.** It reads `auth.json` and, for every login that
contributes no models, prints the reason and the fix next to the model list:

```
! anthropic: logged in (oauth) but not listed. OAuth session -- it surfaces only
  through an auth plugin. Add "opencode-anthropic-auth" to the `plugin` list in
  ~/.config/opencode/opencode.json
! deepseek: logged in (api) but not listed. API key present but no deepseek/
  models -- check `disabled_providers` in ~/.config/opencode/opencode.json, or
  refresh the catalog: `opencode models --refresh`
```

**Fix.**

- **OAuth (subscription) login, plugin not built in** — add the plugin and
  re-run `opencode models` (the first run installs it):

  ```json
  { "plugin": ["opencode-anthropic-auth"] }        // ~/.config/opencode/opencode.json
  ```

  Google: `opencode-antigravity-auth`. Note that Anthropic has restricted use of
  Claude subscription sessions outside Claude Code, so that route may stop
  working regardless of the plugin. For a Claude subscription the dependable
  path in og is the **Claude Code agent itself** (`claude`), which og supports
  directly as orchestrator, coder, or reviewer — there is no need to go through
  OpenCode.
- **OAuth login, plugin built in (OpenAI, Copilot)** — the session has likely
  expired: `opencode auth login` again.
- **API-key login not listed** — it is either in `disabled_providers` in your
  OpenCode config, or the models.dev catalog is stale: `opencode models --refresh`.
- **`could not list models: opencode models timed out / exit N`** — the
  installer now prints OpenCode's own stderr. A first run can take a while
  (plugin install + catalog fetch); run `opencode models` once by hand and
  re-run the installer.

---

## "Models unavailable" in the composer, or `409 Conflict` on `model-options`

```
GET /v1/hosts/<id>/harnesses/claude-native/model-options HTTP/1.1" 409 Conflict
GET /v1/hosts/<id>/filesystem?limit=1000 HTTP/1.1" 409 Conflict
```

`GET /v1/hosts` still returns 200 and shows the host as `"status": "online"`,
which makes this confusing — the picker looks like it should work.

**Cause.** `og` attached the host daemon with `omnigent host --background
--server ""`. An empty `--server` doesn't mean "attach to the server og just
started" — it means *local mode*, which tells Omnigent to start or reuse
**its own** local server, tracked in `~/.omnigent/local_server.pid`. Since
og's server binds `0.0.0.0` (so it's reachable from other devices) rather
than loopback, it never satisfies Omnigent's own "is there already a local
server?" check, so the daemon spawns a *second*, ephemeral, loopback-only
server and tunnels to that instead. Both processes share the same
`chat.db` — which is why `/v1/hosts` (a plain DB read) looks fine — but
each has its own in-memory host registry, and the one your browser is
actually talking to has no live tunnel for the host, so anything routed
over that tunnel (model options, filesystem) 409s as "unreachable here."

Check for the second process:

```bash
og status   # a "Background server: running at http://127.0.0.1:<other port>"
            # line alongside the real "og server" line is the tell
```

**Fix.** Fixed in this repo's `bin/og`: the host attach now passes an
explicit `--server "http://127.0.0.1:$PORT"` — og's own server's loopback
address — instead of `""`, so the daemon's tunnel lands on the same process
serving the UI. (`mint_token` already stores a matching entry in
`auth_tokens.json` keyed to that exact URL, so this needs no extra auth
plumbing.) If you're on an older `og`:

```bash
og update            # or: cp /path/to/og/bin/og "$(command -v og)"
og stop && og start
```

`og start` now prints the installed version against the latest release (and
auto-updates when `OG_AUTO_UPDATE=1`, see the README's *Updating* section), so
this class of "fixed in the repo, still broken on the machine" no longer goes
unnoticed.

---

## New sessions open in `~` or `~/projects`, not the directory `og start` ran from

The composer's default workspace is the pinned project's `workspace`, which
`og start` is meant to set to the directory it was launched from
(`OG_LAUNCH_DIR`, or `OG_WORKSPACE` if set).

**Cause.** Before this was fixed, `ensure_project` read the pin from an
`OG_WS` variable that was never passed in, so the workspace was silently left
unset and every session opened wherever the composer last was.

**Fix.** Update `og` (`og update`), then `og stop && og start` **from the repo
you want** — the pin is re-applied on every start, so launching from a
different directory moves it. Setting `OG_WORKSPACE=/path` overrides `$PWD`.

---

## `og start` succeeds but the UI only lists built-in agents

**Cause.** `omnigent server --background` discards every other flag, including
`--agent`, so the orchestrator is never registered. `og` works around this by
detaching a plain foreground `omnigent server --agent <dir>` itself.

**Related:** the host daemon needs `OMNIGENT_LOCAL_SINGLE_USER=1` when
`OMNIGENT_AUTH_ENABLED=1`, or re-owning an existing `host_id` fails with
HTTP 409 "already registered to a different account", `/v1/hosts` returns
empty, and the agent is in the picker with nowhere to run. Escape hatch:
`omnigent host reset-id`.

---

## `inner executor error: Internal error: You need to sign in to use this model.`

```
ERROR ... turn surfaced to UI as failed for <conversation-id> (harness=acp): {'code': 'runner_error', 'message': 'inner executor error: Internal error: You need to sign in to use this model.'}
```

A `WARN ... Sub-agent 'coder_X' not found under spec 'coder_X'; no workdir
resolved` line right before this is the usual red herring above, not the
cause — check it isn't distracting you from the real error on the next line.

**Cause.** The coder's own CLI has no stored credentials for the model it was
dispatched with. Nothing in `og`/the installer checks or enforces per-agent
login during install — a coder is fully selectable into the active roster
without ever having been logged in, and the first sign of that is a dispatch
failing here, not anything surfaced earlier.

**Find which coder:** the log line names `harness=acp` (or `=kiro-native`,
etc.) and the conversation id; grep a few lines above it in the same runner
log for `acp gateway routing: ... model=<id>` to see which coder that id
belongs to (cross-reference `og-install.json`'s `coders` list).

**Fix.** Run that coder's login command, then retry. `installer/registry.json`
has one per agent (`"login"` key); as of this `og` version, `./install.sh`
also prints the full checklist after every apply:

```bash
kilo auth login      # Kilo Code
cline auth            # Cline
opencode auth login   # OpenCode (Zen)
kiro-cli               # Kiro (AWS) — run once interactively to authenticate
# ...one per active coder + the orchestrator + the reviewer
```

`kilo auth list` (or the equivalent for another vendor) confirms whether
credentials are actually stored before you retry.

---

## Kiro asks for permission on every tool, and approving does nothing

Runner log:

```
ERROR kiro_native.permissions  failed to deliver kiro permission verdict for <id>
RuntimeError: kiro-native permission prompt was not safely focused before verdict delivery
```

**Cause.** The worker is on the `kiro-native` harness. Omnigent's
headless-worker seam maps Claude/Codex/Cursor/Kimi/Antigravity to their
no-prompt flags but has no `kiro-native` entry, so Kiro launches in its default
ask-every-time mode and relies on Omnigent mirroring each prompt to the web and
typing the verdict back into the TUI over tmux — which is what is failing
above. Nothing a policy or the orchestrator does changes this.

**Fix.** og now wires Kiro over ACP as `kiro-cli acp --trust-all-tools`
(`acp:kiro-aws`). Re-apply the install and restart:

```bash
og update            # or ./install.sh --plan ~/.omnigent/og-install.json
og stop && og start
```

Check `~/.omnigent/agents/<name>/agents/coder_kiro/config.yaml` says
`harness: acp:kiro-aws` and `~/.omnigent/config.yaml` has an `acp.agents` row
named `Kiro (AWS)`.

---

## A Cline worker "completes" instantly with no output

`og audit` shows the worker with **NO TOOL CALLS**, one user turn, and no
report; the orchestrator says "completed with no output — the roster's
silent-failure signature". No error in any log.

**Cause.** Omnigent's generic ACP executor never delivers the model pin.
Traced on the wire, it sends exactly `initialize`, `session/new`,
`session/prompt` — `HARNESS_ACP_MODEL` is documented as inert unless
`send_model` is set (which only adds a non-standard `model` field to
`session/new`, and Cline ignores it), and `session/set_config_option` is used
for interactive `/model` picks only. Cline's ACP `newSession` therefore falls
back to its hard-coded default, `anthropic/claude-sonnet-5` on Cline
usage-billing, and with no credits behind it Cline answers `end_turn` with no
content. `cline -m`, Cline's saved settings, and `session/new.model` are all
ignored in ACP mode; the session model comes from **`CLINE_MODEL`** (provider
from `CLINE_PROVIDER`).

**Fix.** The registry row declares `model_env: CLINE_MODEL` and the installer
renders the `acp.agents` command as
`env CLINE_MODEL=<pin> cline --acp --auto-approve true` — Omnigent exec's the
argv directly, and `env` is a real binary. Re-apply and restart:

```bash
./install.sh --plan ~/.omnigent/og-install.json   # or: og setup
og restart
```

`~/.omnigent/config.yaml` should show the `env CLINE_MODEL=...` prefix on the
Cline row. Verified: two Cline workers then implemented, tested and committed
their tasks in ~2 minutes each.

---

## Every Cursor worker dies in 2 seconds with `Harness stream connection error`

Runner log, per dispatch:

```
tmux capture-pane probe failed for terminal cursor:main: ... no server running on .../tmux.sock
tmux unavailable after 3 consecutive probes for terminal cursor:main
turn surfaced to UI as failed ... (harness=cursor-native): {'code': 'ReadError', 'message': 'Harness stream connection error.'}
```

The orchestrator retries, every retry dies identically, and it hard-blocks.
`cursor-agent` launched by hand in tmux works fine.

**Cause.** Omnigent always launches `cursor-agent --yolo --approve-mcps
--model <id>`, where `<id>` is `launch_config.model_override or <spec model>`.
With no pin on the Cursor spec, the id that arrives is the **orchestrator's**
(`claude-opus-5[1m]`); cursor-agent rejects it — `Cannot use this model:
claude-opus-5[1m]. Available models: auto, gpt-5.3-codex, …` — and exits 1,
the pane's only process is gone, the tmux server follows, and the probes see
no server. Captured by pointing `OMNIGENT_CURSOR_PATH` at a wrapper that logs
argv and stderr. Same family as *A worker dies instantly with `Model not
found: <orchestrator's model>`* above, one harness further down.

**Fix.** The registry row for Cursor is `model.required: true` with `auto`
preferred, so the installer always pins a valid id (`cursor-agent models`
lists them). Re-apply and restart. Verified: two Cursor workers implemented,
tested and committed their tasks in about a minute each.

---

## An OpenCode worker never starts, and the orchestrator waits forever

`og audit` shows the worker with **NO TOOL CALLS**, no report, and a `Rate
limit exceeded` hint; the orchestrator's last message is the dispatch. Nothing
is running, nothing is failing, nothing wakes up.

**Cause.** The pinned model (a Zen free id, typically) is over its daily
quota. opencode logs `stream error … Rate limit exceeded`; Omnigent launches
it with retries disabled and its forwarder (0.13) maps only `session.error`
to a failed turn, which this is not — so the worker's turn never completes,
no inbox message is produced, and the orchestrator, which rightly waits on the
inbox instead of polling, sleeps. This is a quota, not a bug in the CLI: the
same pin implemented tasks reliably earlier the same day.

**Fix.** Cancel the run (`og stop`, or stop the session in the UI), then pin
a model with headroom — `og setup` lists everything the CLI offers, paid ids
included — or wait for the quota to reset. The worker's own log is at
`~/.omnigent/opencode-native/<hash>/xdg-data/opencode/log/opencode.log`.

---

## Kilo dies with `Add credits to continue, or switch to a free model`

Runner log:

```
turn surfaced to UI as failed for <id> (harness=acp): {'code': 'runner_error',
 'message': 'inner executor error: Internal error: Add credits to continue, or switch to a free model'}
```

**Cause.** Not a missing free tier — `kilo/kilo-auto/free` exists and works.
A Kilo ACP session starts on Kilo's *own* default model (its last-used one;
`kilo/google/gemini-3-pro-image` on the day this was seen), and only moves to
the pinned id when Omnigent's `session/set_config_option` lands. If that
switch is skipped or rejected, the first prompt runs on a paid model with no
credits behind it. Verified by driving `kilo acp` directly: the identical
error without the switch, a correct answer with it.

**Fix.** Make the free router Kilo's own default, so the pin is a
confirmation rather than the only guard:

```jsonc
// ~/.config/kilo/kilo.jsonc
{ "$schema": "https://app.kilo.ai/config.json", "model": "kilo/kilo-auto/free" }
```

`og audit` shows the failed dispatch as a worker with no tool calls; the
runner log carries the message above.

---

## A Kilo (or any ACP) worker behaves like a different vendor

`acp:<slug>` is resolved against `acp.agents[].name` slugified by Omnigent
(lowercase, non-alphanumerics → `-`). A slug that matches no row does **not**
error: Omnigent falls back to the first configured row. og's registry used to
say `acp:kilo` for a row named `Kilo Code` (slug `kilo-code`), so Kilo
dispatches silently ran on Cline. Fixed in the registry; `og update` re-applies
it. `test_acp_user_harness_matches_omnigent_slug` guards every acp-user row.

---

## An OpenCode worker sits on a question card until someone answers

You left a run overnight; in the morning a coder session shows a card like
"B3 scope — Full voucher PDF / Email-only / Defer", and nothing has moved since
it appeared. Server log:

```
POST /v1/sessions/<coder>/hooks/native-permission-request HTTP/1.1" 200 OK 594252.2ms
```

**Cause.** Not a permission. The model called OpenCode's `question` tool
(free-tier models are fond of it), and Omnigent mirrors that as a web card and
waits — up to a day, hard-coded in the server hook. Your global
`~/.config/opencode/opencode.json` cannot help: Omnigent runs each worker under
an isolated `XDG_CONFIG_HOME` and carries over only `provider`, `plugin` and
`model` from your file. The permission engine was never the problem: every
other tool call was auto-allowed by policy in the same session.

**Fix.** The installer now writes `~/.omnigent/opencode/opencode.json` with
`permission: {"*": "ask", "question": "deny"}` and og exports it as
`OPENCODE_CONFIG_DIR` (merged *after* Omnigent's per-session config). The
coder prompt also says to decide, note the assumption, and keep going. Then:

```bash
./install.sh --plan ~/.omnigent/og-install.json   # or: og setup
og restart
```

(`og update` only re-applies when a newer *release tag* exists; with
auto-update on, `og start` does that by itself.) Check `og start` prints
`opencode workers: ~/.omnigent/opencode (question tool off)`. If it does not, OpenCode is either not a coder or is the orchestrator
(which needs `question` for its plan gate — the installer warns about that
pairing).

Why `permission` and not `tools: {question: false}`: OpenCode rewrites the
`tools` form into a `question: deny` rule placed *before* Omnigent's `*: ask`,
and its last-match-wins evaluation keeps the tool visible. Verified on 1.18.30
over `opencode serve` — the form the installer writes removes `question` from
the model's tool list; the `tools` form does not.

---

## The reviewer runs on my main Claude account, not the second one

`og-install.json` names `accounts.claude: ~/.claude-work`, `og start` prints
`account: CLAUDE_CONFIG_DIR=~/.claude-work`, and yet the runner log says:

```
claude_native.status_file  claude status file resolved: path=/Users/me/.claude/sessions/72881.json
```

**Cause.** The host daemon builds each runner's environment from an allowlist
(`omnigent/host/connect.py`, `_build_runner_env`), not from its own. Neither
`CLAUDE_CONFIG_DIR` nor `OPENCODE_*` is on it, so og's exports stopped at the
daemon. The same gap meant `OPENCODE_DISABLE_EXTERNAL_SKILLS` never reached
the Zen worker either.

**Fix — two hops, both needed.** (1) og names them in
`OMNIGENT_RUNNER_ENV_PASSTHROUGH` (the operator-controlled daemon→runner
forward, itself allowlisted). (2) That alone did nothing: `omnigent host
--background --server …` builds the *daemon's* env from a second allowlist
(`cli.py`, `_build_host_daemon_env`) that keeps the passthrough list but
strips the variables it names — verified by reading the daemon's environment:
none of og's exports were there. og now runs the host in the foreground under
`nohup` with its own pidfile (`~/.omnigent/og-host.pid`, the pattern the
server already used), so the daemon inherits og's full environment and hop (1)
has something to forward. Re-apply (`./install.sh --plan
~/.omnigent/og-install.json` or `og setup`), then `og restart`; `og status`
shows `og host: pid N online`. Verify on the next reviewer dispatch: the
status-file line should resolve under the second account's dir.

---

## Workers burn tokens re-reading the codebase

A coder transcript shows the same file read forty or fifty times and a hundred
`grep` shells before a small change. Two causes, both addressed in the coder
prompt and the `fanout` skill:

- **No orientation tool.** The coder prompt now says: if a code-intelligence
  tool is present — an MCP tool or CLI over an indexed code graph — query it
  first; it returns the relevant symbols plus callers/callees in one call. The
  prompt names no tool (you may run CodeGraph, GitNexus, or nothing), and
  installs nothing. What makes it *findable* for a weak model is the tool
  showing up in its list: when the installer sees the `codegraph` CLI on PATH
  it wires `codegraph serve --mcp` (a plain stdio process) into the worker's
  `opencode.json`, since your global OpenCode config never reaches workers.
  Index the target repo once yourself (and add the index dir to its
  `.gitignore`). Worktrees the orchestrator cuts do not inherit the index, so
  the `fanout` skill has the orchestrator build each worktree's own right after
  `worktree add` — only when the repo root already has one. Workers never
  index: the tool's own no-index message tells agents that is the owner's call.
- **Re-reading after every edit.** The prompt now says read once, trust the
  edit result, re-read only the next region. This is a strong default, not a
  guarantee; a weak model may still do it, and the roster's preference order
  is the lever if one model is much worse than another here.

---

## `Sub-agent 'X' not found under spec 'X'; no workdir resolved`

**Red herring.** This WARN fires for every sub-agent dispatch including
healthy ones. It is not the cause of whatever you are chasing.

---

## Where to look

Start with `og audit` (latest run) or `og audit <session-id>`: per worker it
prints harness, tool mix, `ASKED` / `parked` stalls, malformed tool calls,
"NO TOOL CALLS" for a worker that never acted, and its final report. Then:

| Symptom | File |
|---|---|
| terminal won't start, tmux errors, ACP model lines | `~/.omnigent/logs/runner/runner-*.log` |
| model rejections, session turn failures | `~/.omnigent/logs/server/server-*.log` |
| host/tunnel attach problems | `~/.omnigent/logs/host/host-*.log` |
| what an agent actually did or was told | `~/.omnigent/chat.db` (`conversation_items`) |
| which model OpenCode really used | `~/.omnigent/opencode-native/*/xdg-config/opencode/opencode.json` |
| which model Cline really has | `~/.cline/data/settings/providers.json` |

Model ids do **not** appear in the runner logs for native harnesses — query
`chat.db` or the synthesized config instead.
