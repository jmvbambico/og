# Architecture

How the pieces fit, and why each one is where it is.

---

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│ CONTROL          bin/og                                      │
│                  ngrok → server → register agent → pin project│
├──────────────────────────────────────────────────────────────┤
│ CONFIGURATION    installer/og-install + registry.json         │
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
                   ├──► og-install ──► og-install.json   (source of truth)
   your answers ───┘         │
                             ├──► ~/.omnigent/agents/<name>/config.yaml
                             ├──► ~/.omnigent/agents/<name>/agents/*/config.yaml
                             ├──► ~/.omnigent/agents/<name>/skills/roster/SKILL.md
                             ├──► ~/.omnigent/config.yaml   (merged, not replaced)
                             └──► ~/.omnigent/og.env
```

**`og-install.json` is the only file you should edit or copy between machines.**
Everything else is regenerated from it. This is what makes the setup
reproducible and the installer safely rerunnable: there is no state hiding in
the generated YAML.

`~/.omnigent/config.yaml` is *merged*, never overwritten — it also holds
machine-local values (`host.host_id`) that must not travel between machines.

---

## Delivery flow at runtime

```
  goal
   │
   ├─ orchestrator decomposes into independent tasks
   │
   ├─ one git worktree per task      ~/projects/.worktrees/<repo>/<task>
   │    └─ feature/<slug> branched from the integration base
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

`registry.json` records these per agent, and `og-install` refuses combinations
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
