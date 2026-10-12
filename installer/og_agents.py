#!/usr/bin/env python3
"""`og agents` — a terminal view of the Omnigent session tree.

It shows the orchestrator (a ROOT session) and the workers it delegated to,
built from two sources and nothing else:

  * the SESSION LISTING, which supplies the tree (`parent_session_id`), the
    titles, the statuses, the timestamps and the pending-elicitation counts; and
  * the DETAIL row (`GET /v1/sessions/{id}`), read lazily, which supplies the
    `workspace` (to resolve "which session is this directory") and the report
    SNIPPET.

The listing is cheap and drives the live view; the detail row is 121 KB and is
fetched as little as possible. See `AgentModel.refresh` for the budget and why a
per-row-per-poll fetch is a defect rather than a slow path.

The shape of the view: one plain header per root (the harness label), then a
full box per worker — `├─`/`└─` down a tree spine, a `▸` caret on the selected
box, one ASCII glyph per state, a 6-dot braille spinner for anything working, a
boxed access panel sized to its own content, and a one-line footer. `render` is
a pure function from state to a list of lines and curses only paints them; that
separation is what lets the view be tested with `--once`, with no tty in sight.

This module is deliberately NOT herdr-specific: it reads Omnigent's HTTP API and
must run in any terminal. It sends NOTHING to a session — no prompting, no
answering an elicitation, no dispatches — and changes no session state. Its read
path is GET-only and goes entirely through `og_herdr_watch`, which owns the
transport, pagination, `kind=any` default, truncation handling and per-server
token matching; none of that is re-implemented here. It makes exactly ONE
outbound call of its own: `agent.focus` (see `focus_in_herdr`), which moves the
operator's own herdr UI to a worker's session and touches no session state. That
call is reached lazily — the herdr client is imported inside the handler, so `og
agents` still runs with no herdr installed, outside a herdr pane, or with the
socket down — and it is pinned by the AST guard in tests/test_og_agents.py, which
asserts `agent.focus` is the ONLY method this module ever hands to `call`.
"""
from __future__ import annotations

import argparse
import ipaddress
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from og_herdr import DEFAULT_AGENT_LABEL, harness_labels
from og_herdr_watch import (
    DEFAULT_BASE_URL,
    SessionWatcher,
    _session_id,
    herdr_state,
)

DEFAULT_INTERVAL = 3.0

# The status glyph shown beside a worker. ASCII on purpose: a pane that renders
# this view's box-drawing characters correctly is not guaranteed to have the
# geometric shapes, and a `?` where a tick should be reads as "I do not know
# this state" when the state is merely idle. `working` has no glyph here — it
# gets the spinner (`state_glyph`), because a moving dot is the one glyph that
# says "this is live" without a legend.
STATE_GLYPH = {
    "idle": "+",
    "blocked": "!",
    "failed": "x",
    "unknown": "?",
}

# The spinner: SIX braille dots, and only the top two rows of each cell (dots
# 1-6, never the 7/8 descenders). That restriction is the whole reason for the
# choice — the 7/8 forms hang below the baseline, so a spinner that used them
# would change the glyph's height every frame and make the row it sits in jitter
# as the box is redrawn. Six frames at SPIN_TICK is one ~0.7s cycle.
SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴"
SPIN_TICK = 0.12

# The DISPLAY state `herdr_state` does not have. See `display_state` for why it
# is derived here instead of being added to the shared vocabulary.
FAILED_STATUSES = frozenset({"failed", "error", "errored", "cancelled", "canceled"})

# Ceilings. The view is meant to run for a long time, so its two caches are
# bounded rather than growing one entry per session the machine has ever seen.
# Both are order-of-magnitude above anything real: MAX_ROOT_DETAILS is the
# number of roots whose detail is worth a 121 KB fetch at startup (the "handful"
# the design budgets for, and the ceiling on the cwd probe in `_match_cwd_root`
# — same dict, same eviction), and MAX_SNIPPETS is a window of reports.
MAX_ROOT_DETAILS = 32
MAX_SNIPPETS = 256

# The access panel (the `a` toggle). It is pinned to the bottom of the pane and
# sized to its own content — the QR block, one URL line and one caveat — never to
# a fraction of the terminal. The list above it shrinks to make room but keeps at
# least ACCESS_LIST_FLOOR rows, so the toggle can never squeeze the tree out of
# view entirely. Three is the smallest number that still shows a header and a
# worker or two, which is enough to stay oriented on a short pane.
ACCESS_LIST_FLOOR = 3

# Two borders plus one space of padding on each side: what a box costs around
# its widest line. The panel is sized to this, not to the pane.
BOX_PADDING = 4

ACCESS_TITLE = "access"

# A long meta line would eat the worker's name inside the box, which is the one
# thing in it that has to stay readable.
MAX_META = 28

# The width `render` assumes when it is not told one: the classic terminal. The
# live view always passes the real width from curses; `--once` falls back here.
DEFAULT_WIDTH = 80

# The footer. Its own constant so the tests can assert the binding is listed
# without driving a tty. Enter FOCUSES the selected worker in herdr (see
# `focus_in_herdr`); snippet expand/collapse moved to SPACE when Enter was taken.
#
# THREE forms, chosen by width in `_footer`. The agents pane is a SPLIT of the
# chat and is often only ~46 columns, while this full sentence is 64, so a single
# fixed footer gets clipped by `_clip` — which is exactly how "space expand" and
# "a access" used to fall off the end, leaving the operator without the key that
# had just inherited expand from Enter. FOOTER_COMPACT swaps the Enter and Space
# words for their key glyphs and drops the doubled spacing; FOOTER_KEYS is the
# floor, naming every key with no labels at all. Only LABELS ever degrade, never
# a key: a key with no label is still findable by pressing it, a key clipped off
# the end is gone. Every glyph here but the pre-existing ↑/↓ is Narrow (checked
# with east_asian_width — ⏎ U+23CE and ␣ U+2423 are not the Wide traps they
# could be); a Wide one would spill past the pane even after `_footer` said it
# fit, so a future label must be measured before it is trusted.
FOOTER = "q quit  r refresh  ↑/↓ move  enter focus  space expand  a access"
FOOTER_COMPACT = "q quit r refresh ↑↓ ⏎ focus ␣ expand a access"
FOOTER_KEYS = "q r ↑↓ ⏎ ␣ a"

# The caveats shown under the address, mirroring bin/og's `show_access`: the
# operator must know whether the address is scannable only on the same wifi or
# from anywhere. Which one it is is the difference between "hand this to the
# person next to you" and "this link is public".
LAN_NOTE = "same wifi only — not reachable from outside your network"
TUNNEL_NOTE = "public — anyone with this link can open the session"
NO_ADDRESS_NOTE = "access: no address resolved (no tunnel URL cached, no LAN IP)"

# A truncated listing is missing rows beyond the page cap, and since the listing
# is newest-first those are the OLDEST sessions. Say so rather than let an
# absence read as a finished conversation.
TRUNCATED_NOTICE = "… partial listing: some sessions may be missing"

# The paint role of a line. Plain strings so the composer stays pure and
# testable; the curses attributes that go with them live in `_run_tui`.
# SELECTED belongs to the CARET ROW of the selected worker and no other line:
# the rest of its box is NORMAL, so selecting a worker marks it with the caret
# instead of reverse-videoing the whole four-row block.
NORMAL, SELECTED, DIM = "normal", "selected", "dim"

# The port used when the server URL names none. Matches DEFAULT_BASE_URL, which
# is `http://127.0.0.1:6767`.
DEFAULT_PORT = urllib.parse.urlsplit(DEFAULT_BASE_URL).port or 6767


# ---------------------------------------------------------------------------
# pure: snippet extraction
# ---------------------------------------------------------------------------

def snippet_from_detail(detail: Any) -> Optional[str]:
    """The report text of a session, from its DETAIL row, or None.

    The snippet is the LAST `items` element that is an assistant message
    (`type == "message"` and `data.role == "assistant"`), with its text taken
    from the `output_text` parts of `data.content`:

        {"id": "25f8b2d7…", "type": "message", "status": "completed",
         "data": {"role": "assistant",
                  "content": [{"type": "output_text", "text": "The work…"}]}}

    The LAST one, not the first: the report is what the agent most recently
    said, and older assistant messages are the turns before it. When that last
    message carries no `output_text` the snippet is None rather than an earlier
    message's text — falling back would show a stale line as if it were the
    report, which is worse than showing nothing.

    Every shape is tolerated without raising, because the detail row is an
    enrichment and a missing one must cost the snippet and nothing else: a
    missing/None/empty `items`, an element that is not a dict, `content` that is
    not a list, and parts that are not dicts or carry no string `text`.
    """
    if not isinstance(detail, dict):
        return None
    items = detail.get("items")
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        data = item.get("data")
        if not isinstance(data, dict) or data.get("role") != "assistant":
            continue
        content = data.get("content")
        if not isinstance(content, list):
            return None
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "output_text"
            and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts) if parts else None
    return None


# ---------------------------------------------------------------------------
# pure: the display state, the worker's identity, and its clock
# ---------------------------------------------------------------------------

def display_state(session: Any) -> str:
    """The state this view DRAWS: `herdr_state`, plus `failed`.

    Deliberately NOT a change to `og_herdr_watch.herdr_state`. That function is
    shared with the herdr bridge, which feeds `should_project` (which sessions
    get a herdr tab) and the pane badges; widening its vocabulary to a fifth
    state would change which sessions are projected and what a pane reads — for
    every consumer — to serve one view's glyph. So the extra case is derived
    here.

    The ONLY difference from `herdr_state` is `unknown` -> `failed`: a failed
    session is exactly what `herdr_state` calls unknown (a status outside its
    two known sets), and `x` is the one thing the operator must not be left to
    infer from an absence. `blocked` still wins over `failed` — a worker holding
    an elicitation is waiting on a human, whatever its status also says.
    """
    state = herdr_state(session)
    if state != "unknown":
        return state
    status = session.get("status") if isinstance(session, dict) else None
    if isinstance(status, str) and status.strip().lower() in FAILED_STATUSES:
        return "failed"
    return state


def state_glyph(state: str, spin: int) -> str:
    """The glyph for a display state: the spinner for `working`, else ASCII.

    Pure in `spin`: the caller owns the frame counter, so render stays a
    function of its arguments and `--once` (which passes 0) has no animation to
    be inconsistent about.
    """
    if state == "working":
        return SPIN_FRAMES[spin % len(SPIN_FRAMES)]
    return STATE_GLYPH.get(state, "?")


def split_title(title: Any) -> tuple[Optional[str], str]:
    """(`zen`, `stock-concurrency-lock`) from `coder_zen:stock-concurrency-lock`.

    A delegated worker's title is `<role>_<agent>:<task>`. The ROLE is dropped
    because it says who dispatched, not what is running; the agent name is what
    tells two workers running side by side apart, and the task is what the
    operator is actually looking for.

    `sub_agent_name` would name the agent too, but it is NOT on the listing row
    (measured: it is a detail field), so reading it would cost a 121 KB fetch per
    worker to learn what the title already carries.

    A title with no colon yields `(None, title)`: an older session or one the
    server titled plainly still has to render as a row. A role prefix other than
    `coder_` is kept whole rather than guessed at — the box has room for it, and
    inventing a second parse rule would mislabel more titles than it fixes.
    """
    text = title if isinstance(title, str) else ""
    prefix, sep, task = text.partition(":")
    if not sep:
        return None, text
    agent = prefix[len("coder_"):] if prefix.startswith("coder_") else prefix
    return agent or None, task.strip() or text


def _epoch(value: Any) -> Optional[float]:
    """A LISTING timestamp as epoch seconds, or None when there is not one.

    `bool` is excluded explicitly: it is an `int` in Python, and `True` as a
    timestamp is data the server did not send.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _age(seconds: float) -> str:
    """A duration as `41s`, `2m14s`, `1h04m`. Compact, no leading zero.

    Below a minute the seconds are the useful precision; above it they are noise
    on a line two columns from the worker's name.
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _ago(seconds: float) -> str:
    """How long ago, coarse: `12s ago`, `6m ago`, `2h ago`.

    "How long since" is read at a glance, so it drops to the largest unit that
    still says something; `_age` is for the running figure, which is watched
    ticking.
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s ago"
    if total < 3600:
        return f"{total // 60}m ago"
    return f"{total // 3600}h ago"


# The listing row fields a failure reason might arrive in, if it ever does. The
# listing carries none of them today (see og_herdr_watch's measured key list),
# so `_failure_reason` normally finds nothing and the row reads plain `failed`.
_FAILURE_KEYS = ("failure_reason", "error_message", "error", "reason")


def _failure_reason(session: dict) -> Optional[str]:
    """A short reason for a failed session, from the LISTING row only.

    Free when the server carries one — no fetch, no wait, and the operator gets
    `boot failed` instead of a bare `x` — which is the only reason to look: a
    detail fetch per failed worker would be a new bill for information the
    listing row is the right place to put.
    """
    for key in _FAILURE_KEYS:
        value = session.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return None


def worker_meta(session: dict, state: str, now: float) -> str:
    """The right half of a worker's second line: `2m14s`, `waiting on you`, …

    The clock is honest about what it actually knows. BOTH timestamps come off
    the LISTING row (so no detail fetch buys a clock on every row), and neither
    is a measured start: `created_at` is when the server created the session
    ROW, and `updated_at` is its LAST ACTIVITY — a worker that has been thinking
    for ten minutes updates it constantly. So:

      * working -> how long the session has EXISTED. For a worker dispatched to
        run a task that is close to its runtime, and it is labelled for what it
        is rather than dressed up as a measured duration.
      * idle -> `updated_at` IS when it stopped, so `done 6m ago` is exact.

    A row with no usable timestamp falls back to a plain word rather than a
    figure derived from the epoch.
    """
    if state == "blocked":
        return "waiting on you"
    if state == "failed":
        reason = _failure_reason(session)
        return _clip(reason, MAX_META) if reason else "failed"
    if state == "working":
        created = _epoch(session.get("created_at"))
        return _age(now - created) if created is not None else "working"
    updated = _epoch(session.get("updated_at"))
    return f"done {_ago(now - updated)}" if updated is not None else "idle"


# ---------------------------------------------------------------------------
# pure: which roots, and the rows of the tree
# ---------------------------------------------------------------------------

def _norm_path(path: str) -> str:
    """A directory string normalised for comparison.

    realpath also collapses `..`, trailing separators and symlinks, so a
    workspace recorded as `/repo/og` matches a shell `$PWD` of `/repo/og/` or a
    path that reached the same place through a symlink. It does NOT require the
    path to exist, which matters because the session may name a directory this
    process cannot stat.

    Note this is deliberately not `Path.resolve`: the read-only guard in the
    tests forbids this module from CALLING anything whose name contains
    `resolve`, so that a stray call can never look like an elicitation
    resolver. `realpath` does the same job under a name the guard leaves alone.
    """
    return os.path.realpath(os.path.expanduser(path))


def select_scope(
    roots: Iterable[dict], cwd: str, all_roots: bool
) -> list[str]:
    """The root sessions the view shows, as a list of session ids.

    The cwd rule, and the same one the launcher uses: show the tree whose
    session's `workspace` is `$PWD`, if there is one; otherwise show every live
    root. `all_roots` (`--all`) forces the wide view even when one matches.

    Roots come in already filtered to the live set, so this only decides
    NARROWING. `workspace` is a detail-row field; a root whose detail could not
    be read simply has no match and does not narrow anything — the view falls
    back to the wide set rather than hiding the session the user is in because
    one optional fetch failed.
    """
    ids = [sid for sid in (_session_id(s) for s in roots) if sid]
    if all_roots or not cwd:
        return ids
    target = _norm_path(cwd)
    for root in roots:
        workspace = root.get("workspace")
        if isinstance(workspace, str) and workspace and \
                _norm_path(workspace) == target:
            return [_session_id(root)]
    return ids


def _is_root(session: dict) -> bool:
    """A session with no parent is a root: the conversation the human drives."""
    parent = session.get("parent_session_id")
    return not (isinstance(parent, str) and parent)


def _index(sessions: Iterable[dict]):
    """`(by_id, children)` in listing order.

    `by_id` maps a session id to its object; `children` maps a parent id to the
    ids of its children, in the order the listing gave them (newest-first, so a
    parent's newest worker is first among its siblings). A child whose parent is
    not in this listing is not indexed as a child — it would have no row to hang
    under; see `_preorder`.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for session in sessions:
        sid = _session_id(session)
        if sid and sid not in by_id:
            by_id[sid] = session
            order.append(sid)
    children: dict[str, list[str]] = {}
    for sid in order:
        parent = by_id[sid].get("parent_session_id")
        if isinstance(parent, str) and parent and parent in by_id:
            children.setdefault(parent, []).append(sid)
    return by_id, children


def _preorder(by_id: dict, children: dict, root_ids: Iterable[str]):
    """`[(session_id, depth, spine, last)]` for the selected roots' trees.

    Pre-order, and `spine` is the tree columns ABOVE this row: one two-column
    entry per ancestor level, `│ ` while something follows at that level and `  `
    once nothing does. `last` says the same thing for this row's own level, and
    is what picks `└─` over `├─`. A root carries no spine and is always `last`:
    it is drawn as a plain header, so its workers start in the first column and
    nothing can hang off an edge that was never drawn.

    A `seen` set makes a malformed listing — a cycle, or a session that is its
    own parent — terminate rather than recurse forever.
    """
    out: list[tuple[str, int, tuple[str, ...], bool]] = []
    seen: set[str] = set()

    def walk(sid: str, depth: int, spine: tuple[str, ...], last: bool) -> None:
        if sid in seen or sid not in by_id:
            return
        seen.add(sid)
        out.append((sid, depth, spine, last))
        kids = children.get(sid, [])
        # A root is a header: it draws no edge, so its workers start in the
        # first column and there is nothing above them to continue. A worker's
        # children inherit its column, closed once nothing follows at that level.
        kid_spine = spine if depth == 0 else spine + ("  " if last else "│ ",)
        for position, child in enumerate(kids):
            walk(child, depth + 1, kid_spine, position == len(kids) - 1)

    for rid in root_ids:
        walk(rid, 0, (), True)
    return out


@dataclass
class Row:
    """One row of the tree, before it is turned into text.

    A ROOT row is a header: `text` is the harness label and nothing else is
    drawn. A WORKER row is a box: `text` is the task (the title with its
    `<role>_<agent>:` prefix removed), `agent` and `meta` are the second line,
    and `last`/`spine` place it in the tree.
    """

    session_id: str
    text: str
    state: str
    depth: int
    is_root: bool = False
    snippet: Optional[str] = None
    expanded: bool = False
    agent: Optional[str] = None
    meta: str = ""
    last: bool = True
    spine: tuple[str, ...] = ()


@dataclass
class Access:
    """What the bottom panel shows: an address, a QR to scan, and a caveat.

    `url` is None when nothing resolved, and then `note` is the one honest line
    the panel shows instead of a fabricated address. `qr` holds the candidate
    block, already generated; whether it is actually drawn is decided at render
    time against the pane's width, where the width is known.
    """

    url: Optional[str] = None
    qr: list[str] = field(default_factory=list)
    note: Optional[str] = None


@dataclass
class Frame:
    """The whole view at one instant: the rows, plus anything to say about it.

    `selected` is an index into `rows` and `spin` is the spinner frame counter.
    Both live on the frame rather than being read from a clock so `render` stays
    pure: the same frame rendered twice is the same two screens.
    """

    rows: list[Row] = field(default_factory=list)
    truncated: bool = False
    notice: Optional[str] = None
    access: Optional[Access] = None
    selected: int = 0
    spin: int = 0


@dataclass
class Pane:
    """The composed view: every line with its paint role, and where the list ends.

    `items` is parallel with itself — `items[i] == (text, role)` — and
    `list_rows` counts the lines above the tail, so the TUI can scroll the list
    and pin the panel and the footer to the bottom of the pane without
    re-deriving the layout that produced them.
    """

    items: list[tuple[str, str]] = field(default_factory=list)
    list_rows: int = 0


def harness_label(harness: Any) -> str:
    """The product name for a harness id, from installer/registry.json.

    `claude-native` -> "Claude Code". A harness the registry does not list falls
    back to its raw id, which is ugly but honest: it names exactly what ran. A
    session that names no harness at all falls back further, to
    `og_herdr.DEFAULT_AGENT_LABEL`.

    The label table is read through `og_herdr.harness_labels` rather than a
    second copy of it here, so renaming a harness in the catalog cannot leave
    this view saying the old name. That loader never raises: a broken catalog
    degrades every label to a raw harness id, and a row is never lost over a
    cosmetic failure.
    """
    if not isinstance(harness, str) or not harness:
        return DEFAULT_AGENT_LABEL
    return harness_labels().get(harness, harness)


def build_rows(
    sessions: Iterable[dict],
    root_ids: Iterable[str],
    snippets: Optional[dict[str, Optional[str]]] = None,
    expanded_ids: Iterable[str] = (),
    now: Optional[float] = None,
) -> list[Row]:
    """The display rows for the selected roots' trees, in listing order.

    A root row is labelled by its harness (the product name), matching the
    orchestrator header the operator's web UI shows, and carries no glyph: it is
    a header, not a worker. A worker row is labelled by its task and carries the
    agent name, a glyph and a meta line. `snippets` is consulted per row, and a
    row is marked expanded only when its id is in `expanded_ids` AND it has a
    snippet to show.

    `now` is the clock the meta lines are measured against; it defaults to the
    wall clock so a caller that only wants a tree need not supply one. Every
    test passes it explicitly — a duration asserted against the real clock is a
    flake waiting for a slow machine.
    """
    snippets = snippets or {}
    expanded = set(expanded_ids)
    clock = time.time() if now is None else now
    sessions = list(sessions)
    by_id, children = _index(sessions)
    rows: list[Row] = []
    for sid, depth, spine, last in _preorder(by_id, children, root_ids):
        session = by_id[sid]
        state = display_state(session)
        if depth == 0:
            harness = session.get("harness")
            label = harness_label(harness) if isinstance(harness, str) \
                and harness else None
            rows.append(Row(
                session_id=sid,
                text=label or session.get("title") or sid,
                state=state,
                depth=depth,
                is_root=True,
            ))
            continue
        agent, task = split_title(session.get("title"))
        snippet = snippets.get(sid)
        rows.append(Row(
            session_id=sid,
            text=task,
            state=state,
            depth=depth,
            snippet=snippet,
            expanded=sid in expanded and snippet is not None,
            agent=agent,
            meta=worker_meta(session, state, clock),
            last=last,
            spine=spine,
        ))
    return rows


# ---------------------------------------------------------------------------
# pure: the renderer (state -> lines)
# ---------------------------------------------------------------------------

def _oneline(text: str) -> str:
    """Collapse a multi-line report to one line for the tree."""
    return " ".join(text.split())


def _clip(text: str, width: int) -> str:
    """`text` cut to `width` columns, with an ellipsis if it had to be cut.

    An ellipsis rather than a hard cut because a silently truncated row reads as
    the whole name, and the operator goes looking for a session that does not
    exist under the name they were shown.
    """
    if width <= 0:
        return ""
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


def _pad(text: str, width: int) -> str:
    """`text` clipped to `width` and right-padded out to exactly `width`."""
    return _clip(text, width).ljust(width)


# SGR colour escapes: `\x1b[` … `m`. `qrencode -t ANSIUTF8` wraps every line in
# one of these, and they must never reach `addnstr`: curses does not interpret
# escapes, it PRINTS them, which is how the operator's pane came to show
# `^[[40;37;1m` down both sides of the code. `--once` looked fine only because
# the terminal running it interpreted them. Stripping is safe because the escapes
# carry no information here: ANSIUTF8's `40;37` is black-on-white, which is what
# a dark terminal already gives the block characters.
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _visible_width(line: str) -> int:
    """The display columns of one rendered line, ignoring ANSI SGR escapes.

    `len(line)` counts the escapes as visible characters, so a `qrencode -t
    ANSIUTF8` block measures two to three times too wide that way — a code that
    fits is judged not to, and the panel silently degrades to URL-only forever.
    (The equivalent for measurement only is bin/og's `print_qr`, which reads the
    ASCII render — two single-byte characters per module, so bytes/2 is the
    column count — but this view already has the displayed block in hand, so it
    measures that instead of spending a second `qrencode` call.)
    """
    return len(_ANSI_SGR.sub("", line))


def _display_width(text: str) -> int:
    """The display columns of plain `text`: a Wide glyph costs two, the rest one.

    `_clip` and `_visible_width` count with `len`, which is right for every
    character in this view EXCEPT a Wide/Fullwidth one: counting that as a single
    column judges a string narrower than the terminal paints it, so it is passed
    as fitting and then spills past the pane — the CJK-locale form of the very
    bug the footer keeps re-learning. Only the footer is measured here, and it
    names its glyphs Narrow for exactly this reason.

    No ANSI stripping: the footer is plain text. A line that may carry escapes is
    measured through `_visible_width` instead.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _boxed(lines: Iterable[str], width: int) -> list[str]:
    """`lines` in a box as wide as their widest row, never wider than `width`.

    Content-sized, not pane-sized: a QR boxed out to eighty columns is mostly
    border, and the border is what the eye lands on when scanning for the code.
    """
    rows = [_clip(text, max(1, width - BOX_PADDING)) for text in lines]
    # Never narrower than the title rule, or the box loses its rectangle.
    inner = max(max((len(row) for row in rows), default=0),
                len(ACCESS_TITLE) + 3) + 2
    head = f"┌─ {ACCESS_TITLE} " + "─" * (inner - len(ACCESS_TITLE) - 3) + "┐"
    body = [f"│ {row.ljust(inner - 2)} │" for row in rows]
    return [head] + body + ["└" + "─" * inner + "┘"]


def access_lines(access: Access, width: int) -> list[str]:
    """The access panel, as a titled box sized to its own content.

    Flush LEFT on purpose: it is not a worker, so a tree spine in front of it
    would read as one more agent in the tree — and the operator has been reading
    this pane for a month with the workers indented under a header.

    The QR is drawn only when the box can be at least as wide as the code. A
    wrapped code is unreadable noise — worse than none, because someone will
    still try to scan it — so a pane narrower than the code keeps the URL line,
    which is still a usable address.

    Every line out of here is stripped of ANSI SGR escapes: curses prints what
    it is given, so an escape that reaches `addnstr` shows up as `^[` in the
    pane rather than as a colour.
    """
    if access.url is None:
        return _boxed([access.note or NO_ADDRESS_NOTE], width)
    content: list[str] = []
    qr = [_ANSI_SGR.sub("", line) for line in access.qr]
    # Measured with `_visible_width` rather than `len`: "does the code fit" is a
    # question about the columns the terminal will paint, and the rule that says
    # so lives in one place.
    if qr and max(_visible_width(row) for row in qr) + BOX_PADDING <= width:
        content.extend(qr)
        # A blank row between the code and the prose, so the eye lands on the
        # code first and the URL is not read as part of it.
        content.append("")
    content.append(access.url)
    if access.note:
        content.append(access.note)
    return _boxed(content, width)


def row_block(row: Row, width: int, spin: int = 0,
              selected: bool = False) -> list[str]:
    """One worker as a full box: four rows, or five with its report expanded.

    The tree spine is BAKED INTO the returned rows rather than added by the
    caller, so the tree reads top to bottom whatever a box costs in rows — the
    `├─` on the top row, `│ ` beside the middle rows while something follows,
    and spaces once this is the last row.

    The caret goes on the FIRST row only. Repeating it down the box would read
    as four selected things.
    """
    inner = max(4, width - 6 - 2 * len(row.spine))
    prefix = "".join(row.spine)
    elbow = "└─" if row.last else "├─"
    side = "  " if row.last else "│ "
    caret = "▸" if selected else " "
    info = f"{row.agent} · {row.meta}" if row.agent else row.meta

    def body(content: str) -> str:
        return f"{prefix}{side}  │" + _pad(content, inner) + "│"

    lines = [
        f"{prefix}{elbow} {caret}┌" + "─" * inner + "┐",
        body(f" {state_glyph(row.state, spin)} {row.text}"),
        body(f"   {info}"),
    ]
    if row.expanded and row.snippet:
        lines.append(body(f"   {_clip(_oneline(row.snippet), inner - 3)}"))
    lines.append(f"{prefix}{side}  └" + "─" * inner + "┘")
    return lines


def frame_working(frame: Frame) -> bool:
    """Whether any WORKER is working — the only thing that animates.

    A root is a plain header and carries no glyph, so a working root changes
    nothing on screen and must not schedule a repaint; see `paint_needed`.
    """
    return any(not row.is_root and row.state == "working" for row in frame.rows)


def _workers(rows: list[Row]) -> list[int]:
    """The indexes of the rows that draw a box — everything but the headers."""
    return [i for i, row in enumerate(rows) if not row.is_root]


def snap_selection(rows: list[Row], selected: int) -> int:
    """`selected` if it still names a worker row, else the first one.

    Run after every refresh, when the row list has just changed underneath the
    caret. The fallback is the first worker rather than row 0 because row 0 is
    the group header, which draws no box and therefore no caret: a view that
    opened on it would look like it had no selection at all.
    """
    workers = _workers(rows)
    if not workers:
        return 0
    return selected if selected in workers else workers[0]


def move_selection(rows: list[Row], selected: int, delta: int) -> int:
    """The selection after a keypress: the next or previous WORKER.

    Headers are stepped over instead of landed on, for the same reason
    `snap_selection` prefers a worker: the caret belongs to a box.
    """
    workers = _workers(rows)
    if not workers:
        return 0
    at = workers.index(selected) if selected in workers else -1
    return workers[min(max(at + (1 if delta > 0 else -1), 0), len(workers) - 1)]


def _footer(width: int) -> str:
    """The footer form that fits `width` columns, widest readable first.

    The same shape the rest of the pane already uses for an element that will not
    fit: `_pane` trims the list to `height` and `access_lines` drops the QR when
    the box is too narrow for it. Each picks a SMALLER representation rather than
    being clipped, and the footer does too — full, then FOOTER_COMPACT, then the
    bare FOOTER_KEYS. The widths compared are the forms' own measured widths, so
    there is no hard-coded threshold to drift out of step with the strings.

    Below the KEYS width there is nothing left to drop but keys themselves; the
    agents pane is never that narrow (it is a ~46-column split, and `access_lines`
    has already given up its QR by 31), so `_pane`'s `_clip` guard is the only
    thing that could cut here, and it would cut a key list, not mid-sentence.
    """
    if width >= _display_width(FOOTER):
        return FOOTER
    if width >= _display_width(FOOTER_COMPACT):
        return FOOTER_COMPACT
    return FOOTER_KEYS


def _pane(frame: Frame, width: int = DEFAULT_WIDTH,
          height: Optional[int] = None) -> Pane:
    """The frame composed into lines with their paint roles. Pure.

    The single place the view is put together: `render` returns just the text,
    and the TUI paints these same lines with the attributes their roles name.
    `height`, when given, is the pane's content height — the tail (panel and
    footer) is kept whole and the list above it is trimmed to fit, never below
    ACCESS_LIST_FLOOR rows and never by cutting a box in half.

    A selected worker's block is NORMAL except its first line, the caret row,
    which carries SELECTED: the caret is the selection marker, not a reverse
    block over all four rows.
    """
    items: list[tuple[str, str]] = []
    if frame.notice:
        items.append((_clip(frame.notice, width), NORMAL))
    elif frame.truncated:
        items.append((_clip(TRUNCATED_NOTICE, width), NORMAL))

    blocks: list[list[tuple[str, str]]] = []
    for index, row in enumerate(frame.rows):
        if row.is_root:
            blocks.append([(_clip(row.text, width), NORMAL)])
            continue
        on_caret = index == frame.selected
        block = [(line, NORMAL)
                 for line in row_block(row, width, frame.spin, on_caret)]
        if on_caret:
            # Only the caret row carries SELECTED; the body below it stays
            # NORMAL. Tagging the whole block, as this once did, made the TUI
            # reverse-video all four rows — the operator read that as four
            # selected things and asked for the caret to be the marker. The
            # role is kept on that one line rather than dropped because it is
            # what the live view scrolls to keep visible (`_run_tui`), and it
            # is painted bold there so the caret row stays findable when the
            # glyph itself is easy to miss.
            block[0] = (block[0][0], SELECTED)
        blocks.append(block)

    tail: list[tuple[str, str]] = [("", NORMAL)]
    if frame.access is not None:
        tail.extend((line, DIM) for line in access_lines(frame.access, width))
        tail.append(("", NORMAL))
    tail.append((_clip(_footer(width), width), DIM))

    room = None if height is None else max(ACCESS_LIST_FLOOR, height - len(tail))
    kept: list[list[tuple[str, str]]] = []
    used = 0
    for block in blocks:
        if room is not None and kept and used + len(block) > room:
            break
        kept.append(block)
        used += len(block)

    for block in kept:
        items.extend(block)
    return Pane(items=items + tail, list_rows=len(items))


def render(frame: Frame, width: int = DEFAULT_WIDTH,
           height: Optional[int] = None) -> list[str]:
    """A frame as a list of plain-text lines. Pure; no terminal, no clock.

    This is the whole renderer. curses paints exactly these lines, and `--once`
    prints them, so the shape of the view is tested here without a tty — the
    access panel included, since it is part of the lines and not something drawn
    onto the window.

    `width` is the pane's column count. `height`, when given, is the content
    height: the tail is reserved and the list is trimmed above it. `--once` and
    the tests pass height=None, meaning "show everything".
    """
    return [text for text, _role in _pane(frame, width, height).items]


def paint_needed(items: list[tuple[str, str]],
                 painted: Optional[list[tuple[str, str]]]) -> bool:
    """Whether these lines have to be written to the screen.

    Nothing is repainted unless the composed view actually changed: a new data
    poll that brought something new, a keypress that moved the caret, a resize,
    or a spinner frame while something is working. That is the difference
    between an `og agents` pane an operator leaves open all day and one that
    redraws itself forever — with nothing working there is no spinner to
    advance, the render is identical, and this is False, so `erase`/`refresh`
    never run and the loop blocks on the keyboard instead.
    """
    return painted is None or items != painted


def next_wait(now: float, poll_due: float, working: bool,
              tick: float = SPIN_TICK) -> float:
    """Seconds to block on the keyboard before anything could change on screen.

    Nothing working: only the next data poll can change the view, so block until
    then — the pane wakes once per poll rather than eight times a second.
    Something working: the spinner advances on its own tick, so wake then,
    unless the poll comes first.
    """
    wake = min(now + tick, poll_due) if working else poll_due
    return max(0.0, wake - now)


# ---------------------------------------------------------------------------
# the model: listing + lazy detail
# ---------------------------------------------------------------------------

class AgentModel:
    """Builds a `Frame` from the listing, fetching detail only when it must.

    THE BUDGET. One refresh is exactly one listing walk plus a bounded number of
    detail fetches, and detail fetches happen on these occasions and no others:

      1. once per LIVE ROOT, to learn its `workspace` (the cwd rule needs it)
         and its `harness` (the header label). Roots come and go, so this is
         "once per root, on its first sighting", not "once per poll".
      2. once per root the cwd rule probes past the live set, and only until one
         matches (see `_match_cwd_root`). Also once per root, because the result
         is cached under the same session id.
      3. once per worker that has gone IDLE, to capture the report it just
         produced. An idle report stops changing, so it is fetched once and kept.
      4. once for a row the user SELECTED, so its snippet can be read on demand
         (and refetched when the user forces a refresh, while it is still
         running).

    Never "one per row per poll": a session already in a cache is not fetched
    again, so a stable listing costs nothing after the first refresh. The detail
    row is 121 KB and cannot be trimmed (`?items_limit`, `?limit` and
    `?include=summary` are all ignored by the server and return the full item
    list), so at ~16 workers a per-poll fetch would be ~2 MB every few seconds.
    """

    def __init__(
        self,
        watcher: SessionWatcher,
        cwd: Optional[str] = None,
        all_roots: bool = False,
    ) -> None:
        self.watcher = watcher
        self.cwd = cwd if cwd is not None else os.getcwd()
        self.all_roots = all_roots
        # root id -> {workspace?, harness?}, from one detail fetch each.
        self._root_info: dict[str, dict] = {}
        # session id -> snippet (or None, meaning "asked, nothing there"). Cached
        # so a running worker is not re-read every poll; dropped on a forced
        # refresh of the selected row.
        self._snippets: dict[str, Optional[str]] = {}

    # -- caches -------------------------------------------------------------

    def _root_detail(self, session_id: str) -> dict:
        """The workspace/harness of a root, fetched once and cached.

        A failed fetch is cached as `{}` too, deliberately: "once at startup"
        has to mean once, and a root whose detail endpoint is unhealthy for a
        while must not turn every poll into a retry. The cost is that the cwd
        rule cannot narrow on that root until the process restarts.

        The cache is FIFO-bounded at MAX_ROOT_DETAILS, which is also the bound
        on how far the cwd probe in `_match_cwd_root` can walk.

        The report is extracted from the same response and seeded into the
        snippet cache. It is already in hand, and a root that becomes the scope
        — which is exactly what the cwd probe does to a quiet root — is idle by
        definition, so without this the lazy "first idle" fetch would pay for a
        SECOND 121 KB read of a row this one just read.
        """
        if session_id in self._root_info:
            return self._root_info[session_id]
        if len(self._root_info) >= MAX_ROOT_DETAILS:
            self._root_info.pop(next(iter(self._root_info)))
        detail = self.watcher._fetch_detail(session_id)
        info: dict = {}
        if isinstance(detail, dict):
            workspace = detail.get("workspace")
            if isinstance(workspace, str) and workspace:
                info["workspace"] = workspace
            harness = detail.get("harness")
            if isinstance(harness, str) and harness:
                info["harness"] = harness
        self._root_info[session_id] = info
        if session_id not in self._snippets:
            self._snippets[session_id] = snippet_from_detail(detail)
        return info

    def _snippet(self, session_id: str) -> Optional[str]:
        """The report of a session, fetched once and cached.

        `self.watcher._fetch_detail` already swallows a transport or decode
        error and returns None, so a session whose detail cannot be read costs
        its snippet and nothing else — the row still renders.
        """
        if session_id in self._snippets:
            return self._snippets[session_id]
        if len(self._snippets) >= MAX_SNIPPETS:
            self._snippets.pop(next(iter(self._snippets)))
        text = snippet_from_detail(self.watcher._fetch_detail(session_id))
        self._snippets[session_id] = text
        return text

    # -- refresh ------------------------------------------------------------

    def _match_cwd_root(self, by_id: dict, live_root_ids: list[str],
                        asked: set[str]) -> Optional[str]:
        """The root session whose `workspace` is `$PWD`, or None.

        Checked against the live roots first (their details are already in hand,
        so this costs nothing), and only then against the roots the live
        heuristic EXCLUDED. That second pass is the fix for the wrong-directory
        bug: "live" means working, blocked, or the parent of a worker, so a
        brand-new `og chat` in this directory — idle, nothing dispatched yet —
        is not live, its `workspace` was never fetched, nothing matched `$PWD`,
        and the view fell back to every root on the machine. The cwd question is
        not a question about activity, so it is answered independently of it.

        The probe stops at the first match and every result is cached per session
        id (`_root_detail`), so this is one fetch per root ONCE, not one per
        poll; `asked` records which roots were put to the question this refresh
        so the prune keeps exactly their cache entries.
        """
        target = _norm_path(self.cwd)
        for sid in live_root_ids:
            workspace = self._root_info.get(sid, {}).get("workspace")
            if isinstance(workspace, str) and workspace and \
                    _norm_path(workspace) == target:
                return sid
        for sid, session in by_id.items():
            if sid in asked or not _is_root(session):
                continue
            asked.add(sid)
            workspace = self._root_detail(sid).get("workspace")
            if isinstance(workspace, str) and workspace and \
                    _norm_path(workspace) == target:
                return sid
        return None

    def refresh(
        self,
        selected_id: Optional[str] = None,
        expanded_ids: Iterable[str] = (),
        force: bool = False,
    ) -> Frame:
        """One listing walk -> a `Frame`. The only method that touches the wire."""
        rows, truncated = self.watcher.fetch_listing(kind="any")

        # Only archived sessions are dropped. `og_herdr_watch.should_project` is
        # NOT applied here on purpose: it drops an idle sub-agent, which is
        # right for a herdr pane but wrong for this view — the whole point of
        # the lazy "first idle" snippet is to read a worker's report AFTER it
        # stopped, so an idle worker must stay on screen long enough to show it.
        sessions = [s for s in rows
                    if isinstance(s, dict) and not s.get("archived")]

        by_id, _children = _index(sessions)
        workers: dict[str, list[dict]] = {}
        for session in sessions:
            parent = session.get("parent_session_id")
            if isinstance(parent, str) and parent and parent in by_id:
                workers.setdefault(parent, []).append(session)

        live_root_ids = [
            sid for sid, session in by_id.items()
            if _is_root(session) and self._is_live_root(session, workers.get(sid, []))
        ]
        asked: set[str] = set(live_root_ids)
        for sid in live_root_ids:
            self._root_detail(sid)

        scope = select_scope(
            [by_id[sid] for sid in live_root_ids if sid in by_id],
            self.cwd,
            self.all_roots,
        )
        # `select_scope` reports "nothing matched `$PWD`" by handing back exactly
        # what it was given, so the equality IS the no-match signal — the rule
        # itself stays defined in one place.
        if self.cwd and not self.all_roots and scope == live_root_ids:
            match = self._match_cwd_root(by_id, live_root_ids, asked)
            if match is not None:
                # A match IS the scope: one root's tree, not the live set plus
                # this one.
                scope = [match]

        # Merge each root's detail into its session object so the pure layer
        # below can decide the cwd match and the header label without knowing
        # anything about HTTP.
        merged = []
        for session in sessions:
            sid = _session_id(session)
            copy = dict(session)
            if sid in asked:
                copy.update(self._root_detail(sid))
            merged.append(copy)

        # Root caches are pruned to the roots this refresh actually asked about:
        # a root that is gone (or has gone quiet) costs one re-fetch if it comes
        # back, and the cache cannot grow without bound across a long run. The
        # probed roots are IN `asked`, so pruning cannot turn the cwd probe into
        # a per-poll refetch — which is exactly what it did before this
        # separation existed.
        for sid in list(self._root_info):
            if sid not in asked:
                del self._root_info[sid]

        by_id, children = _index(merged)
        order = _preorder(by_id, children, scope)

        # The lazy fetches. An idle worker is read once and kept; a selected row
        # is read once on selection, and re-read only on a forced refresh.
        for sid, _depth, _spine, _last in order:
            if sid not in by_id:
                continue
            if display_state(by_id[sid]) == "idle":
                self._snippet(sid)
            if sid == selected_id:
                if force:
                    self._snippets.pop(sid, None)
                self._snippet(sid)

        frame_rows = build_rows(merged, scope, self._snippets, expanded_ids)
        return Frame(rows=frame_rows, truncated=truncated)

    @staticmethod
    def _is_live_root(root: dict, workers: list[dict]) -> bool:
        """Whether a root is part of the live set this view shows.

        A root is live while it is working or blocked (it is the conversation
        the human is driving, or waiting on them), OR while it has ANY worker —
        including a worker that has already finished.

        "Has any worker" rather than "has an ACTIVE worker" on purpose: the whole
        reason this view keeps an idle worker on screen is so its report can be
        read, and that is exactly the case of a conversation whose orchestrator
        has answered and whose workers are done. A root with no workers at all
        and no activity is a finished solo conversation; dropping it is what
        keeps "all live roots" — and its per-root detail fetches — to a handful
        on a machine with a long history of sessions.

        This decides what is WORTH SHOWING, never which directory the operator is
        in: a quiet root in `$PWD` is still the answer to that question, and
        `_match_cwd_root` asks it of every root regardless.
        """
        if herdr_state(root) in ("working", "blocked"):
            return True
        return bool(workers)


# ---------------------------------------------------------------------------
# the access address: recovery from the tunnel cache or the LAN
# ---------------------------------------------------------------------------

def qr_lines(url: str) -> list[str]:
    """A scannable QR for `url` as ANSIUTF8 lines, or [] when qrencode is absent.

    Optional by design: a missing qrencode must cost the QR and nothing else —
    the URL line below it still carries the address. Mirrors bin/og's
    `print_qr` flags: `-m 1` is a one-module quiet zone (the default 4 makes
    the code too tall for a terminal) and `-s 1` the smallest module.

    The ANSI SGR escapes are stripped HERE, at the source. qrencode colours each
    module `40;37` (black on white) and terminates every line; curses does not
    interpret escapes, it prints them, so handing those lines to `addnstr` put
    a literal `^[[40;37;1m` down both sides of the operator's code. The block
    characters alone render correctly and already read as black-on-white on a
    dark terminal.
    """
    if shutil.which("qrencode") is None:
        return []
    try:
        done = subprocess.run(
            ["qrencode", "-t", "ANSIUTF8", "-m", "1", "-s", "1", url],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    lines = [_ANSI_SGR.sub("", line) for line in done.stdout.splitlines()]
    return [line for line in lines if line]


def _probe(argv: list[str]) -> str:
    """stdout of a read-only probe command, or "" when it cannot run."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def _lan_ip() -> Optional[str]:
    """This machine's LAN address, or None.

    The Python counterpart of bin/og's `lan_ip`: macOS exposes it through
    `ipconfig getifaddr <default-route interface>`, Linux through `ip route`,
    and `hostname -I` is the last-ditch fallback. Every probe is best-effort; a
    machine with no route (offline, a container) yields None and the panel says
    so rather than naming an address that will not answer.
    """
    iface = ""
    for line in _probe(["route", "-n", "get", "default"]).splitlines():
        stripped = line.strip()
        if stripped.startswith("interface:"):
            iface = stripped.split(":", 1)[1].strip()
            break
    if iface:
        candidate = _probe(["ipconfig", "getifaddr", iface]).strip()
        if _is_ipv4(candidate):
            return candidate
    tokens = _probe(["ip", "-4", "route", "get", "1.1.1.1"]).split()
    if "src" in tokens:
        candidate = tokens[tokens.index("src") + 1]
        if _is_ipv4(candidate):
            return candidate
    for token in _probe(["hostname", "-I"]).split():
        if _is_ipv4(token):
            return token
    return None


def _first_line(path: str) -> Optional[str]:
    """The first non-empty line of a small text file, or None."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    return line
    except OSError:
        pass
    return None


# The tunnel providers bin/og recognises by name — its `tunnel_url` case arm,
# the same set `normalize_tunnel_provider` canonicalises to. A cached URL whose
# provider record names anything else is not vouched for.
_TUNNEL_PROVIDERS = ("ngrok", "tunnl")


def _pid_alive(pid: int) -> bool:
    """Whether `pid` names a live process: the Python form of `kill -0`.

    `os.kill(pid, 0)` sends no signal; it only asks the kernel to resolve the
    pid, which is exactly what bin/og's `tunnel_pid` tests with `kill -0`. Any
    OSError counts as dead, including EPERM — `kill -0` also exits non-zero for a
    process belonging to another user (verified: `kill -0 1` prints "Operation
    not permitted" and exits 1), so this mirrors the shell check rather than
    second-guessing it. A pidfile naming someone else's pid is stale anyway.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _cached_tunnel_url(home: str) -> Optional[str]:
    """The cached public URL, but only while the tunnel that wrote it is alive.

    The three gates of bin/og's `tunnel_url`, in its order: a non-empty URL file,
    a provider record naming a provider og recognises, and that provider's
    pidfile holding a live pid. Any gate failing reads as "there is no cache", so
    the caller falls through to the LAN address — a dead tunnel URL looks
    authoritative and is the worst address to hand a phone, worse than an honest
    same-wifi address that visibly fails.

    Deliberately does NOT clear the cache on a stale entry, unlike bin/og: `og
    agents` sends nothing to a session and changes no session state (the test AST
    guard pins that, and that its ONE outbound call is `agent.focus`), so the
    operator's state files are `og`'s to clean up, not a viewer's to delete. The
    stale value is ignored instead; `og start`/`tunnel_url` will clear it.

    The legacy `og-ngrok.pid` is NOT consulted, matching `tunnel_url`: a tunnel
    that predates the provider record writes no `og-tunnel.provider`, so the
    second gate already reads it as no tunnel.
    """
    url = _first_line(os.path.join(home, "og-tunnel.url"))
    if not url:
        return None
    provider = _first_line(os.path.join(home, "og-tunnel.provider"))
    if provider not in _TUNNEL_PROVIDERS:
        return None
    pid_text = _first_line(os.path.join(home, f"og-tunnel-{provider}.pid"))
    if not pid_text:
        return None
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    return url if _pid_alive(pid) else None


def _server_port(server: str) -> int:
    """The port in the `--server` URL, or the default.

    Only the port is borrowed: the LAN address takes its host from this
    machine, not from `--server`, which is usually the loopback URL the panes
    attach to.
    """
    try:
        port = urllib.parse.urlsplit(server).port
    except ValueError:
        port = None
    return port or DEFAULT_PORT


def access_info(server: str, environ: Optional[dict] = None) -> Access:
    """Recover the address a phone can use, for the `a` panel.

    The ONE place this view does that recovery. It is deliberately a second
    implementation of bin/og and not a call into it: `og agents` is standalone
    and runs in any terminal with no dependency on `og` being on PATH, so
    shelling out to the shell script would be the wrong coupling. bin/og's
    counterparts are `access_url` (the dispatcher), `tunnel_url` (the cache
    read — `_cached_tunnel_url` mirrors its three gates, including the owning
    tunnel's pid, but IGNORES a stale entry instead of clearing it, since a
    viewer must not delete the operator's state), `lan_ip`, and `show_access`
    (whose caveat wording the notes above mirror). Consolidating the two is one
    edit here.

    Order, and why: a cached tunnel URL is a public address that works from
    anywhere, so it wins when present AND its tunnel is still alive; otherwise
    the host's LAN address at the server's port is the same-wifi fallback. When
    neither resolves the panel names no address at all — a banner pointing
    somewhere dead is worse than none, because someone will scan it and get
    nothing.
    """
    env = os.environ if environ is None else environ
    home = env.get("OMNIGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".omnigent")
    cached = _cached_tunnel_url(home)
    if cached:
        return Access(url=cached, qr=qr_lines(cached), note=TUNNEL_NOTE)
    ip = _lan_ip()
    if ip:
        url = f"http://{ip}:{_server_port(server)}"
        return Access(url=url, qr=qr_lines(url), note=LAN_NOTE)
    return Access(url=None, qr=[], note=NO_ADDRESS_NOTE)


# ---------------------------------------------------------------------------
# the terminal
# ---------------------------------------------------------------------------

def _tty_available() -> bool:
    """Whether a curses TUI can run: a tty on stdout and curses importable."""
    if not sys.stdout.isatty():
        return False
    try:
        import curses  # noqa: F401
    except Exception:
        return False
    return True


# What the pane says when Enter could not focus, one line each. Kept short so the
# reason survives `_clip` in a narrow pane, and kept distinct so the operator can
# tell "never going to work here" from "try again": NOT_INSIDE is a fact about
# where the pane runs, the other two are facts about herdr's state.
NOTICE_FOCUS_OUTSIDE_HERDR = "focus: not inside herdr (no HERDR_ENV)"
NOTICE_FOCUS_UNAVAILABLE = "focus: herdr client unavailable"
NOTICE_FOCUS_FAILED = "focus: {0}"


def focus_in_herdr(session_id: str,
                   environ: Optional[dict] = None) -> Optional[str]:
    """Jump the operator's herdr UI to `session_id`. None on success, else why not.

    THE ONE THING THIS VIEW EVER SENDS. Everything else `og agents` does is a GET
    through `og_herdr_watch`; this is the single outbound call, `agent.focus`, and
    it only moves the operator's own terminal to where that session lives — it
    starts nothing, answers nothing, and changes no session state. The AST guard
    in tests/test_og_agents.py asserts `agent.focus` is the only method this
    module ever hands to `HerdrClient.call`.

    Returns None when the UI moved, or a short one-line notice saying why it did
    not, which `_run_tui` shows in the pane (`Frame.notice`). It NEVER raises: an
    `og agents` pane an operator leaves open all day must survive a herdr that is
    absent, a terminal outside a herdr pane, or a socket that is down, so every
    failure — the client failing to import, a socket that will not connect, an
    error reply — becomes a notice instead of a traceback that kills the pane.
    The text keeps the character of the failure, so "this can never work here"
    reads differently from "try again".

    The herdr client is imported HERE, inside the handler, never at module
    import: this view is deliberately not herdr-specific and must run in any
    terminal, so nothing may require herdr (or its client module) to be present
    just to start. `og_herdr_launch.is_inside` is the single definition of the
    "inside herdr" signal (`HERDR_ENV=1`) and is reused rather than restated.
    """
    env = os.environ if environ is None else environ
    try:
        from og_herdr_launch import is_inside
    except Exception:
        return NOTICE_FOCUS_UNAVAILABLE
    if not is_inside(env):
        return NOTICE_FOCUS_OUTSIDE_HERDR
    try:
        from og_herdr_client import HerdrClient
    except Exception:
        return NOTICE_FOCUS_UNAVAILABLE
    try:
        with HerdrClient() as client:
            client.call("agent.focus", {"target": session_id})
    except Exception as exc:
        return NOTICE_FOCUS_FAILED.format(exc)
    return None


def _run_tui(model: AgentModel, interval: float, server: str,
             start_access: bool = False) -> int:
    """The live view. curses paints exactly what `render` returns; keys drive
    the model.

    Keys: `q` quits, `r` forces a refresh, the arrows (or `j`/`k`) move the
    selection, Enter FOCUSES the selected worker in herdr (jumping the operator's
    herdr UI to that session, see `focus_in_herdr`), Space expands or collapses
    the selected row's snippet, and `a` toggles the access panel. It sends
    nothing to a session and changes no session state: `agent.focus` is the ONE
    outbound call it makes, and it moves the operator's own terminal alone.

    TWO CLOCKS, deliberately. The data poll (`interval`, 3s) asks the server for
    new rows; the spinner tick (SPIN_TICK, 120ms) only advances a counter that
    render turns into a glyph. Driving the spinner off the poll would make a
    three-second-wide animation, and driving the poll off the tick would ask the
    server for the listing eight times a second.

    And nothing is painted unless the composed lines actually CHANGE
    (`paint_needed`). With nothing working there is no spinner to advance, the
    render is byte-identical to the last one, and the loop skips `erase` and
    `refresh` entirely — it blocks on the keyboard until the next poll
    (`next_wait`) instead. An `og agents` pane an operator leaves open all day
    then costs one wakeup per poll, not eight repaints a second forever.
    """
    import curses

    def loop(stdscr) -> None:
        curses.curs_set(0)
        # Take the terminal's own colours for pair 0. `curses.wrapper` calls
        # `start_color()` before this point, and once colour is started pair 0
        # is the curses implementation's default white-on-black rather than the
        # terminal's: every cell the view touches — including the spaces
        # `stdscr.erase()` writes — then paints an opaque black background over
        # the operator's theme and transparency. Nothing here sets a colour
        # pair, so with the default in place the pane has no background of its
        # own and matches every other native CLI in the window. The call raises
        # `curses.error` on a terminal that cannot do it; the view still has to
        # paint there, so it runs uncoloured rather than dying at startup.
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        stdscr.keypad(True)
        selected = 0
        offset = 0
        expanded: set[str] = set()
        # The one-line reason the last Enter could not focus, shown in the pane
        # through `Frame.notice`. It is cleared on the NEXT keypress, so it sits
        # for as long as the operator is reading it and goes once they act.
        notice: Optional[str] = None
        # `poll` asks for a refresh now; `force` additionally drops the selected
        # row's cached report so it is read again. They are separate so Space can
        # redraw the tree this tick without paying for a second 121 KB fetch.
        poll = True
        force = True
        frame = Frame()
        show_access = start_access
        # Resolved once, on the toggle: the address does not change while the
        # view runs, and probing for it every poll would shell out on a timer.
        access: Optional[Access] = access_info(server) if show_access else None
        spin = 0
        next_spin = 0.0
        next_poll = 0.0
        painted: Optional[list[tuple[str, str]]] = None

        def put(row: int, text: str, role: str) -> None:
            """One `addnstr`, tolerant of the edges.

            curses raises when a write touches the last cell of the window, and
            a line one column too long lands exactly there — the rest of the
            line is already on screen, so swallowing it is a no-op rather than a
            lost frame.
            """
            if not 0 <= row < height or width < 1:
                return
            # SELECTED is bold, not reverse: it lands on the caret row alone,
            # so reverse-videoing it would still light a full row of the box.
            # Bold keeps that one row findable without re-reading the old
            # whole-block highlight the operator asked to be rid of.
            attr = {NORMAL: curses.A_NORMAL, SELECTED: curses.A_BOLD,
                    DIM: curses.A_DIM}[role]
            try:
                stdscr.addnstr(row, 0, text, width - 1, attr)
            except curses.error:
                pass

        while True:
            height, width = stdscr.getmaxyx()
            now = time.monotonic()

            if poll or now >= next_poll:
                ids = [row.session_id for row in frame.rows]
                sel_id = ids[selected] if 0 <= selected < len(ids) else None
                frame = model.refresh(selected_id=sel_id,
                                      expanded_ids=expanded, force=force)
                selected = snap_selection(frame.rows, selected)
                next_poll = now + interval
                poll = False
                force = False

            # The panel is part of the frame, so `render` stays the source of
            # what is painted — and it is set HERE, not inside the refresh, so
            # the `a` toggle takes effect on the next paint rather than on the
            # next poll.
            frame.access = access if show_access else None
            frame.selected = selected
            frame.spin = spin
            frame.notice = notice
            working = frame_working(frame)
            if working and now >= next_spin:
                spin += 1
                next_spin = now + SPIN_TICK

            pane = _pane(frame, width)
            items = pane.items
            tail_len = len(items) - pane.list_rows

            # Keep the selected box on screen: the list scrolls, the tail stays
            # pinned to the bottom of the pane.
            list_view = max(1, height - tail_len)
            first_selected = next((i for i, (_, role) in enumerate(items)
                                   if role == SELECTED), None)
            if first_selected is not None:
                if first_selected < offset:
                    offset = first_selected
                elif first_selected >= offset + list_view:
                    offset = first_selected - list_view + 1
            offset = max(0, min(offset, max(0, pane.list_rows - list_view)))

            if paint_needed(items, painted):
                painted = items
                stdscr.erase()
                for row, (text, role) in enumerate(
                        items[offset:offset + list_view]):
                    put(row, text, role)
                tail_top = max(0, height - tail_len)
                for i, (text, role) in enumerate(items[pane.list_rows:]):
                    put(tail_top + i, text, role)
                stdscr.refresh()

            # Two clocks, and the wait is whichever comes first: the next data
            # poll, or the next spinner frame when something is working. With
            # nothing working this is a single blocking `getch` per poll, so the
            # loop costs nothing at all between polls.
            stdscr.timeout(max(1, math.ceil(next_wait(time.monotonic(),
                                                     next_poll, working) * 1000)))
            key = stdscr.getch()
            if key == -1:
                continue
            # Any real keypress dismisses the last focus notice: it was painted
            # for the operator to read, and they have now acted.
            notice = None
            if key in (ord("q"), 27):
                return
            if key == ord("r"):
                poll = force = True
                continue
            if key == ord("a"):
                show_access = not show_access
                if show_access:
                    access = access_info(server)
            if key in (curses.KEY_UP, ord("k")):
                selected = move_selection(frame.rows, selected, -1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = move_selection(frame.rows, selected, 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                # Enter FOCUSES the selected worker in herdr — it jumps the
                # operator's own terminal to where that session lives. It is a
                # no-op on a header (nothing to jump to), and it deliberately
                # does NOT set `poll`: focus changes nothing this view draws, so
                # a refetch would be a listing walk for nothing. A failed focus
                # sets a notice, and the next paint shows it. See
                # `focus_in_herdr` for every way it can decline.
                if 0 <= selected < len(frame.rows) \
                        and not frame.rows[selected].is_root:
                    notice = focus_in_herdr(frame.rows[selected].session_id)
            elif key == ord(" "):
                # Expand/collapse moved off Enter when Enter took focus. It DOES
                # set `poll`: the expanded set feeds the rebuild, so the tree has
                # to be redrawn, and the report is already cached, so this costs
                # nothing beyond the listing walk.
                if 0 <= selected < len(frame.rows):
                    sid = frame.rows[selected].session_id
                    if sid in expanded:
                        expanded.discard(sid)
                    else:
                        expanded.add(sid)
                    poll = True

    curses.wrapper(loop)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="og agents",
        description="Live view of the Omnigent session tree.",
    )
    parser.add_argument("--server", default=DEFAULT_BASE_URL,
                        help="Omnigent server base URL")
    parser.add_argument("--all", action="store_true", dest="all_roots",
                        help="show every live root, not just this directory's")
    parser.add_argument("--once", action="store_true",
                        help="render one frame as plain text and exit")
    parser.add_argument("--access", action="store_true",
                        help="show the access panel (QR + URL); toggled live "
                             "with 'a'")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between refreshes in the live view")
    return parser


def build_model(args: argparse.Namespace, opener: Optional[Any] = None) -> AgentModel:
    """The model for a parsed command line.

    `opener` is the transport seam: tests and callers pass a stub so no test can
    reach the operator's live server. It defaults to urllib inside
    `SessionWatcher`.
    """
    watcher = SessionWatcher(
        base_url=args.server,
        token=SessionWatcher.discover_token(args.server),
        opener=opener,
    )
    return AgentModel(watcher, cwd=os.getcwd(), all_roots=args.all_roots)


def main(argv: Optional[Iterable[str]] = None, opener: Optional[Any] = None) -> int:
    """`og agents`. `--once` (and any non-tty fallback) prints one plain frame."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    model = build_model(args, opener)

    if args.once or not _tty_available():
        if not args.once:
            # Say so rather than crash or silently block: the live view needs a
            # tty and curses, and one plain frame is the honest fallback.
            sys.stderr.write(
                "og agents: no tty or no curses; rendering one frame instead\n")
        frame = model.refresh()
        if args.access:
            frame.access = access_info(args.server)
        # No animation headless: one frame, at spinner frame 0, with the caret
        # where the live view opens it — on the first worker.
        frame.spin = 0
        frame.selected = snap_selection(frame.rows, frame.selected)
        # The real pane width drives the QR fit; off a tty there is none, so
        # fall back to the classic 80 columns.
        width = shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns
        for line in render(frame, width=width):
            print(line)
        return 0

    try:
        return _run_tui(model, args.interval, args.server, args.access)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())