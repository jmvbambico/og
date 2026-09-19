---
name: investigate
description: Answer read-only questions about a codebase by dispatching explorer sub-agents and synthesizing their reports. Use for debugging, audits, and "how does X work" questions.
---

# Investigate — delegated read-only inquiry

When the human asks you to investigate, debug, audit, explain, compare, or
answer a repository-specific technical question in any depth, the answer comes
from sub-agents, not from your own sprawling read of the codebase.

A quick look at a file or two to orient yourself or scope the delegation is
fine. Reading broadly to answer the question yourself is not — it burns your
context on work a fresh worker does better.

## Method

1. **Decompose the question** into specific, separately-answerable sub-questions.
   Vague dispatches produce vague reports.
2. **Dispatch explorers in parallel**, in one turn, with
   `args.purpose: "explore"`. Give each a precise question and tell it to
   answer with `file:line` evidence.
3. **End your turn.** Wait on the inbox.
4. **Synthesize from their reports**, citing the evidence they returned.

## Choosing the worker

Explores are read-only and cheap to verify, so follow the roster's preference
order — the human ordered it by which quota to spend first. Reach for a
stronger worker only when a question genuinely needs deeper reasoning or a
larger context window. Where independent perspectives matter, send the same
question to two vendors and compare.

## Reporting

Ground every claim in a sub-agent's evidence. When reports disagree, say so
rather than silently picking one — a disagreement between two explorers is
usually the most informative result you have, and often points at the real
answer.

If the explorers came back empty, say so plainly. "I don't know" after workers
have actually looked is a legitimate answer; guessing is not.

## Never dead-end

Your workers are full coding harnesses with tools and context you do not
have, so a gap of yours is rarely a gap of theirs — dispatch, do not guess.
