# `antigravity-native` never signals turn completion

An Omnigent harness finding, recorded here so an Omnigent maintainer can act on
it. This repo ships no harness plugins, so nothing here fixes it; the og-side
mitigation is a note in the generated `roster` skill (see below).

Observed 2026-09-27 against a generated og bundle whose reviewer is `agy`
(Antigravity/Google).

## Symptom

A dispatch to an `agy`-backed reviewer produces its answer, and then nothing:
the task sits `status: running` with `runner_online: true` indefinitely, no
result lands in the orchestrator's inbox, and the answer is visible only on
screen. It never transitions to a terminal state, so the orchestrator waits
forever on a turn that is already over.

## Evidence

The harness already knows the turn ended — it just does not report it.

- `~/.omnigent/antigravity-native/<id>/state.json` contains
  `{"active_turn_id": null, ...}` while the session is still `running`.
- `sys_session_get_info` reports `runner_online: true` throughout; nothing is
  posted to the conversation.
- Both steps below recover the finished answer by hand:
  - read `active_turn_id: null` from that state file to detect turn end;
  - `tmux -S <socket_path from tmux.json> capture-pane -p -S -100000 -t main`
    to read the result off the pane.

## Cause

`antigravity-native` drives `agy` through a **tmux pane** and screen-scrapes the
interactive TUI. On completion the TUI simply returns to its `>` prompt with a
footer and no machine-readable completion marker, so the scraper has nothing to
key a turn-end on even though the harness's own state says the turn is done.

In **print mode** this does not happen: `agy --print "…"` exits cleanly on its
own, which is why a five-second `agy --print` test returns normally while the
interactive session does not.

## Suggested fix (Omnigent side)

Either:

1. drive `agy` non-interactively — `--print --output-format json` (or
   `stream-json`) — and parse the emitted result instead of scraping the TUI;
   or
2. treat the harness's own `active_turn_id: null` as turn-end and post the
   scraped pane content at that point, rather than waiting for a marker the TUI
   never emits.

## og-side mitigation

`render_roster_skill()` in `installer/og_install.py` now documents the workaround
for any `antigravity-native` worker, whichever role it fills: poll
`~/.omnigent/antigravity-native/<id>/state.json` for `"active_turn_id": null`,
then read the answer with
`tmux -S <socket_path from tmux.json> capture-pane -p -S -100000 -t main`.
