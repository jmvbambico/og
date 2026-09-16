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

## First run: `no stored credentials (run: og login)`

```
no stored credentials (run: og login)

    The server and tunnel are up, but no session could be minted.
    If you have not stored credentials yet:

      og login
```

**Cause.** This is expected on a genuinely first install — no admin account
exists on the server yet, and `og`'s own CLI session (Keychain-backed) is
separate from creating that account. Omnigent's server normally auto-opens a
browser to the create-admin form for you, but only when bound to loopback;
`og` always binds the LAN/tunnel address instead, so that auto-open never
fires, and older `og` builds just printed this message and stopped.

**Fix.** Current `bin/og` handles this itself: when minting a session fails,
it checks `GET /v1/info`'s `needs_setup`, opens the browser to the create-admin
form, and prompts you in the terminal for the same username + password so it
can store them in the Keychain — then continues straight through to attaching
the host daemon. If you're on an older `og` (or running non-interactively,
where there's no terminal to prompt), do it manually:

```bash
og login     # same username + password you set in the browser
og attach
```

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

## `Sub-agent 'X' not found under spec 'X'; no workdir resolved`

**Red herring.** This WARN fires for every sub-agent dispatch including
healthy ones. It is not the cause of whatever you are chasing.

---

## Where to look

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
