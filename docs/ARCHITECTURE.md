# Architecture

How the pieces fit, and why each one is where it is.

---

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│ CONTROL          bin/og                                      │
│                  ngrok → server → register agent → pin project│
├──────────────────────────────────────────────────────────────┤
│ CONFIGURATION    installer/og_install.py + registry.json         │
│                  og-install.json ──renders──► agent bundles   │
├──────────────────────────────────────────────────────────────┤
│ RUNTIME          Omnigent server + host daemon + runner       │
│                  sessions, sub-agent dispatch, tool relay     │
├──────────────────────────────────────────────────────────────┤
│ ENFORCEMENT      policies/omnigent_local_policies.py          │
│                  merge_gate · blast_radius (branch-cleanup)   │
├──────────────────────────────────────────────────────────────┤
│ CONTRACT         <repo>/AGENTS.md + .agents/orchestration.yaml│
│                  branches · gates · never_write · merge policy│
└──────────────────────────────────────────────────────────────┘
```

Each layer only knows about the one below it. The agent bundle knows nothing
about your projects; the project contract knows nothing about which agents you
installed. That is what lets one global orchestrator serve every repo.

---

## Configuration flow

```
   registry.json ──┐
                   ├──► og_install.py ─► og-install.json   (source of truth)
   your answers ───┘         │
                             ├──► ~/.omnigent/agents/<name>/config.yaml
                             ├──► ~/.omnigent/agents/<name>/agents/*/config.yaml
                             ├──► ~/.omnigent/agents/<name>/skills/roster/SKILL.md
                             ├──► ~/.omnigent/config.yaml   (merged, not replaced)
                             ├──► ~/.omnigent/opencode/opencode.json  (worker overrides)
                             └──► ~/.omnigent/og.env
```

**`og-install.json` is the only file you should edit or copy between machines.**
Everything else is regenerated from it. This is what makes the setup
reproducible and the installer safely rerunnable: there is no state hiding in
the generated YAML.

`~/.omnigent/config.yaml` is *merged*, never overwritten — it also holds
machine-local values (`host.host_id`) that must not travel between machines.

`~/.omnigent/opencode/opencode.json` exists only when OpenCode is a coder and
not the orchestrator. Omnigent synthesizes its own per-session OpenCode config
(model, MCP relay, `permission: ask` so every tool call routes through the
policy engine) under an isolated `XDG_CONFIG_HOME`, which also hides the
user's global `~/.config/opencode`. This file is the one channel og has into
that session: OpenCode merges `$OPENCODE_CONFIG_DIR/opencode.json` *after* the
synthesized config, and `og start` exports the variable. It carries
`question: deny` — so a worker cannot park the run on a "which design do you
prefer?" card — and, when the CLI is on PATH at install time, a
code-intelligence MCP server (stdio, no Docker) the worker can query instead of
grepping. Everything else stays on the policy route.

## How env reaches a worker

```
og start ──export──► omnigent host ──allowlist──► runner ──prefix──► opencode serve
                                                         └─inherit─► claude
```

Both hops are **allowlists**, not inheritance. The daemon→runner hop drops
`CLAUDE_CONFIG_DIR` and `OPENCODE_*` unless named in
`OMNIGENT_RUNNER_ENV_PASSTHROUGH`; `og start`, `og attach` and `og chat` set
that list (`export_worker_scoping` in `bin/og`). The CLI→daemon hop
(`omnigent host --background`) keeps that list but strips the variables it
names, so og runs the host in the **foreground** under `nohup` with its own
pidfile instead — the daemon then inherits og's environment. Before both were
in place, the second-account reviewer silently ran on the interactive account
and the OpenCode `question`-tool override never reached a worker.

---

## Delivery flow at runtime

```
  goal
   │
   ├─ orchestrator decomposes into independent tasks
   │
   ├─ one git worktree per task      ~/projects/.worktrees/<repo>/<task>
   │    └─ feature/<slug> branched from the integration base
   │       (the `fanout` skill is the canonical statement of both)
   │
   ├─ dispatch coders in parallel (bounded by max_dispatches_per_turn)
   │    └─ each runs ONLY gates marked `scope: worker`
   │       (the integration suite is NOT worker-scoped — parallel runs
   │        would corrupt a shared test database)
   │
   ├─ collect commits onto ONE integration branch
   │    └─ run the FULL gate there, exactly once
   │
   ├─ send the combined diff to the reviewer (different vendor, text only,
   │  no worktree, cannot edit)
   │
   └─ open ONE PR ── merge_gate decides ALLOW / ASK / DENY
```

The orchestrator writes no product code. Docs and prose are its own; source and
tests always delegate. The line is by *kind*, not size.

---

## Why the roles are separated

**Orchestrator ≠ coder.** An agent that both plans and implements will quietly
reshape the plan to suit what it already wrote. Separating them makes the plan
an artifact you can approve before any code exists.

**Reviewer ≠ implementer's vendor.** Same-vendor review shares blind spots — a
model family's characteristic mistakes are exactly what it is worst at seeing.
The installer warns when the roster forces a same-vendor pairing, and the
orchestrator labels such PRs `degraded-review` rather than letting it pass
silently.

**Preference order.** Free tiers are day-capped or credit-capped. Ordering
workers means the cheapest capacity burns first and the run degrades gracefully
down the list instead of failing when one vendor runs dry.

---

## Harness properties that change behaviour

Omnigent declares per-harness capabilities. Three of them decide whether a
given agent can fill a given role:

| Property | Values | Consequence |
|---|---|---|
| `instruction_delivery` | `argv` | prompt rides the command line → subject to the ~13.3 KB tmux ceiling |
| | `per_turn` | composed per turn over the harness transport → no ceiling |
| | `none` | spec prompt **never delivered**; cannot orchestrate, and as a coder needs rules inlined per dispatch |
| tool relay | present / absent | absent ⇒ leaf worker: can implement, cannot dispatch |
| model resolution | loud / silent | silent ⇒ a bad pin yields an empty transcript, not an error |

`registry.json` records these per agent, and the installer refuses combinations
that cannot work rather than writing a config that fails at launch.

---

## Enforcement: mechanism, not prompt

A system prompt is a strong default, not a guarantee — a model under pressure
can reason past it. Anything that must hold is a policy:

| Policy | Decides |
|---|---|
| `merge_gate` | DENY into protected branches. ALLOW auto-merge into the one sanctioned target only when diff size, high-risk paths, test-count delta and a recorded cross-vendor review all check out. ASK otherwise — **including anything it cannot determine**. |
| `blast_radius_with_branch_cleanup` | DENY force-push / `--mirror` / `--prune` / `rm -rf` of system paths. ALLOW deleting a non-protected branch under a cleanup prefix. |
| `spawn_bounds` | Caps worker dispatches per turn so fan-out happens in waves. |
| `subagent_purpose_guard` | Sub-agents may only be dispatched for declared purposes. |

The failure mode these are designed around is *indeterminacy*: a gate that
cannot read the diff, find the contract, or confirm a review returns ASK. A new
risk surface therefore protects itself until somebody classifies it.

---

## Trust boundaries

| Boundary | Enforced by |
|---|---|
| orchestrator cannot author ad-hoc agents | `spawn` absent from the spec ⇒ `sys_session_create` unregistered |
| workers cannot push or merge | worker specs run blast-radius with `gate_pushes: true` |
| reviewer cannot edit | `skills: none`, no worktree, diff passed as text |
| nothing merges to `main`/`staging` | `merge_gate` DENY on protected refs |
| an agent cannot weaken its own gate | `.agents/**` listed under `high_risk_paths` |

That last one matters: the orchestrator can edit documentation, and
`.agents/orchestration.yaml` *is* configuration it could otherwise relax.
Listing it as high-risk forces a human decision on any change to it.
