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

The shape of the view is the operator's C2 template, iterated against the pane's
measured feedback: a rounded box per root drawn at column 0 (OUTDENTED relative
to its workers), then the root's workers GROUPED BY STATE, each group opened by a
breathing rail row and each worker a rounded box indented behind a four-column
gutter. There are NO worded group labels: the animated glyph in the gutter says
the state, and the breathing rail alone marks where one group ends and the next
begins. The gutter is the rail's own column (`⦿` where an agent's box hangs off
it, `│` on every other row), a space, the state glyph's column, and a space — so
one unbroken line runs down the pane's left edge, with a node at each agent, out
of the root box's own bottom-left corner and across the breathing rows.
SELECTION IS BRIGHTNESS: the selected worker's box paints normal while every
other box paints dim — there is no caret glyph. Inside the box the harness brand
mark and the title sit flush left with the meta flush right; the WORKING glyph is
a braille spinner (see SPIN_FRAMES), so a live row visibly animates while
blocked, failed and finished rows stay static. A boxed access panel sized to its
own content and a one-line footer pin to the bottom. `render` is a pure function
from state to a list of lines and curses only paints them; that separation is
what lets the view be tested with `--once`, with no tty in sight.

This module is deliberately NOT herdr-specific: it reads Omnigent's HTTP API and
must run in any terminal. It sends NOTHING to a session — no prompting, no
answering an elicitation, no dispatches — and changes no session state. Its read
path is GET-only and goes entirely through `og_herdr_watch`, which owns the
transport, pagination, `kind=any` default, truncation handling and per-server
token matching; none of that is re-implemented here. It makes exactly ONE
outbound call of its own: `agent.focus` (see `focus_in_herdr`), which moves the
operator's own herdr UI to the pane a row belongs to — a worker's ROOT, the only
session herdr has a pane for, see `focus_target` — and touches no session state. That
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

from og_herdr import (
    DEFAULT_AGENT_LABEL,
    agent_harness,
    harness_labels,
    harness_vendors,
)
from og_herdr_watch import (
    DEFAULT_BASE_URL,
    SessionWatcher,
    _session_id,
    herdr_state,
)
from og_themes import (
    CURSES_DEFAULT,
    Theme,
    curses_value,
    hex_to_ansi256,
    load_catalog,
    ordered_themes,
    readable_index,
    save_theme,
    theme_for,
    valid_slugs,
)

DEFAULT_INTERVAL = 3.0

# The static per-state glyph shown in a worker's gutter, the operator's chosen
# TUI vocabulary: ⏸ blocked, ✗ failed, ✓ finished. `working` has NO entry here —
# it gets the SPINNER (see SPIN_FRAMES), because the operator's rule is "the
# spinner before the agent logo is the status": a live row has to visibly
# animate, and a moving dot is the one glyph that says "this is running" without
# a legend. The three glyphs here stay STATIC on purpose — a paused or finished
# thing must not look busy. This replaces the ASCII `+ ! x ?` the pane used to
# draw, and keeps the six-dot braille spinner it also already had for working;
# `?` stays the one honest glyph for a status `display_state` could not name
# (-> "unknown"). `✓` finished pairs with `✗` failed: same Dingbats block, same
# weight, and it reads as done rather than merely not-running.
#
# ✓ is East Asian NARROW (measured: `east_asian_width("\u2713") == "N"`), one
# layout column. ○, the circle it replaced, was AMBIGUOUS and also composed as
# one column under the len-based ruler, so the swap is width-neutral.
STATE_GLYPH = {
    "blocked": "\u23f8",   # ⏸
    "failed": "\u2717",    # ✗
    "unknown": "?",
    "idle": "\u2713",      # ✓
}

# The spinner that IS the working glyph: SIX braille dots, and only the top two
# rows of each cell (dots 1-6, never the 7/8 descenders). That restriction is the
# whole reason for the choice — the 7/8 forms hang below the baseline, so a
# spinner that used them would change the glyph's height every frame and make the
# row it sits in jitter as the box is redrawn. Six frames at SPIN_TICK is one
# ~0.7s cycle. Braille is East Asian NARROW, so it spends the same one layout
# column the static glyphs do and the box geometry does not move frame to frame.
SPIN_FRAMES = "\u280b\u2819\u2839\u2838\u283c\u2834"
SPIN_TICK = 0.12

# --- the C2 pane geometry (docs/proto/tui_chosen.py, variant C2) ---------------
#
# A GUTTER runs down the pane's left edge: the NODE column (the rail `│`, or `⦿`
# where an agent's box hangs off it), a space, the STATE glyph column, a space —
# and the worker's rounded box starts AFTER it. So the rail carries the unbroken
# vertical line down a group alone, with a node at each agent (the box no longer
# draws that line itself, because the box has moved right), and the state glyph
# points OUT of the box rather than sitting inside it. The root is a box in
# column 0, so the rail drops out of its own bottom-left corner.
#
# WIDTH IS COMPOSED WITH ONE RULER: the len-based one (`_clip`, the
# `_visible_width` notion). Every box-drawing glyph here is East Asian AMBIGUOUS,
# so a whole box line measured with `_display_width` (Ambiguous charged two) would
# double its borders and overflow the pane — exactly why the two rulers exist.
# The len ruler charges Ambiguous one, which is what a western terminal paints,
# and it matches the approved prototype line for line. The MARK SLOT was once the
# one place that charged Ambiguous two, and it sheared the titles of the marks
# then Ambiguous (kilo, cognition, z-ai — since swapped for Narrow glyphs); it
# measures with its own western ruler (`_slot_width`), which charges Ambiguous
# one and Wide/Fullwidth two. `_display_width` itself survives
# solely as `_footer`'s backstop — the footer must not overflow under EITHER
# locale, and carries no Ambiguous glyph for it to bite on.
ROUND_CORNERS = ("\u256d", "\u2500", "\u256e", "\u2502", "\u2570", "\u256f")
RAIL = "\u2502"          # U+2502, the group rail, its own column at the left edge
# ⦿ (U+29BF) is the NODE: the point on the rail where one agent's box branches
# off it, drawn in the rail's column on that box's content row. U+29BF CIRCLED
# BULLET is East Asian NARROW — the box-drawing glyphs it rides between (`│`,
# the corners) stay Ambiguous, but the node's width is now unconditional. It was
# ⊙ (U+2299, Ambiguous), which paints TWO columns in a CJK-locale terminal and
# would leave the node wider than the content column it sits beside; Narrow paints
# one column in every locale. The len-based layout ruler charges it one column
# exactly as it does the rail, so the node still sits in the same column in the
# western terminal the operator runs.
NODE = "\u29bf"

# The four columns a box spends BEFORE its rounded border: the NODE column (the
# rail, or `⦿` where the box hangs off it), a space, the STATE glyph column, a
# space. The node and the state glyph are drawn on the box's CONTENT row alone
# and blank on its border and snippet rows, so the rail reads as one unbroken
# line with a node at each agent — the rule the pre-C2 renderer already followed
# for its caret. The breathing rails keep the rail in column 0 too, so the line
# runs unbroken down the whole group.
GUTTER = 4

# The narrowest box there is: two corners and two rules is the smallest rectangle
# `_round_box` can draw. It is also the floor of `worker_box`'s degradation ladder
# — the point below which no box is drawn at all, because a one- or two-column
# "box" would be a lie.
BOX_MIN = 4

# The state groups, in the order they are DRAWN: what needs attention first.
# `unknown` (a status `display_state` could not name) is its own group rather than
# folded into `failed`, so its row keeps its own `?` glyph. A state outside this
# tuple still gets a group, sorted last (see `_grouped`), so no worker is dropped.
# The groups carry NO worded label: the state glyph in each box's gutter says it,
# and a breathing rail row alone marks where one group ends and the next begins.
# Only the ORDER survives, and it is still what puts what needs attention first.
GROUP_ORDER = ("working", "blocked", "failed", "unknown", "idle")

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
# the end is gone.
#
# NO LOCALE-DEPENDENT GLYPH HERE. The move keys were once ↑/↓ (U+2191/U+2193),
# which are East Asian AMBIGUOUS: one column in a Latin locale, TWO in a CJK one.
# `_display_width` charged them one, so the full form measured exactly 64 and was
# chosen for a 64-column pane — but a CJK terminal painted 66 and `_clip` cut the
# trailing keys off. That is the very bug the three forms exist to end, merely
# relocated to another locale. They are ASCII now (`^/v`), so every footer glyph
# measures one column under BOTH rules and the question cannot arise. ⏎ (U+23CE)
# and ␣ (U+2423) are Narrow — checked, not assumed — and `_display_width` charges
# Ambiguous two as a backstop for anything a future edit adds. See
# `test_the_footer_is_measured_the_same_in_a_cjk_locale`.
#
# `t theme` JOINED ALL THREE FORMS WITHOUT MOVING ANY THRESHOLD, and that took a
# deliberate choice about WHICH label degrades. The full form could simply grow to
# 73, but the COMPACT form is the one the ~46-column agents split actually shows,
# and appending ` t theme` to it would have pushed it to 53 — handing every real
# pane the bare KEYS form, a far worse trade than losing one word. So COMPACT
# sheds `refresh` and keeps `q quit`, `⏎ focus`, `␣ expand`, `a access` and
# `t theme`: the key `r` still EXISTS in all three forms (the rule is that a key
# may never degrade, only its label), and `r` is the one binding whose loss costs
# the operator nothing — it is a convenience refresh, where every other binding is
# something you cannot guess. COMPACT is therefore 45 columns wide, the width it
# has always been, and `test_the_footer_chooses_a_form_by_measured_width` pins
# that.
FOOTER = "q quit  r refresh  ^/v move  enter focus  space expand  a access  t theme"
FOOTER_COMPACT = "q quit r ^v ⏎ focus ␣ expand a access t theme"
FOOTER_KEYS = "q r ^v ⏎ ␣ a t"

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
#
# SELECTION IS BRIGHTNESS. An UNSELECTED worker's box paints DIM on every line;
# the SELECTED worker's box paints NORMAL, so it reads as white against the
# dimmed others. SELECTED is a role of its own even though it maps to the same
# attribute as NORMAL, because it is hung on exactly ONE line — the selected
# box's first row — and `_run_tui` scans for `role == SELECTED` to find the
# block to scroll to. Lose it and scroll-to-selection breaks silently, with no
# symptom until the list outgrows the pane.
#
# THE BRIGHTNESS A LINE HANGS IS A DEFAULT, NOT A VERDICT: a span carrying the
# `undim:` role prefix paints NORMAL whatever its line says (see below). That is
# what keeps the gutter and the status glyph full-brightness on an unselected
# box — reported as "I cannot tell which workers are running at a glance".
#
# There is no TITLE/A_REVERSE role any more: the root is a rounded box like a
# worker's, not a reverse-video bar, so the pane spends no A_REVERSE at all.
NORMAL, SELECTED, DIM = "normal", "selected", "dim"

# COLOUR IS ITS OWN AXIS, orthogonal to brightness. A line is a sequence of
# SPANS — each a run of text with ONE colour role — plus the brightness to paint
# them at, so a DIM box still shows its mark's brand colour, merely dimmed. That
# is why colour is not just more brightness values: the mark, the title and the
# meta share ONE line and each wants a different look, so one paint role per LINE
# (`Pane.items` used to be `(text, role)`) could not express them. The role is a
# plain string so the composer stays pure and testable without curses; `_run_tui`
# maps it to a curses attribute.
#
#   PLAIN      no colour, the default foreground — everything not named below.
#   META       the worker's meta (`12s`, `done`, `failed`, `waiting on you`):
#              ONE colour for every meta, not bold.
#   ORCH       the ROOT box's NAME: bold, no colour.
#   mark:<v>   a harness brand mark: bold, plus vendor `v`'s brand colour.
#   state:<s>  a worker's STATUS GLYPH, in that state's colour (see
#              `_STATE_COLOURS`); PLAIN for the states that have none.
#   undim:<r>  NOT A COLOUR: a prefix that exempts a span from its line's DIM and
#              hands it back `r` as its colour role (see `_bare_role`). This is
#              how brightness became a PER-SPAN axis — the gutter and the status
#              glyph are exempt on every row, selected or not.
PLAIN, META, ORCH = "plain", "meta", "orch"
UNDIM = "undim:"

# PLAIN is a COLOUR ROLE; `text` is the THEME role it asks the catalog for. They
# are different vocabularies on purpose: `plain` means "the body of the pane",
# which is exactly what a theme's `text` is the colour of, and the two are spelled
# differently so a reader cannot mistake one for a lookup key in the other.
PLAIN_TEXT = "text"


def undim(role: str) -> str:
    """`role`, EXEMPT from the brightness its line would otherwise paint at.

    DIM is a per-LINE default (`_blocks`), so before this a segment that had to
    stay bright regardless was unreachable: the operator reporting "the dim
    swallows the gutter and the status glyph" could not tell which workers were
    running, because the rail, the node and the spinner dimmed WITH the box
    drawn around them. Naming the exemption on the span is what makes it
    explicit at the site that composes it, rather than inferred from its
    position in the line.

    Exempt means NORMAL, never BOLD: a status glyph at full brightness is the
    whole point, and there is no brighter axis to spend.
    """
    return UNDIM + role


# (text, colour_role) — one run of a line and the role it paints under.
Span = tuple[str, str]
# (spans, brightness) — a whole line: the brightness its spans paint at BY
# DEFAULT. A span whose role carries the `undim:` prefix is exempt (see `undim`).
Line = tuple[list[Span], str]

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
    """The glyph for a display state: the SPINNER for `working`, else static.

    Pure in `spin`: the caller owns the frame counter, so render stays a function
    of its arguments and `--once` (which passes 0) has no animation to be
    inconsistent about. Only `working` animates — a paused or finished state must
    not look busy — so `⏸ ✗ ✓` come straight from `STATE_GLYPH` and a state the
    vocabulary does not name draws `?` rather than nothing.
    """
    if state == "working":
        return SPIN_FRAMES[spin % len(SPIN_FRAMES)]
    return STATE_GLYPH.get(state, "?")


def split_title(title: Any) -> tuple[Optional[str], str]:
    """(`zen`, `stock-concurrency-lock`) from `coder_zen:stock-concurrency-lock`.

    A delegated worker's title is `<role>_<agent>:<task>`. The ROLE is dropped
    because it says who dispatched, not what is running; the TASK is what the
    operator reads on the row, and the agent name is kept because it is the key
    that resolves the row's harness MARK (`worker_mark` looks it up in the
    installed specs). The name is no longer DRAWN — the mark carries it.

    `sub_agent_name` would name the agent too, but it is NOT on the listing row
    (measured: it is a detail field), so reading it would cost a 121 KB fetch per
    worker to learn what the title already carries.

    A title with no colon yields `(None, title)`: an older session or one the
    server titled plainly still has to render as a row. A role prefix other than
    `coder_` is kept whole rather than guessed at: it is the mark's lookup key,
    and inventing a second parse rule would mislabel more titles than it fixes.
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
    """The right half of a worker's content row: `2m14s`, `waiting on you`, `done`.

    The clock is honest about what it actually knows. `created_at` comes off the
    LISTING row (so no detail fetch buys a clock on every row), and it is not a
    measured start: it is when the server created the session ROW, so a worker
    that has been thinking for ten minutes reads as its whole EXISTENCE, not as
    a measured duration. So:

      * working -> `_age(now - created_at)`, the count-up while it runs.
      * idle -> a bare `done`. It once read `done 6m ago` — exact, since for an
        idle row `updated_at` IS when it stopped — but the elapsed figure was
        noise beside the name, so a stopped worker now says only that it stopped.

    `blocked` and `failed` keep their own words (`waiting on you`, and the failure
    reason or `failed`): a row that is waiting on a human, or has failed, must not
    be flattened into `done`. The states the vocabulary does not name share the
    `done` branch they have always shared.

    `updated_at` is deliberately NOT read any more: the state already says the
    worker has stopped, so there is no figure to derive from the epoch and no
    timestamp to require. A stopped row reads `done` whether or not the listing
    gave it an `updated_at`.
    """
    if state == "blocked":
        return "waiting on you"
    if state == "failed":
        reason = _failure_reason(session)
        return _clip(reason, MAX_META) if reason else "failed"
    if state == "working":
        created = _epoch(session.get("created_at"))
        return _age(now - created) if created is not None else "working"
    return "done"


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

    ONE narrower's worth of caution, because this is where the wrong-session bug
    lived: narrowed within the live roots, this rule can only ever return a LIVE
    root, and a stale root in a repo with a history of sessions is live for as
    long as it still has workers. `AgentModel._match_cwd_root` is what decides
    which root is `$PWD` among ALL of them, newest first; this function supplies
    the wide fallback and the `--all` path. The two agreeing is the common case,
    and when they disagree the caller uses the probe's answer.
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


def _recency(session: dict) -> float:
    """A root's recency, as epoch seconds, for "which of these is THIS one".

    `updated_at` — the listing row's LAST ACTIVITY — first, because the root the
    operator is standing in is the one that was used most recently. That is the
    same field `worker_meta` reads and the reason it is the right one here: an
    old root whose workers kept a long job alive can have been updated after a
    newer conversation was opened, and neither field can see the difference, but
    that is the truth of the data rather than a guess this function could avoid.

    `created_at` is the fallback for a row the server sent without an
    `updated_at`, and a row with neither sorts last rather than first: an unknown
    age is not evidence of recency. There is no `last_activity_at` on a listing
    row — see og_herdr_watch's measured key list — so this is the same free
    LISTING field the rest of the view reads, not a detail-fetch one.
    """
    updated = _epoch(session.get("updated_at"))
    if updated is not None:
        return updated
    created = _epoch(session.get("created_at"))
    return created if created is not None else 0.0


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
    """`[(session_id, depth)]` for the selected roots' trees, in pre-order.

    Pre-order, and `depth` is how far below its root a row sits. C2 FLATTENS the
    tree into state groups (`build_rows`), so the spine columns the old renderer
    drew are gone; `depth` is kept because it is the row's nesting level and
    nothing may crash on a listing that nests deeper than one level.

    The `seen` set is the reason this walk still exists as its own function: a
    malformed listing — a cycle, or a session that is its own parent — has to
    terminate rather than recurse forever.
    """
    out: list[tuple[str, int]] = []
    seen: set[str] = set()

    def walk(sid: str, depth: int) -> None:
        if sid in seen or sid not in by_id:
            return
        seen.add(sid)
        out.append((sid, depth))
        for child in children.get(sid, []):
            walk(child, depth + 1)

    for rid in root_ids:
        walk(rid, 0)
    return out


@dataclass
class Row:
    """One row of the view, before it is turned into text.

    THREE kinds, told apart by a flag, because C2 draws each differently:

      * a ROOT row (`is_root`): an outdented rounded box. `text` is the root's
        name and `harness` the label flush right on its one content row. It draws
        in column 0, OUTDENTED from its workers at column `GUTTER`, because it is
        their parent; the group rail below it drops out of its bottom-left corner.
      * a RAIL row (`rail`): the blank breathing row above a group, drawn as the
        rail alone so the vertical line is unbroken between boxes. It carries the
        group's state, but no text — the worded group label is gone and the state
        is read from the animated glyph in each box's gutter.
      * a WORKER row (none of the above): a rounded box. `text` is the task (the
        title with its `<role>_<agent>:` prefix removed); `agent`, `meta` and
        `harness` are its identity; `depth` is how far below the root it sits.

    `harness` holds a HARNESS ID when the row knows one — a root, whose detail
    fetch supplies it — and is "" otherwise, which is every worker: a worker's
    LISTING row carries no `harness`, so its mark is resolved from the harness
    the agent's installed spec declares, found by `agent` (see `worker_box` and
    `worker_mark`). "" is the honest blank, never a guessed vendor.

    Only a WORKER row is selectable (see `_is_box`): selection is a property of a
    box, so the rails and the roots are stepped over by the selection.
    """

    session_id: str
    text: str
    state: str
    depth: int
    is_root: bool = False
    rail: bool = False
    snippet: Optional[str] = None
    expanded: bool = False
    agent: Optional[str] = None
    meta: str = ""
    harness: str = ""
    # The ROOT session this row's pane belongs to, needed because a sub-agent's
    # OWN `session_id` addresses no herdr pane: herdr writes a pane record (and so
    # an `agent_session_id` a focus target can match) only for a root, so Enter on
    # a worker must aim at the root instead — see `focus_target`. It has to be a
    # FIELD rather than re-derived at focus time: `build_rows` FLATTENS the tree
    # and groups by state, so with `--all` several roots' workers interleave and a
    # worker's root is not recoverable from a walk of `Frame.rows`. Set for every
    # worker in `_worker_rows`; a root row leaves it "" and resolves to its own
    # `session_id` through `is_root`.
    root_session_id: str = ""


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
    pure: the same frame rendered twice is the same two screens, and a caller
    that only wants a tree need not own a clock. Only a WORKING row reads `spin`
    (`state_glyph`), so a frame with nothing working renders identically at every
    value.
    """

    rows: list[Row] = field(default_factory=list)
    truncated: bool = False
    notice: Optional[str] = None
    access: Optional[Access] = None
    selected: int = 0
    spin: int = 0


@dataclass
class Pane:
    """The composed view: every line with its spans and brightness, and where the
    list ends.

    `items` is parallel with itself — `items[i] == (spans, brightness)`, a list of
    `(text, colour_role)` spans plus the brightness they share — and `list_rows`
    counts the lines above the tail, so the TUI can scroll the list and pin the
    panel and the footer to the bottom of the pane without re-deriving the layout
    that produced them.
    """

    items: list[Line] = field(default_factory=list)
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


# The operator's chosen one-cell brand mark per VENDOR, ported unchanged from
# the approved tier-0 table in docs/proto/tui_marks.py. Keyed by vendor, not by
# harness, because the registry puts two harnesses on one vendor (agy and gemini
# are both `google`) and a per-harness table would hand one company two
# different glyphs. The harness -> vendor hop is read from installer/registry.json
# through `og_herdr.harness_vendors`, so renaming or re-vendoring a harness in
# the catalog moves its mark without an edit here.
#
# Every pick is East Asian NARROW, so no mark's width depends on the locale.
# Three of them (kilo, cognition, z-ai) were once AMBIGUOUS; the operator replaced
# them with Narrow glyphs so each paints one column in a CJK-locale terminal too.
# The slot below still pads every mark to the same two columns — see `_mark_slot`.
_HARNESS_MARKS = {
    "anthropic": "\u2733",       # ✳ eight-spoked asterisk
    "opencode-zen": "\u25AF",    # ▯ white vertical rectangle
    "google": "\u2726",          # ✦ black four-pointed star
    "openai": "\u2B21",          # ⬡ white hexagon
    "deepseek": "\U0001D5D6",    # 𝗖 mathematical sans-serif bold C, for CLINE, the
                                 #   harness. This row's vendor names the MODEL
                                 #   (DeepSeek); the mark names the TOOL, so the old
                                 #   whale (DeepSeek's logo) was wrong. Do NOT put it back.
    "kilo": "\U0001D5DE",        # 𝗞 mathematical sans-serif bold K (was 🄺, Ambiguous)
    "aws-kiro": "\U0001680C",    # 𖠌 Bamum letter
    "cursor": "\u21D7",          # ⇗ north-east double arrow
    "goose": "\U0001316F",       # 𓅯 Egyptian hieroglyph
    "hermes": "\u2624",          # ☤ caduceus
    "xai": "\U0001D54F",         # 𝕏 double-struck X
    "cognition": "\u2316",       # ⌖ position indicator (was ⛯, Ambiguous)
    "z-ai": "\U0001D5D9",        # 𝗙 mathematical sans-serif bold F, for FREEBUFF, the
                                 #   harness. This row's vendor names the MODEL provider
                                 #   (z-ai); the mark names the TOOL, so an F, not a Z.
                                 #   (was ╔, Ambiguous — a box-drawing corner read as
                                 #   frame damage here)
    "moonshotai": "\u2318",      # ⌘ place-of-interest sign. This row's vendor names the
                                 #   MODEL provider (Moonshot), but the mark happens to
                                 #   name the TOOL — `⌘` reads "Command" for acp:command-code.
                                 #   It survives by luck, not design: keyed to a
                                 #   provider vendor, so do NOT "fix" it into a Moonshot logo.
}

# The ANSI-256 foreground each vendor's mark is painted in, derived from the
# brand hex and checked for luminance against a dark pane. Keyed exactly like
# `_HARNESS_MARKS` (a vendor with a mark has a colour, and vice versa — see the
# catalog guard in the tests), so the two tables move together.
#
# TWO DELIBERATE SUBSTITUTIONS, and do NOT "correct" either back to the true
# brand colour: a mark painted its real colour is a mark nobody can see, and an
# invisible logo is worse than an uncoloured one.
#   * moonshotai's brand is #1F1F1F -> ansi 234, luminance 31: correctly
#     coloured and INVISIBLE on a dark pane. 252, a light neutral, is used.
#   * cursor's is #3C3C3C -> ansi 237, luminance 60: the same problem, less
#     severe. 244 is used.
#
# KILO IS NOT A SUBSTITUTION but its hex is recorded anyway, because its index is
# not re-derivable by eye and 99 (135,135,255) was a blue-violet that matched
# nothing about Kilo — the mark's colour simply did not correspond to its brand.
#   * the brand is #F3EF4E, a bright lemon, and only the TOP rung of the 6x6x6
#     cube can hold it: 243 and 239 are both above the 215 rung. Blue 78 has no
#     rung nearer than the lowest non-zero one (95), so the whole of the
#     conversion error is blue 78 -> 95 and the colour is 227 (255,255,95). Its
#     two cube neighbours both read further from the hex — 221 (255,215,95) is
#     orange, 191 (215,255,95) is green — and neither is nearer in sRGB either
#     (1009 and 1329 against 689). The greyscale ramp is not a candidate at all:
#     its closest rung is 26x further out than the winner.
#
# 227 IS ADJACENT TO 226, the spinner's neon yellow (`_STATE_COLOURS`), and both
# paint on the same worker row — spinner in the gutter, mark in the box. They do
# not smear, but not because the numbers do it: the ONLY channel between them is
# blue, 0 against 95, so 226 is a saturated yellow and 227 a pale one. Three axes
# carry the rest of the separation, and any of them can go: the mark is BOLD and
# the spinner is not (`_role_bold`), the mark takes its line's brightness while the
# spinner is `undim`-exempt, and the glyphs are different shapes in different
# columns. Worth re-checking rather than assuming the index alone separates them.
#
# FREEBUFF IS THE THIRD NON-SUBSTITUTION, AND THE ROW THAT MISLED: the key is
# `z-ai`, which names z.ai the MODEL PROVIDER, but the glyph and this colour are
# FREEBUFF'S, the harness the registry files under that vendor — which is exactly
# what `_HARNESS_MARKS` says on this very key ("the mark names the TOOL, so an F,
# not a Z"). A logo here is the HARNESS's, glyph AND colour; the tables are keyed
# by vendor only as STORAGE, and the harness -> vendor hop is the registry's job.
# So a reader who takes the key at face value converts a model provider's brand
# instead of the tool's, which is how a sage green mark came to be painted 26 — a
# saturated blue belonging to nothing in this table. Read `_HARNESS_MARKS` before
# converting a colour; `deepseek` and `moonshotai` are the same trap, already
# flagged on their marks.
#   * the brand is #A4BA96, a muted sage. The NEAREST index in the whole palette is
#     144, (175,175,135), at OKLab 3.087 and sRGB 21.6 — but 108, (135,175,135), is
#     shipped instead, at 5.274 and 34.45. It is deliberately NOT the nearest, and it
#     is the next-nearest of all 240. The cube takes the brand's red 164 DOWN to the
#     135 rung (29, where the next rung 95 is 69 away), green 186 down to 175 (11,
#     next 29 away) and blue 150 down to 135 (15, against 25 for 175), so the whole
#     of the conversion error is 29, 11 and 15. The greyscale ramp is still not a
#     candidate: its best is 249, a pure neutral, at 5.567.
#
# Two things pay for the 2.187 of OKLab the second-nearest costs, and both were
# decided rather than drifted into:
#
# HUE IS THE CHANNEL A BRAND MARK IS RECOGNISED BY. The brand's OKLab hue is 133
#     degrees; 144 sits at 108 and reads OLIVE, 108 at 145 and reads green. For a
#     logo that is the error that shows — a sage gone olive is simply the wrong
#     colour — and 2.187 of total distance is a fair price for keeping the hue.
#
# 108 ALSO CLEARS THE TIGHTEST PAIR IN THIS TABLE. At 144 the mark would have sat
#     5.578 from 145, `opencode-zen`'s mark: red and green EQUAL at 175 each and
#     OKLab lightness barely separating them (0.744 against 0.754), so the whole of
#     the difference is 40 of blue — 145 exactly NEUTRAL (chroma 0.000) where 144
#     carries 0.055. That is closer than openai 36 against goose 37 at 5.969, which
#     has the SAME one-channel geometry and has always shipped unremarked. 108 puts
#     the pair at 8.234, clear of it. So the row that moved is not the one whose
#     brand colour was closest; it is the one that sat next to a neutral.
#
# 145 IS NOT A BRAND COLOUR AT ALL, which is worth having on the record before
#     anyone reads the 108/145 distance as a collision between two brands.
#     `opencode-zen`'s glyph is ▯, U+25AF WHITE VERTICAL RECTANGLE, and "white"
#     there is the Unicode naming convention for HOLLOW — unfilled — not a colour.
#     The operator confirmed the mark carries no brand colour, so 145 is a flat
#     neutral standing in for no identity. That row had nothing to preserve and
#     COULD have been the one to move; it did not have to be, because 108 is
#     independently the better sage. Written down so the next person to sit with
#     this pair does not re-run the investigation from scratch.
#
# Luminance falls from 0.415 to 0.376 — far above the dark-pane floor, and still NOT
#     the darkest colour the table ships: hermes 130 at 0.173 keeps that slot, which
#     is why the floor test still names hermes rather than this row.
#
# THE FLOOR ABOVE IS A DARK-PANE FLOOR, AND IT IS NOW AN ASSUMPTION ABOUT A
# BACKGROUND RATHER THAN ABOUT A TERMINAL. Before themes, `_Colours.attr` coloured
# a FOREGROUND on the TERMINAL'S OWN background (the `-1` in `init_pair`), so on a
# light terminal every entry here inverted into the very problem the floor exists
# to prevent — 227 is the second-brightest mark in the table (only xai's white is
# brighter) and washed out against white. That comment was right that the fix is
# a background decision and wrong that it is not made here: it is made in
# `themes.json`, and `_role_colour` now asks the ACTIVE THEME for its `bg` before
# it paints anything.
#
# THE TABLE ITSELF IS UNCHANGED AND MUST STAY THAT WAY. These fourteen indices are
# the operator's logo colours and they ship byte-identical; a brand colour is not a
# taste. What answers the light-terminal case is `readable_index` at PAINT time:
# on a theme dark enough for a mark it returns the table index unchanged — all
# fourteen, on dracula and on every other dark row — and only where a mark would
# be invisible does it walk that mark's HUE to a readable LIGHTNESS. It never
# writes here.
_VENDOR_COLOURS = {
    "anthropic": 173,
    "openai": 36,
    "google": 69,
    "kilo": 227,
    "aws-kiro": 208,
    "xai": 231,
    "goose": 37,
    "hermes": 130,
    "cognition": 32,
    "z-ai": 108,
    "opencode-zen": 145,
    "deepseek": 63,
    "cursor": 244,
    "moonshotai": 252,
}

# TODAY'S COLOURS, AS A THEME. `og` reproduces exactly what this pane painted
# before themes existed — bg, text and sel_bg all `"default"`, so the pane keeps
# the terminal's own background, and the three coloured roles converted straight
# back to the indices above. It is the ONLY way back to a transparent pane now
# that every other theme fills it, which makes it the regression guard for the
# whole background change rather than a cosmetic extra: with `og` selected the
# pane spends `init_pair(n, fg, -1)` exactly as it did, and the golden test pins
# `render` byte-for-byte against the pre-theme output.
#
# `order: 0` puts it FIRST in the picker. dracula remains the default theme; `og`
# is the way back, not the way in.
_OG_COLOURS = {
    "bg": "default",
    "text": "default",
    "meta": "#87afaf",
    "sel_bg": "default",
    "idle": "#00ff00",
    "working": "#ffff00",
    "rail": "default",
}

# The one colour every worker META paints in under the `og` theme: ansi 109, a
# light slate that reads as secondary on a dark pane. Not bold — see `_role_bold`.
#
# NOT A CONSTANT ANY MORE: it is the `og` theme's `meta` row converted, which is
# the honest direction of travel (the catalog is the source, this module reads it)
# and is also what makes it checkable — `hex_to_ansi256("#87afaf") == 109` is a
# test, so if the converter and this ever disagree the failure is named rather
# than shipped as a colour nobody chose. Under any other theme `_role_colour`
# asks THAT theme instead, and this is simply what `og` paints.
META_COLOR = hex_to_ansi256(_OG_COLOURS["meta"])

# THE STATUS GLYPH's colours, asked for by name: the check mark NEON GREEN and the
# spinner NEON YELLOW, so a pane with four workers running reads as four lit
# glyphs down the gutter rather than four identical grey ⠋ braille cells at two
# brightnesses. Only these two states are coloured — `⏸` blocked, `✗` failed and
# `?` unknown stay PLAIN, because a red `✗` and a grey `⏸` are taste as much as
# they are meaning and nobody asked for them; the table is the whole of it, and a
# state absent here paints uncoloured.
#
# BOTH INDICES ARE PURE HUE AT FULL VALUE ON THE 6x6x6 CUBE, not a pale tint of
# one: 46 is (0,255,0) and 226 is (255,255,0), the two cells in the cube that are
# a neon green and a neon yellow with nothing shaded out of them. The cells beside
# them shade toward white (118, 190, 229) and read as a pastel, which on a gutter
# reads as "secondary"; the named 8 are duller still. Both indices are > 16, so
# like every vendor colour here they want a 256-colour terminal and fall back to
# uncoloured below that (`_Colours.attr`).
#
# Like `META_COLOR`, these are the `og` theme's rows read back through the
# converter, not literals: the mapping is which STATE gets a colour, and that is
# still the whole table.
_STATE_COLOURS = {
    "idle": hex_to_ansi256(_OG_COLOURS["idle"]),
    "working": hex_to_ansi256(_OG_COLOURS["working"]),
}


def mark_role(vendor: str) -> str:
    """The colour role for a harness mark painted on `vendor`'s brand colour."""
    return f"mark:{vendor}"


def state_role(state: str) -> str:
    """The colour role for `state`'s status glyph: `state:<state>`, or PLAIN.

    The same shape as `mark_role`, and for the same reason — the ROLE is the
    colour's NAME and the colour is looked up in one table (`_STATE_COLOURS`), so
    the layout composes glyph and state and never a palette.
    """
    return f"state:{state}" if state in _STATE_COLOURS else PLAIN


# A mark is padded into a FIXED two display columns, not left as-is. Without the
# slot, a Narrow mark (one cell) and a Wide or Ambiguous one (more) on
# consecutive rows start the column after them at different offsets, and every
# box edge, rule and right-aligned clock downstream shears by that difference.
# Padding each mark to the same column removes it.
#
# THE SLOT MEASURES WITH THE WESTERN RULE (`_slot_width`): an Ambiguous glyph is
# charged ONE column and padded exactly like a Narrow one. Every mark ships Narrow
# now, so today that is simply glyph plus one space for all fourteen and the title
# starts in the same column on every row; the Ambiguous branch is HYPOTHETICAL,
# kept for a future mark rather than any that ships. It was once the live case:
# the three then-Ambiguous picks (kilo, cognition, z-ai, since replaced with
# Narrow glyphs) were charged one under this rule — but an earlier slot charged
# Ambiguous two and so left them UNPADDED, which in a western terminal draws them
# one cell wide and shears their titles one column LEFT of every other row. That
# is the locale the operator actually runs, so it was the wrong rule kept for the
# wrong terminal. Do NOT "fix" it back: see `_slot_width` for why the slot and
# `_display_width` disagree.
MARK_SLOT = 2


def _slot_width(text: str) -> int:
    """The columns the SLOT charges `text`: Wide/Fullwidth two, AMBIGUOUS one.

    The slot's ruler, and only the slot's. Its job is to start the title in the
    same column on every row in the terminal the operator actually runs, so it
    measures what a WESTERN terminal paints: Wide and Fullwidth glyphs really do
    spend two cells there, an Ambiguous one spends one. Charging Ambiguous two —
    the pessimistic rule `_display_width` keeps for the footer — is what once left
    the then-Ambiguous marks (kilo, cognition, z-ai) unpadded and sheared their
    titles one column left. Those three are Narrow now, so no shipped mark takes
    the Ambiguous branch; it is kept so a future one is still charged correctly.

    Deliberately NOT `len`: a future Wide mark is one python character that
    paints two cells, and `len` would pad it as if it were one — the same shear,
    in the other direction. Deliberately NOT `_display_width`: that rule is
    correct for the footer, which must not overflow in ANY locale, and wrong
    here, where the whole pane already assumes the western paint rule (the rail
    and the corners are Ambiguous and composed as one column — see ROUND_CORNERS;
    the node was too until it became Narrow). The slot stays consistent with them.

    CJK TRADE-OFF: in a CJK-locale terminal an Ambiguous mark paints two columns,
    so a row here composes one column narrower than it paints and shears right by
    one. That trade is HYPOTHETICAL today — no shipped mark is Ambiguous, so no
    current row takes it. It is kept, not removed, so a future Ambiguous mark is
    charged by the same one-column rule the rest of the pane composes under.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


# The mark for an agent whose VENDOR could not be identified — U+269B ATOM SYMBOL.
# It is East Asian NARROW (measured: `east_asian_width("\u269b") == "N"`), so its
# slot is the glyph plus one trailing space, exactly two display columns like
# every other Narrow mark. It means "an agent whose harness I could not identify",
# NOT "no agent here": a row that names no agent identity at all draws the blank
# slot instead (see `harness_mark` / `worker_mark`), so the atom never appears on
# a row that simply has nothing to mark.
ATOM = "\u269b"


def _mark_slot(mark: str) -> str:
    """`mark` padded to exactly `MARK_SLOT` display columns. THE slot.

    A mark that names no agent fills the slot with spaces; every other is padded
    with the difference between its `_slot_width` and `MARK_SLOT` — one trailing
    space for a Narrow mark (and, hypothetically, for an Ambiguous one, which the
    western rule also charges one cell), because in the western terminal this pane
    is composed for both spend one cell and need the same pad to spend two layout
    columns. A Wide or Fullwidth mark fills the slot on its own and gets no pad.
    Every mark shipped today is Narrow, so the first case is the only one taken.

    The ruler is `_slot_width`, never `len` and never `_display_width`: see that
    function for the western rule and the (now hypothetical) CJK trade-off this
    slot would make for an Ambiguous mark, and for why the two rulers disagree.
    """
    if not mark or mark == " ":
        return " " * MARK_SLOT
    return mark + " " * max(0, MARK_SLOT - _slot_width(mark))


def _vendor_mark(vendor: Any) -> str:
    """The mark slot for an already-resolved vendor, atom when it resolved to
    nothing.

    The atom is the honest answer for a row that NAMES an agent (`claude-native`,
    `zen`) whose vendor the catalog does not carry: something is there, we just
    cannot say what. It is reached both by a harness id whose registry row is
    missing and by an agent name the registry does not list, which is why it lives
    here rather than in either lookup.
    """
    return _mark_slot(_HARNESS_MARKS.get(vendor) or ATOM)


def harness_mark(harness: Any) -> str:
    """The brand mark for a HARNESS ID, in its fixed two-column slot.

    `claude-native` -> "✳ " (vendor `anthropic`). Resolves harness id -> vendor
    through `og_herdr.harness_vendors` (the same registry the labels come from, so
    the two cannot disagree) and then vendor -> glyph through `_HARNESS_MARKS`.

    A harness that is ABSENT — None, empty, whitespace — draws the BLANK slot
    (two spaces): there is no agent to identify, and a mark would be meaningless.
    A harness that is PRESENT but unresolvable — not in the registry, or on a
    vendor the mark table does not carry, or a registry that could not be read —
    draws the ATOM, because an agent IS named and only its vendor is unknown.

    NEVER raises and never loses a row over a cosmetic failure, exactly as
    `harness_label` degrades to a raw id. A pane must be able to render with no
    marks at all rather than not render; the atom is the honest "unknown agent",
    the blank the honest "no agent".
    """
    if not isinstance(harness, str) or not harness.strip():
        return _mark_slot("")
    return _vendor_mark(harness_vendors().get(harness))


def worker_mark(harness: Any, agent: Any) -> str:
    """The brand mark for a WORKER row, by the resolution order (highest first).

    A mark identifies THE HARNESS THE SESSION ACTUALLY RAN ON, and nothing else —
    never a role, never inferred from what the agent is merely CALLED. So:

      1. the row's OWN harness, when it carries one. A worker's LISTING row does
         not, but any source that supplies one is reporting what ACTUALLY ran, so
         it wins over anything the name would imply;
      2. otherwise the harness the agent's installed SPEC declares, looked up by
         the row's agent name through `og_herdr.agent_harness`. This is the
         harness the worker was dispatched on, read from the truth rather than
         guessed from the name;
      3. the ATOM when an agent IS named but no harness could be found for it —
         the honest "something ran here that I cannot identify".

    A row with NO agent identity at all (neither a harness nor a name) draws the
    BLANK slot instead: there is nothing to mark, which is a different fact from
    "an agent I could not identify". Never raises.
    """
    if isinstance(harness, str) and harness.strip():
        return harness_mark(harness)
    if not isinstance(agent, str) or not agent.strip():
        return _mark_slot("")
    declared = agent_harness(agent)
    if not declared:
        return _vendor_mark(None)
    return harness_mark(declared)


def harness_vendor(harness: Any) -> Optional[str]:
    """The vendor whose brand colour `harness`'s mark carries, or None.

    The SAME harness -> vendor hop `harness_mark` makes, kept beside it so a
    mark's GLYPH and its COLOUR can never come from two different vendors. None
    is the honest "no brand colour": an absent harness draws the blank slot, and
    one whose vendor the mark table does not carry draws the atom — both paint
    PLAIN.
    """
    if not isinstance(harness, str) or not harness.strip():
        return None
    vendor = harness_vendors().get(harness)
    return vendor if vendor in _HARNESS_MARKS else None


def worker_vendor(harness: Any, agent: Any) -> Optional[str]:
    """The vendor whose brand colour a WORKER row's mark carries, or None.

    `worker_mark`'s resolution order, exactly — the row's own harness, then the
    harness the agent's installed spec declares, then nothing (the atom or the
    blank slot) — so the mark and its colour agree by construction.
    """
    if isinstance(harness, str) and harness.strip():
        return harness_vendor(harness)
    if not isinstance(agent, str) or not agent.strip():
        return None
    declared = agent_harness(agent)
    if not declared:
        return None
    return harness_vendor(declared)


def build_rows(
    sessions: Iterable[dict],
    root_ids: Iterable[str],
    snippets: Optional[dict[str, Optional[str]]] = None,
    expanded_ids: Iterable[str] = (),
    now: Optional[float] = None,
) -> list[Row]:
    """The display rows for the selected roots' trees, in DISPLAY order (C2).

    The order built here IS the order on screen, and it is what the selection
    indexes into: per root an outdented box row, then for each state group a
    breathing rail row and the group's worker boxes. So the whole view reads top
    to bottom the way it is painted.

    THE GROUPING HAPPENS HERE, not in `_pane`, and that is load-bearing. If the
    view grouped the workers while `Frame.rows` stayed in tree order, the arrow
    keys — `move_selection` and `_run_tui`'s scroll — would walk the rows in
    tree order while the eye read state order, and the selection would appear to
    jump around at random. Sorting the ROWS themselves keeps every index-based
    helper correct without a change; the SELECTION follows its session across
    this reorder, because a held index would land on whichever worker now sits
    there (see `snap_selection`).

    The sort is STABLE (`_grouped`), so two polls that brought the same set of
    workers produce byte-identical rows; an unstable order would make otherwise
    identical frames differ and defeat `paint_needed` forever.

    A worker can have children, and the tree is FLATTENED: a descendant is
    grouped by its OWN state like any other worker, and the spine columns are not
    drawn. Grouping by state and drawing a tree spine are mutually exclusive —
    a child placed under its parent's group would sit under a state that disagrees
    with its own — so the parent link is dropped rather than lied about. `depth`
    is still carried, and nothing crashes on nesting.

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
    for root_id in root_ids:
        if root_id not in by_id:
            continue
        workers = _worker_rows(by_id, children, root_id, snippets, expanded, clock)
        rows.append(_root_row(by_id[root_id], root_id))
        for state, group in _grouped(workers):
            # The rail row opens the group: the ONE thing that marks where one
            # group ends and the next begins, now the worded label is gone. It
            # carries the group's state (never a mystery) and is the group's
            # opening brace for `_blocks`' height trimming.
            rows.append(Row(session_id="", text="", state=state, depth=1, rail=True))
            rows.extend(group)
    return rows


def _root_row(session: dict, root_id: str) -> Row:
    """The row for one root: its name and harness, drawn as an outdented box.

    The done-gauge that used to sit under the name is gone; the harness LABEL is
    kept, moved to the box's right edge. `workers` was only ever read to count the
    gauge's numerator and denominator, so the parameter is gone with it.
    """
    harness = session.get("harness")
    title = (session.get("title") or "").strip() or root_id
    return Row(
        session_id=root_id,
        text=title,
        state=display_state(session),
        depth=0,
        is_root=True,
        harness=harness if isinstance(harness, str) else "",
    )


def _worker_rows(by_id: dict, children: dict, root_id: str,
                 snippets: dict, expanded: set, clock: float) -> list[Row]:
    """The worker Rows under one root, in pre-order (nesting flattened)."""
    out: list[Row] = []
    for sid, depth in _preorder(by_id, children, [root_id]):
        if depth == 0 or sid not in by_id:
            continue
        session = by_id[sid]
        state = display_state(session)
        agent, task = split_title(session.get("title"))
        snippet = snippets.get(sid)
        harness = session.get("harness")
        out.append(Row(
            session_id=sid,
            text=task,
            state=state,
            depth=depth,
            snippet=snippet,
            expanded=sid in expanded and snippet is not None,
            agent=agent,
            meta=worker_meta(session, state, clock),
            harness=harness if isinstance(harness, str) else "",
            root_session_id=root_id,
        ))
    return out


def _grouped(workers: list[Row]) -> list[tuple[str, list[Row]]]:
    """`workers` bucketed by state, groups in `GROUP_ORDER`, STABLE within a group.

    Stable is a requirement, not a nicety: two frames built from the same workers
    must compose to identical lines, or `paint_needed` sees a difference that is
    not one and the pane repaints forever. `sorted` is stable, so the pre-order
    the walk produced is preserved inside each bucket. A state outside
    `GROUP_ORDER` still gets its own group rather than being dropped, so an
    unheard-of status can never silently lose a worker.
    """
    buckets: dict[str, list[Row]] = {}
    for row in workers:
        buckets.setdefault(row.state, []).append(row)
    order = sorted(buckets, key=lambda st: GROUP_ORDER.index(st)
                   if st in GROUP_ORDER else len(GROUP_ORDER))
    return [(state, buckets[state]) for state in order]


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

    KNOWN INCONSISTENCY: this cuts by `len`, i.e. code point, while
    `_display_width` counts DISPLAY columns (a Wide/Ambiguous glyph as two). The
    two agree on everything the footer — the only line `_display_width` guards —
    now contains, since it is ASCII-plus-Narrow. They disagree on a wide glyph,
    which a session snippet could still carry: such a row could be clipped a
    column short or long. Reconciling them means width-aware clipping throughout
    the renderer (grapheme clusters included), a larger change than it looks and
    deliberately not made here.
    """
    if width <= 0:
        return ""
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


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
    """The display columns of plain `text`: a Wide, Fullwidth, or AMBIGUOUS glyph
    costs two, the rest one.

    `_clip` and `_visible_width` count with `len`, which is right for every
    character in this view EXCEPT a wide one: counting that as a single column
    judges a string narrower than the terminal paints it, so it is passed as
    fitting and then spills past the pane — the CJK-locale form of the very bug
    the footer keeps re-learning.

    AMBIGUOUS (East Asian "A") is charged two as well, deliberately. Its width is
    locale-dependent — one column in a Latin locale, two in a CJK one — so the
    only rule correct in BOTH is the pessimistic one; charging it one is how the
    footer's old ↑/↓ cost 64 measured but 66 painted. The footer now names no
    Ambiguous glyph, so this backstop does not move any current threshold: it only
    bites a future edit that adds one. See the footer comment.

    NOT the mark slot's ruler: `_slot_width` measures that column, because a
    title has to line up in the locale the operator RUNS, not in both. Do not
    "unify" the two by pointing `_mark_slot` back here — that re-creates the
    sheared-title bug the slot exists to prevent.

    No ANSI stripping: the footer is plain text. A line that may carry escapes is
    measured through `_visible_width` instead.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
               for ch in text)


def _boxed(lines: Iterable[str], width: int,
           title: str = ACCESS_TITLE) -> list[str]:
    """`lines` in a box as wide as their widest row, never wider than `width`.

    Content-sized, not pane-sized: a QR boxed out to eighty columns is mostly
    border, and the border is what the eye lands on when scanning for the code.

    `title` defaults to the access panel's own so the one existing caller is
    unchanged; the theme picker passes its own, because a box that says "access"
    while listing twenty-six themes is worse than no title. An EMPTY title drops
    the `┌─ ` rule with it — four columns the picker cannot always spend on a pane
    narrow enough to need the untitled form in the first place.
    """
    rows = [_clip(text, max(1, width - BOX_PADDING)) for text in lines]
    # Never narrower than the title rule, or the box loses its rectangle. An
    # UNTITLED box has no rule to be narrower than, so it is sized to its content
    # alone -- four columns of `┌─ ` that buy nothing at a width narrow enough to
    # need the untitled form.
    inner = max(max((len(row) for row in rows), default=0),
                len(title) + 3 if title else 0) + 2
    head = (f"┌─ {title} " + "─" * (inner - len(title) - 3) + "┐") if title \
        else "┌" + "─" * inner + "┐"
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


# --- span composition --------------------------------------------------------
#
# Spans only PARTITION a line; they never change its text. Every helper here
# reproduces, on a span list, exactly what the old whole-string layout did on the
# JOIN — `_clip_spans` cuts and ellipsises like `_clip`, `_pad_spans` ljusts the
# same way — so `"".join(text for text, _ in spans)` still equals the line the
# layout has always produced. That invariant is the whole reason the pane's text
# and its paint are separable, and a test pins it.

def _spans_width(spans: list[Span]) -> int:
    """The columns of a span list, under the layout's `len` ruler (`_clip`)."""
    return sum(len(text) for text, _role in spans)


def _clip_spans(spans: list[Span], width: int) -> list[Span]:
    """`spans` cut to `width` columns with an ellipsis, exactly as `_clip` on the
    join.

    The ellipsis rides on the LAST KEPT span's role, so a cut inside a run keeps
    that run's colour, and a cut with nothing kept falls back to PLAIN. The ruler
    is `len`, matching `_clip`, so the join is byte-for-byte the old clip.
    """
    if width <= 0:
        return []
    if _spans_width(spans) <= width:
        return list(spans)
    keep = width - 1
    out: list[Span] = []
    used = 0
    for text, role in spans:
        if used >= keep:
            break
        take = text[:keep - used]
        out.append((take, role))
        used += len(take)
    out.append(("…", out[-1][1] if out else PLAIN))
    return out


def _pad_spans(spans: list[Span], width: int) -> list[Span]:
    """`spans` clipped to `width` columns and right-padded out to exactly `width`."""
    clipped = _clip_spans(spans, width)
    gap = width - _spans_width(clipped)
    if gap > 0:
        clipped = clipped + [(" " * gap, PLAIN)]
    return clipped


def _two_col(left: list[Span], right: list[Span], width: int) -> list[Span]:
    """`left` flush left, `right` flush right, exactly `width` columns.

    The span twin of the string `_two_col`, and the same rule: the right part
    never loses its last column to the left part. When `left` has more to say
    than there is room for, the RIGHT part is what is drawn and the left is
    ellipsised into the columns that remain. The right side is the short, fixed
    thing the row is really about, so it is worth keeping whole while the elastic
    left gives way — which is also what preserves its COLOUR ROLE: a tight line
    loses the title, never the meta's META. Both callers rely on this: the worker
    row hands it the meta — `2m14s`, `failed`, `waiting on you`, `done` —
    and the root box's content row the harness label (`Claude Code`). Only when
    `right` alone is wider than `width` is it clipped itself.
    """
    if width <= 0:
        return []
    right = _clip_spans(right, width)
    room = width - _spans_width(right)
    if room <= 0:
        return _clip_spans(right, width)
    return _pad_spans(left, room) + right


def _round_box_lines(inner_lines: list[list[Span]], width: int) -> list[list[Span]]:
    """`inner_lines` in a rounded box EXACTLY `width` columns wide (len ruler).

    The span twin of the old string `_round_box`, and its output joins to the
    same line: light `─` under curved `╭╮╰╯`, the borders PLAIN, each body line
    padded to the box's inner width. Both the root (drawn at column 0) and each
    worker box (behind the gutter) are rounded, and the weight contrast is the
    interest.
    """
    width = max(BOX_MIN, width)
    tl, hz, tr, vt, bl, br = ROUND_CORNERS
    out = [[(tl + hz * (width - 2) + tr, PLAIN)]]
    out += [[(vt, PLAIN)] + _pad_spans(line, width - 2) + [(vt, PLAIN)]
            for line in inner_lines]
    out.append([(bl + hz * (width - 2) + br, PLAIN)])
    return out


def _root_lines(row: Row, width: int) -> list[list[Span]]:
    """A root as a rounded box drawn at column 0: name left, harness right.

    OUTDENTED relative to its workers, which sit at column `GUTTER`: the root is
    their parent, and the group rail below it drops out of its own bottom-left
    corner `╰` — both in column 0 — so the rail visibly continues out of the box.
    This replaces the heavy `━` title bar and its done-gauge; the harness LABEL is
    kept, moved to the box's right edge, and no line of the pane spends A_REVERSE.

    The NAME is `ORCH` (the bold the operator asked for) and the mark carries its
    vendor's brand colour; the label and the borders stay PLAIN.

    Below `BOX_MIN` a one- or two-column "box" would be a lie, so the root
    degrades to a single clipped line, exactly as `worker_box`'s ladder does.
    """
    if width < BOX_MIN:
        return [[(_clip(row.text, width), ORCH)]]
    vendor = harness_vendor(row.harness)
    left = [(" ", PLAIN),
            (harness_mark(row.harness), mark_role(vendor) if vendor else PLAIN),
            (" ", PLAIN),
            (row.text, ORCH)]
    content = _two_col(left, [(harness_label(row.harness), PLAIN)], width - 2)
    return _round_box_lines([content], width)


def worker_box(row: Row, width: int, selected: bool,
               spin: int = 0) -> tuple[list[list[Span]], int]:
    """One worker as a guttered, rounded box, and the index of its SELECTED row.

    Each returned line is a list of SPANS; joined, a returned line is exactly
    `width` columns: the four-column gutter plus a box `width - GUTTER` wide. The
    caller hangs the line's DEFAULT brightness on it — everything here paints at
    that brightness except a span whose role carries the `undim:` prefix, which is
    exempt from it (see `undim`).

    The gutter's NODE column carries `⦿` on the box's CONTENT row — the point
    where this agent's box hangs off the rail — and the rail `│` on every other
    row of the box; the STATE glyph column carries `state_glyph` on that same
    content row and a space elsewhere. So the state glyph has LEFT the box: the
    content row inside is just ` <mark>  <title>` flush left with the meta flush
    right. The returned index is 0 — the caller hangs the SELECTED role there,
    which is the row the live view scrolls to keep visible.

    `selected` does NOT change the text this returns: selection is the paint role
    the caller hangs on `selected_row`, never a glyph. So `--once`, which has no
    attributes, cannot show which worker is selected — a one-shot dump carries no
    selection signal at all (see `main`).

    THE GUTTER IS EXEMPT FROM DIM, ON EVERY ROW. The user reported that the dim
    swallowed it: an unselected worker's rail, node and status glyph dimmed with
    the box, and "which workers are running at a glance" — the one thing the
    gutter exists to answer — became unreadable. Every gutter span is hung through
    `undim`, so it paints at full brightness whatever the box around it does,
    while the box's border and body still take the line's brightness. Selection is
    unaffected: the selected box was already at full brightness, so it renders
    exactly as before.

    The box is THREE rows — top border, ONE content row, bottom border — and FOUR
    when the report is expanded (the extra row is the dimmed snippet, INSIDE the
    box, never a loose line that would break the rectangle). The single content
    row carries the harness mark in its fixed two-column slot and the title flush
    left, with the meta (`2m14s`, `failed`, `waiting on you`) flush right on the
    SAME row. The AGENT NAME is gone: the mark already names the harness the name
    did, so the meta — the one thing on the row the mark does not say — is all
    that is left to draw. `_two_col` keeps the meta's columns and clips the TITLE
    first when the line is tight.

    `spin` is the frame counter `state_glyph` turns into a glyph, and it animates
    the WORKING row alone: a blocked or finished box renders identically at every
    value, which is what keeps a pane with nothing working byte-identical between
    polls.

    NARROW PANES DEGRADE IN A FIXED ORDER (the ladder below), and the one
    invariant they never break is this: at ANY width >= 1, no line returned here
    is longer than `width`. An over-wide line is WRAPPED by the terminal, not
    clipped, and a wrapped row in this layout is the failure the operator reports
    as "the agents pane is broken".
    """
    glyph = state_glyph(row.state, spin)

    # THE DEGRADATION LADDER. A worker line is gutter + box, and below some width
    # they stop fitting together. The gutter's two named columns give up in this
    # order — the STATE glyph column and its space BEFORE the box, and the NODE
    # column before the box too, because the box is what carries the content:
    #
    #   1. width >= GUTTER + BOX_MIN (8): the shipped shape — the node column, a
    #      space, the state glyph column, a space, then a box of width - GUTTER.
    #   2. width >= 2 + BOX_MIN (6): the state glyph column and its space are
    #      dropped, there being no room for them, leaving the node column + one
    #      space before the box. The node stays: it is the column that still says
    #      an agent's box hangs off the rail, and the row keeps its paint role, so
    #      the selection is still findable.
    #   3. width >= BOX_MIN (4): the node column goes too; the box is the row.
    #   4. width < 4: no box is possible at all — the smallest is `╭─╮` — so the
    #      row degrades to ONE clipped line: the state glyph FIRST, then as much
    #      of the task as fits. The glyph is the one column worth keeping — it is
    #      the status — so it is composed before the clip and never lost: at
    #      width 1 it IS the row, at 2 and 3 it is followed by the clipped task.
    #      Honest and short rather than a broken box.
    #
    # Below step 1 the pane is absurd in practice; the invariant is not. `_clip`
    # is the only thing between a composed line and a wrapped pane, and it cannot
    # rescue a line built longer than the pane in the first place.
    if width < BOX_MIN:
        # The glyph is composed FIRST because `_clip` does not keep it: handed
        # the whole line at width 1 it spends that one column on its own ellipsis
        # (`…`) and the state is gone — a bare `…` where the comment above
        # promises the state. So only the trailing task is clipped, into the
        # columns left after the glyph; width 1 leaves none, and the glyph stands
        # alone. Width <= 0 has no column at all; `_clip`'s own floor.
        #
        # Two spans, not one concatenated string, and for the same reason the
        # guttered row splits them: there is no box here, but the GLYPH is still
        # the status and is still exempt from the line's DIM and still wears its
        # state's colour, while the task beside it takes the line's brightness.
        if width < 1:
            return [[]], 0
        return [[(glyph, undim(state_role(row.state))),
                 (_clip(" " + row.text, width - 1), PLAIN)]], 0

    if width >= GUTTER + BOX_MIN:
        box_width, node_column, glyph_column = width - GUTTER, True, True
    elif width >= 2 + BOX_MIN:
        box_width, node_column, glyph_column = width - 2, True, False
    else:
        box_width, node_column, glyph_column = width, False, False

    inner = max(0, box_width - 2)
    # The row's own harness wins; a worker carries none, so its mark comes from
    # the harness its SPEC declares (found by the agent name the title gave) —
    # only when BOTH the harness and the name are absent is the slot blank (see
    # `worker_mark`). The mark paints its VENDOR's brand colour (`worker_vendor`),
    # the meta its one META colour, the title plain.
    mark = worker_mark(row.harness, row.agent)
    vendor = worker_vendor(row.harness, row.agent)
    # ONE content row. The mark and the task sit flush left, the meta flush right
    # on the SAME line; the state glyph is in the gutter, not here. The agent name
    # is DROPPED: the mark already names the harness the name did, and the meta is
    # the only thing on the row the mark does not say. The title yields first when
    # the line is tight (`_two_col` clips the LEFT part with `_clip`, so it is
    # ellipsised, not cut blank), and the meta keeps its columns — it is the thing
    # being waited on.
    left = [(" ", PLAIN),
            (mark, mark_role(vendor) if vendor else PLAIN),
            (" ", PLAIN),
            (row.text, PLAIN)]
    content = _two_col(left, [(row.meta, META)], inner)
    lines = [content]
    if row.expanded and row.snippet:
        lines.append([("  " + _clip(_oneline(row.snippet), inner - 2), PLAIN)])
    boxed = _round_box_lines(lines, box_width)

    out: list[list[Span]] = []
    for index, spans in enumerate(boxed):
        # The CONTENT row is the box's first inner row, just below the top border.
        # The node and the state glyph ride on that row alone; the rail and a space
        # fill every other row, so the line down column 0 is unbroken.
        on_content = index == 1
        # THE GUTTER IS NOT PART OF THE BOX, so it does not paint like it. Both of
        # its spans go through `undim` and the glyph's through `state_role`, which
        # is the whole of the reported "the dim swallows the gutter and the status
        # glyph": the rail, the node and the `⠋`/`✓` used to dim with the box they
        # sit beside, so a pane could not be read for what was RUNNING. Exempt from
        # brightness and coloured are ORTHOGONAL — the glyph is `undim:` AND
        # `state:working`, and paints neon yellow at full brightness on an
        # unselected row while the box around it stays DIM.
        gutter: list[Span] = []
        if node_column:
            gutter.append(((NODE if on_content else RAIL) + " ", undim(PLAIN)))
        if glyph_column:
            glyph_role = state_role(row.state) if on_content else PLAIN
            gutter.append(((glyph if on_content else " ") + " ", undim(glyph_role)))
        out.append(gutter + spans if gutter else spans)
    return out, 0


def _is_box(row: Row) -> bool:
    """Whether a row is a worker box — the only rows the selection can land on.

    The roots and the breathing rails draw no box of their own to select; the
    selection steps over them (`_workers`, `move_selection`).
    """
    return not (row.is_root or row.rail)


def _workers(rows: list[Row]) -> list[int]:
    """The indexes of the rows that draw a box — the selectable worker rows."""
    return [i for i, row in enumerate(rows) if _is_box(row)]


def snap_selection(rows: list[Row], selected_id: Optional[str] = None,
                   selected: int = 0) -> int:
    """The index the caret takes after a refresh: the row whose SESSION was selected.

    Run after every refresh, when the row list has just changed underneath the
    caret. The caret follows the SESSION, not the index it used to sit at, and
    that is the whole point: a selected worker that changes state moves to
    another group, the rows REORDER (`build_rows` puts them in display order),
    and an index held across that refresh lands on a DIFFERENT worker — the
    operator presses Enter and focuses the wrong session. `selected_id` is the
    session id of the row that was selected; when that session is still on screen
    as a BOX the caret returns to it wherever the reorder moved it.

    When that session is GONE — it fell off the listing, or the scope moved to
    another tree — the fallback is the box NEAREST the old index (`selected`),
    never the top of the list: the operator's eye stays where it was instead of
    the caret teleporting to a part of the pane they were not reading. A tie goes
    to the upper box. The old index is also the whole story when the caller has no
    session id at all (the first frame of a view that opens on the first WORKER);
    nearest to index 0 finds that first worker rather than row 0, which is the
    root's title bar and draws no box, so a view that opened there would look
    like it had no selection at all.
    """
    workers = _workers(rows)
    if not workers:
        return 0
    if selected_id:
        for index in workers:
            if rows[index].session_id == selected_id:
                return index
    return min(workers, key=lambda index: (abs(index - selected), index))


def move_selection(rows: list[Row], selected: int, delta: int) -> int:
    """The selection after a keypress: the next or previous WORKER.

    Roots and rails are stepped over instead of landed on, for the same reason
    `snap_selection` prefers a worker: the caret belongs to a box. Because
    `Frame.rows` is in DISPLAY order (`build_rows`), this walks the workers in
    the order the eye reads them.
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


# ---------------------------------------------------------------------------
# the theme picker (`t`)
# ---------------------------------------------------------------------------
#
# An OVERLAY, not a mode: the pane behind it keeps its lines and its selection,
# the picker paints over the middle of them, and Esc puts back exactly what was
# there. It is drawn by the same `put`/`paint_line` pair as everything else, so
# its entries go through the same colour roles and the same theme as the pane —
# which is what makes applying a theme LIVE and checkable rather than a claim.

# The caret on the cursor row. `▸` is East Asian NARROW (measured, not assumed),
# for the same reason the footer is ASCII: an Ambiguous glyph here would shear the
# box by a column in a CJK locale, exactly the bug the three footer forms exist to
# end.
PICKER_CARET = "▸"
PICKER_TITLE = "theme"

# The keys the overlay answers, and it answers ONLY these: every other key is
# ignored, so a stray key cannot quit the pane from inside a dialog.
PICKER_KEYS = "^/v move  ⏎ apply  esc cancel"

# What `picker_lines` spends on the box itself before any entry: the top and
# bottom border, the title row, the blank row and the keys row. Named because the
# entry count is derived from it and a bare `4` here would read as a magic number
# the first time someone adds a row to the overlay.
PICKER_CHROME = 5

# Cursor keys arrive from curses as KEY_UP / KEY_DOWN, which is an import this
# module does not want at module scope (it is standalone and paints without
# curses). `_run_tui` maps them onto these two sentinels so the handler below can
# be driven by a test with no terminal at all.
PICKER_UP, PICKER_DOWN = -1, -2


@dataclass
class Picker:
    """The picker's whole state: which themes, where the caret is, what is live.

    `applied` is None until Enter, which is what makes Esc unambiguous — a cancel
    and an apply are different values rather than the same one meaning "both".
    """
    slugs: list
    labels: dict
    cursor: int = 0
    active: str = ""
    applied: Optional[str] = None
    closed: bool = False

    def current(self) -> str:
        """The slug under the caret."""
        return self.slugs[self.cursor] if self.slugs else self.active


def _picker(themes: Optional[list] = None, active: str = "") -> Picker:
    """A picker over the catalog, caret on the ACTIVE theme rather than the top.

    Opening on the active row is the difference between a list you can read and a
    list you have to hunt: the operator is looking for the thing that is already
    on, not for row one.
    """
    rows = ordered_themes() if themes is None else themes
    slugs = [theme.slug for theme in rows]
    cursor = slugs.index(active) if active in slugs else 0
    return Picker(slugs=slugs, labels={t.slug: t.label for t in rows},
                  cursor=cursor, active=active)


def picker_key(state: Picker, key: int) -> Picker:
    """`state` after `key`, by value. Pure: no curses, no terminal.

    Up/down and j/k both move (the pane's own move keys are KEY_UP/KEY_DOWN and
    `j`/`k`, so the overlay matches what the operator's fingers already do), Enter
    applies and closes, `q` and Esc cancel and close. The caret CLAMPS rather than
    wrapping: a list that runs off its own end and comes back is a list you can
    get lost in, and this one is read by eye.
    """
    if state.closed:
        return state
    if key in (PICKER_UP, ord("k")):
        state.cursor = max(0, state.cursor - 1)
    elif key in (PICKER_DOWN, ord("j")):
        state.cursor = min(max(0, len(state.slugs) - 1), state.cursor + 1)
    elif key in (10, 13):
        state.applied = state.current()
        state.closed = True
    elif key in (27, ord("q")):
        state.closed = True
    return state


def _picker_keycode(curses_mod: Any, key: int) -> int:
    """`key` with curses' two cursor keys folded onto `PICKER_UP` / `PICKER_DOWN`.

    The only reason this exists at all is that `picker_key` is pure and this
    module does not import curses at module scope — the overlay must be testable
    by handing it plain integers. `getattr` rather than a direct attribute
    because the mapping is applied against whatever curses object the view is
    holding, which in the tests is a stand-in.
    """
    if key == getattr(curses_mod, "KEY_UP", -9999):
        return PICKER_UP
    if key == getattr(curses_mod, "KEY_DOWN", -9999):
        return PICKER_DOWN
    return key


# The narrowest pane a box can be drawn in at all: two borders, one column of
# content and the two of padding `_boxed` charges around it. Below this the
# picker draws NOTHING rather than a rectangle that does not close — which is the
# `access_lines`-drops-its-QR rule taken to its honest end. The agents pane is
# never this narrow (its own footer floor is 14 columns), so this is a guard on
# the arithmetic, not a mode an operator will meet.
PICKER_MIN_WIDTH = BOX_PADDING + 1


def picker_lines(state: Picker, width: int, height: int) -> list[Line]:
    """The overlay as LINES, exactly like `_pane` produces them.

    FEWER ENTRIES RATHER THAN OVERFLOWING, which is the precedent `access_lines`
    set when it dropped its QR and `_pane` sets when it trims the list: a picker
    taller than the pane is a picker with its own bottom edge off-screen, and the
    key hint that says how to close it is usually the thing at the bottom. So the
    entry list is trimmed to what fits and scrolls to keep the caret visible — the
    one case where dropping rows is correct, because the caret is what the
    operator is tracking and it is never dropped.

    Never returns a row wider than `width` or more rows than `height`, at ANY
    size. `attr_for`'s counterpart for the overlay is `paint_line`, which clips
    by column anyway; this is the backstop, and it holds at widths the pane itself
    will never reach.
    """
    if width < PICKER_MIN_WIDTH or height < 1:
        return []
    # The box's own two borders, its title row, the blank row and the keys row.
    room = max(1, height - PICKER_CHROME)
    top = 0
    if len(state.slugs) > room:
        top = max(0, min(state.cursor - room + 1, len(state.slugs) - room))
    shown = state.slugs[top:top + room]

    rows = ["".join(text for text, _role in _picker_entry(
        state, slug, slug == state.current(), slug == state.active))
        for slug in shown]
    if top + room < len(state.slugs):
        # Say what is hidden BELOW rather than letting the list read as complete —
        # the same rule as TRUNCATED_NOTICE for the listing. Not when the window
        # is at the end of the catalog: there is nothing below, and a marker there
        # would be claiming entries that do not exist.
        rows[-1] = rows[-1] + " …"

    # The keys hint goes before an entry does. A pane too short to hold the box
    # AND the hint loses the hint first: an overlay you cannot tell how to close
    # is a worse failure than an overlay showing fewer themes. Only when there is
    # no room for anything but the frame do entries go, and then there is nothing
    # left to draw in them anyway.
    extra = ["", _clip(PICKER_KEYS, width)]
    while len(rows) + len(extra) + 2 > height and extra:
        extra.pop()
    while len(rows) + len(extra) + 2 > height and rows:
        rows.pop()
    # The title goes before the content does, for the same reason: a rectangle
    # with no name is still a rectangle, and a truncated name is neither.
    title = PICKER_TITLE if width >= len(PICKER_TITLE) + BOX_PADDING + 3 else ""
    body = _boxed(rows + extra, width, title=title)
    return [([(line, PLAIN)], NORMAL) for line in body[:height]]


def _picker_entry(state: Picker, slug: str, on_cursor: bool,
                  is_active: bool) -> list[Span]:
    """One entry: the caret, a marker on the LIVE theme, and the label.

    The active theme is marked rather than left implicit, so an operator who
    cannot tell whether the pane has already changed does not have to close the
    overlay and look to find out. The marker rides on META so it dims with the
    rest of the secondary text instead of competing with the label for attention.
    """
    caret = PICKER_CARET if on_cursor else " "
    mark = " •" if is_active else "  "
    return [(caret, PLAIN), (mark, META), (state.labels.get(slug, slug), PLAIN)]


def _blocks(frame: Frame, width: int) -> list[list[Line]]:
    """The frame's rows as atomic BLOCKS, in display order, composed to spans.

    A block is one ROOT (its outdented box) or one GROUP (its breathing rail and
    all of the group's boxes). Grouping into blocks is what keeps `_pane`'s
    height trimming honest: the trimmer keeps or drops a whole block, so a box can
    never be cut in half and a rail can never be left without the group it opens.

    A new block starts at a root row or a rail row (a rail row is emitted by
    `build_rows` at the head of every group, so it is the group's opening brace).

    This is ALSO where BRIGHTNESS is hung: the layout functions return spans with
    colour roles only, and the selection axis is decided here (see below). What
    is hung here is the LINE'S DEFAULT: a span whose role carries the `undim:`
    prefix paints NORMAL whatever this decides, which is how the gutter and the
    status glyph stay bright on an unselected box without this function needing
    to know which spans they are.
    """
    blocks: list[list[Line]] = []
    unit: Optional[list[Line]] = None
    for index, row in enumerate(frame.rows):
        if (row.is_root or row.rail) and unit is not None:
            blocks.append(unit)
            unit = None
        if unit is None:
            unit = []
        if row.is_root:
            unit.extend((spans, NORMAL) for spans in _root_lines(row, width))
        elif row.rail:
            unit.append(([(RAIL, PLAIN)], NORMAL))
        else:
            on_selected = index == frame.selected
            lines, selected_row = worker_box(row, width, on_selected, frame.spin)
            for position, spans in enumerate(lines):
                # SELECTION IS BRIGHTNESS, and it is ORTHOGONAL to colour: a DIM
                # box still shows its mark's brand colour, merely dimmed. Every
                # line of an UNSELECTED worker's box is DIM; the SELECTED worker's
                # box is NORMAL, so it reads as white against the dimmed others.
                # SELECTED still rides on ONE line — the selected box's first row
                # — because that is the row `_run_tui` scrolls to keep visible; it
                # maps to the same attribute as NORMAL, so the box reads uniformly
                # white.
                #
                # DIM IS THE BOX, NOT THE ROW: `worker_box` marks its gutter and
                # status glyph `undim:`, so this value is what the BOX paints at
                # and those segments override it. That exemption is the reported
                # fix, and it lives in the spans rather than here so that this
                # function keeps one rule for every line it composes.
                if not on_selected:
                    brightness = DIM
                elif position == selected_row:
                    brightness = SELECTED
                else:
                    brightness = NORMAL
                unit.append((spans, brightness))
    if unit is not None:
        blocks.append(unit)
    return blocks


def _pane(frame: Frame, width: int = DEFAULT_WIDTH,
          height: Optional[int] = None) -> Pane:
    """The frame composed into lines — each a span list plus its brightness. Pure.

    The single place the view is put together: `render` returns just the text, and
    the TUI paints these same lines with the attributes their spans' roles and
    their brightness name. `height`, when given, is the pane's content height —
    the tail (panel and footer) is kept whole and the list above it is trimmed to
    fit, never below ACCESS_LIST_FLOOR rows, never by cutting a box in half, and
    never by dropping a group's rail away from its boxes (the trimmer works in
    whole blocks; see `_blocks`).

    An unselected worker's box is DIM, the selected worker's box NORMAL except its
    first line, which carries SELECTED: selection is brightness, not a glyph. A
    line's brightness is the DEFAULT its spans paint at — a span hung through
    `undim` (the worker's gutter and status glyph) ignores it and stays bright.

    A focus NOTICE takes the footer's row while it is up (see the tail below), so
    the pane is exactly as tall with a notice as without one — a failed Enter must
    not change the view's shape.
    """
    items: list[Line] = []
    # The truncation banner stays at the TOP: it is a fact about the LISTING
    # (rows past the page cap are missing), not about an action, so it belongs
    # over the rows it warns about. The focus notice has a different home — it
    # takes the footer's row, below. The two are independent, so a notice no
    # longer suppresses this banner the way it did when both fought for row 0.
    if frame.truncated:
        items.append(([(_clip(TRUNCATED_NOTICE, width), PLAIN)], NORMAL))

    blocks = _blocks(frame, width)

    tail: list[Line] = [([("", PLAIN)], NORMAL)]
    if frame.access is not None:
        tail.extend(([(line, PLAIN)], DIM)
                    for line in access_lines(frame.access, width))
        tail.append(([("", PLAIN)], NORMAL))
    # The focus notice, while it is up, TAKES the footer's row rather than adding
    # one above it: the tail is the same length either way, so the pane's row
    # count (and its list window) does not move when a notice appears. The notice
    # is clipped to the pane exactly as the footer is, so a long herdr error can
    # never overflow the row. It paints NORMAL, not the footer's DIM: it reports
    # an action that JUST FAILED, and dim is the treatment for chrome — an error
    # an operator skims past is the one thing this line must not be.
    if frame.notice:
        tail.append(([(_clip(frame.notice, width), PLAIN)], NORMAL))
    else:
        tail.append(([(_clip(_footer(width), width), PLAIN)], DIM))

    room = None if height is None else max(ACCESS_LIST_FLOOR, height - len(tail))
    kept: list[list[Line]] = []
    used = 0
    for block in blocks:
        # THE FIRST BLOCK IS KEPT EVEN WHEN IT EXCEEDS `room`, deliberately: one
        # whole box reads as a list where none reads as an empty pane, and a box
        # cut in half is the failure this whole-block trimmer exists to prevent.
        # The cost is that `list_rows` can exceed `room` when the pane is shorter
        # than even one block — and that is safe because the LIVE VIEW SCROLLS
        # the list (`_run_tui` paints `height - tail_len` of its rows and pins
        # the tail below) rather than trusting it to fit. Only a caller of
        # `render(height=...)` can see the overrun, and it sees a longer list,
        # never an overflowing window.
        if room is not None and kept and used + len(block) > room:
            break
        kept.append(block)
        used += len(block)

    for block in kept:
        items.extend(block)
    return Pane(items=items + tail, list_rows=len(items))


def line_text(line: Line) -> str:
    """The composed text of one line: its spans joined back together.

    The ONE place a line's paint is dropped for its text, used by `render`. Colour
    is a paint attribute and never reaches the text, so `--once` stays byte-for-
    byte what it always was.
    """
    spans, _brightness = line
    return "".join(text for text, _role in spans)


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
    return [line_text(line) for line in _pane(frame, width, height).items]


def frame_working(frame: Frame) -> bool:
    """Whether any WORKER is working — the only thing that animates.

    A root is a title bar and draws no spinner, and a group's breathing rail
    draws no glyph either, so neither can animate and neither may schedule a
    repaint on its own: the rail row carries its group's state, so a `working`
    rail would tick the clock for a pane whose only working row is the rail. Only
    a BOX counts (`_is_box`), which is exactly "a non-root worker".
    """
    return any(_is_box(row) and row.state == "working" for row in frame.rows)


def paint_needed(items: list[Line],
                 painted: Optional[list[Line]]) -> bool:
    """Whether these lines have to be written to the screen.

    Nothing is repainted unless the composed view actually changed: a new data
    poll that brought something new, a keypress that moved the caret, a resize,
    or a spinner frame while something is working. That is the difference between
    an `og agents` pane an operator leaves open all day and one that redraws
    itself forever — with nothing working there is no spinner to advance, the
    render is identical, this is False, and the loop blocks on the keyboard
    instead of repainting.

    The comparison is over `(spans, brightness)`, so a move of the SELECTION alone
    — which changes no text, only which line is bright — still counts as a change
    and repaints. A quiet pane re-composes to an equal item list, so it does not.
    """
    return painted is None or items != painted


def next_wait(now: float, poll_due: float, working: bool,
              tick: float = SPIN_TICK) -> float:
    """Seconds to block on the keyboard before anything could change on screen.

    TWO clocks, and the wait is whichever comes first. Nothing working: only the
    next data poll can change the view, so block until then — the pane wakes once
    per poll rather than eight times a second. Something working: the spinner
    advances on its own tick, so wake then, unless the poll comes first.
    """
    wake = min(now + tick, poll_due) if working else poll_due
    return max(0.0, wake - now)


# ---------------------------------------------------------------------------
# the painter: which rows get which line, and the one write itself
# ---------------------------------------------------------------------------

def paint_plan(items: list[Line], list_rows: int, offset: int,
               list_view: int, height: int) -> list[tuple[int, list[Span], str]]:
    """The `(row, spans, brightness)` triples a paint writes, top to bottom. Pure.

    The scrolling window covers the LIST only — `items[:list_rows]` — so it can
    never reach into the tail. The tail (blank, optional access panel, footer)
    is then pinned to the bottom in exactly one place. Windowing over ALL of
    `items` as the old loop did painted the tail twice on a short list, because
    a window taller than the list ran past `list_rows` into the very footer the
    second loop was about to draw at the bottom.
    """
    plan: list[tuple[int, list[Span], str]] = []
    for i, (spans, brightness) in enumerate(
            items[offset:min(offset + list_view, list_rows)]):
        plan.append((i, spans, brightness))
    tail_top = max(0, height - (len(items) - list_rows))
    for i, (spans, brightness) in enumerate(items[list_rows:]):
        plan.append((tail_top + i, spans, brightness))
    return plan


def _coalesce(spans: list[Span]) -> list[Span]:
    """Merge ADJACENT spans that share a colour role into one.

    A plain line is mostly PLAIN runs — the gutter, the box border, the padding —
    so coalescing makes a border row ONE write of the full width, exactly as the
    old whole-line writer did, rather than one tiny write per logical span.
    """
    out: list[Span] = []
    for text, role in spans:
        if out and out[-1][1] == role:
            out[-1] = (out[-1][0] + text, role)
        else:
            out.append((text, role))
    return out


def _clip_columns(text: str, width: int) -> str:
    """`text` cut to at most `width` DISPLAY columns (`_visible_width`), no ellipsis.

    The layout has already ellipsised; the painter only guarantees the write never
    runs past the pane. A wide glyph that would straddle the edge is dropped whole
    rather than half-written — the pessimistic rule, the same one `_display_width`
    keeps, so a CJK-locale glyph cannot overflow the pane.
    """
    if width <= 0:
        return ""
    if _visible_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        w = _visible_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _bare_role(role: str) -> str:
    """`role` with any `undim:` exemption prefix stripped — its colour role proper.

    The prefix is a modifier on the other axis, so colour and bold both have to
    look past it: `undim:mark:anthropic` is still anthropic's brand colour and
    still bold, and `undim:state:idle` is still the neon green of `✓`.
    """
    return role[len(UNDIM):] if role.startswith(UNDIM) else role


def _role_brightness(role: str) -> Optional[str]:
    """The BRIGHTNESS `role` paints at, or None to inherit its line's.

    Only the `undim:` prefix sets one, and it sets NORMAL. Colour and brightness
    stay independent exactly as they were: a vendor mark is still its brand
    colour merely dimmed, and an exempt span is at full brightness while keeping
    whichever colour role it carries.
    """
    return NORMAL if role.startswith(UNDIM) else None


def _role_colour(role: str, theme: Optional[Theme] = None) -> Optional[int]:
    """The ANSI-256 foreground a colour role names, or None for the default.

    With no `theme` this is the historical resolution and nothing else — which is
    what keeps every caller that has no active theme (and every existing test) on
    exactly today's colours. With one, the theme is asked instead:

        PLAIN   -> `text`, the theme's dominant body colour. This is the operator's
                   parchment: on dracula PLAIN is what paints agent names, titles
                   and box borders, so making PLAIN the theme's text is what makes
                   parchment DOMINANT rather than merely present.
        META    -> `meta`.
        state:* -> `idle` / `working`, the two states in `_STATE_COLOURS`; a state
                   with no role paints uncoloured, exactly as before.
        mark:*  -> the vendor's entry in `_VENDOR_COLOURS`, then the READABILITY
                   GUARD (below).
        ORCH    -> None, still: a bold weight and no colour, as it has always been.

    A theme whose role is `"default"` answers None, which is the point of having
    that value: under `og`, PLAIN resolves exactly as it did before themes
    existed and META still resolves to the same 109.
    """
    role = _bare_role(role)
    if role.startswith("mark:"):
        return _mark_colour(role[len("mark:"):], theme)
    name = _theme_role(role)
    if name is None:
        return None
    if theme is None:
        return META_COLOR if name == META else _STATE_COLOURS.get(name)
    return _theme_index(theme, name)


def _theme_role(role: str) -> Optional[str]:
    """The THEME role a colour role names, or None when it names no colour.

    ONE mapping, two conversions: `_role_colour` turns it into a 256 palette
    index (the pure, testable seam) and `_role_value` into whatever `init_pair`
    wants on the terminal in hand. Keeping them apart here is what stops the two
    from drifting into two different opinions about which role a span names.
    """
    bare = _bare_role(role)
    if bare.startswith("mark:"):
        return None                      # a mark is the table's, not a theme's
    if bare == ORCH:
        return None                      # a weight, and no colour, as always
    if bare == META:
        return META
    if bare.startswith("state:"):
        state = bare[len("state:"):]
        return state if state in _STATE_COLOURS else None
    return PLAIN_TEXT


def _role_value(role: str, theme: Optional[Theme], colors: int) -> Optional[int]:
    """`role` as the int `init_pair(n, fg, bg)` wants on a terminal with `colors`.

    THEME ROLES GO THROUGH THE CATALOG'S HEX, so a truecolor terminal takes the
    exact value the catalog names: dracula's parchment #ffd7af stays parchment
    instead of being quantised. Below truecolor the same hex goes through
    `hex_to_ansi256`, which will NOT let a colour with chroma land on a grey —
    the hex this replaced, #f0e6d2, resolved to cell 254, a pure grey, and took
    every chromatic theme role in the catalog down with it.

    MARK ROLES STAY 256-PALETTE INDICES even on a truecolor terminal, because
    `_VENDOR_COLOURS` IS a table of palette indices and the readability guard
    above it is reasoned in that same space — moving marks into 24-bit would be
    answering a question nobody asked and would put the guard's output through a
    second, different conversion.
    """
    bare = _bare_role(role)
    if bare.startswith("mark:"):
        return _mark_colour(bare[len("mark:"):], theme)
    name = _theme_role(role)
    if name is None:
        return None
    if theme is None:
        return META_COLOR if name == META else _STATE_COLOURS.get(name)
    return None if theme.is_default(name) \
        else curses_value(theme.colour(name), colors)


def _theme_index(theme: Theme, role: str) -> Optional[int]:
    """`role`'s palette index under `theme`, or None where it is `"default"`.

    None rather than `-1`, because `-1` is itself a value curses understands and
    this function's callers OR it into an attribute: handing back `-1` would look
    like a colour. `"default"` and "no colour" are the same thing to the painter.
    """
    return None if theme.is_default(role) else theme.index(role)


def _mark_colour(vendor: str, theme: Optional[Theme] = None) -> Optional[int]:
    """`vendor`'s brand index, walked in HUE only if the theme's pane would hide it.

    The table is never written to — see `_VENDOR_COLOURS`. `readable_index` returns
    the index unchanged whenever the mark reads against the active `bg`, which on
    dracula and on every other dark row means ALL FOURTEEN paint at exactly their
    table index. It is a no-op when `bg` is `"default"` too, because a background
    this process cannot see has no computable contrast and guessing would silently
    recolour the logos on the one theme that promises not to.

    Mark colours are the 256 palette's, never a 24-bit value: `_VENDOR_COLOURS`
    IS a table of palette indices, and a guard that walked true colour would be
    answering a different question from the one the table asks.
    """
    index = _VENDOR_COLOURS.get(vendor)
    if index is None or theme is None:
        return index
    return readable_index(index, theme.background())


def _role_background(role: str, theme: Optional[Theme] = None, *,
                     on_selected: bool = False) -> Optional[str]:
    """The theme hex a span of `role` paints its background in, or None.

    EVERY SPAN GETS THE PANE'S BACKGROUND under a theme, and that is the whole
    change: `init_pair(n, fg, -1)` put a coloured foreground on the terminal's own
    background, which is why a light terminal inverted the whole logo table. Now
    `bg` is the theme's own and the marks are judged against it.

    EXCEPT THE SELECTED ROW, which is the one place `sel_bg` is spent. Selection
    was a BRIGHTNESS axis (DIM vs NORMAL) and still is — `_role_bold` and the
    `undim:` exemption are untouched — so this only puts a background underneath
    the brightness of the one line already marked SELECTED. It is a FLAG rather
    than a role because SELECTED is hung as a LINE's brightness by `_blocks`, not
    as a span role, and the painter is what knows both. It is what a theme's
    `sel_bg` is for, and what the catalog's `text`/`sel_bg` >= 4.5 floor is
    actually testing. Under `og` both answers are None, which is why the golden
    render is unaffected.
    """
    if theme is None:
        return None
    key = "sel_bg" if on_selected else "bg"
    return None if theme.is_default(key) else theme.colour(key)


def _role_bold(role: str) -> bool:
    """Whether a colour role is BOLD.

    Bold is a WEIGHT, not a colour, so it applies whether or not the terminal can
    colour: the root's name (ORCH) and every harness mark are bold even on a
    monochrome terminal. The unknown-harness atom is PLAIN, so it is not. An
    `undim:` prefix is not a weight either, so it is stripped first.
    """
    role = _bare_role(role)
    return role == ORCH or role.startswith("mark:")


class _Colours:
    """Lazily allocates ONE curses colour pair per (foreground, background), cached.

    `init_pair(n, fg, bg)` is what makes a THEME a theme: with `bg` at `-1` the
    pair paints a coloured foreground on the terminal's OWN background, and the
    pane has no colour of its own — the behaviour `og` still reproduces and the
    golden test pins. With `bg` carrying the theme's `bg` the pane fills, and that
    is the decision the `_VENDOR_COLOURS` comment block deferred and this now
    makes.

    `use_default_colors()` is still called first, and is still what lets a
    `"default"` role inherit: pair 0 is the terminal's own colours, so a role that
    asks for `-1` on both sides spends no pair at all.

    Pairs are spent on FIRST USE and cached, so a terminal that never shows a
    vendor mark never spends a pair on it, and a colour is never re-initialised.
    The cache key is the PAIR, not the foreground: one fg against two backgrounds
    is two different paints and has to be two allocations.

    Degrades rather than crashes, in this order: no colour support (`has_colors()`
    false) -> every role paints uncoloured; a terminal with fewer colours than a
    role needs (the vendor brand colours and the status glyph's neon green and
    yellow want 256) -> that role paints uncoloured, the glyph falling back to the
    default foreground exactly as every other colour here does; a background or
    foreground out of range is the same case, and so is
    the pair table exhausted -> the roles that could not be allocated paint
    uncoloured. NEVER a wrong colour and never an exception: a terminal that
    cannot colour still has to show the pane, and a bare-but-correct pane beats a
    crash. A bold role still bolds (see `_role_bold`) — bold is not a colour, and
    neither is the `undim:` exemption, so it survives a terminal with no colour at
    all.
    """

    def __init__(self, curses: Any, theme: Optional[Theme] = None):
        self._curses = curses
        self.theme = theme
        self._pairs: dict = {}
        self._next = 1
        try:
            self._enabled = bool(curses.has_colors())
            self._colors = int(curses.COLORS)
        except Exception:
            self._enabled = False
            self._colors = 0

    def attr(self, fg: int, bg: int = CURSES_DEFAULT) -> int:
        """The curses attribute for foreground `fg` on background `bg`, or 0."""
        if not self._enabled:
            return 0
        key = (fg, bg)
        if key in self._pairs:
            return self._pairs[key]
        attr = 0
        try:
            # BOTH ends are range-checked against COLORS. That is the existing
            # degradation rule applied to the new half: a truecolor background on
            # a 256-colour terminal is as unallocatable as a 256 foreground on an
            # 8-colour one, and the honest answer is to paint the glyph bare.
            if (fg < self._colors and (bg < 0 or bg < self._colors)
                    and self._next < self._curses.COLOR_PAIRS):
                self._curses.init_pair(self._next, fg, bg)
                attr = self._curses.color_pair(self._next)
                self._next += 1
        except Exception:
            attr = 0
        # Cached either way: a pair that could not be allocated this once is not
        # retried on every paint, and never painted as a DIFFERENT colour.
        self._pairs[key] = attr
        return attr

    def background_attr(self) -> Optional[int]:
        """The attribute the WINDOW background is set to, or None to leave it alone.

        Set on `stdscr.bkgd` so an OPAQUE theme fills the whole pane rather than
        only the cells behind glyphs — without it the background shows in stripes,
        because every cell not covered by an `addnstr` keeps whatever was there.

        None when the theme's `bg` is `"default"`, and that is the whole of the
        OG theme's promise: leaving `bkgd` untouched is what lets the terminal's
        own background through, and setting it to a pair that happened to be
        allocated for some other role would put a colour on the pane nobody chose.
        """
        if self.theme is None or self.theme.is_default("bg"):
            return None
        # `attr(fg, bg)`, and the theme's background is the SECOND of the two.
        # Passing it first paints the pane's own colour as its text and leaves
        # the background terminal-owned, which looks almost right and is not.
        return self.attr(CURSES_DEFAULT,
                         curses_value(self.theme.colour("bg"), self._colors))

    def role_attr(self, role: str, theme: Optional[Theme] = None, *,
                    on_selected: bool = False) -> int:
        """The pair attribute `role` paints at, or 0 when it carries no colour."""
        active = self.theme if theme is None else theme
        fg = _role_value(role, active, self._colors)
        if fg is None:
            return 0
        background = _role_background(role, active, on_selected=on_selected)
        bg = CURSES_DEFAULT if background is None \
            else curses_value(background, self._colors)
        return self.attr(fg, bg)


def paint_line(stdscr, row: int, spans: list[Span], width: int, height: int,
               attr_for) -> None:
    """One `addnstr` PER SPAN, each at its cumulative column, tolerant of the last
    cell.

    A box closes its border in the pane's rightmost column, so a line has to be
    written out to `width` or every box renders open on the right. The one cell
    curses refuses is the window's bottom-right — the last line's last column — so
    the exception is swallowed there, per write, instead of narrowing every other
    line to dodge it.

    Spans are clipped by COLUMN as they are written (`_visible_width`, the layout's
    ruler): a span that would start at or past `width` is skipped entirely, so
    nothing written ever exceeds `width` at ANY width >= 1 — the pane's standing
    invariant, enforced here as a backstop the layout already satisfies. Adjacent
    spans with the same role are coalesced first (see `_coalesce`).
    """
    import curses

    if not 0 <= row < height or width < 1:
        return
    col = 0
    for text, role in _coalesce(spans):
        if col >= width:
            break
        chunk = _clip_columns(text, width - col)
        if chunk:
            try:
                stdscr.addnstr(row, col, chunk, width - col, attr_for(role))
            except curses.error:
                pass
        col += _visible_width(chunk)


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
      2. once per root the cwd rule puts to the question, walking every root
         newest-first and stopping at the first one whose `workspace` is `$PWD`
         (see `_match_cwd_root`). That stop IS the "most recent match" — the walk
         is ordered by recency — so the rule costs the roots more recent than the
         answer, and none of the ones after it. Also once per root, because the
         result is cached under the same session id.
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

    def _match_cwd_root(self, by_id: dict, asked: set[str]) -> Optional[str]:
        """The MOST RECENTLY ACTIVE root whose `workspace` is `$PWD`, or None.

        Asked of EVERY root, live or not, in ONE pass newest-first, stopping at
        the first match — so the stop IS the recency rule rather than something
        that has to be checked for afterwards.

        The failure this replaces: there were two passes, and pass 1 walked the
        LIVE roots and returned on the first hit. In a repo with a history of
        sessions that made the implemented rule "the first LIVE root in this
        directory", and the live root there is the STALE one — it stays live
        forever precisely because it still has workers (see `_is_live_root`),
        while the session the operator is actually standing in is a brand-new
        idle one that nothing dispatched from yet. Measured on a fresh `og chat`
        in this repo: ~16 finished workers, done 22-41h earlier, belonging to the
        previous root session in the same directory. A stale sibling with workers
        beat the live session with none, every time, and the pass that would have
        found the right one never ran.

        So liveness is not a filter here at all. `workspace` is a DETAIL-row
        field (see og_herdr_watch's measured key list), and the budget is kept by
        the two things that were always keeping it: every root is fetched at most
        ONCE per process (`_root_detail`, bounded by MAX_ROOT_DETAILS — the same
        dict, the same eviction, so the walk is still capped), and the walk stops
        at the first match. A root already fetched this refresh costs nothing,
        which is every live root and every root behind an earlier match.

        `asked` is how "asked, and therefore worth keeping in the cache" is
        recorded, so the prune in `refresh` cannot turn this into a per-poll
        refetch.
        """
        target = _norm_path(self.cwd)
        newest_first = sorted(
            (sid for sid, session in by_id.items() if _is_root(session)),
            key=lambda sid: -_recency(by_id[sid]),
        )
        for sid in newest_first:
            if sid not in asked:
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

        # The wide view is every LIVE root; `select_scope` narrows it to `$PWD`
        # and forces the wide view under `--all`. The narrowing only ever looks
        # WITHIN the live roots, which is what made it unsafe to treat as final:
        # a stale sibling root in the same repo stays live precisely because it
        # still has workers (`_is_live_root`), so it answers `$PWD` on its own and
        # the probe below — the only thing that can find the session the operator
        # is actually standing in — gets skipped as unnecessary.
        scope = select_scope(
            [by_id[sid] for sid in live_root_ids if sid in by_id],
            self.cwd,
            self.all_roots,
        )
        # So while the cwd rule is in force it is asked unconditionally, and its
        # answer REPLACES the narrowing rather than only applying when that
        # narrowing found nothing. `select_scope` keeps its own contract — it
        # reports "nothing matched" by handing back exactly what it was given —
        # because it is still what supplies the wide fallback below and the
        # `--all` path, both of which are decisions about LIVENESS.
        if self.cwd and not self.all_roots:
            match = self._match_cwd_root(by_id, asked)
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
        for sid, _depth in order:
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

        What this heuristic MUST NOT do is decide that question on its own. A
        stale root is live for as long as its workers are on screen, so in a repo
        with a history of sessions "the live root in this directory" is usually
        the PREVIOUS session, not the one the operator is in. That is exactly how
        a fresh `og chat` came to show ~16 workers finished 22-41h earlier.
        Liveness is what gets shown when nothing else decides it, nothing more.
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
# Enter on a row that resolves to no herdr pane at all — a worker whose listing
# carried no root, or a malformed row. Deliberately NOT sent as a target: an id
# known to match no record would only draw herdr's own `agent_not_found` back.
NOTICE_FOCUS_NO_ROOT = "focus: no root session to focus"


def focus_target(row: Row) -> Optional[str]:
    """The session id herdr can resolve for `row`: a root is its own, a worker's is its root.

    WHY A WORKER TARGETS ITS ROOT. herdr matches an `agent.focus` target against
    its PANE RECORDS, and it opens a pane — writing a record that carries
    `agent_session_id` — only for a ROOT session (see `og_herdr.py`). A sub-agent
    never gets a pane of its own, so its own id matches no record and herdr answers
    `agent_not_found`: on every worker, every time, which is the failure this
    resolves. The worker's work IS visible, though — in its ROOT's pane — so the
    root is the one target herdr can match and the one Enter should send. A root
    row owns its own pane and so targets itself.

    None when the row carries no resolvable root (a worker built without one, a
    malformed row). The caller declines with `NOTICE_FOCUS_NO_ROOT` rather than
    sending an id it knows will fail.
    """
    if row.is_root:
        return row.session_id or None
    return row.root_session_id or None


def focus_in_herdr(session_id: str,
                   environ: Optional[dict] = None) -> Optional[str]:
    """Jump the operator's herdr UI to `session_id`. None on success, else why not.

    `session_id` is the id the CALLER has already resolved to something herdr can
    match — a root's own id, or a sub-agent's ROOT (see `focus_target`). It is not
    necessarily the selected row's own session: only a root has a pane record, so
    a sub-agent's own id would draw `agent_not_found`. This function sends whatever
    it is given, unchanged; the row-to-root resolution belongs to the caller that
    holds the rows.

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
        # The probe is INSIDE the guard, not after it. This function promises
        # "NEVER raises", and `is_inside` reads the environment for a live pane an
        # operator leaves open all day: an unexpected error in the probe must
        # become a notice like every other failure, not a traceback that kills the
        # TUI loop on a keypress.
        inside = is_inside(env)
    except Exception:
        return NOTICE_FOCUS_UNAVAILABLE
    if not inside:
        return NOTICE_FOCUS_OUTSIDE_HERDR
    try:
        from og_herdr_client import HerdrClient
    except Exception:
        return NOTICE_FOCUS_UNAVAILABLE
    try:
        with HerdrClient() as client:
            client.call("agent.focus", {"target": session_id})
    except Exception as exc:
        # The notice is FORMED inside the handler, not after it: "NEVER raises"
        # is the contract, and a failure whose own message cannot be rendered
        # (`str(exc)` raising, say) must still become a notice rather than a
        # traceback that kills the pane. So even the formatting is guarded.
        try:
            return NOTICE_FOCUS_FAILED.format(exc)
        except Exception:
            return NOTICE_FOCUS_FAILED
    return None


def _run_tui(model: AgentModel, interval: float, server: str,
             start_access: bool = False,
             theme: Optional[Theme] = None) -> int:
    """The live view. curses paints exactly what `render` returns; keys drive
    the model.

    Keys: `q` quits, `r` forces a refresh, the arrows (or `j`/`k`) move the
    selection, Enter FOCUSES the selected row in herdr (jumping the operator's
    herdr UI to the pane it belongs to — a worker's ROOT, see `focus_target` and
    `focus_in_herdr`), Space expands or collapses
    the selected row's snippet, `a` toggles the access panel, and `t` opens the
    theme picker. It sends
    nothing to a session and changes no session state: `agent.focus` is the ONE
    outbound call it makes, and it moves the operator's own terminal alone.

    `theme` None means "whatever is configured" (`load_theme`: `$OG_AGENTS_THEME`,
    then the stored slug, then dracula). It is a parameter rather than only a
    lookup so a caller — `main`, or a test that wants to paint the `og` theme's
    exact colours — can name one, and so that the choice `--theme` made once is
    the same object the picker then replaces.

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

    The one exception to "nothing is painted unless the lines change" is a THEME
    CHANGE, which repaints with identical text on purpose: the composer is pure
    and knows nothing about colour, so the lines really are unchanged and
    `paint_needed` would correctly report False. `painted` is reset instead, and
    that is the whole of what makes applying a theme LIVE.
    """
    import curses

    def loop(stdscr) -> None:
        # The pane's live theme. It starts as whatever the caller resolved and is
        # REPLACED by the picker, so it is a local rather than the parameter: the
        # parameter is the starting choice, not the constraint.
        active_theme = theme_for() if theme is None else theme
        curses.curs_set(0)
        # Take the terminal's own colours for pair 0. `curses.wrapper` calls
        # `start_color()` before this point, and once colour is started pair 0
        # is the curses implementation's default white-on-black rather than the
        # terminal's: every cell the view touches — including the spaces
        # `stdscr.erase()` writes — then paints an opaque black background over
        # the operator's theme and transparency. With the default taken here the
        # pane has no background of its own and matches every other native CLI in
        # the window. The call raises `curses.error` on a terminal that cannot do
        # it; the view still has to paint there, so it runs uncoloured rather
        # than dying at startup.
        #
        # COLOUR AND TRANSPARENCY COEXIST BECAUSE OF THIS CALL — but only under
        # the `og` theme, or any theme whose `bg` is `"default"`. Every pair this
        # view spends is allocated with a `-1` BACKGROUND when the theme asks for
        # that (see `_Colours`), which paints the glyph on whatever pair 0 now is,
        # i.e. the terminal's own background. An OPAQUE theme inverts that: it
        # allocates the same pairs against its own `bg` and sets `bkgd` below, so
        # the pane fills instead of tinting. Both are available to the operator and
        # neither is a special case in the painter.
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        # The colour pairs, allocated lazily on first use and cached. Guarded by
        # `has_colors()` inside, so no-colour terminals fall back to no colour
        # and the pane renders exactly as it did before colour existed.
        colours = _Colours(curses, active_theme)
        # FILL THE PANE, not just the glyphs. Every cell no `addnstr` covers keeps
        # whatever `bkgd` says, so without this an opaque theme renders as
        # stripes: coloured text floating on the terminal's own background.
        # Skipped when the theme's `bg` is `"default"`, which is the whole of what
        # `og` promises.
        bkgd = colours.background_attr()
        if bkgd is not None:
            try:
                stdscr.bkgd = bkgd
            except Exception:
                pass
        stdscr.keypad(True)
        selected = 0
        offset = 0
        expanded: set[str] = set()
        # The one-line reason the last Enter could not focus, shown in the pane
        # through `Frame.notice` — on the FOOTER row, replacing it while it is up
        # (`_pane`). It is cleared on the NEXT keypress, so it sits for as long as
        # the operator is reading it and goes once they act.
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
        # The spinner counter and its clock. `spin` is turned into a glyph by
        # render for a WORKING row only; `next_spin` is when the NEXT frame is
        # due while something is working. With nothing working neither moves, so
        # the loop blocks on the keyboard until the next poll.
        spin = 0
        next_spin = 0.0
        next_poll = 0.0
        painted: Optional[list[Line]] = None

        def put(row: int, spans: list[Span], brightness: str) -> None:
            """One span-aware write per line (see `paint_line`).

            The attribute is composed from TWO ORTHOGONAL axes: the BRIGHTNESS —
            DIM for an unselected worker's box, NORMAL for the selected one, so
            the selection is brightness and the pane spends no A_REVERSE at all
            (SELECTED maps to A_NORMAL on purpose: it is a role the composer hangs
            on the selected box's first row so the scroll target survives, not a
            look of its own) — and each span's COLOUR ROLE, its brand colour
            and/or bold. A dim box still shows its mark's colour, merely dimmed.

            The line's brightness is the DEFAULT, not a per-line verdict: a span
            whose role carries `undim:` (the worker gutter and status glyph, which
            dimmed with the box until the operator reported that a pane could not
            be read for what was RUNNING) overrides it with NORMAL. Because that
            is a property of the ROLE and not of its position, colour and
            brightness stay independent — the exempt glyph paints its neon colour
            at full brightness on an unselected row, and a dim vendor mark keeps
            its brand colour.
            """
            attr_of = {NORMAL: curses.A_NORMAL, SELECTED: curses.A_NORMAL,
                       DIM: curses.A_DIM}

            def attr_for(role: str) -> int:
                own = _role_brightness(role) or brightness
                attr = attr_of[own]
                attr |= colours.role_attr(role, active_theme, on_selected=own == SELECTED)
                if _role_bold(role):
                    attr |= curses.A_BOLD
                return attr

            paint_line(stdscr, row, spans, width, height, attr_for)

        def _choose_theme(screen, curses_mod, colours: _Colours,
                          current: Theme, put) -> Optional[Theme]:
            """Run the picker to a close, and return the applied theme or None.

            The ONE place curses is spoken to for the overlay, and deliberately
            thin: every decision the overlay makes — which rows fit, what a key
            does — lives in `picker_lines` and `picker_key`, which are pure and
            tested without a terminal. What is left here is the loop that draws
            them and the translation of the two curses cursor keys into the
            sentinels `picker_key` understands.

            THE PREVIEW IS LIVE, not on Enter. Moving the caret repaints the pane
            underneath the overlay in that theme, because a theme you have to
            commit to in order to look at is a theme nobody changes, and the whole
            cost of the live repaint (`painted = None` back in the caller) is
            paid on close either way.
            """
            state = _picker(active=current.slug)
            preview = current
            while not state.closed:
                lines = picker_lines(state, width, height)
                first = max(0, min((height - len(lines)) // 2,
                                   height - len(lines)))
                screen.erase()
                # `first`, never `offset`: `offset` is the PANE's scroll offset in
                # the enclosing loop, and shadowing it here would repaint the
                # tree behind the overlay at the overlay's own row number.
                for row, (spans, brightness) in enumerate(lines):
                    put(first + row, spans, brightness)
                screen.refresh()
                # BLOCKING, like every other key in this view: the overlay owns the
                # keyboard until it closes, so no key reaches the pane behind it.
                screen.timeout(-1)
                key = screen.getch()
                if key == -1:
                    # ERR from `getch` -- a curses error, not a keypress. Closing
                    # is the only safe answer: a dialog that spins forever because
                    # the keyboard is broken is worse than one that gives up and
                    # leaves the pane as it was.
                    break
                state = picker_key(state, _picker_keycode(curses_mod, key))
                if state.current() != state.active:
                    chosen = _theme_by_slug(state.current())
                    if chosen is not None:
                        preview = chosen
                        colours.theme = chosen
                        # Repaint the PANE under the overlay in the previewed
                        # theme. `painted` is deliberately not consulted here:
                        # the overlay is a modal layer, not a mode, and the pane
                        # behind it must show the candidate.
                        stdscr.erase()
                        for row, spans, brightness in paint_plan(
                                items, pane.list_rows, offset, list_view, height):
                            put(row, spans, brightness)
            if state.applied is None:
                # A CANCEL puts the previous theme back, including the pairs
                # already spent on it — `painted` was never reset, so nothing
                # about the pane changes.
                colours.theme = current
                return None
            colours.theme = preview
            return preview

        def _theme_by_slug(slug: str) -> Optional[Theme]:
            """The catalog row for `slug`, or None if the catalog changed under us."""
            for row in ordered_themes():
                if row.slug == slug:
                    return row
            return None

        while True:
            height, width = stdscr.getmaxyx()
            now = time.monotonic()

            if poll or now >= next_poll:
                # The SESSION under the caret, read BEFORE the refresh: the rows
                # about to be replaced are the only place the caret's session id
                # exists, and the id is what survives the reorder a state change
                # causes. A row that is not a box (a rail carries "") reads as no
                # id at all, and `snap_selection` falls back to the old index.
                ids = [row.session_id for row in frame.rows]
                sel_id = ids[selected] if 0 <= selected < len(ids) else None
                frame = model.refresh(selected_id=sel_id,
                                      expanded_ids=expanded, force=force)
                selected = snap_selection(frame.rows, sel_id, selected)
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
            # The spinner tick: advance the counter only while something is
            # working, and only once the tick is due. `frame.spin` was set from
            # the PREVIOUS value, so the frame just painted uses it and the
            # increment lands on the next paint — a frame per tick, no more.
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
            first_selected = next((i for i, (_spans, brightness) in enumerate(items)
                                   if brightness == SELECTED), None)
            if first_selected is not None:
                if first_selected < offset:
                    offset = first_selected
                elif first_selected >= offset + list_view:
                    offset = first_selected - list_view + 1
            offset = max(0, min(offset, max(0, pane.list_rows - list_view)))

            if paint_needed(items, painted):
                painted = items
                stdscr.erase()
                for row, spans, brightness in paint_plan(items, pane.list_rows,
                                                         offset, list_view, height):
                    put(row, spans, brightness)
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
            elif key == ord("t"):
                # The theme picker. It draws over the pane and drives its own keys,
                # so it is entered and left in one blocking call; on the way back
                # the pane's own state (selection, scroll, expanded, access) is
                # untouched, which is what "cancel puts back exactly what was
                # there" means here.
                chosen = _choose_theme(stdscr, curses, colours, active_theme,
                                       put)
                if chosen is not None:
                    active_theme = chosen
                    colours.theme = chosen
                    # THE LIVE REPINT. The lines are byte-identical under any
                    # theme (the composer is pure and never sees colour), so
                    # `paint_needed` would correctly say "nothing changed" and the
                    # operator would have chosen a theme and seen no evidence.
                    # Forcing `painted = None` is the whole fix.
                    painted = None
                    save_theme(chosen.slug)
                continue
            elif key in (curses.KEY_UP, ord("k")):
                selected = move_selection(frame.rows, selected, -1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = move_selection(frame.rows, selected, 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                # Enter FOCUSES the selected row in herdr — it jumps the
                # operator's own terminal to the pane that row belongs to. The
                # TARGET is resolved first (`focus_target`): a worker sends its
                # ROOT's id, because a worker's OWN id matches no herdr pane (only
                # a root gets a pane record), and a root sends its own. It
                # deliberately does NOT set `poll`: focus changes nothing this view
                # draws, so a refetch would be a listing walk for nothing. A failed
                # focus, or a row that resolves to no pane at all, sets a notice,
                # and the next paint shows it. See `focus_in_herdr` for every way
                # it can decline.
                if 0 <= selected < len(frame.rows):
                    target = focus_target(frame.rows[selected])
                    notice = focus_in_herdr(target) if target is not None \
                        else NOTICE_FOCUS_NO_ROOT
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
    parser.add_argument("--theme", metavar="SLUG", default=None,
                        help="paint this theme for THIS RUN and do not save it; "
                             "overrides $OG_AGENTS_THEME and the stored choice")
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
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    # `--theme` REFUSES BY NAME rather than falling back to dracula, which is what
    # `theme_for` does for a stored slug. The difference is who typed it: a stored
    # slug can be stale because someone edited `themes.json`, and falling back is
    # right; a slug on the command line was typed a moment ago, and quietly
    # painting something else is how a flag gets believed and stops being read.
    # A `--theme` that IS known also beats `$OG_AGENTS_THEME`: both are explicit,
    # and the one on the command line was typed more recently.
    catalog = load_catalog()
    theme = theme_for() if args.theme is None else catalog.get(args.theme)
    if theme is None:
        sys.stderr.write(
            f"og agents: unknown theme {args.theme!r}. "
            f"Valid: {', '.join(valid_slugs(catalog))}\n")
        return 2

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
        # No animation headless: one frame, at spinner frame 0, with the
        # selection where the live view opens it — on the first worker. A working
        # row still draws its spinner, frozen on the first frame.
        frame.spin = 0
        # No previous frame, so no session to follow: this snaps to the first
        # worker, which is where the live view opens too.
        frame.selected = snap_selection(frame.rows, None, frame.selected)
        # `--once` PRINTS TEXT ONLY, and selection is now a paint attribute (DIM
        # vs NORMAL), never a glyph — so a one-shot dump shows NO selection at
        # all. That is acceptable for a dump that is read once and not navigated,
        # but it is a real difference from the live view: do not read a `--once`
        # frame and expect to see which worker was selected.
        # The real pane width drives the QR fit; off a tty there is none, so
        # fall back to the classic 80 columns.
        width = shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns
        for line in render(frame, width=width):
            print(line)
        return 0

    try:
        return _run_tui(model, args.interval, args.server, args.access, theme)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())