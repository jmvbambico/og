---
name: cross-review
description: Verify a batch diff with an independent different-vendor reviewer before opening the PR. Use after fanout has produced a green integration branch.
---

# Cross-review — independent verification

The point is **independence**. A model reviewing work from its own family
shares the blind spots that produced it, so the reviewer must be a different
vendor than whoever implemented.

## Rules

1. **Different vendor, always.** `coder` is Google; `reviewer` is Anthropic;
   `coder_zen` is neither. Never route a diff to a reviewer of the same family
   as its implementer.
2. **Never use `coder_zen` as a reviewer.** Its free model lineup rotates, so
   its judgement quality varies run to run — and unlike implementation, review
   has no gate behind it to catch a bad result.
3. **Diff and contract only.** Pass the combined diff and the acceptance
   contracts as TEXT. Never point the reviewer at a worktree. A reviewer with
   filesystem access becomes an implementer, and its stray edits can reach the
   deliverable.
4. **The reviewer never edits.** It reports; you route.

## Dispatch

```
sys_session_send(
  agent: "reviewer",
  title: "review-<goal-slug>",
  args: { purpose: "review", input: "<contracts>\n\n<combined diff>" }
)
```

Then end your turn and wait on the inbox.

## Acting on the result

- **BLOCKING issues** → re-dispatch fix-tasks to an implementer, in a clean
  worktree branched from the integration branch. Then re-gate and re-review.
  Do not fix them yourself — you do not write code.
- **NON-BLOCKING / SUGGESTIONS** → include them in the PR body so the human can
  decide. Do not silently act on them; they were explicitly judged not to
  justify holding the batch.

If the reviewer says it could not verify something, treat that as an open
question for the human — not as an approval.

## When cross-vendor review is impossible

If the only available workers are the same vendor, you may proceed with
same-vendor review ONLY if you:
1. say so explicitly in chat, before dispatching, and
2. label the PR body `degraded-review`.

Never let a degraded review pass silently. If no reviewer is available at all,
stop and hand the batch to the human unreviewed — clearly labelled as such.

## Then

Open ONE PR for the batch. Report the link to the human. **Do not merge.**
