---
name: fanout
description: Run independent tasks in parallel git worktrees, then batch them onto one integration branch for a single gated PR. Use when a goal splits into 2+ tasks that do not depend on each other.
---

# Fanout — parallel work, batched delivery

Use when the goal decomposes into tasks that can proceed independently. If the
tasks depend on each other, do them sequentially instead — fanning out
dependent work produces conflicting diffs and wasted quota.

**Preference order is not exclusivity.** The roster order decides who gets the
FIRST task and who absorbs overflow — it does not mean queueing everything onto
the first worker. With N independent tasks and N available workers, spread
them so they run concurrently.

## 0. Check capacity

Before decomposing, run `og stats --json` (og is on PATH; if it is missing or
errors, proceed and say so once). Map `coder_<id>` -> `<id>` and `reviewer` ->
the reviewer's id. A worker whose state is `dry` with `reset_at` in the future
is out of capacity: skip it and take the next worker in preference order.
Preference order still wins — only when two candidates are otherwise equal does
`ok` outrank `unknown`. The roster skill's Capacity section says the rest,
including how to mark a worker dry on a quota failure and re-admit it after
`reset_at`.

## 1. Decompose

Split the goal into tasks that touch **disjoint file sets** wherever possible.
Overlapping tasks will conflict when you batch them, and resolving that costs
more than running them serially.

For each task write an **acceptance contract** before dispatching:
- the files and scope it may touch
- what "done" means, concretely
- the worker-scope gates it must pass
- anything it must NOT do

You will hand this same contract to the reviewer later. If you cannot write a
crisp contract, the task is not scoped well enough to delegate yet.

## 2. Create a worktree per task

Worktrees live OUTSIDE the repo, in a `.worktrees/` directory beside it, one
subdirectory per repo (`~/projects/.worktrees/<repo>/<task>` for a repo at
`~/projects/<repo>`). Branches are `feature/<task-slug>` — the prefix the
branch-cleanup policy allows you to delete after a merge; `task/` is not.

```bash
WT=$(dirname "$REPO")/.worktrees/$(basename "$REPO")
git -C "$REPO" worktree add "$WT/<task-slug>" -b feature/<task-slug> <integration_base>
```

One worktree per task; never let two workers share one. Pass the absolute
worktree path in the dispatch — the worker must never touch the main checkout.

A worktree does not inherit the repo's code-intelligence index. If the repo
root has one (the owner opted in — e.g. a `.codegraph/` directory), build the
worktree's own with that tool's init command right after `worktree add`; it
takes seconds and lets the worker query instead of grep. If the repo has no
index, do nothing: indexing is the owner's decision, not yours or the worker's.

## 3. Dispatch

Dispatch all implementers in the SAME turn so they run concurrently, then end
your turn. Each `sys_session_send` sets `title` (the WORK, not the vendor),
`args.purpose: implement`, and `args.input` — the task, its acceptance
contract, and its worktree path. Never pass `args.model`: every worker pins
its own, and the only exception is the model preflight in your prompt.

Two lines belong in EVERY `args.input`, because some harnesses never see
their spec prompt and only read what you send:

- **Unattended.** "Nobody is watching: never call a question / ask-user tool
  or end a turn on a question. When the task leaves a choice open, take the
  option most consistent with the contract and AGENTS.md, continue, and state
  the assumption in your report." A worker parked on a question blocks the
  whole run until a human happens to look.
- **Orient cheaply.** "If a code-intelligence tool is available — an MCP tool
  or CLI that queries an indexed code graph — use it before grepping; read
  each file once and do not re-read whole files after edits." Name no
  specific tool: the worker checks what is actually present.

Respect the per-turn dispatch cap. If you have more tasks than the cap, run
them in waves rather than trying to exceed it.

## 4. Collect

Wait via the inbox. When a worker reports, verify its claims against the
worktree yourself — read the diff; do not trust a gate result you did not see.
On failure, classify it (boot / task / quota) per your prompt's failure rules.
On a QUOTA failure (rate limit, usage cap, out of credits, Kilo's `Add credits
to continue, or switch to a free model`, freebuff's `not enough Freebucks`, an
OpenCode worker gone silent with `Rate limit exceeded` in its log, or a Cline
worker returning an empty turn), mark it dry with
`og stats --mark <id> dry --until <reset_at from stats if known, else +1h>
--reason "<the error line>"` before moving to the next worker, and re-dispatch
from a CLEAN worktree.

## 5. Batch

Once ALL workers are done, create the integration branch and land each
worktree's commits onto it:

```bash
git -C "$REPO" worktree add "$WT/integration-<goal-slug>" -b integration/<goal-slug> <integration_base>
# then merge or cherry-pick each feature/<task-slug>, preserving attribution
```

Keep each worker's commits separate and attributed — squashing destroys the
information you need when the batch fails.

## 6. Gate once

Run the FULL gate on the integration branch — every gate in the contract,
including those with no `scope`. If it fails, bisect by task branch rather
than re-running everything, and re-dispatch only the failing task in a clean
worktree.

## 7. Hand off to review

Produce the combined diff against `integration_base` and pass it, with all the
task contracts, to the `cross-review` skill.

## Cleanup

Remove worktrees once their work is landed and the batch is green, then
prune the registry so a deleted directory is not still listed:

```bash
git -C "$REPO" worktree remove "$WT/<task-slug>"
git -C "$REPO" worktree prune
```

Leaving stale worktrees behind confuses the next run's branch state.
