# AGENTS.md — for an AI installing or maintaining `og`

Two audiences: an AI asked to **install** og on someone's machine, and an AI
asked to **change this repo**. Both start here.

---

## Part 1 — Installing og for a user

You have a shell. Do not guess the user's setup; measure it, ask them the
choices that are genuinely theirs, then apply.

### 1. Measure

```bash
./install.sh --check        # prerequisites + which coding CLIs exist
./install.sh --questions    # decision schema + detected agents, as JSON
./install.sh --show         # existing install, if any
```

`--questions` emits every question, its valid choices *restricted to CLIs that
are actually present*, the current state if there is one, and the full agent
registry. Drive your conversation from that output — never offer an agent whose
CLI is missing.

### 2. Ask

Ask the user, in this order. Each maps to a key in the plan JSON.

| Key | Ask |
|---|---|
| `agent_name` | Name for the orchestrator bundle. Default `dev-lead`. |
| `orchestrator` | Which agent plans and delegates, and never writes product code. |
| `coders` | Which agents implement, **in preference order** — first is tried first, later ones absorb overflow. |
| `coders[].model` | Model to pin per coder. Required where `registry.model.required` is true. For multi-provider CLIs (OpenCode, Kilo) run the registry's `model.list_cmd` (e.g. `opencode models`) and offer everything it prints, grouped by `provider/` prefix — a user with a DeepSeek or Anthropic login there wants `deepseek/…`, not only Zen's `opencode/…-free` ids. |
| `reviewer` | Which agent reviews the batched diff. |
| `accounts` | For agents with `multi_account.supported`, whether the reviewer runs on a second account, and its config dir. |
| `port`, `ngrok_domain`, `max_dispatches` | Runtime knobs; defaults are fine. |

**Ask, don't assume, about:**

- **Preference order.** People care which quota burns first. Do not infer
  it from the order they happened to name agents.
- **The model.** Offer everything the CLI lists, paid ids included; a user
  with a subscription wants it used. Never filter to free tiers.
- **The reviewer's vendor.** If the only reviewer available shares a vendor
  with a coder, say so plainly — review quality is the thing being traded.
  Judge vendor by the model, not the bill: `opencode/claude-*` reviewed by
  Claude Code is same-vendor. `--dry-run` applies this rule and warns.
- **A second account.** Only offer it for agents where
  `multi_account.supported` is true. Explain that it isolates the reviewer from
  their interactive login rather than just asking "multiple accounts?".

**Decide yourself, don't ask:**

- Where the pin goes in the YAML (always `executor.model`).
- Which harness id an agent maps to (the registry knows).
- Whether a `.pth` or `policy_modules` entry is needed (always).

### 3. Apply

```bash
./install.sh --plan plan.json --dry-run   # validate first
./install.sh --plan plan.json
```

Plan shape:

```json
{
  "agent_name": "dev-lead",
  "orchestrator": "claude",
  "coders": [
    {"id": "opencode", "model": "opencode/mimo-v2.5-free"},
    {"id": "cline",    "model": "deepseek/deepseek-v4-flash"}
  ],
  "reviewer": {"id": "claude"},
  "accounts": {"claude": "/Users/me/.claude-work"},
  "port": 6767, "ngrok_domain": "", "max_dispatches": 4
}
```

`priority` is assigned from array order; you may omit it.

### 4. Verify

The installer validates before writing and **refuses** a config that cannot
work. Do not work around a refusal — it is reporting a real incompatibility.
After a successful run, confirm:

```bash
./install.sh --show
```

Then tell the user to run `og start` from the repo they want as the default
workspace. The first real dispatch is the only proof a model pin holds; say so
rather than claiming the setup is verified end to end.

### Constraints you must respect

1. **Model pins go at `executor.model`.** `executor.config` is a free-form
   dict; a model there is silently ignored and the worker inherits the
   orchestrator's model id.
2. **An `argv`-delivery orchestrator has a hard prompt ceiling** (~13.3 KB
   shell-quoted). Adding coders grows the prompt. If the installer errors on
   the ceiling, the fix is fewer coders, guidance moved into a skill, or a
   `per_turn` orchestrator — *not* editing `PROMPT_CEILING`.
3. **`prompt_delivery: none` harnesses never receive their spec prompt.** They
   cannot orchestrate, and as coders their rules must be inlined into each
   dispatch. The generated `roster` skill already says this.
4. **Never commit secrets.** `~/.omnigent/auth_tokens.json`, `.cookie-secret`,
   and `host.host_id` are machine-local. Nothing under `~/.omnigent` belongs in
   this repo except the generated-from-template sources already here.

---

## Part 2 — Changing this repo

### Layout

```
install.sh                    interpreter resolution, then hands off
installer/og_install.py       the installer (python3 + PyYAML)
installer/registry.json       agent catalog — add vendors HERE, not in code
installer/templates/*.tmpl    orchestrator / coder / reviewer YAML
bin/og                        control script (bash)
agents/dev-lead/skills/       static skills, copied verbatim
policies/                     local policy handlers
```

### Adding a coding agent

Add one row to `installer/registry.json`. No code change should be needed.

```jsonc
{
  "id": "newagent",
  "label": "New Agent",
  "harness": "acp:newagent",        // or a native harness id
  "binary": "newagent",             // what the PATH scan looks for
  "kind": "acp-user",               // native | acp-builtin | acp-user
  "acp_command": "newagent --acp",  // acp-user only
  "roles": ["coder"],
  "vendor": "somevendor",           // cross-vendor review depends on this
  "relay": false,                   // false = leaf worker, cannot dispatch
  "silent_model_failure": true,     // true = bad model ⇒ empty transcript
  "prompt_delivery": "unknown",     // argv | per_turn | first_user | none
  "model": {"required": true, "pin_path": "executor.model"},
  "install": {"npm": "..."},
  "login": "newagent auth",
  "unverified": true                // until you have seen it commit real code
}
```

Get `prompt_delivery` from Omnigent rather than guessing:

```python
from omnigent.harness_plugins import plugin_state
plugin_state().contributions[0].capabilities["<harness>"].instruction_delivery
```

Mark a new row `unverified: true` until you have watched it produce a real
commit. The installer surfaces that flag to the user.

### Changing the orchestrator prompt

`installer/templates/orchestrator.yaml.tmpl` is the prompt. It is inlined into
the harness command line for `argv` harnesses, so **every byte counts**.

Before adding prose, ask whether it belongs in a skill instead —
`agents/dev-lead/skills/*/SKILL.md` loads from disk and costs the command line
nothing. Only put something in the prompt when the orchestrator must know it
*before* deciding to read anything.

Check your change:

```bash
./install.sh --plan plan.json --dry-run   # prints the shell-quoted size
```

Contractions matter: shell-quoting expands each `'` to 5 bytes, so `do not`
is cheaper than `don't`.

### Testing

Always test into a sandbox, never your live `~/.omnigent`:

```bash
S=$(mktemp -d)
HOME="$S" OMNIGENT_HOME="$S/.omnigent" python3 installer/og_install.py --plan plan.json
find "$S" -type f
```

Then confirm the generated bundle parses under Omnigent's own loader and that
pins resolve — this is the check that catches a pin in the wrong place:

```python
from omnigent.spec import parse
from omnigent.runtime.workflow import _resolve_spec_model
spec = parse(f"{S}/.omnigent/agents/dev-lead")
[(s.name, _resolve_spec_model(s)) for s in spec.sub_agents]   # None = dead pin
```

### Navigating the code

This repo is indexed with CodeGraph. Reach for it before grep when you need to
find or understand something:

```bash
codegraph explore "where does the model pin get written"
codegraph index          # rebuild after a large change
```

It indexes the Python (`installer/og_install.py`, `policies/`); the bash in
`bin/og` and `install.sh` it does not parse, so read those directly. The
`.codegraph/` directory is machine-local — only its `.gitignore` is committed.

**Documentation goes in `docs/`.** Anything generated or derived — architecture
notes, analyses, guides produced while working on this repo — belongs there,
not scattered at the root. `README.md` and `AGENTS.md` are the only two
top-level docs.

### Conventions

- Comments explain **why**, especially where behaviour is surprising. Most
  comments in this repo document a failure that actually happened; keep them.
- The installer must stay **rerunnable and idempotent**. `og-install.json` is
  the source of truth; generated YAML is disposable.
- Validation should **refuse** impossible configs rather than emit something
  that fails later at launch. A loud refusal beats a silent breakage.
