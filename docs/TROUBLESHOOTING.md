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
