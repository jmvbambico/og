---
name: fanout
description: Run independent tasks in parallel git worktrees, then batch them onto one integration branch for a single gated PR. Use when a goal splits into 2+ tasks that do not depend on each other.
---

# Fanout — parallel work, batched delivery

Use when the goal decomposes into tasks that can proceed independently. If the
tasks depend on each other, do them sequentially instead — fanning out
dependent work produces conflicting diffs and wasted quota.

**Preference order is not exclusivity.** The roster lists workers in preference
order (`coder_zen`, then `coder_cline`, then `coder`), but that order decides
who gets the FIRST task and who absorbs a single task's overflow — it does not
mean queueing everything onto `coder_zen`. When you have N independent tasks,
spread them across N distinct workers so they run concurrently.

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

One worktree per task, branched from the configured `integration_base`:

```bash
git worktree add ../wt-<task-slug> -b task/<task-slug> <integration_base>
```

Never let two workers share a worktree.

## 3. Dispatch

Dispatch all implementers in the SAME turn so they run concurrently, then end
your turn. Each `sys_session_send` sets:
- `title` — names the WORK, not the vendor (`fix-sse-retry`, not `coder`)
- `args.purpose` — `implement`
- `args.input` — the task plus its acceptance contract plus its worktree path
- `args.model` — **omit it.** Every worker pins its own model in its spec, and
  `args.model` silently overrides that pin. Pass it only in the one case the
  Zen model preflight names: `coder_zen`'s pinned id has dropped out of the
  free lineup and you are substituting a replacement for this run.

Respect the per-turn dispatch cap. If you have more tasks than the cap, run
them in waves rather than trying to exceed it.

## 4. Collect

Wait via the inbox. Do not poll. When a worker reports:
- Verify its claims against the worktree yourself — read the diff, and do not
  trust a reported gate result you did not see.
- On failure, classify it (boot / task / quota) and respond per the
  orchestrator's failure rules.

## 5. Batch

Once ALL workers are done, create the integration branch and land each
worktree's commits onto it:

```bash
git worktree add ../wt-integration -b integration/<goal-slug> <integration_base>
# then merge or cherry-pick each task branch, preserving attribution
```

**Keep each worker's commits separate and attributed.** If the batch fails its
gate you must be able to identify which worker caused it. Squashing here
destroys exactly the information you will need.

## 6. Gate once

Run the FULL gate on the integration branch — every gate in
`.agents/orchestration.yaml`, including those with no `scope`. This is the only
place the integration suite runs.

If the batch fails, bisect by task branch rather than re-running everything:
the per-worker commits tell you where to look. Re-dispatch only the failing
task, in a clean worktree.

## 7. Hand off to review

Produce the combined diff against `integration_base` and pass it, with all the
task contracts, to the `cross-review` skill.

## Cleanup

Remove worktrees once their work is landed and the batch is green:

```bash
git worktree remove ../wt-<task-slug>
```

Leaving stale worktrees behind confuses the next run's branch state.
