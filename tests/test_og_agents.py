"""Unit tests for installer/og_agents.py.

No network, ever: every test injects a stub opener (see `RoutingOpener`), so
nothing here can touch the operator's live server on 127.0.0.1:6767. The module
under test is read-only by contract, and `test_source_only_makes_read_only_calls`
pins that with an AST scan the same way tests/test_og_herdr.py pins "the bridge
never resolves an elicitation".
"""
from __future__ import annotations

import ast
import io
import itertools
import json
import math
import os
import sys
import types
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import og_agents as a
import og_herdr as h
import og_herdr_launch
import og_herdr_watch as w
import og_themes as th


# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    """Minimal stand-in for an http.client.HTTPResponse: read(n) + context mgr."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class RoutingOpener:
    """A stub that answers by URL path, and records every request.

    `listing` is a queue of bodies for `/v1/sessions` (the last one repeats, so a
    stable listing can be polled any number of times); `details` maps a session
    id to a body, or to an exception to raise for that id alone. An unmapped
    detail id raises rather than falling back to the listing body, so a fetch
    that was never expected fails loudly instead of looking like a session with
    no report.
    """

    def __init__(self, listing, details=None):
        self.listing = list(listing)
        self.details = dict(details or {})
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        path = urllib.parse.urlsplit(request.full_url).path
        if path == "/v1/sessions":
            body = self.listing.pop(0) if len(self.listing) > 1 else self.listing[0]
        else:
            assert path.startswith("/v1/sessions/"), path
            sid = urllib.parse.unquote(path[len("/v1/sessions/"):])
            assert sid in self.details, f"unexpected detail lookup for {sid!r}"
            body = self.details[sid]
        if isinstance(body, BaseException):
            raise body
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return FakeResponse(body)

    @property
    def listing_requests(self):
        return [r for r in self.requests
                if urllib.parse.urlsplit(r.full_url).path == "/v1/sessions"]

    @property
    def detail_requests(self):
        return [r for r in self.requests
                if urllib.parse.urlsplit(r.full_url).path != "/v1/sessions"]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def listed(sid, parent=None, status="running", title=None, elicit=0, **extra):
    """A session row shaped like the live listing's."""
    row = {
        "id": sid,
        "parent_session_id": parent,
        "status": status,
        "title": title if title is not None else f"title-{sid}",
        "archived": False,
        "pending_elicitations_count": elicit,
    }
    row.update(extra)
    return row


# The verbatim `items` element from the task's measured data.
ASSISTANT_MESSAGE = {
    "id": "25f8b2d7c0a04d7f8c2e6f835a7e905a",
    "type": "message",
    "status": "completed",
    "response_id": "msg_123",
    "created_at": 1791608100,
    "data": {
        "role": "assistant",
        "content": [{"type": "output_text",
                     "text": "The work was already committed (`573414d`) "
                             "before the interruption…"}],
    },
}


def detail(sid, workspace=None, harness=None, items=None, parent=None, **extra):
    row = {
        "id": sid,
        "workspace": workspace,
        "harness": harness,
        "items": items if items is not None else [],
        "parent_session_id": parent,
    }
    row.update(extra)
    return row


# The clock every fixture is measured against. A duration asserted against the
# real clock is a flake waiting for a slow machine, so `build_rows` is always
# given one.
NOW = 1_700_000_000.0


def worker(sid, state, *, text="t", agent=None, meta="", harness=""):
    """A worker Row assembled directly, for the renderer tests.

    Deliberately NOT built through `build_rows`: these tests are about the box
    around a row, not about the title parse or the elapsed figure, so the row is
    filled in by hand. That also keeps each of them failing for its OWN reason
    when run against a build that lacks one piece — a glyph test that failed
    because the title parser was missing would be passing for the wrong reason.

    `harness` is a HARNESS ID (the mark's input); `meta`/`agent` are the second
    line.
    """
    return a.Row(session_id=sid, text=text, state=state, depth=1,
                 agent=agent, meta=meta, harness=harness)


def header(text="conversation", harness="claude-native", *, sid="root",
           state="working"):
    """A ROOT row: an outdented rounded box, name left and harness label right."""
    return a.Row(sid, text, state, 0, is_root=True, harness=harness)


def rail(state="working"):
    """A breathing rail row, as `build_rows` emits it: the rail alone, opening a
    state group. It carries the group's state but draws no word — the worded group
    label is gone from the design."""
    return a.Row("", "", state, 1, rail=True)


def _join(spans):
    """A span list back to its composed text — the pane's own join."""
    return "".join(text for text, _role in spans)


def _line_text(item):
    """A `(spans, brightness)` pane item back to its composed text."""
    return _join(item[0])


def _fmts(lines):
    """Join each span list of a `worker_box`/`_root_lines` result to text."""
    return [_join(spans) for spans in lines]


def _footer_text(width):
    """The footer row exactly as `render` composes it: the form `_footer` chooses
    for `width`, laid out in spans and padded out to `width` columns.

    The footer is no longer one clipped string — it is a key span, a label span and
    a `∙` between each entry, all padded with the background strip out to the pane
    edge — so a test that wants the painted ROW compares against this. `a.FOOTER`
    and friends remain the form's TEXT, unpadded, and are still the right thing to
    compare when the FORM itself is under test.
    """
    return _join(a._footer_spans(width))


# The worded group labels the C2 design used to draw, one per group. The pane
# dropped them — the animated glyph inside each box carries the state now — so
# every render test asserts none of these lines survives.
OLD_GROUP_LABELS = ("│ working", "│ blocked - waiting on you", "│ failed",
                    "│ unknown", "│ finished")


# ---------------------------------------------------------------------------
# 1. the renderer, as a pure function
# ---------------------------------------------------------------------------

def test_renderer_shows_a_root_box_not_a_bare_line():
    """The operator's complaint ("the root is not boxed") is what this fixes:
    the root is now a rounded box drawn at column 0 — name flush left, harness
    label flush right on its one content row — not the old one-line header, and
    no longer a heavy `━` bar over a `█/░` gauge."""
    root = dict(listed("root", title="Omnigent Herdr Integration", status="running"),
                harness="claude-native")
    sessions = [
        root,
        listed("w1", parent="root", status="running", title="coder_zen:space-per-root"),
        listed("w2", parent="root", status="idle", title="fix-stale-comments"),
        listed("w3", parent="root", status="running", elicit=1,
               title="waiting-on-you"),
    ]

    rows = a.build_rows(sessions, ["root"], now=NOW)
    lines = a.render(a.Frame(rows=rows))

    # A rounded box, OUTDENTED at column 0 (the workers sit at column GUTTER).
    assert lines[0].startswith("╭") and lines[0].endswith("╮"), lines[0]
    assert lines[0].index("╭") == 0, "the root box is at column 0"
    assert lines[1].startswith("│") and lines[1].endswith("│"), lines[1]
    assert "Omnigent Herdr Integration" in lines[1]
    assert lines[1].endswith("Claude Code│"), "the harness label is flush right"
    assert lines[2].startswith("╰") and lines[2].endswith("╯"), lines[2]
    # The gauge and the heavy bar are gone. The gauge's word `done` is no longer
    # a marker to forbid — an idle worker's meta is the bare word `done` now — so
    # the bar glyphs carry the guard.
    joined = "\n".join(lines)
    assert "━" not in joined and "█" not in joined


def test_workers_are_grouped_by_state_and_separated_by_breathing_rails():
    """Workers are grouped by state, each group opened by a breathing rail — the
    order on screen is the group order (working, blocked, failed, finished), not
    listing order. The worded labels are gone: the glyph inside each box says the
    state and the rail alone marks where one group ends and the next begins."""
    sessions = [
        dict(listed("root", title="conv", status="running"), harness="claude-native"),
        listed("w1", parent="root", status="idle", title="coder_zen:done-already"),
        listed("w2", parent="root", status="running", title="coder_claude:busy"),
        listed("w3", parent="root", status="failed", title="coder_freebuff:broke"),
    ]
    rows = a.build_rows(sessions, ["root"], now=NOW)

    # The rows themselves are in display order, which is what the caret indexes.
    boxes = [row.text for row in rows if a._is_box(row)]
    assert boxes == ["busy", "broke", "done-already"]

    lines = a.render(a.Frame(rows=rows))
    # No worded group label survives anywhere in the render.
    for word in OLD_GROUP_LABELS:
        assert word not in lines, word
    # A breathing rail row opens each of the three groups (rail, boxes, rail,
    # boxes, rail, boxes) — the rail alone, in column 0.
    assert sum(1 for line in lines if line == "│") == 3
    # The state is still told apart, by the glyph in each box's gutter: a working
    # spinner frame, and the static `×` and `✓`.
    joined = "\n".join(lines)
    assert a.SPIN_FRAMES[0] in joined
    assert "×" in joined and "✓" in joined


def test_renderer_flattens_a_nested_worker():
    """A grandchild is grouped by its OWN state like any other worker. Grouping
    by state and drawing a spine are mutually exclusive, so the tree is
    flattened."""
    root = dict(listed("root", status="running"), harness="claude-native")
    sessions = [
        root,
        listed("w1", parent="root", status="running", title="coder_zen:worker"),
        listed("g1", parent="w1", status="running", title="coder_zen:grandchild"),
    ]
    rows = a.build_rows(sessions, ["root"], now=NOW)
    lines = a.render(a.Frame(rows=rows))

    assert lines[0].startswith("╭"), "the root is a box at column 0"
    # The grandchild is a box of its own, in the same (working) group...
    assert any("grandchild" in line and "╭" not in line for line in lines)
    # Every WORKER box sits in the gutter: the rail at column 0, the rounded
    # corner at column 4, and the whole line as wide as the pane. (The root box is
    # the one `╭` at column 0.)
    tops = [line for line in lines if line.startswith("│") and "╭" in line]
    assert tops and all(line.index("╭") == a.GUTTER for line in tops)
    assert all(len(line) == a.DEFAULT_WIDTH for line in tops)


def test_renderer_notes_a_truncated_listing():
    frame = a.Frame(rows=[header()], truncated=True)
    assert a.render(frame)[0].startswith("… partial listing")


# ---------------------------------------------------------------------------
# 2. the C2 shape, verbatim
# ---------------------------------------------------------------------------

C2_WIDTH = 46


def test_c2_shape_is_verbatim_root_box_gutter_nodes_and_rounded_boxes():
    """The operator's iterated C2 template, line for line: the root as an
    OUTDENTED rounded box at column 0, then a breathing rail opening each state
    group, and a ROUNDED box per worker BEHIND a node+state gutter. Built through
    `build_rows` from a listing, so the names, agents and glyphs are the ones the
    model produces at width 46. The working box's glyph is the first spinner frame.

    A box is THREE rows: top border, ONE content row, bottom border. The content
    row is the operator's order — 2-column mark slot, title flush left — with the
    meta right-aligned on the SAME row. The AGENT NAME is gone (the mark already
    says it), so nothing on a row repeats the mark; and the STATE GLYPH has left
    the box for the gutter.
    """
    sessions = [
        dict(listed("root", title="Project Status Review", status="running"),
             harness="claude-native"),
        dict(listed("w1", parent="root", status="running",
                    title="coder_zen:coders", created_at=NOW - 134),
             harness="opencode-native"),
        dict(listed("w2", parent="root", status="running", elicit=1,
                    title="cmdcode:reviewers"),
             harness="acp:command-code"),
        dict(listed("w3", parent="root", status="failed",
                    title="coder_freebuff:scouts"),
             harness="acp:freebuff"),
        dict(listed("w4", parent="root", status="idle", title="cmdcode:docs",
                    updated_at=NOW - 360),
             harness="acp:command-code"),
    ]
    rows = a.build_rows(sessions, ["root"], now=NOW)

    lines = a.render(a.Frame(rows=rows, selected=2), width=C2_WIDTH)

    # NOTE the two spaces after 𝗙 (w3's z-ai mark — Freebuff, the TOOL) and after every other mark:
    # the slot pads them all to the same two columns, which is what starts every
    # title in the same column. One space would be the sheared-title bug this pins
    # against.
    assert lines == [
        "╭────────────────────────────────────────────╮",
        "│ ✳  Project Status Review        Claude Code│",
        "╰────────────────────────────────────────────╯",
        "│",
        "│   ╭────────────────────────────────────────╮",
        "⦿ ⠋ │ ▯  coders                         2m14s│",
        "│   ╰────────────────────────────────────────╯",
        "│",
        "│   ╭────────────────────────────────────────╮",
        "⦿ ⏸ │ ⌘  reviewers             waiting on you│",
        "│   ╰────────────────────────────────────────╯",
        "│",
        "│   ╭────────────────────────────────────────╮",
        "⦿ × │ 𝗙  scouts                        failed│",
        "│   ╰────────────────────────────────────────╯",
        "│",
        "│   ╭────────────────────────────────────────╮",
        "⦿ ✓ │ ⌘  docs                            done│",
        "│   ╰────────────────────────────────────────╯",
        "",
        # The 46-column footer: now that the separator is U+2219 (Narrow) and
        # COMPACT carries none at all, the compact form measures 30 and a
        # ~46-column pane gets it — the LABELLED form, as it should be (see
        # `test_the_footer_chooses_a_form_by_measured_width`). Whatever form it is,
        # the row is padded out to the pane with the strip.
        _footer_text(C2_WIDTH),
    ]
    # Every BOX row — the root's and each worker's — is exactly the pane width.
    box_rows = [line for line in lines
                if (line.startswith(("│", "⦿")) and line.endswith("│")
                    and len(line) > 1)]
    assert box_rows and all(len(line) == C2_WIDTH for line in box_rows)
    # FIVE boxes: the root plus FOUR workers, ONE content row each — five tops and
    # five bottoms, so each box is top border, content row, bottom border.
    assert len(box_rows) == 5, box_rows
    assert sum(1 for line in lines if "╭" in line) == 5
    assert sum(1 for line in lines if "╰" in line) == 5
    # No caret anywhere; the node `⦿` marks each of the FOUR workers at column 0,
    # and the root box starts at column 0 too.
    assert "▸" not in "\n".join(lines)
    assert lines[0].index("╭") == 0, "the root box is OUTDENTED at column 0"
    assert lines[4].startswith("│   ╭"), "a worker box starts after the gutter"
    assert lines[4].index("╭") == a.GUTTER
    nodes = [i for i, line in enumerate(lines) if line.startswith("⦿")]
    assert nodes == [5, 9, 13, 17], nodes
    # The state glyph rides the gutter's column 2, on the CONTENT row alone, and
    # is NOT inside the box: the spinner is w1's, and every non-content box row
    # carries a space there.
    assert lines[5][0] == "⦿" and lines[5][2] == a.SPIN_FRAMES[0]
    assert lines[9][2] == "⏸" and lines[13][2] == "×" and lines[17][2] == "✓"
    assert lines[6][:4] == "│   " and a.SPIN_FRAMES[0] not in lines[5][4:], \
        "the state glyph is in the gutter, never inside the box"
    # The AGENT NAME is dropped from every row: the mark carries it, so `zen`,
    # `cmdcode` and `freebuff` — the whole of the old second row — appear NOWHERE.
    for name in ("zen", "cmdcode", "freebuff"):
        assert name not in "\n".join(lines), name
    # The meta is right-aligned on the CONTENT row: each one ends at the box's
    # right edge, on the same line as the title.
    for row_index, meta in ((5, "2m14s"), (9, "waiting on you"),
                            (13, "failed"), (17, "done")):
        assert lines[row_index].endswith(meta + "│"), lines[row_index]


def test_the_root_box_has_no_gauge_and_shows_the_label_at_every_width():
    """The done-gauge is gone. The root box is a rounded box at column 0 whose one
    content row is the root's name flush left and its harness label flush right;
    there is no `█/░` bar and no `N/M done` count anywhere."""
    def root_lines(title="conversation"):
        return a.render(a.Frame(rows=[header(text=title)]), width=C2_WIDTH)

    lines = root_lines()
    joined = "\n".join(lines)
    assert "█" not in joined and "░" not in joined and "done" not in joined
    assert lines[0].startswith("╭") and lines[0].index("╭") == 0
    assert "conversation" in lines[1] and lines[1].endswith("Claude Code│")
    assert lines[2].startswith("╰")
    assert all(len(line) == C2_WIDTH for line in lines[:3])

    # A long name is ellipsised, the label keeps its columns, and the box still
    # fits the pane.
    long_name = "a root conversation with a very long name indeed"
    long_lines = root_lines(long_name)
    assert long_lines[1].endswith("Claude Code│")
    assert all(len(line) == C2_WIDTH for line in long_lines[:3])

    # Below a box there is no rectangle, so the root degrades to one clipped line.
    for width in range(1, a.BOX_MIN):
        narrow = a.render(a.Frame(rows=[header()]), width=width)
        assert all(len(line) <= width for line in narrow), (width, narrow)


def test_the_rail_runs_down_the_group_at_the_pane_left_column():
    """The rail is a column of its OWN at the pane's left edge — one unbroken `│`
    down the whole group, with a `⦿` NODE at each agent's box. On a worker box's
    borders and the breathing rows the column carries the rail; on the box's
    content row it carries the node."""
    rows = [header(),
            rail(),
            worker("w1", "working", text="one"),
            worker("w2", "working", text="two")]
    lines = a.render(a.Frame(rows=rows), width=C2_WIDTH)

    assert lines[0].startswith("╭"), "the root box is at column 0"
    assert lines[3] == "│", "the breathing row is the rail alone"
    # Every group row carries the rail in column 0, except the boxes' content
    # rows, which carry the node. The blank above the footer and the footer last.
    for index, line in enumerate(lines[3:-2], start=3):
        assert line[0] in (a.RAIL, a.NODE), (index, line)
    # w1's content row (5) and w2's (8) are the nodes.
    assert [i for i, line in enumerate(lines) if line.startswith(a.NODE)] == [5, 8]
    # The root's bottom-left corner is at column 0 and the rail below it is too,
    # so the rail continues out of the box's corner.
    assert lines[2].startswith("╰") and lines[3].startswith("│")
    # The worker corners sit AFTER the four-column gutter, at column 4.
    corners = [line for line in lines if line.startswith("│") and
               ("╭" in line or "╰" in line)]
    assert corners and all(
        line.index("╭" if "╭" in line else "╰") == a.GUTTER for line in corners)


def test_the_node_and_spinner_sit_in_the_gutter_not_in_the_box():
    """`⦿` is drawn in the gutter's NODE column (0) and the state glyph in column
    2, both on the box's CONTENT row alone; every other row of the box carries the
    rail and a space. The state glyph has LEFT the box — the content inside is just
    the mark and the title."""
    rows = [header(),
            rail(),
            worker("w1", "working", text="one"),
            worker("w2", "working", text="two")]
    lines = a.render(a.Frame(rows=rows, selected=2), width=C2_WIDTH)   # w1

    # w1's content row (5): node, space, spinner, space, then the box at column 4.
    assert lines[5][:4] == "⦿" + " " + a.SPIN_FRAMES[0] + " ", lines[5]
    assert "╭" not in lines[5], "the content row carries no corner"
    assert lines[5].index("│", 4) == a.GUTTER, "the box edge starts at column 4"
    # The border and bottom rows carry the rail and a blank glyph column.
    assert lines[4][:4] == "│   " and lines[6][:4] == "│   "
    # The UNSELECTED box's content row still carries the node and the spinner: the
    # node marks the AGENT, not the selection (selection is the paint role).
    assert lines[8][:4] == "⦿" + " " + a.SPIN_FRAMES[0] + " ", lines[8]
    # No caret glyph anywhere.
    assert "▸" not in "\n".join(lines)


def test_a_worker_box_is_three_rows():
    """The box collapsed from FOUR rows to THREE: top border, ONE content row,
    bottom border. The second content row held the agent name and is gone; the box
    carries the harness mark, the title and the meta on its single content row."""
    spans, selected_row = a.worker_box(
        worker("w", "working", text="task", agent="zen", meta="12s"),
        C2_WIDTH, True)
    assert len(spans) == 3, spans
    assert selected_row == 0, "the SELECTED role sits on the box's first row"
    plain = _fmts(spans)
    assert plain[0].startswith("│   ╭") and plain[2].startswith("│   ╰")
    assert all(len(line) == C2_WIDTH for line in plain)
    # The state glyph and the node are in the GUTTER, on the content row.
    assert plain[1].startswith("⦿ " + a.SPIN_FRAMES[0] + " │")
    # ONE content row carries both halves: the title flush left, the meta flush
    # right, on the SAME line.
    assert "task" in plain[1]
    assert plain[1].rstrip("│").endswith("12s")
    # The agent name appears NOWHERE in the box: the mark carries it.
    assert "zen" not in "\n".join(plain)


def test_the_title_yields_to_the_meta_on_the_shared_content_row():
    """TITLE and META share one line. The meta keeps its columns — it is the thing
    being waited on — and the TITLE is what gives way first, ELLIPSISED by `_clip`
    rather than silently blanked, so a tight row still reads the task (cut) and
    the meta (intact), and never exceeds the pane."""
    title = "fix: keep the state glyph at width one"
    row = worker("w", "blocked", text=title, agent="cmdcode",
                 meta="waiting on you")
    # Wide: the whole title is present, the meta flush right on the same line.
    wide = _fmts(a.worker_box(row, 78, False)[0])
    assert title in wide[1], wide[1]
    assert wide[1].rstrip("│").endswith("waiting on you")

    # Tight: the title is ellipsised and the meta is INTACT and flush right.
    tight = _fmts(a.worker_box(row, 34, False)[0])
    assert len(tight) == 3
    assert "…" in tight[1], tight[1]
    assert title not in tight[1], "the title gave way"
    assert tight[1].rstrip("│").endswith("waiting on you"), tight[1]
    assert len(tight[1]) == 34, "and the row still fits the pane"

    # A meta longer than the line can hold wins outright: the title is dropped and
    # only the meta (clipped) remains — the row still never exceeds the pane.
    over = _fmts(a.worker_box(worker("w", "blocked", text="task", agent="cmdcode",
                                     meta="waiting on you forever and ever"),
                              20, False)[0])
    assert len(over) == 3 and all(len(line) == 20 for line in over)
    assert "task" not in over[1]
    # The content row is the gutter (node, blank, spacer) + the box edge, then the
    # meta alone (clipped). The state glyph for a blocked box is the static `⏸`.
    assert over[1] == "⦿ ⏸ │waiting on yo…│", over[1]


def test_the_state_glyphs_are_static_and_working_is_the_spinner():
    # The operator's static glyphs, one per state the viewer draws — and NO
    # static glyph for `working`, which the spinner owns...
    assert a.STATE_GLYPH == {"blocked": "⏸", "failed": "×",
                             "unknown": "?", "idle": "✓"}
    for state, glyph in (("idle", "✓"), ("blocked", "⏸"), ("failed", "×"),
                         ("unknown", "?")):
        frame = a.Frame(rows=[header(), worker("w", state)])
        assert glyph in "\n".join(a.render(frame, width=48)), state
        # ...and a static glyph does not move as the spinner advances.
        assert a.state_glyph(state, 0) == a.state_glyph(state, 5) == glyph
    # The finished glyph is a CHECK, not a circle: `✓` reads as done. The failed
    # glyph is now the HEAVIER `×` MULTIPLICATION SIGN (U+00D7), replacing the
    # thinner `✗` BALLOT X the operator asked to make heavier — Latin-1, so every
    # monospace font has it; see STATE_GLYPH for the ruler argument.
    assert a.STATE_GLYPH["idle"] == "\u2713"
    assert a.STATE_GLYPH["failed"] == "\u00d7"
    assert "○" not in a.STATE_GLYPH.values()
    # `working` is the SPINNER: a different frame each tick.
    assert a.STATE_GLYPH.get("working") is None
    assert a.state_glyph("working", 0) == a.SPIN_FRAMES[0]
    assert a.state_glyph("working", 1) == a.SPIN_FRAMES[1]
    assert a.state_glyph("working", 0) != a.state_glyph("working", 1)


def test_the_failed_glyph_costs_the_same_columns_as_finished():
    """The FAILED mark must cost the SAME one layout column as the FINISHED `✓`.

    The gutter composes the state glyph with the len-based ruler (`_visible_width`
    / `_clip`), which charges an East Asian AMBIGUOUS glyph ONE column — so `×`
    (U+00D7, Ambiguous) and `✓` (U+2713, Narrow) cost the same and the column is
    neutral between them. This is asserted against THAT ruler and NOT
    `_display_width`: the display ruler charges Ambiguous two and would fail `×`
    for a swap nobody made (`_display_width` guards only the footer, never the
    gutter).

    `len` has a blind spot, though — it charges a Wide glyph ONE too — so the
    swap that must fail LOUDLY is a Wide/Fullwidth mark, which a terminal would
    paint two cells wide and which would shear every title downstream. The
    western paint rule `_slot_width` (Wide/Fullwidth two, Ambiguous one) closes
    that gap, and the East Asian Width guard names the class directly.
    """
    failed, finished = a.STATE_GLYPH["failed"], a.STATE_GLYPH["idle"]
    # Under the gutter's own ruler: one column each, and one glyph each.
    assert a._visible_width(failed) == a._visible_width(finished) == 1
    assert len(failed) == len(finished) == 1
    # A Wide/Fullwidth swap would slip past `len`; this is the guard that fails.
    assert a._slot_width(failed) == a._slot_width(finished) == 1
    assert unicodedata.east_asian_width(failed) not in ("W", "F")
    assert unicodedata.east_asian_width(finished) not in ("W", "F")
    # And the composed gutter cell — glyph plus the one space after it — is the
    # same width for both, which is what keeps the box edges aligned.
    assert a._visible_width(failed + " ") == a._visible_width(finished + " ") == 2


def test_only_the_working_glyph_moves_with_the_spin_counter():
    """The spinner animates the WORKING row alone: at two spin values the working
    box differs in exactly one line, while a blocked, failed and idle box are
    byte-identical between the same two values."""
    def render_at(state, spin):
        return a.render(a.Frame(rows=[header(), rail(state), worker("w", state)],
                                spin=spin), width=C2_WIDTH)

    working0, working1 = render_at("working", 0), render_at("working", 1)
    assert working0 != working1, "a working row must visibly animate"
    # Exactly ONE line differs — the working box's content row, whose gutter
    # carries the spinner — and the glyph that changed is the working one.
    changed = [(before, after) for before, after in zip(working0, working1)
               if before != after]
    assert len(changed) == 1, changed
    assert a.SPIN_FRAMES[0] in changed[0][0]
    assert a.SPIN_FRAMES[1] in changed[0][1]

    for state, glyph in (("blocked", "⏸"), ("failed", "×"), ("idle", "✓")):
        assert render_at(state, 0) == render_at(state, 1), state
        assert glyph in "\n".join(render_at(state, 0)), state


def test_a_quiet_pane_renders_the_same_lines_and_schedules_no_repaint():
    """An `og agents` pane with nothing changing must not repaint at all.

    The operator leaves these open in a pane for a whole day. With nothing working
    there is no spinner to advance, so the render is byte-identical between polls
    and the loop skips `erase`/`refresh` — and blocks on the keyboard until the
    next poll.
    """
    quiet = a.Frame(rows=[header(), rail("idle"),
                          worker("w1", "idle"), worker("w2", "failed")])

    first = a._pane(quiet, C2_WIDTH).items
    assert a.paint_needed(first, None) is True, "the first frame is painted"
    painted = first
    for _ in range(12):
        items = a._pane(quiet, C2_WIDTH).items
        assert items == painted, "nothing changed, so nothing moved"
        assert a.paint_needed(items, painted) is False

    # Nothing is working, so the wait is the whole poll interval...
    assert a.frame_working(quiet) is False
    assert a.next_wait(now=100.0, poll_due=103.0, working=False) == 3.0
    # ...and a working BOX wakes on the spinner tick instead.
    busy = a.Frame(rows=[header(), rail(), worker("w1", "working")])
    assert a.frame_working(busy) is True
    assert a.next_wait(now=100.0, poll_due=103.0, working=True) == \
        pytest.approx(a.SPIN_TICK)
    # A working ROOT or a working RAIL alone does not animate: only a box draws
    # the spinner, so only a box may schedule the next tick.
    assert a.frame_working(a.Frame(rows=[header(state="working"), rail(),
                                         worker("w1", "idle")])) is False
    assert a.frame_working(a.Frame(rows=[header(), rail("working")])) is False


def test_the_selection_walks_every_agent_row_in_the_order_they_are_displayed():
    """THE HAZARD: grouping by state reorders the rows, so the selection must walk
    them in the order the eye reads — not in tree order. `Frame.rows` is itself
    in display order, which is what makes `move_selection` correct unchanged.

    EVERY agent row is now in the ring, the ROOT included (it used to be stepped
    over): the operator asked to be able to select the orchestrator, so the walk
    starts on the root and steps down through its workers.

    The walk starts from an ADOPTED selection rather than an assumed one: the
    pane opens untouched, and the first movement key is what takes a row."""
    rows = [header(),
            rail("working"),
            worker("w1", "working", text="one"),
            rail("idle"),
            worker("w2", "idle", text="two"),
            worker("w3", "idle", text="three")]
    # THE VIEW OPENS UNTOUCHED. No session to follow and no caret to keep, so
    # nothing is selected and every box paints at the same brightness.
    assert a.snap_selection(rows) is None
    assert a.snap_selection(rows, None, None) is None
    # And the first movement key ADOPTS the first AGENT row in BOTH directions —
    # the root, at the top, now that roots are selectable — rather than being
    # swallowed and costing a second keypress.
    assert a.move_selection(rows, None, 1) == 0, "down adopts the first agent: the root"
    assert a.move_selection(rows, None, -1) == 0, "up adopts it too, not the last"
    frame = a.Frame(rows=rows, selected=a.move_selection(rows, None, 1))
    assert frame.selected == 0, "the root, the first agent row"
    # Every selectable row's text, by index — the root included (it is not an
    # `_is_box`: that predicate means "a box that animates", see `_selectable`).
    row_text = {i: row.text for i, row in enumerate(rows) if a._selectable(row)}

    seen = []
    for step in (1, 1, 1, -1, -1, -1):
        frame.selected = a.move_selection(rows, frame.selected, step)
        seen.append(row_text[frame.selected])
        items = a._pane(frame, 48).items
        # Exactly one row is the selected (bright) one.
        assert sum(1 for _t, role in items if role == a.SELECTED) == 1
    assert seen == ["one", "two", "three", "two", "one", "conversation"], \
        "display order (root first), not tree order"

    # No selectable rows at all: there is nothing to adopt and nowhere to move
    # to, so the state passes through unchanged in both directions rather than
    # crashing or inventing a selection.
    assert a.move_selection([rail()], None, -1) is None, "still untouched"
    assert a.move_selection([rail()], 0, 1) == 0, "the index is left alone"
    assert a.snap_selection([rail()], None, 3) == 3, "and so is snap's"
    # A single ROOT row is itself the whole ring, so both moves clamp onto it.
    assert a.move_selection([header()], 0, 1) == 0
    assert a.move_selection([header()], None, -1) == 0, "it adopts the root"
    assert a.snap_selection([header()], None, 3) == 0, "the only row to land on"



def test_an_untouched_pane_paints_every_box_identically_and_none_of_them_selected():
    """THE REPORTED BUG: a pane nobody has moved the caret in opened with one box
    undimmed and `sel_bg` behind it, and every OTHER box dimmed — a selection on
    a row the operator never chose.

    Before the fix the very first frame already had a SELECTED line (index 0 is
    the root, so the nearest worker won), which meant both a brightness lie and —
    once the theme engine wired `sel_bg` to the selected line — a coloured bar on
    an arbitrary row. `Frame()` with no `selected` is the untouched pane, and it
    has to carry no SELECTED line at all for `attr_for` to keep `on_selected`
    False everywhere.
    """
    rows = [header(),
            rail("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]

    pane = a._pane(a.Frame(rows=rows), 48)
    # The LIST only: it is the selection axis. The tail below it — the access
    # panel (pane-width, and NORMAL like the root box, since it is not an agent
    # the caret can be on) and the footer — carries no selection either way.
    items = pane.items[:pane.list_rows]

    assert not [role for _t, role in items if role == a.SELECTED], \
        "an untouched pane has no selected line to scroll to or to paint"
    assert not [role for _t, role in items if role == a.DIM], \
        "and dimming is what says 'not the one your cursor is on'"
    assert {role for _t, role in items} == {a.NORMAL}

    # ONE key, and the pane has a selection: adopted on the ROOT (the first agent
    # row now that roots are selectable), which reads SELECTED on its first row
    # while BOTH workers' boxes go DIM.
    moved = a.Frame(rows=rows, selected=a.move_selection(rows, None, 1))
    moved_items = a._pane(moved, 48).items
    assert sum(1 for _t, role in moved_items if role == a.SELECTED) == 1
    assert moved_items[0][1] == a.SELECTED, "the root box, first row"
    assert [role for _t, role in moved_items[4:7]] == [a.DIM] * 3, "w1's box"
    assert [role for _t, role in moved_items[7:10]] == [a.DIM] * 3, "w2's box"


def _unsprung(frame):
    """A painted frame with the spinner glyphs dropped, so two frames can be
    compared for everything except the one thing that is animating."""
    return [line for line in frame
            if not any(glyph in line for glyph in a.SPIN_FRAMES)]


def test_enter_on_an_untouched_pane_focuses_nothing_and_shows_no_notice(monkeypatch):
    """There is no target to focus, so Enter does NOTHING and says nothing.

    Not a notice: a notice would report a failure the operator did not cause —
    they pressed the key the footer tells them focuses the selected row, and the
    honest answer is that they have to select a row first, which is one arrow key
    away and visible the moment they press it. And never a focus: Enter moves the
    operator's OWN terminal, and moving it somewhere they never pointed is worse
    than not moving it at all.
    """
    monkeypatch.setenv("HERDR_ENV", "1")
    calls = _fake_focus_agent_terminal(monkeypatch)
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})

    screen = _run_tui_capture(monkeypatch, [ENTER, QUIT], model=_model(opener))

    assert calls == [], f"nothing was focused: {calls}"
    assert screen.painted, "the pane was painted"
    assert not any("focus:" in text for text in screen.painted)
    # Enter changed NOTHING — the frame after it is the frame before it, spinner
    # aside, so the key did not quietly adopt the first row either. The contrast
    # is the point: one DOWN first and the very same Enter focuses.
    assert _unsprung(screen.frames[-1]) == _unsprung(screen.frames[0])
    focused = _run_tui_capture(monkeypatch, [DOWN, ENTER, QUIT], model=_model(opener))
    assert calls == [("root", "claude-native", "")], calls


def test_the_selection_follows_its_session_across_a_state_change_reorder():
    """THE HAZARD THE GROUPING CREATES, and why the selection tracks an id.

    A selected worker that changes state moves from one group to another, so the
    rows reorder underneath it. Holding the INDEX across that refresh lands the
    selection on whichever worker now occupies it — here `w2`, selected at index 4
    in the idle group, becomes `working` and moves to index 2, while index 4 in the
    new rows is its sibling `w3`. The id is what survives the move.
    """
    before = [header(), rail("working"),
              worker("w1", "working", text="one"),
              rail("idle"),
              worker("w2", "idle", text="two"),
              worker("w3", "idle", text="three")]
    after = [header(), rail("working"),
             worker("w2", "working", text="two"),
             worker("w1", "working", text="one"),
             worker("w3", "working", text="three")]

    # w2 was selected at index 4 in `before`; index 4 in `after` is w3's box
    # instead, so holding the index lands on the wrong worker — and Enter would
    # focus w3's session.
    assert before[4].session_id == "w2"
    assert after[4].session_id == "w3" and a._is_box(after[4])
    assert a.snap_selection(after, "w2", 4) == 2, "the selection follows w2"

    # The session GONE (it fell off the listing or the scope moved) falls back to
    # the row NEAREST the old index — so the selection stays where the operator's
    # eye is, rather than teleporting to the top of the pane.
    assert a.snap_selection(after, "no-such-session", 4) == 4
    assert a.snap_selection(after, "no-such-session", 9) == 4, "clamped, not off the end"
    assert a.snap_selection(after, "no-such-session", 0) == 0, "the root, at the top"
    # A tie between the row above and the row below goes to the UPPER one: index 3
    # in `before` sits between w1 (2) and w2 (4).
    assert a.snap_selection(before, "no-such-session", 3) == 2
    # The ROOT's id captures the caret now that roots are selectable — index 0
    # here. A rail's empty string still never does: rails hold no agent.
    assert a.snap_selection(before, before[0].session_id, 0) == 0
    assert a.snap_selection(before, "", 3) == 2
    # The no-id first frame is UNTOUCHED, not open-on-the-first-row: nothing was
    # selected before the refresh, so there is nothing to snap onto and nothing to
    # snap FROM. Every box paints NORMAL and the pane opens with no bar behind any
    # of them — which is the whole point of the fix.
    assert a.snap_selection(after, None, None) is None
    assert a.snap_selection(after, None, 0) == 0, \
        "once a selection exists, an unreadable id still falls back to nearest"


def test_the_selected_box_is_normal_and_every_other_box_is_dim_but_its_gutter():
    """SELECTION IS BRIGHTNESS. The selected worker's box paints NORMAL — it reads
    white against the dimmed others — and every line of an UNSELECTED box paints
    DIM. SELECTED still rides on exactly ONE line (the selected box's first row),
    because `_run_tui` scans for it to find the block to scroll to.

    DIM IS THE BOX, NOT THE ROW: the reported fix. An operator could not tell
    which workers were running, because the unselected box's gutter — the rail,
    the node, the status glyph — dimmed with it. Those spans are `undim:` and so
    paint NORMAL regardless; the box's border and body still take the line's DIM.
    """
    rows = [header(),
            rail("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]
    frame = a.Frame(rows=rows, selected=2)   # w1

    items = a._pane(frame, 48).items

    # root box (0-2), breathing rail (3), w1's box (4-6), w2's box (7-9), tail.
    assert [role for _t, role in items[0:3]] == [a.NORMAL] * 3, "the root box"
    assert items[3] == ([("│", a.PLAIN)], a.NORMAL), "the breathing rail"
    assert [role for _t, role in items[4:7]] == [a.SELECTED, a.NORMAL, a.NORMAL]
    assert [role for _t, role in items[7:10]] == [a.DIM] * 3
    assert sum(1 for _t, role in items if role == a.SELECTED) == 1
    # Only the three roles survive: no TITLE/A_REVERSE any more.
    assert set(role for _t, role in items) <= {a.NORMAL, a.SELECTED, a.DIM}


def test_an_unselected_box_dims_its_border_and_body_but_not_its_gutter():
    """The regression this pins: an UNSELECTED worker's box dims its BORDER and
    BODY and leaves the GUTTER — the rail, the node, the status glyph — at full
    brightness, because that is what the operator reports being unable to read.

    The exemption is a property of the span's ROLE (`undim:`), not of where the
    span sits, so it is asserted by role here and the gutter is picked out by what
    it draws rather than by index. The SELECTED box is unchanged by the fix: its
    gutter carries the same exempt roles and NORMAL is what its line already
    painted at, so nothing on that row differs.
    """
    rows = [header(), rail("working"), worker("w1", "working", text="one",
                                              meta="2m14s")]

    def gutter_of(spans):
        """The spans that make up the GUTTER — the first `GUTTER` columns of the
        line — picked out by what the gutter IS, not by where its spans sit in the
        list."""
        out, used = [], 0
        for text, role in spans:
            if used >= a.GUTTER:
                break
            out.append((text, role))
            used += len(text)
        return out

    def is_box_span(role):
        return not role.startswith(a.UNDIM)

    unselected = a._pane(a.Frame(rows=rows, selected=1), 48).items
    content_spans, brightness = unselected[5]
    assert brightness == a.DIM, "the unselected box still dims"
    # The gutter is exempt: the NODE on the content row, and the WORKING spinner.
    gutter = gutter_of(content_spans)
    assert {text.strip() for text, _role in gutter} == {a.NODE, a.SPIN_FRAMES[0]}
    assert all(role.startswith(a.UNDIM) for _text, role in gutter), gutter
    # The BOX is what still dims: every span past the gutter — the box edge, the
    # mark, the title, the meta — carries a role that takes the line's brightness,
    # border and body alike.
    assert all(is_box_span(role)
               for _text, role in content_spans[len(gutter):]), content_spans
    assert any(text.startswith("╭") and is_box_span(role)
               for text, role in unselected[4][0]), "the border dims"
    assert any("2m14s" in text and is_box_span(role)
               for text, role in content_spans), "the meta dims"
    # EVERY row of the box is exempted in its gutter, not just the content row:
    # the top and bottom borders carry the rail at full brightness too.
    border_gutter = gutter_of(unselected[4][0])
    assert [text for text, _role in border_gutter] == [a.RAIL + " ", "  "], \
        border_gutter
    assert all(role.startswith(a.UNDIM)
               for item in unselected[4:7] for _text, role in gutter_of(item[0]))

    # The SELECTED box is UNCHANGED by the fix: the same spans with the same
    # exempt roles, on a line that already painted NORMAL, so nothing on that row
    # moves — it was bright before and it is bright now.
    selected = a._pane(a.Frame(rows=rows, selected=2), 48).items[5]
    assert selected[0] == content_spans, "the spans do not depend on the selection"
    assert selected[1] == a.NORMAL, "and its line was already at full brightness"


def test_selection_is_brightness_and_moves_with_the_selection():
    """The selection is the paint role, and it is really applied.

    The TEXT of the two frames is IDENTICAL whichever worker is selected — there is
    no caret glyph any more — so selection is exactly which box is NORMAL (bright)
    and which is DIM. Comparing the two selections proves nothing else marks a row.
    """
    rows = [header(),
            rail("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]

    on_w1 = a._pane(a.Frame(rows=rows, selected=2), 48).items   # w1
    on_w2 = a._pane(a.Frame(rows=rows, selected=3), 48).items   # w2

    # The composed text is identical; only the roles differ.
    assert [t for t, _r in on_w1] == [t for t, _r in on_w2]
    assert on_w1[4][1] == a.SELECTED and on_w2[7][1] == a.SELECTED
    assert all(r == a.DIM for _t, r in on_w1[7:10])
    assert all(r == a.DIM for _t, r in on_w2[4:7])


def test_a_selection_move_still_repaints_the_pane():
    """Moving the selection must still schedule a repaint.

    Selection is a paint role, so if it were not part of the composed items a move
    would change nothing and `paint_needed` would say False — the bright box would
    appear stuck until an unrelated repaint.
    """
    rows = [header(),
            rail("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]
    frame = a.Frame(rows=rows, selected=2)   # w1
    before = a._pane(frame, 48).items

    frame.selected = a.move_selection(rows, frame.selected, 1)       # w2
    after = a._pane(frame, 48).items

    assert after != before, "the selection moved, so the composed roles changed"
    assert a.paint_needed(after, before) is True
    # The bright box really did move to the other worker (w2's box is 7-9).
    assert before[4][1] == a.SELECTED and after[7][1] == a.SELECTED
    assert all(r == a.DIM for _t, r in after[4:7])


def test_every_harness_mark_fills_the_slot_and_every_title_starts_in_one_column():
    """The box's content row carries the harness mark, and every mark spends the
    SAME two display columns, so the glyph and the title line up down the list.

    EVERY MARK IS EAST ASIAN NARROW NOW. kilo, cognition and z-ai were once
    Ambiguous — a western terminal paints an Ambiguous glyph one cell wide but a
    CJK one paints it two — and the old slot once charged them two and left them
    unpadded, so their titles started one column LEFT of every other row's. That
    is the shear this pins shut: one title column for all fourteen vendors, in the
    locale the operator runs. The replacement glyphs are Narrow, so no row's width
    depends on the locale at all now — see `test_every_mark_and_the_node_are_narrow`.
    """
    harnesses = _harness_for_vendor()
    all_harnesses = [harnesses[vendor] for vendor in sorted(MARK)]
    assert len(all_harnesses) == 14, all_harnesses

    title_col = {}
    for harness in all_harnesses + ["totally-unknown", ""]:
        slot = a.harness_mark(harness)
        assert a._slot_width(slot) == 2, (harness, slot)
        line = a.render(a.Frame(rows=[worker("w", "working", text="ZX",
                                             harness=harness)]), 48)[1]
        assert "ZX" in line, (harness, line)
        title_col[harness] = line.index("ZX")

    assert len(set(title_col.values())) == 1, title_col
    # And every mark is padded the same way: glyph plus one space, never glyph
    # alone — the slot's western ruler charges every Narrow glyph one cell.
    for vendor, mark in MARK.items():
        assert a.harness_mark(harnesses[vendor]) == mark + " ", vendor


def test_a_breathing_rail_row_separates_one_state_group_from_the_next():
    """The worded group label is gone, but a rail-only row still separates one
    state group from the next and its boxes line up behind the same rail."""
    rows = [header(),
            rail("working"), worker("w1", "working", text="one"),
            rail("failed"), worker("w2", "failed", text="two")]
    lines = a.render(a.Frame(rows=rows), width=C2_WIDTH)
    # A rail-only row (just "│") opens EACH of the two groups, so the eye still
    # sees where one group ends — and no label word follows it.
    rails = [i for i, line in enumerate(lines) if line == "│"]
    assert len(rails) == 2, rails
    for word in OLD_GROUP_LABELS:
        assert word not in lines, word
    # Each group's box top follows its rail, at the same column.
    assert lines[rails[0] + 1][0] == "│"
    assert lines[rails[0] + 1].index("╭") == a.GUTTER
    assert lines[rails[1] + 1][0] == "│"
    assert lines[rails[1] + 1].index("╭") == a.GUTTER


def test_multiple_roots_each_get_their_own_box():
    sessions = [
        dict(listed("r1", title="first conv", status="running"),
             harness="claude-native"),
        listed("a1", parent="r1", status="idle", title="coder_zen:alpha"),
        dict(listed("r2", title="second conv", status="running"),
             harness="acp:cline"),
        listed("b1", parent="r2", status="running", title="coder_cline:beta"),
    ]
    rows = a.build_rows(sessions, ["r1", "r2"], now=NOW)
    lines = a.render(a.Frame(rows=rows), width=C2_WIDTH)
    joined = "\n".join(lines)

    # Each root gets its OWN box at column 0 (the workers sit at GUTTER).
    roots = [line for line in lines if line.startswith("╭")]
    assert len(roots) == 2, roots
    assert all(line.index("╭") == 0 for line in roots)
    assert any("first conv" in line and line.endswith("Claude Code│") for line in lines)
    assert any("second conv" in line and line.endswith("Cline│") for line in lines)
    # No gauge and no heavy bar anywhere. (`done` is a worker's meta now, so the
    # bar glyphs, not the word, mark the gauge's absence.)
    assert "━" not in joined


def test_height_trimming_keeps_a_whole_group_and_never_splits_a_box():
    """The trimmer works in whole blocks (a root, or a rail and its boxes), so a
    box can never be cut in half and a group's rail never leaves its boxes. With
    the worded labels gone there is no label left to orphan, which is why the
    block is now just the rail and the boxes. The root block is THREE rows and a
    worker box is THREE rows, so the block arithmetic below is a rail + three."""
    rows = [header(), rail("working"),
            worker("w1", "working", text="one"),
            rail("idle"),
            worker("w2", "idle", text="two")]
    full = a.render(a.Frame(rows=rows), width=C2_WIDTH)
    joined = "\n".join(full)
    # The root box plus the two worker boxes.
    assert joined.count("╭") == 3 and joined.count("╰") == 3

    # Room for the root block (3 rows) and the first group (rail + 3-row box = 4
    # rows) but not the second group: the second group is dropped WHOLE, so its box
    # is never cut and its rail is never left behind without it.
    short = a.render(a.Frame(rows=rows), width=C2_WIDTH, height=10)
    short_joined = "\n".join(short)
    assert "one" in short_joined
    assert "two" not in short_joined, "a box must not be cut in half"
    # The root box plus the first worker box, each whole.
    assert short_joined.count("╭") == short_joined.count("╰") == 2
    # And no label word is left behind anywhere.
    for word in OLD_GROUP_LABELS:
        assert word not in short_joined, word


def test_a_worker_row_never_exceeds_the_pane_at_any_width():
    """The renderer must never EMIT an over-wide line, at any width >= 1.

    A line that does not fit is WRAPPED by the terminal, not clipped, and a
    wrapped row in this layout is the failure the operator reports as "the pane
    is broken". The gutter is four columns and a box was forced to a minimum of
    four, so a worker row cost at least EIGHT columns whatever the pane: at
    widths below eight it overflowed by construction. It now degrades (the ladder
    in `worker_box`) and this is the invariant the ladder buys.
    """
    for width in range(1, 41):
        for state in ("working", "blocked", "failed", "idle"):
            for selected in (False, True):
                spans, selected_row = a.worker_box(
                    worker("w", state, text="a title long enough to clip",
                           agent="zen", meta="2m14s"),
                    width, selected)
                assert spans, (width, state)
                lines = _fmts(spans)
                assert all(len(line) <= width for line in lines), (width, lines)
                assert selected_row == 0, (width, state)


def test_the_worker_row_degrades_in_the_documented_order():
    """The ladder: the STATE glyph column goes first, then the NODE column, then
    the box — the box carries the content, so it is the last thing to give up.
    Below even a minimal box, the row is one honest short line rather than a
    broken box."""
    row = worker("w", "idle", text="zx")

    # 8 and up: the shipped shape — node, space, state glyph, space, box.
    full = _fmts(a.worker_box(row, 8, selected=True)[0])
    assert full[0].startswith("│   ╭"), full[0]
    assert full[1].startswith("⦿ ✓ │"), full[1]
    assert all(len(line) == 8 for line in full)

    # 6 and 7: the state glyph column AND its space are gone; the node stays.
    slim = _fmts(a.worker_box(row, 7, selected=True)[0])
    assert slim[0].startswith("│ ╭"), slim[0]
    assert slim[1].startswith("⦿ │"), slim[1]
    assert all(len(line) == 7 for line in slim)
    assert "✓" not in "".join(slim), "no state glyph column below the full gutter"

    # 4 and 5: the node column goes too; the box is the whole row.
    bare = _fmts(a.worker_box(row, 5, selected=True)[0])
    assert bare[0].startswith("╭"), bare[0]
    assert all(len(line) == 5 for line in bare)
    assert bare[1][0] != a.NODE

    # 1-3: no box can be drawn at all, so one honest line, state then task.
    tiny = _fmts(a.worker_box(row, 3, selected=True)[0])
    selected_row = a.worker_box(row, 3, selected=True)[1]
    assert len(tiny) == 1 and tiny[0] == "✓ …", tiny
    assert selected_row == 0, "the SELECTED role still has a row to sit on"


def test_the_narrowest_worker_row_keeps_the_state_glyph():
    """Below a box the STATE is what survives — never a bare ellipsis.

    Rung 4 of the ladder promises one honest clipped line that still carries the
    state glyph, because the state is the one column worth keeping. `_clip`
    alone breaks that promise at width 1: handed the whole line it spends the
    single column on its own ellipsis (`…`) and the state is gone. Composing the
    glyph FIRST is what makes the promise true at 1, 2 and 3.
    """
    cases = (("working", a.SPIN_FRAMES[2]), ("blocked", "⏸"),
             ("failed", "×"), ("idle", "✓"))
    for state, glyph in cases:
        for width in (1, 2, 3):
            spans, selected_row = a.worker_box(
                worker("w", state, text="a title long enough to clip"),
                width, selected=False, spin=2)
            assert len(spans) == 1, (state, width, spans)
            line = _join(spans[0])
            assert glyph in line, (state, width, line)
            assert len(line) <= width, (state, width, line)
            assert selected_row == 0, (state, width)
    # Width 1 is the glyph ALONE: there is no column left for the task.
    assert _fmts(a.worker_box(worker("w", "idle", text="zx"), 1, True)[0]) == ["✓"]
    # 2 and 3 follow the glyph with as much of the clipped task as fits.
    assert _fmts(a.worker_box(worker("w", "idle", text="zx"), 2, True)[0]) == ["✓…"]
    assert _fmts(a.worker_box(worker("w", "idle", text="zx"), 3, True)[0]) == ["✓ …"]


def test_a_whole_rendered_frame_fits_any_pane_at_least_one_column_wide():
    # The end-to-end ladder check: the root box, rails, worker boxes and footer
    # all fit at every width from 1 up. A wrapped pane is what this rules out.
    rows = [header(), rail("idle"),
            worker("w1", "idle", text="one", agent="zen", meta="2m14s")]
    for width in range(1, 47):
        lines = a.render(a.Frame(rows=rows, selected=2), width=width)
        assert lines, width
        assert all(len(line) <= width for line in lines), (width, lines)


# ---------------------------------------------------------------------------
# 3. the cwd rule
# ---------------------------------------------------------------------------

def test_scope_matches_the_workspace_of_the_current_directory():
    here = dict(listed("A", status="running"), workspace="/repo/og")
    other = dict(listed("B", status="running"), workspace="/repo/elsewhere")
    assert a.select_scope([here, other], "/repo/og", False) == ["A"]


def test_scope_matches_through_a_trailing_separator():
    here = dict(listed("A", status="running"), workspace="/repo/og")
    assert a.select_scope([here], "/repo/og/", False) == ["A"]


def test_scope_falls_back_to_every_root_when_nothing_matches():
    here = dict(listed("A", status="running"), workspace="/repo/og")
    other = dict(listed("B", status="running"), workspace="/repo/elsewhere")
    assert a.select_scope([here, other], "/somewhere/else", False) == ["A", "B"]


def test_all_overrides_a_match():
    here = dict(listed("A", status="running"), workspace="/repo/og")
    other = dict(listed("B", status="running"), workspace="/repo/og")
    assert a.select_scope([here, other], "/repo/og", True) == ["A", "B"]


def test_scope_ignores_a_root_with_no_workspace():
    # A detail fetch that failed leaves no workspace; it must not match, and it
    # must not hide the roots that do match.
    here = dict(listed("A", status="running"), workspace="/repo/og")
    unknown = dict(listed("B", status="running"))
    assert a.select_scope([here, unknown], "/repo/og", False) == ["A"]
    assert a.select_scope([unknown], "/repo/og", False) == ["B"]


# ---------------------------------------------------------------------------
# 5. the budget: no detail fetch per row per poll
# ---------------------------------------------------------------------------

ROOT_DETAIL = detail("root", workspace="/repo/og", harness="claude-native")


def _live_listing():
    return [
        listed("root", status="running", title="conversation"),
        listed("w1", parent="root", status="running", title="w1"),
        listed("w2", parent="root", status="running", title="w2"),
        listed("w3", parent="root", status="running", title="w3"),
    ]


def _model(opener, cwd="/nowhere", all_roots=True):
    return a.AgentModel(w.SessionWatcher(base_url="http://x", opener=opener),
                        cwd=cwd, all_roots=all_roots)


def test_three_refreshes_fetch_each_root_detail_once_not_once_per_row_per_poll():
    opener = RoutingOpener([_live_listing()] * 3, details={"root": ROOT_DETAIL})
    model = _model(opener)

    for _ in range(3):
        model.refresh()

    assert len(opener.listing_requests) == 3, "one listing walk per refresh"
    # One detail fetch for the single live root — NOT one per worker, and not
    # repeated on refresh two or three.
    assert len(opener.detail_requests) == 1, [r.full_url for r in opener.detail_requests]


def test_an_idle_worker_costs_no_detail_fetch():
    listing = [
        listed("root", status="running", title="conversation"),
        listed("w1", parent="root", status="idle", title="done-worker"),
    ]
    opener = RoutingOpener([listing] * 3, details={"root": ROOT_DETAIL})
    model = _model(opener)

    for _ in range(3):
        model.refresh()

    # Only the live root's detail is fetched. An idle worker's report used to be
    # read once and kept, to expand it on demand; with the expand view gone there
    # is nothing to fetch, and the count must not creep back up.
    assert len(opener.detail_requests) == 1, [r.full_url for r in opener.detail_requests]


def test_a_quiet_root_with_no_workers_is_not_fetched():
    # An old, quiet conversation that delegated nothing: not live, so it costs
    # no detail fetch and does not appear under --all. This is what keeps "all
    # live roots" a handful on a machine with a long history of sessions.
    listing = [listed("root", status="idle", title="old conversation")]
    opener = RoutingOpener([listing], details={})
    model = _model(opener)

    frame = model.refresh()

    assert opener.detail_requests == []
    assert frame.rows == []


def test_an_idle_root_with_a_finished_worker_still_shows():
    # An orchestrator that has answered and a worker that is done must still draw
    # their tree: the root is live because it has a worker, so an idle
    # conversation is not dropped from the view.
    listing = [
        listed("root", status="idle", title="conversation"),
        listed("w1", parent="root", status="idle", title="done-worker"),
    ]
    opener = RoutingOpener([listing], details={"root": ROOT_DETAIL})
    model = _model(opener)

    lines = a.render(model.refresh())

    assert lines[0].startswith("╭"), "the root is a box"
    assert "conversation" in lines[1]
    assert any("done-worker" in line for line in lines)


# ---------------------------------------------------------------------------
# 6. the cwd rule beats the live-root heuristic (the wrong-directory bug)
# ---------------------------------------------------------------------------

def test_a_quiet_workerless_root_in_the_cwd_narrows_the_view_to_its_tree():
    """`og agents` beside a fresh `og chat` showed three unrelated roots.

    "Live" means working, blocked, or the parent of a worker — so a brand-new
    chat in THIS directory is idle with nothing dispatched, its `workspace` was
    never fetched, nothing matched `$PWD`, and the view fell back to every root
    on the machine. The cwd question is not a question about activity, so it is
    now asked of every root, cached per session id, until one answers yes.
    """
    listing = [
        listed("here", status="idle", title="og chat"),
        listed("busy", status="running", title="other project",
               workspace="/somewhere/else"),
        listed("b1", parent="busy", status="running", title="coder_zen:work"),
        listed("elsewhere", status="running", title="third project",
               workspace="/a/third/place"),
    ]
    opener = RoutingOpener([listing] * 4, details={
        "here": detail("here", workspace="/repo/og", harness="claude-native"),
        "busy": detail("busy", workspace="/somewhere/else",
                       harness="claude-native"),
        "elsewhere": detail("elsewhere", workspace="/a/third/place",
                            harness="claude-native"),
        "b1": detail("b1"),
    })
    model = _model(opener, cwd="/repo/og", all_roots=False)

    frame = model.refresh()

    assert [row.session_id for row in frame.rows] == ["here"], frame.rows
    assert frame.rows[0].is_root, "it narrowed to the quiet root's own tree"

    # The cost is one fetch per root, ONCE — four polls, and the detail
    # endpoints are hit exactly as many times as there are roots.
    for _ in range(3):
        model.refresh()
    assert len(opener.detail_requests) == 3, [r.full_url for r in opener.detail_requests]


def test_the_cwd_probe_stops_at_the_first_match():
    listing = [
        listed("a", status="running", title="a"),
        listed("b", status="idle", title="b", workspace="/repo/og"),
        listed("c", status="idle", title="c"),
    ]
    opener = RoutingOpener([listing] * 2, details={
        "a": detail("a", workspace="/elsewhere"),
        "b": detail("b", workspace="/repo/og"),
        "c": detail("c", workspace="/a/third"),
    })
    model = _model(opener, cwd="/repo/og", all_roots=False)

    frame = model.refresh()

    assert [row.session_id for row in frame.rows] == ["b"]
    # "c" is after the match, so it was never put to the question.
    assert [r.full_url for r in opener.detail_requests] == \
        ["http://x/v1/sessions/a", "http://x/v1/sessions/b"]


def test_the_cwd_probe_is_skipped_entirely_under_all_roots():
    listing = [listed("a", status="running", title="a"),
               listed("b", status="idle", title="b", workspace="/repo/og")]
    opener = RoutingOpener([listing], details={"a": detail("a"),
                                               "b": detail("b")})
    model = _model(opener, cwd="/repo/og", all_roots=True)

    model.refresh()

    # --all says "show everything live"; a quiet root is not live, and the cwd
    # rule is not in force, so only the LIVE root's detail is ever fetched.
    assert [r.full_url for r in opener.detail_requests] == \
        ["http://x/v1/sessions/a"]


# ---------------------------------------------------------------------------
# 6b. bug: two roots in the same directory — the STALE one is the live one
# ---------------------------------------------------------------------------
#
# `test_a_quiet_workerless_root_in_the_cwd_narrows_the_view_to_its_tree` (above)
# covers the case where NO root in the cwd was live. It cannot see this one, and
# neither could any other test here, because every fixture until now had at most
# one root in `$PWD` — which is why the wrong-session bug shipped: with a single
# candidate, "first live match" and "root in this directory" are the same answer,
# so the defect is invisible until a repo has a history of sessions in it.

def _two_roots_in_one_repo():
    """A repo with two root sessions in it: yesterday's run and today's.

    `stale` is the trap. It is IDLE and has a finished worker, so `_is_live_root`
    calls it live forever — which is correct for "worth showing" and useless for
    "which directory am I in". `fresh` is the operator's session: brand new,
    idle, nothing dispatched, so it is not in the live set at all.

    Listing order is newest-first (og_herdr_watch's measured fact), which here is
    ALSO the recency order — so on this fixture alone, "first row that matches"
    would score the same as "most recent match". The tests below that put the two
    in disagreement are what actually pin the rule; these pin the failure.
    """
    return [
        listed("fresh", status="idle", title="og chat",
               created_at=NOW - 60, updated_at=NOW - 60),
        listed("stale", status="idle", title="yesterday's run",
               created_at=NOW - 90000, updated_at=NOW - 90000),
        listed("s1", parent="stale", status="idle", title="coder_zen:old work"),
    ]


_TWO_ROOTS_DETAILS = {
    "fresh": detail("fresh", workspace="/repo/og", harness="claude-native"),
    "stale": detail("stale", workspace="/repo/og", harness="claude-native"),
    "s1": detail("s1", items=[ASSISTANT_MESSAGE]),
}


def test_the_newer_root_wins_when_a_stale_sibling_in_the_same_repo_is_live():
    """`og agents` showed ~16 workers finished 22-41h ago. It was the wrong one.

    A brand-new idle session in this repo, nothing dispatched from it, showed the
    worker tree of the PREVIOUS root session in the same directory. `stale` won
    for one reason only: it still has workers, so it is "live", so it matched
    `$PWD` in the pass that checked live roots first and returned immediately —
    and `fresh`, which was never asked at all.

    The cwd question is about recency, not activity: the root in this directory
    is the most recently active one, and a stale sibling with workers on screen
    must not outrank the session the operator is standing in.
    """
    opener = RoutingOpener([_two_roots_in_one_repo()] * 3,
                           details=dict(_TWO_ROOTS_DETAILS))
    model = _model(opener, cwd="/repo/og", all_roots=False)

    frame = model.refresh()

    assert [row.session_id for row in frame.rows] == ["fresh"], frame.rows
    assert frame.rows[0].is_root, "it narrowed to the operator's own tree"
    # The stale tree is not merely unshown behind the new one — it is absent,
    # including its finished worker.
    assert not any("old work" in row.text for row in frame.rows), frame.rows


def test_all_still_forces_the_wide_view_with_two_roots_in_one_repo():
    """`--all` is the operator overriding the cwd rule, so it must still do that.

    The fix moved the cwd decision off liveness and onto recency. `--all` is the
    escape hatch for "I know there are other sessions, show me the live ones",
    and a recency rule that leaked into it would turn it back into "this
    directory's root". It shows every LIVE root — which here is `stale`, because
    it has workers — and it pays nothing for the quiet root it declines to show.
    """
    opener = RoutingOpener([_two_roots_in_one_repo()] * 2,
                           details=dict(_TWO_ROOTS_DETAILS))
    model = _model(opener, cwd="/repo/og", all_roots=True)

    frame = model.refresh()

    # The rail rows are structural — they open a state group — so the SESSIONS
    # are the non-rail rows.
    ids = [row.session_id for row in frame.rows if not row.rail]
    assert ids == ["stale", "s1"], frame.rows
    # The quiet root was never even asked: `--all` is a liveness decision, and
    # liveness is free (the listing row already says idle-with-no-workers). The one
    # fetch is `stale`'s own label; a worker's report is no longer read (that went
    # with the expand view), so `s1` costs nothing.
    assert [r.full_url for r in opener.detail_requests] == [
        "http://x/v1/sessions/stale",
    ], [r.full_url for r in opener.detail_requests]


def test_the_stale_live_root_is_still_fetched_and_cached_not_reshown():
    """The fix narrows the SCOPE; it must not stop `stale` being shown elsewhere.

    A narrowing that dropped the root from the view would hide a real session
    from an operator sitting in a different directory. So it is still fetched
    (that is what its `harness` label and `workspace` cost) and still cached,
    and it is still in scope the moment the operator is somewhere that has no
    root of its own.
    """
    opener = RoutingOpener([_two_roots_in_one_repo()] * 2,
                           details=dict(_TWO_ROOTS_DETAILS))
    elsewhere = _model(opener, cwd="/no/root/here", all_roots=False)

    frame = elsewhere.refresh()

    # No root lives in `/no/root/here`, so the wide live set stands — and
    # `stale` is in it, because it has workers.
    ids = [row.session_id for row in frame.rows if not row.rail]
    assert ids == ["stale", "s1"], frame.rows
    s1 = next(row for row in frame.rows if row.session_id == "s1")
    assert "old work" in s1.text


def test_the_cwd_probe_stops_at_the_newest_match_and_costs_one_fetch_per_root():
    """The budget, asserted on the seam: no fetch per root, per poll.

    The recency walk replaces "live roots first, then the rest". That could have
    cost a fetch per root on every poll, so it is pinned here: the walk fetches
    each root at most once, ever, and stops at the first match — so the roots
    BELOW the answer are never asked at all, and later polls add nothing.

    The timestamps are arranged so the walk has real work to do in both
    directions: two roots more recent than the match (which must be fetched and
    rejected, and are the cost the ordering is buying) and one less recent (which
    must never be touched at all).
    """
    listing = _two_roots_in_one_repo() + [
        # More recent than `fresh`, in other directories: the walk cannot stop at
        # the first row, it has to check them and reject them.
        listed("newest_other", status="idle", title="yet another repo",
               created_at=NOW - 10, updated_at=NOW - 10),
        listed("recent_other", status="idle", title="another repo",
               created_at=NOW - 30, updated_at=NOW - 30),
        # LESS recent than the match and in another directory: the walk stops at
        # `fresh`, so this one is never put to the question.
        listed("older_other", status="idle", title="an older repo",
               created_at=NOW - 120, updated_at=NOW - 120),
    ]
    opener = RoutingOpener([listing] * 4, details={
        **_TWO_ROOTS_DETAILS,
        "newest_other": detail("newest_other", workspace="/a/fourth/place"),
        "recent_other": detail("recent_other", workspace="/a/third/place"),
        "older_other": detail("older_other", workspace="/a/second/place"),
    })
    model = _model(opener, cwd="/repo/og", all_roots=False)

    model.refresh()
    after_first = [r.full_url for r in opener.detail_requests]

    # `stale` is read for its harness label regardless (it is live); the walk
    # then reads the two roots more recent than the answer and rejects them, and
    # matches `fresh`. Four fetches, and `older_other` is not one of them.
    assert after_first == [
        "http://x/v1/sessions/stale",
        "http://x/v1/sessions/newest_other",
        "http://x/v1/sessions/recent_other",
        "http://x/v1/sessions/fresh",
    ], [r.full_url for r in opener.detail_requests]

    # One fetch per root, ONCE. Four polls of a stable listing cost exactly what
    # the first did — the regression this rule exists to prevent is a walk that
    # refetches 121 KB per root every three seconds.
    for _ in range(3):
        model.refresh()
    assert [r.full_url for r in opener.detail_requests] == after_first


def test_two_roots_in_the_cwd_do_not_widen_the_view_to_both():
    """Most-recent wins outright: it is one root's tree, not two.

    The bug showed the WRONG tree. The obvious wrong fix would show both, which
    is no better — the operator still cannot tell which session they are looking
    at. A match is the whole scope, exactly as before.
    """
    opener = RoutingOpener([_two_roots_in_one_repo()],
                           details=dict(_TWO_ROOTS_DETAILS))
    model = _model(opener, cwd="/repo/og", all_roots=False)

    frame = model.refresh()

    assert [row.session_id for row in frame.rows] == ["fresh"]
    assert len([row for row in frame.rows if row.is_root]) == 1


def test_a_root_with_no_usable_timestamp_is_not_treated_as_the_newest():
    """Unknown age sorts LAST, so a row missing `updated_at` cannot win by luck.

    The listing carries `created_at` and `updated_at` and nothing newer (see
    og_herdr_watch's measured key list), so recency comes off the row itself and
    costs no fetch. A root whose row carries neither is not evidence of recency;
    treating it as newest would hand the operator whichever unknown-age root
    happened to sort first, which is the bug being fixed, wearing a different hat.
    """
    listing = [
        listed("dateless", status="idle", title="no timestamps at all"),
        listed("stale", status="idle", title="yesterday's run",
               updated_at=NOW - 90000),
        listed("s1", parent="stale", status="idle", title="coder_zen:old work"),
    ]
    opener = RoutingOpener([listing], details={
        "dateless": detail("dateless", workspace="/repo/og"),
        "stale": detail("stale", workspace="/repo/og"),
        "s1": detail("s1"),
    })
    model = _model(opener, cwd="/repo/og", all_roots=False)

    frame = model.refresh()

    ids = [row.session_id for row in frame.rows if not row.rail]
    assert ids == ["stale", "s1"], frame.rows


def test_recency_breaks_a_tie_by_created_at_when_updated_at_is_absent():
    """`updated_at` is the answer; `created_at` is what is left when it is not.

    Both come off the free LISTING row. Without this, two roots in one repo with
    neither an `updated_at` would tie and fall back to listing order — which is
    newest-first by the server's sort, but is a coincidence rather than a rule.
    """
    listing = [
        listed("dateless", status="idle", title="older, no updated_at",
               created_at=NOW - 90000),
        listed("fresh", status="idle", title="newer, no updated_at",
               created_at=NOW - 60),
        listed("stale", status="idle", title="yesterday's run",
               updated_at=NOW - 120000),
        listed("s1", parent="stale", status="idle", title="coder_zen:old work"),
    ]
    opener = RoutingOpener([listing], details={
        "dateless": detail("dateless", workspace="/repo/og"),
        "fresh": detail("fresh", workspace="/repo/og"),
        "stale": detail("stale", workspace="/repo/og"),
        "s1": detail("s1"),
    })
    model = _model(opener, cwd="/repo/og", all_roots=False)

    frame = model.refresh()

    assert [row.session_id for row in frame.rows] == ["fresh"], frame.rows


def test_a_live_root_in_the_cwd_still_wins_over_a_more_recent_root_elsewhere():
    """The ordinary case must not regress: one live root in `$PWD`, and its tree.

    Every other test in this file puts the answer in a quiet root. The common
    real shape is the opposite — the operator's own session is running, has
    workers, and is the only root in the repo — and that is the case where the
    recency walk must agree with the live-root heuristic rather than fight it.
    """
    listing = [
        listed("mine", status="running", title="this project",
               created_at=NOW - 600, updated_at=NOW - 2),
        listed("m1", parent="mine", status="running", title="coder_zen:work"),
        listed("other", status="running", title="other project",
               created_at=NOW - 300, updated_at=NOW - 1),
        listed("o1", parent="other", status="running", title="coder_zen:elsewhere"),
    ]
    opener = RoutingOpener([listing] * 2, details={
        "mine": detail("mine", workspace="/repo/og", harness="claude-native"),
        "m1": detail("m1"),
        "other": detail("other", workspace="/somewhere/else",
                        harness="claude-native"),
        "o1": detail("o1"),
    })
    model = _model(opener, cwd="/repo/og", all_roots=False)

    frame = model.refresh()

    ids = [row.session_id for row in frame.rows if not row.rail]
    assert ids == ["mine", "m1"], frame.rows
    # The root's harness label resolves on the content row of its box.
    assert frame.rows[0].is_root
    assert _join(a._root_lines(frame.rows[0], 60)[1]).endswith("Claude Code│")
    # `other` is MORE recently active than `mine` and is still not the answer:
    # recency breaks a tie between roots in `$PWD`, it does not outrank the cwd.
    assert opener.detail_requests, "both live roots were read"


# ---------------------------------------------------------------------------
# 7. a detail fetch that fails still renders the row
# ---------------------------------------------------------------------------

def test_a_failed_root_detail_still_renders_the_tree():
    opener = RoutingOpener([_live_listing()], details={"root": URLError("down")})
    model = _model(opener)

    lines = a.render(model.refresh())

    # The root does not vanish: with no harness to label it by, it falls back to
    # its title, and the tree beneath it still renders.
    assert lines[0].startswith("╭") and "conversation" in lines[1]
    assert any(a.SPIN_FRAMES[0] in line and "w1" in line for line in lines), \
        "the worker box survives, under its working spinner"


# ---------------------------------------------------------------------------
# 8. the harness label
# ---------------------------------------------------------------------------

def test_harness_label_uses_the_registry():
    assert a.harness_label("claude-native") == "Claude Code"
    assert a.harness_label("opencode-native") == "OpenCode (Zen)"


def test_harness_label_falls_back_to_the_raw_id():
    assert a.harness_label("brand-new-native") == "brand-new-native"


def test_harness_label_defaults_when_there_is_no_harness():
    assert a.harness_label(None) == a.DEFAULT_AGENT_LABEL
    assert a.harness_label("") == a.DEFAULT_AGENT_LABEL
    assert a.harness_label(7) == a.DEFAULT_AGENT_LABEL


# ---------------------------------------------------------------------------
# 8b. the harness mark (vendor brand glyph, in its two-column slot)
# ---------------------------------------------------------------------------

# The operator's tier-0 picks, copied here so the test states the decision
# rather than reading it back out of the module it is checking. A drift in
# `_HARNESS_MARKS` is exactly what this is meant to catch. Every pick is East
# Asian NARROW; kilo, cognition and z-ai were once AMBIGUOUS and were replaced
# with Narrow glyphs, so no mark's width depends on the locale.
MARK = {
    "anthropic": "\u2733",
    "opencode-zen": "\u25AF",
    "google": "\u2726",
    "openai": "\u2B21",
    "deepseek": "\U0001D5D6",
    "kilo": "\U0001D5DE",
    "aws-kiro": "\U0001680C",
    "cursor": "\u21D7",
    "goose": "\U0001316F",
    "hermes": "\u2624",
    "xai": "\U0001D54F",
    "cognition": "\u2316",
    "z-ai": "\U0001D5D9",
    "moonshotai": "\u2318",
}


def _catalog():
    """The repo catalog, read through the same path the pane resolves against."""
    return json.loads((Path(a.__file__).parent / "registry.json").read_text())


def _harness_for_vendor():
    """{vendor: harness} from the catalog, first row wins (matches the loader)."""
    out = {}
    for row in _catalog()["agents"]:
        out.setdefault(row["vendor"], row["harness"])
    return out


def test_the_mark_table_is_pinned_to_the_catalog_vendors():
    # Adding an agent with a NEW vendor to installer/registry.json must add a
    # mark here in the same commit. Without this, that new vendor resolves
    # through the harness->vendor hop to nothing and draws a silent blank — the
    # pane would look like it had decided not to mark the agent, when the truth
    # is that nobody had chosen a glyph yet.
    vendors = {row["vendor"] for row in _catalog()["agents"]}
    assert set(a._HARNESS_MARKS) == vendors, (
        "the mark table and the registry's vendors disagree; a vendor with no "
        "mark would draw a silent blank")


def test_every_vendor_resolves_to_its_mark_in_a_two_column_slot():
    harnesses = _harness_for_vendor()
    assert set(MARK) == set(harnesses), "test and catalog disagree on vendors"
    for vendor, mark in MARK.items():
        got = a.harness_mark(harnesses[vendor])
        # The glyph is the operator's pick, not something the slot invented.
        assert got.strip() == mark, (vendor, got)
        # ...and whatever its own width, the slot is exactly two columns. Measured
        # with the SLOT's ruler: the general `_display_width` would charge an
        # Ambiguous mark an extra column and so disagree with the slot's contract
        # on purpose (see `_slot_width`). No shipped mark is Ambiguous now, so the
        # two rulers currently agree on all fourteen — but the slot still pins its
        # own two-column promise against its own ruler.
        assert a._slot_width(got) == 2, (vendor, got)


def test_every_mark_is_narrow_and_padded_with_one_space():
    # Every mark is East Asian NARROW, so the slot pads each exactly like any
    # other — glyph plus one space — and every title starts in the same column.
    # The three then-Ambiguous picks (kilo, cognition, z-ai) used to take the
    # western one-column branch under a pessimistic charge that left them unpadded
    # and sheared their titles; they are Narrow glyphs now, so that path is gone.
    # See `_slot_width` for the (now hypothetical) Ambiguous rule.
    harnesses = _harness_for_vendor()
    for vendor, mark in MARK.items():
        got = a.harness_mark(harnesses[vendor])
        assert got == mark + " ", (vendor, repr(got))
    # And the class is what the test claims it is, not assumed.
    for vendor, mark in MARK.items():
        assert unicodedata.east_asian_width(mark) == "N", (vendor, mark)


def test_every_mark_and_the_node_are_narrow():
    # ONE guard for the whole table and the gutter node: every glyph carries an
    # UNCONDITIONAL one-column width — East Asian NARROW, never Ambiguous ("A",
    # locale-dependent) and never Wide/Fullwidth ("W"/"F", two columns). This is
    # what fails if a future edit drops an Ambiguous or Wide glyph back in. The
    # operator made exactly this swap for the node (⊙ U+2299 -> ⦿ U+29BF) and for
    # the three then-Ambiguous marks.
    for vendor, mark in a._HARNESS_MARKS.items():
        assert unicodedata.east_asian_width(mark) == "N", (vendor, mark)
        assert a._slot_width(mark) == 1, (vendor, mark)
    assert unicodedata.east_asian_width(a.NODE) == "N", a.NODE
    assert a._slot_width(a.NODE) == 1, a.NODE


def test_the_narrow_swaps_resolve_to_the_chosen_codepoints():
    # The explicit replacements, spelled as codepoints so a stray edit to a
    # look-alike glyph is caught: node ⦿ U+29BF, kilo 𝗞 U+1D5DE, cognition ⌖ U+2316.
    # 𝗖 (vendor `deepseek` -> CLINE) and 𝗙 (vendor `z-ai` -> FREEBUFF) joined them:
    # the mark names the TOOL, so the old whale (the MODEL's logo) is gone.
    # 𝗖, 𝗙 and 𝗞 are outside the BMP, like the existing 𝕏 (xai) and the two
    # surviving historic glyphs, 𓅯 goose and 𖠌 aws-kiro.
    assert a.NODE == "\u29bf"
    assert a._HARNESS_MARKS["kilo"] == "\U0001D5DE"
    assert a._HARNESS_MARKS["cognition"] == "\u2316"
    assert a._HARNESS_MARKS["z-ai"] == "\U0001D5D9"
    assert a._HARNESS_MARKS["deepseek"] == "\U0001D5D6"
    # The z-ai swap also fixes a second defect: U+2554 was a BOX-DRAWING corner
    # sitting inside a pane built from box-drawing, so it read as frame damage
    # beside a real corner.
    assert a._HARNESS_MARKS["z-ai"] != "\u2554"
    # The whale named the MODEL (DeepSeek), never the harness — that is why it left.
    assert a._HARNESS_MARKS["deepseek"] != "\U0001319E"


def test_the_blank_slot_is_exactly_two_spaces(monkeypatch):
    # A row that names NO agent identity draws the blank slot — a mark there would
    # be meaningless. This is the path the atom must never take.
    monkeypatch.setattr(a, "agent_harness", lambda name: None)
    assert a.harness_mark(None) == "  "
    assert a.harness_mark("") == "  "
    assert a.harness_mark("   ") == "  "
    assert a.worker_mark("", None) == "  "
    assert a.worker_mark("", "") == "  "
    assert a.worker_mark(None, "   ") == "  "
    assert a._slot_width(a.harness_mark(None)) == 2


def test_every_return_is_exactly_two_slot_columns_including_the_blank():
    # Measured with the SLOT's ruler, not `_display_width`: the slot's contract is
    # two western columns, and a general-ruler check would (hypothetically) charge
    # an Ambiguous mark an extra column. Every shipped mark is Narrow now, so the
    # slot still pads each to exactly two.
    for row in _catalog()["agents"]:
        assert a._slot_width(a.harness_mark(row["harness"])) == 2, row["id"]
    for degenerate in ("brand-new-native", "", None, 7, "  "):
        assert a._slot_width(a.harness_mark(degenerate)) == 2, degenerate


def test_every_worker_mark_return_is_two_columns(monkeypatch):
    # The worker path is two columns for a resolved spec harness, for the atom a
    # named-but-unknown agent earns, and for the blank a nameless row draws.
    monkeypatch.setattr(a, "agent_harness",
                        lambda name: "opencode-native" if name == "zen" else None)
    assert a._display_width(a.worker_mark("", "zen")) == 2
    assert a._display_width(a.worker_mark("", "reviewer")) == 2
    assert a._display_width(a.worker_mark("", None)) == 2
    assert a._display_width(a.worker_mark(7, 7)) == 2


def test_an_unresolvable_agent_gets_the_atom_never_a_blank(monkeypatch):
    # An agent that IS named but whose harness we cannot find draws the atom —
    # "an agent whose harness I could not identify" — rather than a silent blank.
    # It must not raise: a pane has to render with no marks rather than not render.
    assert a.harness_mark("brand-new-native") == a.ATOM + " "
    monkeypatch.setattr(a, "agent_harness", lambda name: None)
    assert a.worker_mark("", "reviewer") == a.ATOM + " "
    # A non-string is NO identity, not an unresolvable one: blank.
    assert a.harness_mark(7) == "  "
    assert a.worker_mark("", 7) == "  "


def test_the_atom_is_east_asian_narrow_like_the_other_marks():
    # The slot charges it one trailing space; the whole point is that every mark
    # spends the same two display columns.
    assert unicodedata.east_asian_width(a.ATOM) == "N"
    assert a._mark_slot(a.ATOM) == a.ATOM + " "


def test_an_unknown_vendor_gets_the_atom(monkeypatch):
    # A registry row naming a vendor the mark table does not carry (a catalog
    # edited before the table) still names an agent, so it earns the atom, not a
    # blank — the failure is a missing mark, not a missing agent.
    monkeypatch.setattr(a, "harness_vendors",
                        lambda: {"mystery-native": "unknown-vendor"})
    assert a.harness_mark("mystery-native") == a.ATOM + " "


def test_a_broken_registry_blanks_no_harness_but_atoms_a_named_one(
        monkeypatch, tmp_path):
    # Mirrors the label test in tests/test_og_herdr.py: a missing, malformed or
    # wrong-shaped catalog yields an empty vendor table. A row that names NO
    # harness still draws the blank; a row that names one we can no longer look up
    # draws the atom. Never a raise.
    for name, content in (("missing.json", None),
                          ("broken.json", "{ not json"),
                          ("wrong-shape.json", '{"agents": "nope"}'),
                          ("a-list.json", "[]")):
        monkeypatch.setattr(h, "_REGISTRY_VENDORS", None)
        path = tmp_path / name
        if content is not None:
            path.write_text(content)
        monkeypatch.setattr(h, "REGISTRY_PATH", path)
        assert h.harness_vendors() == {}, name
        assert a.harness_mark("") == "  ", name
        assert a.harness_mark("claude-native") == a.ATOM + " ", name


# ---------------------------------------------------------------------------
# 8c. the agent NAME -> the harness its SPEC declares (the fix for worker marks)
#
# A mark identifies THE HARNESS A SESSION ACTUALLY RAN ON. A worker's LISTING row
# carries no `harness`, so the box resolves it from the spec Omnigent installed
# for the agent the title names — NOT from a vendor guessed off the name. These
# tests build a throwaway home so the resolution is exercised against known specs
# rather than whatever the developer's ~/.omnigent happens to hold.
# ---------------------------------------------------------------------------

def _write_spec(home, bundle, name, harness):
    """Write `<home>/agents/<bundle>/agents/<name>/config.yaml`.

    The harness goes under `executor.config.harness` — where the installer writes
    it (see templates/coder.yaml.tmpl) — so the test exercises the real shape.
    """
    spec_dir = Path(home) / "agents" / bundle / "agents" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "config.yaml").write_text(
        f"spec_version: 1\nname: {name}\nexecutor:\n  type: omnigent\n"
        f"  config:\n    harness: {harness}\n")
    return spec_dir


@pytest.fixture
def spec_home(monkeypatch, tmp_path):
    """Point $OMNIGENT_HOME at a scratch dir and reset the cached scan."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(h, "_REGISTRY_AGENT_HARNESSES", None)
    return tmp_path


def test_a_declared_spec_harness_resolves_to_that_harness_mark(spec_home):
    # The row's agent name is `zen` (split_title strips the `coder_` prefix) while
    # the installed spec is `coder_zen`; both spellings resolve, and the mark is
    # the harness's, not the name's.
    _write_spec(spec_home, "Hivemind", "coder_zen", "opencode-native")
    assert h.agent_harness("zen") == "opencode-native"
    assert h.agent_harness("coder_zen") == "opencode-native"
    assert a.worker_mark("", "zen") == a._HARNESS_MARKS["opencode-zen"] + " "


def test_the_real_roster_specs_resolve_through_their_declared_harness(spec_home):
    # The three roles whose NAMES are not registry ids — `reviewer`, `scout`,
    # `integrator` — now mark from their specs instead of drawing an atom.
    _write_spec(spec_home, "Hivemind", "reviewer", "codex-native")
    _write_spec(spec_home, "Hivemind", "scout", "acp:command-code")
    _write_spec(spec_home, "Hivemind", "integrator", "acp:command-code")
    assert a.worker_mark("", "reviewer") == a._HARNESS_MARKS["openai"] + " "
    assert a.worker_mark("", "scout") == a._HARNESS_MARKS["moonshotai"] + " "
    assert a.worker_mark("", "integrator") == a._HARNESS_MARKS["moonshotai"] + " "


def test_the_rows_own_harness_wins_over_the_spec_when_they_disagree(spec_home):
    # THE PRINCIPLE. The row's own harness is what ACTUALLY ran, so it outranks
    # whatever the agent's name would resolve to. Pin it: a spec that disagrees
    # must not override the harness the row itself reports.
    _write_spec(spec_home, "Hivemind", "coder_zen", "opencode-native")
    assert a.worker_mark("claude-native", "zen") == \
        a._HARNESS_MARKS["anthropic"] + " "


def test_an_agent_with_no_spec_and_no_harness_gets_the_atom(spec_home):
    # An agent IS named, so there is something to mark; we just cannot find a
    # harness for it — the atom, never a blank.
    assert h.agent_harness("brandnew") is None
    assert a.worker_mark("", "brandnew") == a.ATOM + " "


def test_a_row_with_no_identity_at_all_gets_the_blank(spec_home):
    # Neither a harness nor a name: nothing to mark. The genuinely-blank case,
    # which the atom must never take.
    assert a.worker_mark("", None) == "  "
    assert a.worker_mark("", "") == "  "
    assert a.worker_mark(None, "   ") == "  "


def test_a_missing_omnigent_home_degrades_without_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "_REGISTRY_AGENT_HARNESSES", None)
    monkeypatch.setattr(h, "default_home", lambda: tmp_path / "nope")
    assert h.agent_harnesses() == {}
    assert h.agent_harness("zen") is None
    assert a.worker_mark("", "zen") == a.ATOM + " "


def test_an_unreadable_spec_degrades_without_raising(spec_home):
    # `config.yaml` as a DIRECTORY: `read_text` raises OSError, which the scan
    # must swallow. One bad spec costs that one agent's mark, nothing else.
    spec_dir = Path(spec_home) / "agents" / "Hivemind" / "agents" / "coder_zen"
    (spec_dir / "config.yaml").mkdir(parents=True)
    assert h.agent_harnesses() == {}
    assert a.worker_mark("", "zen") == a.ATOM + " "


def test_a_malformed_spec_degrades_without_raising(spec_home):
    spec_dir = Path(spec_home) / "agents" / "Hivemind" / "agents" / "coder_zen"
    spec_dir.mkdir(parents=True)
    (spec_dir / "config.yaml").write_text("executor: [unterminated\n")
    assert h.agent_harnesses() == {}
    assert a.worker_mark("", "zen") == a.ATOM + " "


def test_an_absent_pyyaml_degrades_without_raising(spec_home, monkeypatch):
    # PyYAML is not a dependency of `og agents`; if it is missing the scan must
    # yield no harnesses rather than stop the pane from starting.
    _write_spec(spec_home, "Hivemind", "coder_zen", "opencode-native")
    monkeypatch.setattr(h, "_yaml", None)
    monkeypatch.setattr(h, "_REGISTRY_AGENT_HARNESSES", None)
    assert h.agent_harnesses() == {}
    assert h.agent_harness("zen") is None
    assert a.worker_mark("", "zen") == a.ATOM + " "


def test_the_spec_scan_runs_once_and_is_cached(spec_home, monkeypatch):
    # The pane polls every few seconds; the scan must not walk the filesystem per
    # call. One scan, then the cached table is served.
    calls = []

    def fake_scan(home):
        calls.append(home)
        return {"zen": "opencode-native"}

    monkeypatch.setattr(h, "_scan_agent_harnesses", fake_scan)
    monkeypatch.setattr(h, "_REGISTRY_AGENT_HARNESSES", None)
    assert h.agent_harnesses() == {"zen": "opencode-native"}
    assert h.agent_harnesses() == {"zen": "opencode-native"}
    assert h.agent_harness("zen") == "opencode-native"
    assert h.agent_harness("zen") == "opencode-native"
    assert calls == [h.default_home()]


def test_a_cross_bundle_name_collision_resolves_deterministically(spec_home):
    # Two bundles can both install a `reviewer`. The scan sorts the bundle
    # directories and the FIRST wins, so the answer is the same on every run
    # rather than whatever order the filesystem yielded.
    _write_spec(spec_home, "Bbb", "reviewer", "codex-native")
    _write_spec(spec_home, "Aaa", "reviewer", "claude-native")
    assert h.agent_harness("reviewer") == "claude-native"
    # And again after a fresh scan, to prove the order is fixed, not incidental.
    h._REGISTRY_AGENT_HARNESSES = None
    assert h.agent_harness("reviewer") == "claude-native"


def test_a_worker_box_marks_from_the_spec_because_the_row_has_no_harness(spec_home):
    """A worker's LISTING row carries no `harness`, so the box resolves its mark
    from the harness the agent's installed spec declares."""
    _write_spec(spec_home, "Hivemind", "coder_zen", "opencode-native")
    _write_spec(spec_home, "Hivemind", "reviewer", "codex-native")

    def content_line(**row_kwargs):
        return a.render(a.Frame(rows=[worker("w", "working", text="ZX",
                                             **row_kwargs)]), 48)[1]

    # `zen` resolves through its spec to the opencode mark...
    assert a._HARNESS_MARKS["opencode-zen"] in content_line(agent="zen")
    # ...an agent with no spec shows the atom, NOT a blank...
    assert a.ATOM in content_line(agent="cmdcode")
    # ...and a row naming no agent at all is the genuinely-blank case.
    assert a.worker_mark("", None) == "  "
    assert a.ATOM not in content_line(agent=None)
    # A real `harness` on the row still wins over the spec.
    assert a._HARNESS_MARKS["anthropic"] in content_line(
        agent="zen", harness="claude-native")


def test_harness_vendors_reads_this_repos_catalog():
    # The harness -> vendor hop is a registry fact, read through the same loader
    # the labels use, so it cannot drift from the catalog.
    rows = {row["harness"]: row["vendor"] for row in _catalog()["agents"]}
    vendors = h.harness_vendors()
    for harness, vendor in rows.items():
        assert vendors[harness] == vendor


# ---------------------------------------------------------------------------
# 9. the display state, the worker's name, and its clock
# ---------------------------------------------------------------------------

def test_display_state_adds_failed_without_touching_the_shared_vocabulary():
    # `herdr_state` is shared with the herdr bridge, so its vocabulary is not
    # this view's to widen: `should_project` reads it to decide which sessions
    # get a herdr tab. The fifth state is derived here instead.
    assert a.display_state({"status": "failed"}) == "failed"
    assert a.display_state({"status": "error"}) == "failed"
    assert a.display_state({"status": "completed"}) == "idle"
    assert a.display_state({"status": "running"}) == "working"
    assert a.display_state({"status": "running",
                            "pending_elicitations_count": 2}) == "blocked"
    # A pending elicitation still outranks the status, exactly as in herdr_state.
    assert a.display_state({"status": "failed",
                            "pending_elicitations_count": 1}) == "blocked"
    assert a.display_state({"status": "who-knows"}) == "unknown"
    assert a.display_state(None) == "unknown"
    assert w.herdr_state({"status": "failed"}) == "unknown", "unchanged upstream"


def test_the_agent_name_comes_from_the_title_not_a_detail_fetch():
    assert a.split_title("coder_zen:stock-concurrency-lock") == \
        ("zen", "stock-concurrency-lock")
    assert a.split_title("zen:task") == ("zen", "task")
    assert a.split_title("plain-title") == (None, "plain-title")
    assert a.split_title(None) == (None, "")
    assert a.split_title("coder_zen:") == ("zen", "coder_zen:")


def test_the_meta_line_says_what_the_timestamps_can_support():
    working = {"status": "running", "created_at": NOW - 134}
    assert a.worker_meta(working, "working", NOW) == "2m14s"
    assert a.worker_meta({"status": "running", "created_at": NOW - 41},
                         "working", NOW) == "41s"
    # A stopped worker says a bare `done`: the age it used to carry (`done 6m
    # ago`) was noise next to the name, so `updated_at` is no longer read here.
    assert a.worker_meta({"status": "idle", "updated_at": NOW - 360},
                         "idle", NOW) == "done"
    assert a.worker_meta({}, "blocked", NOW) == "waiting on you"
    # A failure reason is used when the row carries one, and is never invented.
    assert a.worker_meta({"status": "failed"}, "failed", NOW) == "failed"
    assert a.worker_meta({"status": "failed", "error_message": "boot  failed"},
                         "failed", NOW) == "boot failed"
    # No timestamp: a word, not a figure derived from the epoch.
    assert a.worker_meta({}, "working", NOW) == "working"
    assert a.worker_meta({}, "idle", NOW) == "done"


def test_a_finished_worker_says_done_with_no_elapsed_suffix():
    # The reported regression: a stopped worker read `done 6m ago`. It must now
    # read exactly `done`, however long ago it stopped.
    assert a.worker_meta({"status": "idle", "updated_at": NOW - 360},
                         "idle", NOW) == "done"
    assert a.worker_meta({"status": "idle", "updated_at": NOW - 90_000},
                         "idle", NOW) == "done"


def test_a_working_worker_still_counts_up():
    # The live count-up is untouched: a running worker's meta is still its age.
    assert a.worker_meta({"status": "running", "created_at": NOW - 134},
                         "working", NOW) == "2m14s"


# ---------------------------------------------------------------------------
# 10. read-only: the module never calls anything that could write to a session
# ---------------------------------------------------------------------------

def _called_names(source: str) -> set:
    """Every identifier name that is CALLED in `source`, lower-cased."""
    called = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) \
                else getattr(func, "id", "")
            called.add(name.lower())
    return called


def _herdr_call_methods(source: str) -> list:
    """The string method names `source` hands to a `HerdrClient.call(...)`.

    `HerdrClient.call` is the view's only outbound seam, so the set of literal
    first arguments IS the set of requests it can send.

    This scan FAILS CLOSED: anything it cannot read is refused rather than
    skipped, because "we cannot tell what it sends" must never pass as "it sends
    nothing". Three things are refused outright:

      * a `.call(...)` whose method is not a string literal (a variable, an
        f-string, a `+` concatenation, `.format`) — the method is unreadable;
      * the `call` attribute used as a VALUE rather than called directly
        (`send = client.call`, `return client.call`, a method passed into a
        helper). Such an alias is what the original guard walked straight past:
        the call site becomes a bare `send(...)`, a `Name` call no attribute walk
        sees. Following an alias to its call sites needs dataflow this scan does
        NOT do, and this module has no legitimate reason to alias the seam, so it
        is refused — the simple, safe rule;
      * `getattr(<obj>, "call")` (and `__getattribute__("call")`): the seam is
        fetched by string, hiding it from an `Attribute` walk while still
        reaching it.

    THE HONEST BOUNDARY. This is a static walk, not a dataflow analysis, so it
    cannot defeat DELIBERATE indirection: any DYNAMIC DISPATCH to the seam
    escapes — `operator.methodcaller("call", …)`, an aliased `getattr` invoked as
    `alias("call", …)`, a method pulled from a dict or list, `exec`/`eval`, or a
    wrapper function — and it escapes whether or not a literal appears. What the
    walk DOES promise is the shape an accidental edit actually takes: every
    DIRECT and SIMPLY-ALIASED spelling is refused (below), along with the
    dynamic-dispatch entry points this module could never legitimately need,
    which are rejected by mere presence. It does not attempt to out-think
    deliberate indirection; that is the trade, not a gap. See
    `test_the_ast_guard_catches_every_planted_bypass` for the planted cases that
    pin the caught set.
    """
    tree = ast.parse(source)

    # Dynamic dispatch is refused on SIGHT, not by dataflow: this module has no
    # legitimate reason to reach a method by any route but a literal `.call(...)`,
    # and each name below reaches past a static walk rather than through it. The
    # list is deliberately short — a spelling needing type inference to catch
    # belongs in the boundary above, not here.
    called_funcs = {id(node.func) for node in ast.walk(tree)
                    if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        name = node.id if isinstance(node, ast.Name) \
            else node.attr if isinstance(node, ast.Attribute) else None
        if name in ("methodcaller", "exec", "eval"):
            raise AssertionError(
                f"`{name}` reaches a method dynamically; this module never "
                "needs it: " + ast.dump(node))
        if isinstance(node, ast.Name) and node.id == "getattr" \
                and id(node) not in called_funcs:
            raise AssertionError(
                "`getattr` is bound to another name, an alias this scan cannot "
                "follow: " + ast.dump(node))

    directly_called = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    methods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "call":
            args = node.args
            assert args and isinstance(args[0], ast.Constant) \
                and isinstance(args[0].value, str), \
                "a .call(...) must spell its method as a literal: " + ast.dump(node)
            methods.append(args[0].value)
        elif isinstance(node, ast.Attribute) and node.attr == "call" \
                and id(node) not in directly_called:
            raise AssertionError(
                "the `call` seam is used as a value, not called directly; an "
                "alias it could be invoked through cannot be followed: "
                + ast.dump(node))
        elif isinstance(node, ast.Call) \
                and getattr(node.func, "attr", "") == "__getattribute__" \
                and len(node.args) >= 1 and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value == "call":
            raise AssertionError(
                "__getattribute__(\"call\") reaches the outbound seam while "
                "hiding it from this scan: " + ast.dump(node))
        elif isinstance(node, ast.Call) \
                and getattr(node.func, "id", "") == "getattr" \
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                and node.args[1].value == "call":
            raise AssertionError(
                "getattr(..., \"call\") reaches the outbound seam while hiding "
                "it from this scan: " + ast.dump(node))
    return methods


def _assert_only_agent_focus(source: str) -> None:
    """The tightened guard: `agent.focus` is the ONLY method handed to `call`.

    Refuses outright if the seam is aliased, fetched by string, called with a
    method name that is not a literal, or reached by dynamic dispatch (see
    `_herdr_call_methods`).
    """
    methods = _herdr_call_methods(source)
    assert methods, "expected the view to make its one outbound focus call"
    assert set(methods) == {"agent.focus"}, methods


def test_source_only_makes_read_only_calls():
    """AST scan: no CALLED identifier looks like a writer to a session, AND the
    only herdr method this module hands to `call` is `agent.focus`.

    Prose and docstrings are allowed to discuss prompting and elicitations (the
    rationale lives in them); only called names are inspected. A viewer must
    never send a prompt, answer an elicitation, or POST anything.

    The second assertion is the TIGHTENING. The view gained exactly one outbound
    call when Enter was made to focus a worker in herdr, so the guard no longer
    stops at "nothing looks like a writer": it now reads the method name out of
    every `.call(...)` and refuses anything but `agent.focus`. A future edit that
    adds a second outbound request fails HERE, at review time, rather than at the
    operator's terminal.
    """
    source = Path(a.__file__).read_text()
    called = _called_names(source)
    assert called, "expected to find call expressions in the module"
    offenders = [c for c in called
                 if any(bad in c for bad in ("send", "prompt", "resolve",
                                             "elicit", "post"))]
    assert not offenders, offenders
    _assert_only_agent_focus(source)


def test_the_ast_guard_catches_every_planted_bypass():
    """The guard must be able to FAIL — a guard that cannot is decoration.

    Each entry appends one evasion to the real source, standing in for the future
    edit the guard exists to stop, and the tightened assertion must reject every
    one. The first is the obvious spelling; the rest hide a `.call(...)` from a
    bare attribute walk:

      * direct — a second `client.call("workspace.focus", …)`, the spelling the
        old test planted;
      * aliased — `send = client.call; send(...)`: the method is bound to a name,
        so the call site is a plain `Name` call the walk never sees. The scan
        refuses the ALIAS itself (a `call` attribute used as a value) rather than
        trying to follow it, which is exactly why this is caught;
      * non-literal method — a variable, an f-string, a `+` concatenation, and
        `.format`: the name cannot be read, so the scan refuses instead of
        assuming;
      * indirect client — `getattr(client, "call")(…)` and
        `client.__getattribute__("call")(…)`: the seam is fetched by string,
        bypassing the `.call` spelling entirely;
      * dynamic dispatch — `operator.methodcaller("call", …)`, a bare
        `methodcaller`, `exec`, `eval`, and `getattr` bound to another name: none
        of these names the seam directly, so each is refused by its mere
        presence. The guard cannot follow where they point, and does not try.

    THE GUARD'S REAL PROMISE, AND ITS CEILING. This is a static scan, not a
    dataflow analysis, so it catches every DIRECT and SIMPLY-ALIASED spelling —
    the shape an accidental edit actually takes — and does not try to defeat
    DELIBERATE indirection. Any DYNAMIC DISPATCH to the seam escapes it, literal
    or not: `operator.methodcaller("call", …)`, an aliased `getattr` invoked as
    `alias("call", …)`, a method pulled from a dict or list, `exec`/`eval`, a
    wrapper function. The guard refuses everything it can SEE — every literal
    `.call(…)`, every `.call` used as a value, every literal-string `getattr`/
    `__getattribute__`, and the bare dynamic-dispatch entry points this module
    never needs — and an honest boundary beats a false one. A spelling that needs
    type inference to recognise belongs in this paragraph, not in the code.
    """
    source = Path(a.__file__).read_text()
    evasions = {
        "direct": (
            '\n\ndef _planted(client):\n'
            '    return client.call("workspace.focus", {"workspace_id": "w1"})\n'
        ),
        "aliased-method": (
            '\n\ndef _planted(client):\n'
            '    send = client.call\n'
            '    return send("workspace.focus", {"workspace_id": "w1"})\n'
        ),
        "non-literal-name": (
            '\n\ndef _planted(client, method):\n'
            '    return client.call(method, {"workspace_id": "w1"})\n'
        ),
        "f-string-name": (
            '\n\ndef _planted(client, which):\n'
            '    return client.call(f"workspace.{which}", {})\n'
        ),
        "concatenated-name": (
            '\n\ndef _planted(client):\n'
            '    return client.call("workspace." + "focus", {})\n'
        ),
        "format-name": (
            '\n\ndef _planted(client, which):\n'
            '    return client.call("workspace.{}".format(which), {})\n'
        ),
        "indirect-client-getattr": (
            '\n\ndef _planted(client):\n'
            '    send = getattr(client, "call")\n'
            '    return send("workspace.focus", {"workspace_id": "w1"})\n'
        ),
        "indirect-client-dunder": (
            '\n\ndef _planted(client):\n'
            '    return client.__getattribute__("call")("workspace.focus", {})\n'
        ),
        "client-from-elsewhere": (
            '\n\ndef _planted():\n'
            '    c = _make_client()\n'
            '    return c.call("workspace.focus", {"workspace_id": "w1"})\n'
        ),
        "operator-methodcaller": (
            '\n\ndef _planted(client):\n'
            '    import operator\n'
            '    return operator.methodcaller(\n'
            '        "call", "workspace.focus", {})(client)\n'
        ),
        "bare-methodcaller": (
            '\n\ndef _planted(client):\n'
            '    from operator import methodcaller\n'
            '    return methodcaller("call", "workspace.focus", {})(client)\n'
        ),
        "aliased-getattr": (
            '\n\ndef _planted(client):\n'
            '    fetch = getattr\n'
            '    return fetch(client, "call")("workspace.focus", {})\n'
        ),
        "exec": (
            '\n\ndef _planted(client):\n'
            '    return exec("client.call(\'workspace.focus\', {})")\n'
        ),
        "eval": (
            '\n\ndef _planted(client):\n'
            '    return eval("client.call(\'workspace.focus\', {})")\n'
        ),
    }
    missed = []
    for label, planted in evasions.items():
        try:
            _assert_only_agent_focus(source + planted)
        except AssertionError:
            continue
        missed.append(label)
    assert not missed, f"the guard failed to reject: {missed}"


# ---------------------------------------------------------------------------
# 11. no test performs a real network call
# ---------------------------------------------------------------------------

def test_no_test_reaches_the_real_server(monkeypatch):
    # If the module forgot the opener seam, urlopen would reach 127.0.0.1:6767.
    def boom(*args, **kwargs):
        raise AssertionError("a test attempted a real network call")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(w.urllib.request, "urlopen", boom)

    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    model = _model(opener)
    frame = model.refresh()
    assert frame.rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_build_parser_defaults():
    args = a.build_parser().parse_args([])
    assert args.server == a.DEFAULT_BASE_URL
    assert args.all_roots is False
    assert args.once is False
    assert args.interval == a.DEFAULT_INTERVAL


def test_build_parser_overrides():
    args = a.build_parser().parse_args(
        ["--all", "--once", "--server", "http://example:1", "--interval", "1.5"])
    assert args.all_roots and args.once
    assert args.server == "http://example:1"
    assert args.interval == 1.5


def test_main_once_prints_exactly_what_render_returns(monkeypatch, tmp_path, capsys):
    # `--once` has no curses: it prints the lines render hands the painter and
    # nothing else.
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    # The listing fixture carries no timestamps, so the meta lines are clock
    # free and the two frames below are the same frame.
    expected_frame = _model(opener).refresh()
    # The dump is UNTOUCHED, and it has to be computed the way `main` computes it:
    # `refresh` leaves `selected` None and `snap_selection` keeps it None. Passing
    # 0 here would pick the first WORKER instead, and the two frames would still
    # render to identical text — selection is a paint attribute — so the
    # comparison below would pass while pinning the wrong frame.
    assert expected_frame.selected is None, "refresh must not select anything"
    assert a.snap_selection(expected_frame.rows, None,
                            expected_frame.selected) is None
    expected = a.render(expected_frame)

    rc = a.main(["--once", "--all", "--server", "http://x"], opener=opener)

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == expected
    assert out[0].startswith("╭"), "the root's box"
    assert out[1].endswith("Claude Code│"), "the box, with the harness label right"
    assert out[3] == "│", "a breathing rail opens the group"
    for word in OLD_GROUP_LABELS:
        assert word not in out, word
    assert any("╭" in line for line in out), "each worker is a rounded box"
    # `--once` prints TEXT only: selection is a paint attribute now, so a one-shot
    # dump carries no selection signal at all (see `main`).
    assert "▸" not in "\n".join(out)
    assert out[-1] == _footer_text(a.DEFAULT_WIDTH)


def test_main_once_reports_the_missing_tty(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_tty_available", lambda: False)
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})

    assert a.main(["--all", "--server", "http://x"], opener=opener) == 0
    captured = capsys.readouterr()
    assert "rendering one frame instead" in captured.err
    assert "Claude Code" in captured.out


# ---------------------------------------------------------------------------
# 12. the access panel (the `a` show-access toggle)
# ---------------------------------------------------------------------------
#
# The panel is part of `render`'s lines, so everything below is asserted through
# the pure renderer and `--once` — no tty, no curses. `qr_lines` is the only
# impure piece (it shells out to qrencode, a pure text filter) and every test
# that would reach it patches it, so no test spawns anything or touches a
# server.

def _access(qr=(), url="http://192.168.100.176:6767",
            note="same wifi only"):
    return a.Access(url=url, qr=list(qr), note=note)


def test_access_panel_is_off_by_default_and_the_footer_lists_the_binding():
    rows = a.build_rows(
        [listed("root", title="conversation", status="running")], ["root"], now=NOW)

    lines = a.render(a.Frame(rows=rows))

    assert lines[0].startswith("╭") and "conversation" in lines[1]
    assert "┌─ access" not in "\n".join(lines), "the panel starts closed"
    assert a.Frame().access is None, "the toggle starts off"
    assert lines[-1] == _footer_text(a.DEFAULT_WIDTH)
    assert "a access" in a.FOOTER, "the footer must advertise the binding"


def test_the_access_box_fills_the_pane_width_flush_left_and_drops_a_narrow_qr():
    """THE REPORTED BUG (b): the panel was sized to its own content, so it hung as
    a narrow inset under a full-width tree instead of lining up with the root box.
    It is the pane's own bottom panel, so it is the pane's width."""
    qr = ["\x1b[40;37;1m" + "█" * 27 + "\x1b[0m"] + ["\x1b[40;37;1m" + "▀▄" * 13 + "\x1b[0m"]
    acc = _access(qr=qr, note="same wifi only — not reachable outside")

    wide = a.render(a.Frame(rows=[header()], access=acc), width=80)
    box = a.access_lines(acc, 80)
    # The root block is THREE rows, then a blank, then the panel.
    assert "┌─ access " in wide[4], wide[4]
    assert wide[4] == box[0], "flush left: column 0, not under the rail"
    assert box[0].startswith("┌─ access ") and box[0].endswith("┐")
    # PANE-width — the pane's own columns, not the note's — and exactly the root
    # box's width, which is the thing the report compared it to.
    assert len(box[0]) == len(wide[0]) == 80, \
        "as wide as the root box above it, never sized to its content"
    assert all(len(line) == 80 for line in box), \
        "a box whose rows differ in width is not a box"
    # The root box's own interior width is the reference the panel has to agree
    # with: same interior, so the two rectangles line up column for column.
    inner = len(wide[0][1:-1])
    assert box[0] == "┌─ " + a.ACCESS_TITLE + " " \
        + "─" * (inner - len(a.ACCESS_TITLE) - 3) + "┐"
    assert box[-1] == "└" + "─" * inner + "┘"
    assert any("█" * 27 in line for line in box), "27 columns + padding fits"
    assert acc.url in "\n".join(box)

    # The same code in a pane narrower than the code: dropped, address kept
    # (clipped by the pane, which is the only honest way to fit a 27-column URL
    # in 26 columns). The BOX still spans the pane: it is the code that is
    # dropped, never the panel's width.
    narrow = a.access_lines(acc, 30)
    assert not any("█" in line for line in narrow), \
        "a wrapped code is unreadable noise, not a code"
    assert any(line.strip("│ ").startswith("http://192.168.100.176")
               for line in narrow), narrow
    assert narrow[-1].endswith("┘") and narrow[0].startswith("┌─ access ")
    assert all(len(line) == 30 for line in narrow), "still a box, still the pane's width"


def test_the_access_box_spans_the_pane_at_every_width_a_box_can_close_at():
    """(b) pinned positively and at every width, not just the one the report used.

    The panel must never draw a row wider than the pane (a border past the edge
    breaks the row it is on) and never a narrower one (that is the inset the
    report is about). Where the pane is too narrow for the title's rule, the
    TITLE is dropped -- the picker's rule, same reason -- rather than the box
    growing past the pane to hold it.
    """
    acc = _access(qr=["QRROW"])
    row = header()

    for width in range(a.BOX_MIN, 120):
        box = a.access_lines(acc, width)
        assert all(len(line) == width for line in box), (width, box)
        assert box[0].startswith("┌") and box[0].endswith("┐"), (width, box[0])
        assert box[-1].startswith("└") and box[-1].endswith("┘"), (width, box[-1])
        # The root box is the reference the panel is supposed to match.
        assert a._spans_width(a._root_lines(row, width)[0]) == width, width

    # Wide enough for the title, so it is there; the drop is only for panes that
    # cannot hold its rule.
    assert a.access_lines(acc, 80)[0].startswith("┌─ " + a.ACCESS_TITLE + " ")
    assert not a.access_lines(acc, len(a.ACCESS_TITLE) + 4)[0].startswith("┌─ ")


def test_the_access_panel_paints_at_full_brightness_not_the_footer_dim():
    """THE REPORTED BUG (a). The panel was the one box hard-hung DIM, as if it were
    chrome like the footer -- so the URL and the code an operator opened it to READ
    were the least readable thing in the pane, while the root box beside it painted
    NORMAL. Nothing asserted this, which is how the dim shipped.

    The role is the contract, not a colour: the panel must hang the SAME NORMAL
    that `_blocks` hangs on the root box's lines -- so the two boxes read alike,
    which is what the report asked for.
    """
    acc = _access(qr=["QRROW"])
    panel = a.access_lines(acc, 80)

    pane = a._pane(a.Frame(rows=[header()], access=acc), 80)
    tail = pane.items[pane.list_rows:]
    rows = tail[1:1 + len(panel)]
    assert rows and [a.line_text(item) for item in rows] == panel, \
        "the panel is the tail, straight after its blank row"
    assert [role for _t, role in rows] == [a.NORMAL] * len(panel), \
        "the panel is not chrome: every row of it paints NORMAL, not DIM"
    assert pane.items[0][1] == a.NORMAL, \
        "and that is the very brightness the root box paints at"
    # The footer remains the pane's one dim line -- the fix is the panel, not the
    # brightness axis.
    assert tail[-1][1] == a.DIM

    # A moved caret dims the UNSELECTED WORKERS. It never reaches the panel, which
    # is not a worker and is not what the caret is on.
    moved = a._pane(a.Frame(rows=[header(),
                                  worker("w1", "idle", text="one"),
                                  worker("w2", "idle", text="two")],
                            access=acc, selected=1), 80)
    assert any(role == a.DIM for _t, role in moved.items[:moved.list_rows]), \
        "an unselected worker's box still dims"
    moved_tail = moved.items[moved.list_rows:]
    assert [role for _t, role in moved_tail[1:1 + len(panel)]] == [a.NORMAL] * len(panel)


def test_the_access_panel_colours_its_parts_by_role():
    """PART 2. The panel's parts are named by ROLE, and a role is a THEME slot —
    never a hex, so a theme decides every colour. The frame (the title rule and
    both borders) takes `border`, which names the `rail` slot: the slot `ROLES`
    documents as "group rails and box borders", which no role reached until now.
    The URL and the QR take `text` (PLAIN), the caveat takes `meta`."""
    acc = _access(qr=["QRROW"], url="http://192.168.100.176:6767",
                  note="same wifi only")
    rows = a.access_spans(acc, 80)

    def role_of(needle):
        """The set of roles carried by rows whose text contains `needle`."""
        return {role for text, role in rows if needle in text}

    # The frame is `border`, top and bottom, and `border` IS the rail slot.
    assert rows[0][1] == rows[-1][1] == a.BORDER
    assert a._theme_role(a.BORDER) == "rail"
    # The title rides the top rule, so it takes the frame's role with it — the
    # per-row trade: one span list per row, no per-span split for a title.
    assert rows[0][0].startswith("┌─ " + a.ACCESS_TITLE + " ")
    assert role_of(a.ACCESS_TITLE) == {a.BORDER}

    # The URL is the panel's content, in the theme's `text` slot.
    assert role_of("http://192.168.100.176:6767") == {a.PLAIN}
    assert a._theme_role(a.PLAIN) == "text"
    # ...the caveat is secondary text...
    assert role_of("same wifi only") == {a.META}
    assert a._theme_role(a.META) == "meta"
    # ...and the QR is PLAIN (the `text` slot), ONE span for the whole row: its
    # modules are never coloured individually, because a QR has to stay
    # high-contrast to scan and a per-module colour is how you break that.
    assert role_of("QRROW") == {a.PLAIN}


def test_an_addressless_panel_puts_its_one_line_on_the_meta_role():
    """The no-address panel is a box around one honest caveat, and the caveat is
    META like every other caveat — the frame is the only other thing on it."""
    acc = _access(url=None, note=a.NO_ADDRESS_NOTE)
    rows = a.access_spans(acc, 80)

    assert len(rows) == 3, "a box around one line"
    assert rows[0][1] == rows[-1][1] == a.BORDER
    assert a.NO_ADDRESS_NOTE in rows[1][0] and rows[1][1] == a.META


def test_the_access_panel_keeps_its_text_full_width_and_normal_brightness():
    """The MUST-NOT-REGRESS pair, re-checked through the role-carrying path: each
    row still joins to the same TEXT `access_lines` returns, still spans the pane,
    and still hangs NORMAL — never the footer's DIM. The access box was just made
    full-width and bright on this branch; colouring its parts must not undo that."""
    acc = _access(qr=["QRROW"])
    spans = a.access_spans(acc, 80)
    assert [text for text, _role in spans] == a.access_lines(acc, 80)
    assert all(len(text) == 80 for text, _role in spans), "still pane-width"

    pane = a._pane(a.Frame(rows=[header()], access=acc), 80)
    panel = a.access_lines(acc, 80)
    rows = pane.items[pane.list_rows:][1:1 + len(panel)]
    assert [role for _spans, role in rows] == [a.NORMAL] * len(panel), \
        "the panel is not chrome: every row paints NORMAL, not DIM"
    assert pane.items[-1][1] == a.DIM, "the footer is still the one dim line"


def test_the_access_panel_shows_the_qr_and_url_when_wide_enough():
    qr = ["\x1b[40;37;1m" + "█" * 27 + "\x1b[0m"]
    acc = _access(qr=qr, url="http://192.168.100.176:6767")

    lines = a.render(a.Frame(access=acc), width=80)

    assert any("█" * 27 in line for line in lines)
    assert "http://192.168.100.176:6767" in "\n".join(lines)
    assert lines[-2] == "" and lines[-1] == _footer_text(80), "blank, then the footer"


def test_access_panel_drops_the_qr_when_the_pane_is_too_narrow():
    qr = ["\x1b[40;37;1m" + "█" * 27 + "\x1b[0m"]
    acc = _access(qr=qr, url="http://192.168.100.176:6767")

    lines = a.render(a.Frame(access=acc), width=26)

    assert not any("█" in line for line in lines), \
        "a wrapped QR is unreadable noise, not a code"
    # Nothing is lost but the code: a blank, then the address and its caveat,
    # boxed. The address itself is as wide as the code was, so a pane too narrow
    # for the QR is also too narrow for the whole URL — it keeps every column it
    # has and says where it stopped.
    assert any(line.strip("│ ").startswith("http://192.168.100.17")
               for line in lines)
    # 26 columns is below the bare KEYS floor (32), so the footer picks the bare
    # key list rather than either labelled form — and that list is itself wider
    # than this pane, so it is clipped to 26, losing a couple of trailing keys.
    # That is the FLOOR form's point: a trailing KEY clipped off is recoverable by
    # pressing it, where a LABEL clipped mid-word is not. See the footer comment.
    assert lines[0] == "" and lines[-1] == _footer_text(26)
    # The row fills the pane (clipped to its last column); the footer glyphs stay
    # Narrow so the pane's own ruler is unambiguous. See
    # `test_the_footer_is_measured_the_same_in_a_cjk_locale`.
    assert len(lines[-1]) <= 26
    assert len(a.access_lines(acc, 26)) == 4, "borders, address, caveat, borders"


def test_access_panel_survives_a_missing_qrencode(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a.shutil, "which", lambda name: None)
    monkeypatch.setattr(a, "_lan_ip", lambda: "192.168.100.176")

    assert a.qr_lines("http://192.168.100.176:6767") == []
    acc = a.access_info("http://127.0.0.1:6767")
    assert acc.url == "http://192.168.100.176:6767"
    assert acc.qr == []
    lines = a.render(a.Frame(access=acc), width=80)
    assert "http://192.168.100.176:6767" in "\n".join(lines)


def test_access_info_prefers_the_tunnel_cache(tmp_path, monkeypatch):
    # A cache is trusted only while its owning tunnel is alive, so the live pid
    # (this test process's own) is part of the fixture, not decoration.
    _write_tunnel_cache(tmp_path, pid=os.getpid())
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_lan_ip", lambda: "192.168.100.176")
    monkeypatch.setattr(a, "qr_lines", lambda url: [f"QR {url}"])

    acc = a.access_info("http://127.0.0.1:6767")

    assert acc.url == "https://abc.ngrok-free.app"
    assert acc.qr == ["QR https://abc.ngrok-free.app"]
    assert acc.note == a.TUNNEL_NOTE
    assert "public" in acc.note


def test_access_info_uses_the_server_port_for_the_lan_address(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_lan_ip", lambda: "192.168.100.176")
    monkeypatch.setattr(a, "qr_lines", lambda url: [])

    acc = a.access_info("http://127.0.0.1:9999")

    assert acc.url == "http://192.168.100.176:9999"
    assert acc.note == a.LAN_NOTE


def test_access_info_names_no_address_when_nothing_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_lan_ip", lambda: None)

    acc = a.access_info("http://127.0.0.1:6767")

    assert acc.url is None
    lines = a.access_lines(acc, 80)
    assert len(lines) == 3, "a box around one honest line, not a fabricated address"
    assert "no address" in lines[1]
    assert "http" not in lines[1]


def test_access_panel_height_is_content_sized():
    qr = [f"qr-row-{i}" for i in range(13)]
    acc = _access(qr=qr)
    rows = [a.Row("root", "conversation", "working", 0, is_root=True)]

    lines = a.render(a.Frame(rows=rows, access=acc), width=80)
    panel = a.access_lines(acc, 80)

    # The root block (a three-row box), then a blank row, then the panel, then
    # the footer. Not a fraction of the pane, not a padded block.
    assert lines[0].startswith("╭") and "conversation" in lines[1]
    assert lines[2].startswith("╰"), "the root box closes before the panel"
    assert lines[3] == "", "a blank row separates the list from the panel"
    assert lines[4:4 + len(panel)] == panel
    assert len(panel) == 2 + 13 + 1 + 2, "2 borders + 13 QR + blank + URL + note"
    assert lines[-1] == _footer_text(80)


def test_the_list_keeps_a_floor_of_rows_and_the_panel_is_never_squeezed():
    qr = [f"qr-row-{i}" for i in range(13)]   # a 19-row panel: 13 QR + blank + 2 info
    acc = _access(qr=qr)
    # The root block (3 rows) plus one-row rail blocks, so the floor is visible.
    rows = [header()] + [rail() for _ in range(6)]

    pane = a._pane(a.Frame(rows=rows, access=acc), width=80, height=8)

    # The tail (blank + panel + footer) is reserved whole and the list above it
    # keeps at least ACCESS_LIST_FLOOR rows.
    assert pane.list_rows == a.ACCESS_LIST_FLOOR
    # ...and the panel is still shown whole rather than squeezed away.
    assert any("qr-row-0" in _line_text(item) for item in pane.items)
    # A block is never cut: the kept list is the root's three rows, whole.
    assert _line_text(pane.items[0]).startswith("╭") \
        and _line_text(pane.items[2]).startswith("╰")
    assert all(brightness == a.NORMAL for _spans, brightness in pane.items[:3])


def test_qr_fit_measures_visible_columns_not_raw_length():
    # The exact bug: ANSIUTF8 escapes made len() read 41 columns for a 27-column
    # code, so a code that fits was judged not to and the panel silently
    # degraded to URL-only forever.
    escaped = "\x1b[40;37;1m" + "█" * 27 + "\x1b[0m"
    assert a._visible_width(escaped) == 27
    assert len(escaped) > 27

    acc = _access(qr=[escaped])

    assert any("█" * 27 in line for line in a.access_lines(acc, 27 + a.BOX_PADDING))
    assert not any("█" in line for line in
                   a.access_lines(acc, 27 + a.BOX_PADDING - 1))


def test_build_parser_has_the_access_flag():
    assert a.build_parser().parse_args([]).access is False
    assert a.build_parser().parse_args(["--access"]).access is True


def test_main_once_with_access_renders_the_panel(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(
        a, "access_info",
        lambda server: a.Access(url="http://192.168.100.176:6767",
                                qr=["QRROW"], note=a.LAN_NOTE))
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})

    rc = a.main(["--once", "--all", "--access", "--server", "http://x"],
                opener=opener)

    out = capsys.readouterr().out
    assert rc == 0
    assert "┌─ access " in out and "QRROW" in out
    assert "http://192.168.100.176:6767" in out


# ---------------------------------------------------------------------------
# 13. bug: the QR's ANSI escapes must never reach the painter
# ---------------------------------------------------------------------------

def test_no_line_handed_to_the_painter_contains_an_escape():
    """curses does not interpret escapes, it PRINTS them.

    `qrencode -t ANSIUTF8` colours every line `40;37` and terminates it, and the
    operator's pane showed a literal `^[[40;37;1m` down both sides of the code.
    `--once` looked fine only because the terminal interpreted them. The
    invariant is not "the QR renders" — it is that no byte of ESC ever reaches
    `addnstr` — so this asserts on every line render produces, with a QR that
    still carries its escapes in.
    """
    qr = ["\x1b[40;37;1m" + "█" * 27 + "\x1b[0m"] * 3
    acc = _access(qr=qr, note=a.LAN_NOTE)
    rows = [header(), worker("w1", "working")]

    # The plain path first: these are the lines `addnstr` is handed.
    frame = a.Frame(rows=rows, access=acc)
    assert not any("\x1b" in text for text in a.render(frame, width=80))
    # A moved caret cannot change that.
    frame.selected = 1
    assert not any("\x1b" in text for text in a.render(frame, width=80))
    assert not any("\x1b" in _line_text(item) for item in a._pane(frame, 80).items)
    # ...and the block characters survived, so the code still draws.
    assert any("█" * 27 in text for text in a.render(a.Frame(access=acc), width=80))


def test_qr_lines_strips_the_escapes_at_the_source(monkeypatch):
    # The panel is not the only thing that can be handed a QR: this is where one
    # is born, so the escapes are removed here rather than relied on being
    # removed downstream.
    raw = "\x1b[40;37;1m" + "█" * 27 + "\x1b[0m\n\x1b[40;37;1m" + "▀▄" + "\x1b[0m\n"
    monkeypatch.setattr(a.shutil, "which", lambda name: "/usr/bin/qrencode")
    monkeypatch.setattr(
        a.subprocess, "run",
        lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout=raw))

    lines = a.qr_lines("http://192.168.100.176:6767")

    assert lines == ["█" * 27, "▀▄"]
    assert not any("\x1b" in line for line in lines)


# ---------------------------------------------------------------------------
# 14. the tunnel cache is validated, never trusted, and never cleaned up
# ---------------------------------------------------------------------------
#
# A dead tunnel URL is the worst address to show: it looks authoritative and a
# phone that scans it gets nothing. `access_info` therefore mirrors bin/og's
# `tunnel_url` — provider recognised AND its process alive — before it prefers
# the cache. The difference from bin/og is that a viewer must NOT clear the
# stale file; see `test_access_info_never_modifies_or_deletes_the_tunnel_cache`.

def _write_tunnel_cache(home, url="https://abc.ngrok-free.app",
                        provider="ngrok", pid=None):
    """Write the three cached files bin/og's cmd_start writes, in its order.

    `pid=None` uses this test process's own pid, so a "live" cache needs no
    subprocess. Returns the paths written.
    """
    url_file = home / "og-tunnel.url"
    provider_file = home / "og-tunnel.provider"
    pid_file = home / f"og-tunnel-{provider}.pid"
    url_file.write_text(url + "\n")
    provider_file.write_text(provider + "\n")
    pid_file.write_text(f"{os.getpid() if pid is None else pid}\n")
    return url_file, provider_file, pid_file


def _a_dead_pid() -> int:
    """A pid the kernel reports as dead, probed rather than assumed.

    A pid above the platform's maximum resolves to ESRCH (verified on this
    machine), so the probe returns on its first candidate; scanning instead of
    hardcoding keeps the fixture honest if that ever changes.
    """
    for pid in range(2**31 - 1, 2**31 - 200, -1):
        try:
            os.kill(pid, 0)
        except OSError:
            return pid
    pytest.fail("could not find a dead pid")


def test_access_info_ignores_a_cache_whose_tunnel_pid_is_dead(
        tmp_path, monkeypatch):
    _write_tunnel_cache(tmp_path, pid=_a_dead_pid())
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_lan_ip", lambda: "192.168.100.176")
    monkeypatch.setattr(a, "qr_lines", lambda url: [])

    acc = a.access_info("http://127.0.0.1:6767")

    assert acc.url == "http://192.168.100.176:6767", acc.url
    assert acc.note == a.LAN_NOTE


def test_access_info_ignores_a_cache_with_an_unrecognised_provider(
        tmp_path, monkeypatch):
    # A provider og does not know is gate two; a missing provider record (a
    # tunnel written by an og older than the provider file) is the same result.
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_lan_ip", lambda: "192.168.100.176")
    monkeypatch.setattr(a, "qr_lines", lambda url: [])

    _write_tunnel_cache(tmp_path, provider="wireguard")
    assert a.access_info("http://127.0.0.1:6767").url == \
        "http://192.168.100.176:6767"

    (tmp_path / "og-tunnel.provider").unlink()
    assert a.access_info("http://127.0.0.1:6767").url == \
        "http://192.168.100.176:6767"


def test_access_info_ignores_a_cache_with_a_missing_pidfile(
        tmp_path, monkeypatch):
    _write_tunnel_cache(tmp_path)
    (tmp_path / "og-tunnel-ngrok.pid").unlink()
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_lan_ip", lambda: "192.168.100.176")
    monkeypatch.setattr(a, "qr_lines", lambda url: [])

    assert a.access_info("http://127.0.0.1:6767").url == \
        "http://192.168.100.176:6767"


def test_access_info_accepts_a_cache_whose_tunnel_is_alive(
        tmp_path, monkeypatch):
    # The live pid is this test process's own: nothing is spawned.
    _write_tunnel_cache(tmp_path, pid=os.getpid())
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_lan_ip", lambda: "192.168.100.176")
    monkeypatch.setattr(a, "qr_lines", lambda url: [f"QR {url}"])

    acc = a.access_info("http://127.0.0.1:6767")

    assert acc.url == "https://abc.ngrok-free.app"
    assert acc.qr == ["QR https://abc.ngrok-free.app"]
    assert acc.note == a.TUNNEL_NOTE


def test_access_info_ignores_an_empty_or_missing_url_file(
        tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_lan_ip", lambda: "192.168.100.176")
    monkeypatch.setattr(a, "qr_lines", lambda url: [])

    # Nothing at all.
    assert a.access_info("http://127.0.0.1:6767").url == \
        "http://192.168.100.176:6767"

    # A URL file that exists but holds no address, beside an otherwise valid
    # live cache: the first gate still fails.
    _write_tunnel_cache(tmp_path, url="")
    assert a.access_info("http://127.0.0.1:6767").url == \
        "http://192.168.100.176:6767"


def test_access_info_never_modifies_or_deletes_the_tunnel_cache(
        tmp_path, monkeypatch):
    """The read-only property: a stale cache is ignored, not cleaned up.

    Clearing is `og`'s job — it owns the tunnel lifecycle and clears the cache
    on the next start or `tunnel_url` call. A viewer deleting the operator's
    state file is the wrong behaviour the AST guard exists to prevent, so this
    pins the bytes and the existence of every cache file after each access.
    """
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "_lan_ip", lambda: "192.168.100.176")
    monkeypatch.setattr(a, "qr_lines", lambda url: [])

    for provider, pid in (
        ("ngrok", _a_dead_pid()),      # recognised, dead  -> ignored
        ("tunnl", _a_dead_pid()),      # the other provider, dead
        ("wireguard", os.getpid()),    # unrecognised       -> ignored
        ("ngrok", os.getpid()),        # live               -> used
    ):
        _write_tunnel_cache(tmp_path, provider=provider, pid=pid)
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        a.access_info("http://127.0.0.1:6767")
        after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        assert after == before, f"the cache was touched for {provider!r}"
        assert (tmp_path / "og-tunnel.url").exists()


# ---------------------------------------------------------------------------
# 14. the curses painter: full-width writes and a single footer
# ---------------------------------------------------------------------------
#
# Both defects lived BELOW the pure renderer — in the paint loop, at the seam
# between a composed `Pane` and `addnstr` — so no pure-`render` test could see
# them. These drive the real `_run_tui` loop with a fake window that records
# every `addnstr`, which is the smallest seam that reaches the painter.

class _FakeStdscr:
    """A curses window that records what the painter hands it.

    `getch` answers `q` at once, so the loop paints exactly one frame and
    returns; `erase` clears `writes` the way it clears the screen, so what is
    left is one frame's worth of writes. A line is written as ONE `addnstr` per
    span, so a write records its column too — reassemble with `_rows_to_lines`.
    """

    def __init__(self, height, width):
        self.height, self.width = height, width
        self.writes: list[tuple[int, int, str, int]] = []

    def keypad(self, flag): pass
    def getmaxyx(self): return (self.height, self.width)
    def erase(self): self.writes.clear()
    def refresh(self): pass
    def timeout(self, ms): pass
    def getch(self): return ord("q")

    def addnstr(self, row, col, text, n, attr):
        self.writes.append((row, col, text[:n], n))


def _rows_to_lines(writes):
    """Reassemble each painted row from its (possibly several) span writes.

    The painter writes one `addnstr` per span, at the span's cumulative column, so
    a row is recovered by sorting its writes by column and joining their text.
    """
    by_row: dict[int, list] = {}
    for row, col, text, _n in writes:
        by_row.setdefault(row, []).append((col, text))
    return {row: "".join(text for _col, text in sorted(parts))
            for row, parts in by_row.items()}


class _StubModel:
    """A model whose refresh always returns the same frame."""

    def __init__(self, frame):
        self._frame = frame

    def refresh(self):
        return self._frame


def _paint(frame, width, height, monkeypatch):
    """Run one real paint of `frame` and return the `addnstr` writes."""
    import curses

    scr = _FakeStdscr(height, width)
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **k: fn(scr))
    # The `og` theme explicitly, not "whatever is configured": these tests are
    # about paint GEOMETRY and a developer's $OG_AGENTS_THEME or a persisted
    # choice must not change what they assert.
    a._run_tui(_StubModel(frame), 3.0, "http://x", theme=_theme("og"))
    return scr.writes


class _EdgeStdscr:
    """A curses window that refuses ONLY its bottom-right cell, like the real one.

    `addnstr` on the last line's last column is the single write curses rejects
    with `curses.error`; `paint_line` must swallow exactly that and nothing else,
    writing every other row at the full width.
    """

    def __init__(self, height, width, error):
        self.height, self.width, self.error = height, width, error
        self.writes: list[tuple[int, int, str, int]] = []

    def addnstr(self, row, col, text, n, attr):
        if row == self.height - 1 and col + n >= self.width:
            raise self.error("addnstr: bottom-right cell")
        self.writes.append((row, col, text[:n], n))


def test_paint_line_writes_the_full_width_and_swallows_the_last_cell_alone():
    """The writer's own contract: `width`, one tolerant cell, and no narrowing.

    Every box line is exactly `width` columns and its closing border lives in the
    last one, so `paint_line` has to write at full width. The window's
    bottom-right cell is the one cell curses genuinely refuses; swallowing that
    exactly (and no more) is what replaces the old blanket `width - 1`. A span
    list of ONE PLAIN run coalesces to a single write, exactly as before.
    """
    import curses

    scr = _EdgeStdscr(5, 10, curses.error)

    def plain(_role):
        return 0

    # A normal row is written at the FULL width.
    a.paint_line(scr, 0, [("╭────────╮", a.PLAIN)], 10, 5, plain)
    assert scr.writes == [(0, 0, "╭────────╮", 10)], scr.writes

    # The bottom-right cell is attempted at full width and refused — swallowed,
    # never raised, and never pre-empted by writing a column short.
    a.paint_line(scr, 4, [("╰────────╯", a.PLAIN)], 10, 5, plain)

    # Out of range or a zero width is a no-op, not an error.
    a.paint_line(scr, 9, [("x", a.PLAIN)], 10, 5, plain)
    a.paint_line(scr, 0, [("x", a.PLAIN)], 0, 5, plain)
    assert scr.writes == [(0, 0, "╭────────╮", 10)], scr.writes


def test_paint_line_writes_each_span_at_its_cumulative_column_and_never_overflow():
    """Spans are written at their own columns, and no write ever exceeds `width`.

    At ANY width >= 1 the painter is the last line of defence: a span that would
    start at or past `width` is skipped, and a span is column-clipped as it is
    written. This drives the painter directly (no loop) so the guarantee is
    checked for every width, including the pathological one-column pane.
    """
    scr = _EdgeStdscr(5, 40, Exception)
    spans = [("ab", a.PLAIN), ("XY", "mark:anthropic"), ("cd", a.META)]
    for width in range(1, 12):
        scr.writes.clear()
        a.paint_line(scr, 0, spans, width, 5, lambda role: 0)
        for _row, col, text, n in scr.writes:
            assert col >= 0 and col + n <= width + 0, (width, col, n)
            assert col + len(text) <= width, (width, col, text)
        joined = "".join(text for _r, _c, text, _n in sorted(scr.writes))
        assert len(joined) <= width, (width, joined)


def test_the_painter_writes_a_boxes_full_width_so_its_border_survives(
        monkeypatch):
    """A box closes in the pane's last column; `width - 1` clipped it off.

    A line is now written span by span, so the writes for a row are reassembled
    before measuring — which is also the check that the spans JOIN to the pane
    width. The old writer passed `width - 1` to `addnstr`, so every row lost its
    last column — exactly the `╮`, `│` and `╯` of a box's right edge — and every
    box rendered open on the right.
    """
    frame = a.Frame(rows=[worker("w1", "working")], selected=0)
    writes = _paint(frame, 60, 20, monkeypatch)
    lines = _rows_to_lines(writes)

    # A worker draws a guttered, rounded box; its rows are the reassembled lines
    # that begin with the group rail or the node, and a collapsed worker's box is
    # three of them (top border, one content row, bottom border).
    box = [text for text in lines.values()
           if text.startswith(a.RAIL) or text.startswith(a.NODE)]
    assert len(box) == 3, "one rounded box: top, content, bottom"
    assert all(len(line) == 60 for line in box), "each box row is the pane width"
    assert box[0].endswith(a.ROUND_CORNERS[2]), "the top rule closes on the right"
    assert box[-1].endswith(a.ROUND_CORNERS[5]), "the bottom rule closes on the right"
    # No row is written past the pane, and every write is clipped to its columns.
    assert all(len(text) <= 60 for text in lines.values())
    assert all(col + n <= 60 for _row, col, _text, n in writes)


def test_a_short_list_paints_its_footer_once(monkeypatch):
    """With the list shorter than the window the footer was painted twice.

    The window ran over the whole of `items`, tail included, and landed on the
    footer just above where the pinned tail drew it again — two footers, one
    under the last box and one at the pane bottom.

    The footer is SEVERAL span writes on ONE row now (each key, each label, the
    `∙` between them and the strip's padding each get their own write), so "once"
    is counted per ROW: the row whose reassembled text is the footer form must be
    exactly one, and it must be the pane's bottom row.
    """
    frame = a.Frame(rows=[header(), worker("w1", "working")], selected=1)
    writes = _paint(frame, 60, 24, monkeypatch)

    lines = _rows_to_lines(writes)
    footers = [row for row, text in lines.items()
               if text.rstrip() == a._footer(60)]
    assert footers == [23], "the footer is painted exactly once, not twice"


def test_a_long_list_keeps_one_footer_pinned_at_the_bottom(monkeypatch):
    """A list that overflows the window still shows exactly one footer.

    Here the window stops before the tail, so the pinned one is the only one —
    the regression guard that the short-list fix did not drop the tail.
    """
    rows = [header()] + [worker(f"w{i}", "working") for i in range(16)]
    frame = a.Frame(rows=rows, selected=1)
    writes = _paint(frame, 60, 12, monkeypatch)

    lines = _rows_to_lines(writes)
    footers = [row for row, text in lines.items()
               if text.rstrip() == a._footer(60)]
    assert footers == [11], "and stays pinned to the bottom while the list scrolls"


# ---------------------------------------------------------------------------
# 15. the pane has no background of its own
# ---------------------------------------------------------------------------
#
# `_run_tui` imports curses into the function, so a stub in sys.modules IS the
# curses it sees — no tty, and no real screen buffer to assert on. The stub
# hands back one frame's worth of keys (`q`), which is the first key the view
# reads, so the loop paints once and exits: what is asserted is the call it
# makes at startup and that it survives that call failing.

class FakeCurses(types.ModuleType):
    """A curses stand-in that records `use_default_colors` and colour pairs.

    `refuse=True` makes `use_default_colors` raise the way a terminal with no
    colour support does — the call has to stay survivable, because a view that
    cannot start colour still has to paint.

    `colors` and `color_pairs` stand in for `curses.COLORS` / `COLOR_PAIRS`, so a
    test can drive a monochrome terminal (`colors=0`, the default — `has_colors()`
    is then False), an 8-colour one, or a pair table too small to hold every role.
    `init_pair` records every allocation, so a test can see WHICH colour a role
    spent and that nothing was allocated when it should not have been.
    """

    def __init__(self, refuse=False, colors=0, color_pairs=256):
        super().__init__("curses")
        self.refuse = refuse
        self.use_default_colors_calls = 0
        self.COLORS = colors
        self.COLOR_PAIRS = color_pairs
        self.init_pair_calls: list[tuple[int, int, int]] = []
        # `curses.error` is a real exception class the view catches by name, so
        # it must be a class here too, not an instance or a sentinel.
        self.error = type("error", (Exception,), {})
        self.A_NORMAL, self.A_REVERSE, self.A_BOLD, self.A_DIM = \
            0, 1 << 18, 1 << 20, 1 << 19
        self.KEY_UP, self.KEY_DOWN, self.KEY_ENTER = 259, 258, 343
        self.curs_set = lambda n: None
        self.timeout = lambda n: None

    def use_default_colors(self):
        self.use_default_colors_calls += 1
        if self.refuse:
            raise self.error("use_default_colors: ERR")

    def has_colors(self):
        return self.COLORS > 0

    def init_pair(self, n, fg, bg):
        self.init_pair_calls.append((n, fg, bg))

    def color_pair(self, n):
        return n << 8

    def wrapper(self, loop):
        loop(FakeScreen())


class FakeScreen:
    """A stdscr that answers one paint and then hands back `q`.

    Written as a plain class rather than a stub with attribute records: the
    view's only contract with the window is the method set below, and a screen
    that records its own calls would assert less about the fix than the call it
    exists to make possible.

    THE FAKE IS NOT ALLOWED TO BE EASIER TO SATISFY THAN THE REAL OBJECT. Every
    curses method here is a real METHOD, and `__setattr__` refuses to let a test
    rebind one — which is exactly what `_curses.window` does. That guard is not
    decoration: `bkgd` used to be a plain attribute here, so `stdscr.bkgd = attr`
    succeeded in every test and raised `AttributeError` on every real terminal,
    and the blanket `except Exception` around it swallowed the raise. 1213 tests
    passed over code that could not run.
    """

    def __init__(self, height=24, width=80):
        self.height, self.width = height, width
        self.painted: list[str] = []
        self.keys = [ord("q")]
        # Every `(name, ch, attr)` the view handed to the window background, in
        # order. An EMPTY list is meaningful rather than "unset": setting it is
        # half the background decision, and the `og` theme exists to assert it
        # does NOT happen.
        self.bkgd_calls: list[tuple[str, str, int]] = []

    def __setattr__(self, name, value):
        # `curses.window` is a C type with `__dictoffset__ == 0` — an instance
        # carries no dict, so a name that is a method on the class cannot be
        # rebound on the instance and Python raises
        # `AttributeError: ... object attribute 'bkgd' is read-only`. Mirroring
        # that here means an assignment fails in the test exactly where it fails
        # in curses, instead of passing here and failing on the operator's
        # terminal. Keyed on the CLASS, so every screen fake inherits the guard.
        if name in _screen_method_names(type(self)):
            raise AttributeError("%r object attribute %r is read-only"
                                 % (type(self).__name__, name))
        object.__setattr__(self, name, value)

    def keypad(self, flag):
        pass

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        pass

    def addnstr(self, row, col, text, n, attr):
        self.painted.append(text)

    def refresh(self):
        pass

    def timeout(self, ms):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else -1

    # The two spellings the window background can be set with, recorded the same
    # way so a test asserts on the CALL rather than on which name was used.
    # `bkgd` is here as well as `bkgdset` because the view is allowed to pick:
    # these are records, not a pin on the choice.
    def bkgd(self, ch, attr):
        self.bkgd_calls.append(("bkgd", ch, attr))

    def bkgdset(self, ch, attr):
        self.bkgd_calls.append(("bkgdset", ch, attr))


_SCREEN_METHOD_NAMES: dict = {}


def _screen_method_names(cls) -> frozenset:
    """Every public name on `cls` (or a base) that binds a method.

    The set curses makes unassignable. Cached per class because it is walked on
    every attribute assignment the fake receives.
    """
    names = _SCREEN_METHOD_NAMES.get(cls)
    if names is None:
        names = frozenset(
            name
            for klass in cls.__mro__
            for name, value in vars(klass).items()
            if not name.startswith("_") and callable(value))
        _SCREEN_METHOD_NAMES[cls] = names
    return names


def _fake_curses(monkeypatch, refuse=False, colors=0, color_pairs=256):
    fake = FakeCurses(refuse=refuse, colors=colors, color_pairs=color_pairs)
    monkeypatch.setitem(sys.modules, "curses", fake)
    return fake


def _tui_model():
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    return _model(opener)


def test_the_view_takes_the_terminals_own_colours_so_the_pane_is_transparent(
        monkeypatch):
    # `curses.wrapper` calls start_color() before `loop` runs, and that is what
    # made every cell this view paints opaque black: pair 0 is the curses
    # default, not the terminal's. Taking the default for pair 0 is the whole
    # fix. Every pair the view then spends uses a `-1` background (`_Colours`),
    # so the colour tints the glyph and leaves pair 0's background intact — this
    # is a MONOCHROME stub, so nothing is spent here and the call is all there is.
    fake = _fake_curses(monkeypatch)

    assert a._run_tui(_tui_model(), a.DEFAULT_INTERVAL, "http://x") == 0

    assert fake.use_default_colors_calls == 1, \
        "pair 0 must be the terminal's own colours, once, at startup"
    assert fake.init_pair_calls == [], "a monochrome terminal spends no pair"


def test_the_view_still_paints_when_use_default_colors_raises(monkeypatch):
    # A terminal that cannot take the default colours is not a reason to leave
    # the operator with no view: the pane runs uncoloured instead.
    fake = _fake_curses(monkeypatch, refuse=True)
    screens: list[FakeScreen] = []

    def wrapper(loop):
        screens.append(FakeScreen())
        loop(screens[-1])
    fake.wrapper = wrapper

    assert a._run_tui(_tui_model(), a.DEFAULT_INTERVAL, "http://x") == 0

    assert fake.use_default_colors_calls == 1
    assert screens and screens[0].painted, "the frame was painted, not skipped"


# ---------------------------------------------------------------------------
# 15b. the colour roles, and how the painter spends them
# ---------------------------------------------------------------------------
#
# Colour is a SPAN's role, decided by the pure composer; the painter turns it
# into a curses attribute composed with the line's brightness. These tests split
# the two: the first group checks the ROLES the composer hangs (pure, no curses),
# the second drives `_run_tui` with a fake curses that can be monochrome, 8-colour
# or pair-starved, to check the plumbing spends the right pair and degrades.

class _WriteScreen(FakeScreen):
    """`FakeScreen` that also records `(row, col, text, attr)` for every write."""

    def __init__(self, height=24, width=80):
        super().__init__(height, width)
        self.writes: list[tuple[int, int, str, int]] = []

    def erase(self):
        self.writes.clear()

    def addnstr(self, row, col, text, n, attr):
        self.painted.append(text)
        self.writes.append((row, col, text[:n], attr))


def _theme(slug: str):
    """A catalog theme by slug. Read through the loader, so a test can never pin
    a `Theme` object the pane could not actually resolve."""
    return th.load_catalog()[slug]


def _colour_paint(monkeypatch, frame, width=80, height=24, colors=256,
                  color_pairs=256, theme=None, keys=None):
    """Paint one frame through `_run_tui` with a colour-capable fake curses.

    DEFAULT THEME IS `og`, AND THAT IS A CLAIM, NOT A CONVENIENCE. `og` is the
    theme that reproduces what this pane painted before themes existed — a `-1`
    background and meta 109 / idle 46 / working 226 — so every test that asserts
    WHICH colour a role spends keeps asserting today's colour rather than
    dracula's. That is the regression guard the whole change needed: if the theme
    wiring ever changed what a role resolves to, these tests would fail on the
    values, not on the plumbing.

    A caller that wants a real theme's own colours passes one.

    `keys` drives the view past the one paint this normally does: the selection
    lives in `_run_tui`, NOT on the frame the stub model hands back (the loop
    starts untouched and owns the caret from the first keypress), so a test that
    needs a SELECTED row has to send a movement key. Passing none — the default —
    is the untouched pane, which is itself worth asserting.
    """
    fake = FakeCurses(colors=colors, color_pairs=color_pairs)
    monkeypatch.setitem(sys.modules, "curses", fake)
    scr = _WriteScreen(height, width)
    if keys is not None:
        scr.keys = list(keys)
    fake.wrapper = lambda loop: loop(scr)
    a._run_tui(_StubModel(frame), a.DEFAULT_INTERVAL, "http://x",
               theme=_theme("og") if theme is None else theme)
    return fake, scr.writes


def _writes_for(writes, text):
    """The attributes of every write whose text is exactly `text`."""
    return [attr for _row, _col, chunk, attr in writes if chunk == text]


class _PickerScreen(_WriteScreen):
    """A screen that answers a longer key queue, for the modal theme overlay.

    `FakeScreen.getch` pops from a list and returns -1 when it empties, which is
    what lets every other test drive exactly one paint and then quit. The overlay
    needs three keys (open, cancel, quit) and must record that it opened.
    """
    def __init__(self, height=24, width=80, keys=None):
        super().__init__(height, width)
        self.keys = list(keys) if keys else [ord("q")]

    def getch(self):
        return self.keys.pop(0) if self.keys else -1


def test_the_mark_span_carries_its_vendors_brand_colour_role():
    harnesses = _harness_for_vendor()
    assert set(MARK) == set(harnesses), "test and catalog disagree on vendors"
    for vendor, harness in harnesses.items():
        spans = a.worker_box(worker("w", "idle", text="t", harness=harness),
                             60, False)[0][1]
        roles = {text: role for text, role in spans}
        slot = a.harness_mark(harness)
        assert slot in roles, (vendor, spans)
        assert roles[slot] == a.mark_role(vendor), (vendor, spans)
        # The role's colour really exists in the table, so the mark is not blank.
        assert a._VENDOR_COLOURS[vendor], vendor


def _ansi_rgb(index):
    """The sRGB triple xterm paints for ANSI-256 `index`."""
    if index < 232:
        levels = (0, 95, 135, 175, 215, 255)
        n = index - 16
        return tuple(levels[d] for d in (n // 36, (n // 6) % 6, n % 6))
    step = 8 + 10 * (index - 232)
    return (step, step, step)


def _relative_luminance(rgb):
    """WCAG relative luminance, 0 (black) to 1 (white)."""
    lin = [c / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4
           for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _oklab_gap(x, y):
    """How far apart two palette indices read, in OKLab.

    OKLab rather than sRGB because sRGB distance is the thing that goes wrong
    here: openai 36 against goose 37 are (0,175,135) and (0,175,175), 40 of blue
    and nothing else, which sounds like almost nothing and is why the pair was
    always shipped unremarked. OKLab counts that the difference is barely
    visible at all.

    Scaled by 100 only so the numbers it returns are the ones the table's
    comment block already quotes — 8.234, 5.969 — instead of decimals nobody
    reading that comment would recognise.
    """
    def to_oklab(rgb):
        def linear(channel):
            channel /= 255
            return (channel / 12.92 if channel <= 0.04045
                    else ((channel + 0.055) / 1.055) ** 2.4)
        r, g, b = map(linear, rgb)
        # The cone response, then its cube root: the step that makes OKLab's
        # distances match what the eye reports. Cbrt of a negative is undefined,
        # and all three of these are sums of positive terms, so `** (1/3)` is
        # the same value without the domain check.
        long_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
        medium = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
        short = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
        return (0.2104542553 * long_ + 0.7936177850 * medium - 0.0040720468 * short,
                1.9779984951 * long_ - 2.4285922050 * medium + 0.4505937099 * short,
                0.0259040371 * long_ + 0.7827717662 * medium - 0.8086757660 * short)
    left, right = to_oklab(_ansi_rgb(x)), to_oklab(_ansi_rgb(y))
    return 100 * math.dist(left, right)


def test_every_vendor_colour_is_readable_on_a_dark_pane_and_not_a_repeat():
    # The rule the table's comment block records as TWO REAL FAILURES: a brand
    # colour that is converted correctly but is too dark is invisible on a dark
    # pane, which is why moonshotai is held at 252 and not its true 234. Nothing
    # enforced that, so a future straight conversion could reintroduce it. The
    # floor sits ABOVE both recorded failures (234 -> luminance 0.012, 237 ->
    # 0.058) and BELOW the darkest colour the table still ships (hermes 130 ->
    # 0.173), so it costs no existing entry and rejects any brand that would
    # vanish. z-ai used to hold that spot at 26 -> 0.131; it is 108 now, at 0.376,
    # which leaves hermes 130 -> 0.173 as the darkest shipped and the nearest
    # thing to the floor, by 0.073.
    for vendor, colour in sorted(a._VENDOR_COLOURS.items()):
        assert _relative_luminance(_ansi_rgb(colour)) > 0.10, (
            vendor, colour, "too dark to read on a dark pane")
    # And no two vendors share a colour: a repeat means one brand is painted in
    # another's identity, which is as much a lie as a colour that is too dark.
    assert len(set(a._VENDOR_COLOURS.values())) == len(a._VENDOR_COLOURS)
    # Which is the WEAKEST form of "no two read alike". Two marks a hair apart
    # are the same failure as two identical ones — one brand drawn in another's
    # identity — and nothing measured that, so z-ai's own placement claim ("108
    # also clears the tightest pair in this table") was unfalsifiable.
    #
    # The floor is set from the table's own geometry, not chosen. The tightest
    # pair that ships is openai 36 against goose 37 at 5.969, and that has always
    # been accepted; the pair this rule exists to reject is z-ai at its NEAREST
    # index 144 against opencode-zen 145 at 5.578. THE CUBE GEOMETRY IS THE SAME
    # IN BOTH — 36/37 are (0,175,135) and (0,175,175), 144/145 are (175,175,135)
    # and (175,175,175): one channel, blue alone, 40 units of it. Neither pair
    # is the wider one, and OKLab scores them 0.391 apart. What separates them is
    # the NEIGHBOURHOOD, which is the part the table's own block leans on: 145 is
    # exactly neutral at chroma 0.0000 where 144 carries 5.4816, and neither 36
    # nor 37 is near neutral (13.4085, 11.6533) because both are saturated
    # against a zero red. So the line goes in the one window that separates the
    # two numbers: above 5.578, at or below 5.969.
    #
    # OVER THE VENDOR TABLE ONLY, and that is a deliberate limit rather than an
    # oversight: the colours that share a worker row with these and do NOT
    # clear it are adjacent by design. Kilo's 227 against the spinner's 226 is
    # 3.478 and is argued in the table's own comment block (bold versus not, a
    # different column, blue alone between them); opencode-zen 145 and z-ai 108
    # sit 5.197 and 5.733 off META_COLOR 109, which is one step on the same rung
    # and is meant to read as a quiet grey beside the mark.
    for (left, x), (right, y) in itertools.combinations(
            sorted(a._VENDOR_COLOURS.items()), 2):
        gap = _oklab_gap(x, y)
        assert gap > 5.6, (
            left, x, right, y, round(gap, 3),
            "two marks close enough to read as one another")


def test_the_kilo_mark_paints_kilos_brand_lemon(monkeypatch):
    # Kilo's brand is #F3EF4E. It was painted 99 — a blue-violet belonging to no
    # part of Kilo — so this is a brand-accuracy pin, not a preference. 227 is
    # (255,255,95): the cube's top rung is the only one that can hold a red of
    # 243 and a green of 239, and 78 has no blue rung nearer than 95, so the whole
    # of the conversion error is that one channel.
    assert a._VENDOR_COLOURS["kilo"] == 227
    assert _ansi_rgb(227) == (255, 255, 95)
    # ...and the mark really spends that pair, rather than resolving the role to
    # None and painting the default foreground.
    harness = _harness_for_vendor()["kilo"]
    spans = a.worker_box(worker("w", "idle", text="t", harness=harness),
                         60, False)[0][1]
    assert (a.harness_mark(harness), a.mark_role("kilo")) in spans
    frame = a.Frame(rows=[worker("w1", "idle", harness=harness, meta="12s")],
                    selected=0)
    fake, writes = _colour_paint(monkeypatch, frame, colors=256, color_pairs=256)
    # On the colour, not the pair NUMBER: pairs are spent on first use, so the
    # index depends on what else the frame happened to paint first (here the idle
    # glyph takes pair 1) and is an allocation detail, not the claim under test.
    assert [(fg, bg) for _pair, fg, bg in fake.init_pair_calls] == [
        (a._STATE_COLOURS["idle"], -1), (227, -1), (a.META_COLOR, -1)]
    mark = _writes_for(writes, a.harness_mark(harness))
    assert mark, writes
    for attr in mark:
        assert attr & fake.A_BOLD and attr & 0xFF00


def test_the_freebuff_mark_paints_freebuffs_brand_sage(monkeypatch):
    # Freebuff's brand is #A4BA96, a muted sage. This row is keyed `z-ai`, which
    # names the MODEL PROVIDER while the glyph and the colour are FREEBUFF's, the
    # harness the registry files under that vendor — so it was painted 26, a
    # saturated blue belonging to nothing here, by a reader who took the key at
    # face value. Read the table's comment block before "correcting" this.
    # 108 is (135,175,135): the cube takes the brand's red 164 DOWN to the 135
    # rung, green 186 to 175 and blue 150 to 135. It is deliberately NOT the
    # nearest index — see the distance rule in the table-wide test above for why 144
    # was rejected.
    assert a._VENDOR_COLOURS["z-ai"] == 108
    assert _ansi_rgb(108) == (135, 175, 135)
    # ...and the mark really spends that pair, rather than resolving the role to
    # None and painting the default foreground.
    harness = _harness_for_vendor()["z-ai"]
    spans = a.worker_box(worker("w", "idle", text="t", harness=harness),
                         60, False)[0][1]
    assert (a.harness_mark(harness), a.mark_role("z-ai")) in spans
    frame = a.Frame(rows=[worker("w1", "idle", harness=harness, meta="12s")],
                    selected=0)
    fake, writes = _colour_paint(monkeypatch, frame, colors=256, color_pairs=256)
    # On the colour, not the pair NUMBER: pairs are spent on first use, so the
    # index depends on what else the frame happened to paint first (here the idle
    # glyph takes pair 1) and is an allocation detail, not the claim under test.
    assert [(fg, bg) for _pair, fg, bg in fake.init_pair_calls] == [
        (a._STATE_COLOURS["idle"], -1), (108, -1), (a.META_COLOR, -1)]
    mark = _writes_for(writes, a.harness_mark(harness))
    assert mark, writes
    for attr in mark:
        assert attr & fake.A_BOLD and attr & 0xFF00


def test_the_atom_and_blank_mark_spans_carry_plain(monkeypatch):
    # A named agent whose harness cannot be resolved draws the ATOM, and the atom
    # has no vendor: PLAIN, no brand colour, no bold. A row with no identity at
    # all draws the blank slot, also PLAIN.
    monkeypatch.setattr(a, "agent_harness", lambda name: None)
    atom_spans = a.worker_box(worker("w", "idle", text="t", agent="mystery"),
                              60, False)[0][1]
    assert (a.ATOM + " ", a.PLAIN) in atom_spans
    assert a.worker_vendor("", "mystery") is None

    blank_spans = a.worker_box(worker("w", "idle", text="t"), 60, False)[0][1]
    assert ("  ", a.PLAIN) in blank_spans
    assert a.worker_vendor("", None) is None


def test_the_meta_span_carries_the_one_meta_colour():
    spans = a.worker_box(worker("w", "idle", text="t", meta="2m14s"),
                         60, False)[0][1]
    assert ("2m14s", a.META) in spans


def test_the_root_name_span_is_orch_and_the_label_plain():
    spans = a._root_lines(header(text="conversation", harness="claude-native"),
                          60)[1]
    assert ("conversation", a.ORCH) in spans
    assert (a.harness_mark("claude-native"), "mark:anthropic") in spans
    assert ("Claude Code", a.PLAIN) in spans


def test_every_lines_spans_rejoin_to_the_composed_text_at_every_width():
    # The invariant the two axes rest on: colour only PARTITIONS a line, so the
    # text is unchanged whether you join the spans or read the rendered list.
    rows = [header(), rail("idle"),
            worker("w1", "idle", text="one", harness="claude-native", meta="12s"),
            worker("w2", "working", text="two", agent="zen", meta="failed")]
    frame = a.Frame(rows=rows, selected=2, spin=1)
    for width in (80, 46, 20, 8, 4, 1):
        pane = a._pane(frame, width)
        assert a.render(frame, width=width) == [a.line_text(i) for i in pane.items]
        for item in pane.items:
            assert a.line_text(item) == _join(item[0])
    # And a line's spans rejoin to EXACTLY the string the layout composes — pinned
    # against a literal, so a span helper that dropped or duplicated text fails.
    tight = a.worker_box(
        worker("w", "blocked", text="task", agent="cmdcode", meta="waiting on you"),
        20, False)[0][1]
    assert _join(tight) == "⦿ ⏸ │waiting on you│"
    clipped = a.worker_box(
        worker("w", "blocked", text="task", agent="cmdcode",
               meta="waiting on you forever and ever"),
        20, False)[0][1]
    assert _join(clipped) == "⦿ ⏸ │waiting on yo…│"


def test_a_monochrome_terminal_spends_no_pair_and_still_paints(monkeypatch):
    frame = a.Frame(rows=[header(),
                          worker("w1", "idle", harness="claude-native",
                                 agent="zen", meta="12s")], selected=1)
    fake, writes = _colour_paint(monkeypatch, frame, colors=0)
    assert fake.init_pair_calls == [], "no colour support, no pair"
    assert any(chunk.strip() for _r, _c, chunk, _a in writes), "the pane painted"


def test_an_eight_colour_terminal_skips_the_256_colour_roles(monkeypatch):
    # Every vendor colour and the meta slate is >= 8, so an 8-colour terminal can
    # hold none of them: every coloured role falls back to no colour, and no pair
    # is spent. It must not raise, and it must not paint a wrong colour.
    frame = a.Frame(rows=[header(),
                          worker("w1", "idle", harness="claude-native",
                                 agent="zen", meta="12s")], selected=1)
    fake, writes = _colour_paint(monkeypatch, frame, colors=8, color_pairs=256)
    assert fake.init_pair_calls == []
    assert writes


def test_pair_exhaustion_degrades_rather_than_crashing(monkeypatch):
    # Pair 0 is reserved, so a `COLOR_PAIRS` of 1 can allocate nothing and one of
    # 2 can allocate exactly one. Neither may crash, and neither may paint a role
    # the table could not hold.
    frame = a.Frame(rows=[header(),
                          worker("w1", "idle", harness="claude-native",
                                 agent="zen", meta="12s")], selected=1)
    starved, writes = _colour_paint(monkeypatch, frame, colors=256, color_pairs=1)
    assert starved.init_pair_calls == []
    assert writes

    one, _writes = _colour_paint(monkeypatch, frame, colors=256, color_pairs=2)
    assert len(one.init_pair_calls) <= 1
    assert all(pair < 2 for pair, _fg, _bg in one.init_pair_calls)


def test_a_coloured_terminal_spends_a_pair_on_the_mark_and_bolds_it(monkeypatch):
    frame = a.Frame(rows=[header(harness="claude-native"),
                          worker("w1", "idle", harness="claude-native",
                                 meta="12s")], selected=1)
    fake, writes = _colour_paint(monkeypatch, frame, colors=256, color_pairs=256)
    # The anthropic mark spent its brand colour, on a `-1` (terminal-owned)
    # background — the pair that lets colour and transparency coexist.
    assert (1, a._VENDOR_COLOURS["anthropic"], -1) in fake.init_pair_calls
    mark = _writes_for(writes, a.harness_mark("claude-native"))
    assert mark, writes
    for attr in mark:
        assert attr & fake.A_BOLD, "the mark is bold"
        assert attr & 0xFF00, "the mark carries a colour pair"


def test_the_root_name_is_bold_and_uncoloured(monkeypatch):
    frame = a.Frame(rows=[header(text="conversation", harness="claude-native")],
                    selected=0)
    fake, writes = _colour_paint(monkeypatch, frame, colors=256)
    name = _writes_for(writes, "conversation")
    assert name, writes
    for attr in name:
        assert attr & fake.A_BOLD, "ORCH is bold"
        assert not attr & 0xFF00, "ORCH carries no colour"


def test_the_meta_write_carries_the_meta_colour_and_is_not_bold(monkeypatch):
    frame = a.Frame(rows=[worker("w1", "idle", meta="12s")], selected=0)
    fake, writes = _colour_paint(monkeypatch, frame, colors=256)
    meta = _writes_for(writes, "12s")
    assert meta, writes
    for attr in meta:
        assert attr & 0xFF00, "META is coloured"
        assert not attr & fake.A_BOLD, "META is not bold"
    # The PAIR NUMBER is incidental — it is spent in paint order, and the status
    # glyph now spends one of its own from the gutter, before this row's meta. What
    # is pinned is the colour itself, on a `-1` (terminal-owned) background.
    assert any(fg == a.META_COLOR and bg == -1
               for _pair, fg, bg in fake.init_pair_calls), fake.init_pair_calls


def test_the_status_glyph_spans_carry_their_neon_colours():
    """THE SECOND REPORTED DEFECT: the status glyph had NO colour at all. It sat in
    a PLAIN span, so `✓` and `⠋` painted in the terminal's default foreground and
    only the dim ever varied — four workers running read as four identical grey
    glyphs. The check mark is NEON GREEN, the spinner NEON YELLOW, and — since the
    operator asked for the failed mark to be red — the failed `×` NEON RED.

    The green and yellow are the `og` row's cube corners 46 and 226; the red is its
    own pure-red corner, cell 196. `⏸` blocked and `?` unknown are PLAIN, as they
    were before, because nothing was asked of them.
    """
    for state, colour in (("idle", 46), ("working", 226), ("failed", 196)):
        glyph = a.state_glyph(state, 0)
        spans = a.worker_box(worker("w", state, text="t"), 60, False)[0][1]
        roles = [role for text, role in spans if glyph in text]
        assert roles == [a.undim(a.state_role(state))], (state, spans)
        assert a._role_colour(roles[0]) == colour, state
        assert a._STATE_COLOURS[state] == colour, state
    for state in ("blocked", "unknown"):
        assert a.state_role(state) == a.PLAIN, state
        assert a._role_colour(a.undim(a.state_role(state))) is None, state


def test_a_status_glyph_is_neon_and_undimmed_on_an_unselected_row(monkeypatch):
    """BOTH fixes at once, on the row that used to be the unreadable one: the glyph
    of an UNSELECTED worker's box paints its neon colour AND carries no A_DIM,
    while the box drawn beside it still dims.

    This is the interaction the two defects share — the glyph is exempt from the
    dim and coloured by the same span role — and the reason a colour alone would
    not have answered the report: neon yellow at half brightness is still dim.
    """
    for state, glyphs in (("idle", {"✓"}), ("working", set(a.SPIN_FRAMES)),
                          ("failed", {"×"})):
        # w2 carries the state under test and is UNSELECTED, so its box is the
        # dimmed one. The selection lives in the loop, not on the frame, so DOWN
        # is what puts it there — on the root now that every agent row is
        # selectable, which still leaves BOTH workers unselected. A SINGLE-worker
        # frame could not say this: with nothing else to move to, the first key
        # adopted the only row and there was no unselected box to test.
        frame = a.Frame(rows=[header(),
                              worker("w1", "blocked", text="one"),
                              worker("w2", state, text="two", meta="12s")])
        fake, writes = _colour_paint(monkeypatch, frame, colors=256,
                                     keys=[DOWN, QUIT])
        # Any frame of the spinner will do: DOWN costs a repaint, so the glyph on
        # it is whichever frame the clock was on, not the one frozen at 0.
        painted = [(row, attr) for row, _col, text, attr in writes
                   if text[:-1] in glyphs]
        assert painted, (state, writes)
        row, attrs = zip(*painted)
        assert len(set(row)) == 1, (state, painted)
        for attr in attrs:
            assert not attr & fake.A_DIM, f"{state}: the glyph is exempt from the dim"
            assert attr & 0xFF00, f"{state}: and still wears its neon colour"
        assert any(fg == a._STATE_COLOURS[state] and bg == -1
                   for _pair, fg, bg in fake.init_pair_calls), \
            (state, fake.init_pair_calls)
        # The box beside it is still the dim one: the fix is the BOX that dims. Its
        # top border is the `╭` written at the gutter's right edge, on the row above
        # the content row the glyph shares.
        borders = [attr for border_row, col, text, attr in writes
                   if border_row == row[0] - 1 and col == a.GUTTER and "╭" in text]
        assert borders and all(attr & fake.A_DIM for attr in borders), (state, borders)


def test_a_terminal_that_cannot_hold_the_neon_leaves_the_glyph_uncoloured(monkeypatch):
    """Both neon indices are > 16, so a terminal reporting fewer colours cannot
    hold them. The glyph falls back to the default foreground exactly as every
    other colour in this module does — uncoloured, and still EXEMPT from the dim,
    which is a brightness and not a colour and so survives a terminal with no
    colour at all. It must never raise, and never paint a wrong colour.
    """
    for colors in (0, 8, 16, 32):
        for state, glyph in (("idle", "✓"), ("working", a.SPIN_FRAMES[0])):
            frame = a.Frame(rows=[header(), worker("w1", state)], selected=0)
            fake, writes = _colour_paint(monkeypatch, frame, colors=colors)
            assert fake.init_pair_calls == [], (colors, state)
            painted = _writes_for(writes, glyph + " ")
            assert painted, (colors, state, writes)
            for attr in painted:
                assert not attr & 0xFF00, (colors, state, "painted a wrong colour")
                assert not attr & fake.A_DIM, (colors, state, "dimmed a bare glyph")


def test_bold_roles_still_bold_on_a_monochrome_terminal(monkeypatch):
    # Bold is not a colour, so ORCH and the marks stay bold with no colour at all.
    frame = a.Frame(rows=[header(), worker("w1", "idle",
                                           harness="claude-native", meta="12s")],
                    selected=1)
    fake, writes = _colour_paint(monkeypatch, frame, colors=0)
    assert all(attr & fake.A_BOLD
               for attr in _writes_for(writes, "conversation"))
    assert all(attr & fake.A_BOLD
               for attr in _writes_for(writes, a.harness_mark("claude-native")))


# ---------------------------------------------------------------------------
# 16. Enter focuses the selected agent in herdr; Esc clears the selection
# ---------------------------------------------------------------------------
#
# `focus_agent` imports `og_herdr_launch.focus_agent_terminal` LAZILY, so patching
# that MODULE ATTRIBUTE is the seam the view reaches (the real function ships on
# the other branch and is not here). The older `focus_in_herdr` imports
# `og_herdr_client` the same lazy way, so a fake module dropped into sys.modules
# is the client IT reaches — that trick keeps the remaining tests off a real
# socket. HERDR_ENV is set or deleted per test so the "inside herdr" question is
# never answered by whatever environment the suite happens to run in.

ENTER = 13          # what the view treats as Enter (curses.KEY_ENTER, 10 or 13)
QUIT = ord("q")
ESC = 27            # Esc: clears the selection, and MUST NOT quit (was q's twin)
# The pane opens UNTOUCHED, so every test that acts ON a row has to move the caret
# there first. `j` is one of the two keys the view reads as down (the other is
# curses.KEY_DOWN, which `FakeCurses` numbers 258).
DOWN = ord("j")


class _KeyScreen(FakeScreen):
    """`FakeScreen` with a settable key sequence and per-frame paint capture.

    `painted` (inherited) is every line ever written; `frames` is the same lines
    grouped into one list per `erase()`, which is what lets a test ask what the
    LAST frame looked like after a key changed the view. `frame_attrs` is the
    parallel list of curses attributes per frame, so a test can see SELECTION —
    which is a paint attribute (DIM vs NORMAL), not a glyph.

    A line is now written one `addnstr` per SPAN, so `frame_writes` keeps
    `(row, col, text, attr)` for every write: reconstruct whole lines (and their
    brightness) with `_rows_with_attr` when a test needs a row, not a span.
    """

    def __init__(self, keys, height=24, width=80):
        super().__init__(height, width)
        self.keys = list(keys)
        self.frames: list[list[str]] = []
        self.frame_attrs: list[list[int]] = []
        self.frame_writes: list[list[tuple[int, int, str, int]]] = []

    def erase(self):
        self.frames.append([])
        self.frame_attrs.append([])
        self.frame_writes.append([])

    def addnstr(self, row, col, text, n, attr):
        self.painted.append(text)
        if self.frames:
            self.frames[-1].append(text)
            self.frame_attrs[-1].append(attr)
            self.frame_writes[-1].append((row, col, text[:n], attr))


def _rows_with_attr(writes):
    """Reassemble rows from span writes: {row: (text, line_attr)}.

    `line_attr` is the LAST span's attribute, which is the LINE's brightness: a
    span may be exempt from it (the worker's gutter and status glyph are `undim:`
    and paint NORMAL beside a DIM box), and the box's own spans — which end every
    row — always take the line's value. Reading the first span instead would
    report every worker row as bright.
    """
    by_row: dict[int, list] = {}
    for row, col, text, attr in writes:
        by_row.setdefault(row, []).append((col, text, attr))
    out: dict[int, tuple[str, int]] = {}
    for row, parts in by_row.items():
        parts.sort()
        out[row] = ("".join(text for _col, text, _attr in parts),
                    parts[-1][2])
    return out


def _fake_herdr_client(monkeypatch, error=None):
    """Install a fake `og_herdr_client`; return the list it records calls into."""
    calls = []

    class HerdrClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def call(self, method, params=None):
            calls.append((method, params))
            if error is not None:
                raise error
            return {}

    module = types.ModuleType("og_herdr_client")
    module.HerdrClient = HerdrClient
    monkeypatch.setitem(sys.modules, "og_herdr_client", module)
    return calls


def _fake_focus_agent_terminal(monkeypatch, notice=None):
    """Install a fake `og_herdr_launch.focus_agent_terminal`; return its call log.

    `focus_agent` imports it LAZILY (`from og_herdr_launch import
    focus_agent_terminal`) on the keypress, so patching the MODULE ATTRIBUTE is
    the seam the view actually reaches. The real function ships on the other
    branch and is NOT on this one, which is why the attribute is installed with
    `raising=False` — it does not exist here until this creates it. Each call is
    recorded as `(session_id, harness, root_session_id)`.
    """
    calls = []

    def fake(session_id, harness="", root_session_id=""):
        calls.append((session_id, harness, root_session_id))
        return notice

    monkeypatch.setattr(og_herdr_launch, "focus_agent_terminal", fake,
                        raising=False)
    return calls


def _run_tui_capture(monkeypatch, keys, model=None, interval=None):
    """Run the live view with a faked curses; return the screen it painted.

    Every Enter/Space test needs the frame the loop actually wrote, which the
    stock `FakeScreen` stub does not expose. `model` defaults to the shared
    `_tui_model()` (a root and three running workers, opening UNTOUCHED).
    `interval` defaults to `DEFAULT_INTERVAL`; a test that needs the loop's NEXT
    TICK to re-poll immediately (there is no manual-refresh key any more) passes
    a small one.
    """
    fake = _fake_curses(monkeypatch)
    screens: list[_KeyScreen] = []

    def wrapper(loop):
        screen = _KeyScreen(keys)
        screens.append(screen)
        loop(screen)
    fake.wrapper = wrapper

    a._run_tui(model if model is not None else _tui_model(),
               a.DEFAULT_INTERVAL if interval is None else interval, "http://x")
    return screens[0]


# ---- Esc clears the selection; `q` is the only quit ------------------------
#
# The reported defect: pressing an arrow key dims every unselected row, and there
# was no way back — Esc used to quit alongside `q`, so the list stayed dimmed for
# the rest of the session. Esc was rebound to clear the selection, which cost it
# the quit; these tests are the guard on BOTH halves of that trade.

def test_esc_clears_the_selection_and_undims_every_row(monkeypatch):
    """Esc puts the pane back to its freshly-opened look: no agent row dimmed.

    Dim is a PAINT attribute, not a glyph, so it is read off the writes. The
    agent rows are picked out by their NODE column (`⦿`, the content row of each
    worker box) so the pane's one permanently dim line — the footer — cannot
    masquerade as an unselected agent.
    """
    screen = _run_tui_capture(monkeypatch, [DOWN, ESC, QUIT])
    dim = sys.modules["curses"].A_DIM

    def dim_node_rows(frame_writes):
        rows = _rows_with_attr(frame_writes)
        return [text for text, attr in rows.values()
                if text.startswith(a.NODE) and attr & dim]

    # Frames: the untouched paint, then DOWN's — the root selected and all three
    # worker boxes dimmed to mark them unselected.
    assert len(dim_node_rows(screen.frame_writes[1])) == 3, \
        "DOWN dims every unselected agent"
    # ...and the frame Esc produced is undimmed again.
    assert dim_node_rows(screen.frame_writes[-1]) == [], \
        "Esc must return every row to its normal, undimmed appearance"
    # And Esc did not quit: the q that followed is what ended the loop.
    assert screen.keys == [], "Esc did not quit"


def test_esc_with_no_selection_does_nothing_and_does_not_quit(monkeypatch):
    """THE REGRESSION GUARD: Esc used to quit, and this is the behaviour removed.

    With nothing selected Esc is a harmless no-op: it changes nothing, so not a
    single repaint is even scheduled — and, above all, it does NOT quit. The proof
    it kept running is that the `q` behind it was read and ended the loop. This is
    the assertion someone "restoring" Esc-quit would break.
    """
    # An all-idle frame, so the spinner cannot schedule a repaint either: the only
    # thing that could paint a second frame here is Esc itself.
    quiet = a.Frame(rows=[header(state="idle"), rail("idle"),
                          worker("w1", "idle", text="one")])
    screen = _run_tui_capture(monkeypatch, [ESC, QUIT], model=_StubModel(quiet))

    assert screen.keys == [], \
        "Esc must not quit: the q that followed is what ended the loop"
    assert len(screen.frames) == 1, \
        "Esc on an untouched pane changed nothing, so it painted nothing new"
    assert any("conversation" in text for text in screen.painted), \
        "the pane was painted and is still running"


def test_q_quits_unconditionally_even_with_a_row_selected(monkeypatch):
    """`q` is now the ONLY quit — it must end the loop whatever is selected."""
    screen = _run_tui_capture(monkeypatch, [DOWN, QUIT])

    assert screen.keys == [], "the q was read: the loop quit on it"
    assert len(screen.frames) >= 2, "DOWN painted before q ended the loop"



def test_enter_on_a_sub_agent_row_focuses_its_own_session_not_its_root(monkeypatch):
    """REGRESSION: Enter used to aim at a worker's ROOT, never the worker.

    `w1` is a SUB-AGENT of `root` (`parent_session_id == "root"`). The reported
    defect was that selecting a worker and pressing Enter focused the ROOT's tab
    (the old resolution climbed a worker to its root) instead of the worker's own —
    and selecting the orchestrator focused nothing at all. Enter must now hand
    `focus_agent_terminal` the SELECTED row's OWN session id — `w1`, not `root` —
    with the worker's own harness and its root alongside.
    """
    monkeypatch.setenv("HERDR_ENV", "1")
    calls = _fake_focus_agent_terminal(monkeypatch)
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    model = _model(opener)

    # DOWN adopts the root (the first agent row), the second DOWN adopts w1.
    screen = _run_tui_capture(monkeypatch, [DOWN, DOWN, ENTER, QUIT], model=model)

    # The SELECTED row's own session — w1 — is what went through, with its own
    # (empty) harness and the root it hangs off. Never the root as the target.
    assert calls == [("w1", "", "root")], calls
    # Focus is not a reason to refetch: exactly the one listing walk for the
    # first paint, and none from the keypress.
    assert len(opener.listing_requests) == 1, "Enter must not trigger a refetch"
    # A successful focus leaves no notice behind.
    assert screen.painted, "the pane was painted"
    assert not any("focus:" in text for text in screen.painted)


def test_enter_on_the_root_row_focuses_the_root_itself(monkeypatch):
    """The ROOT is selectable and focuses its OWN tab (the `is_root` case).

    Selecting the orchestrator was impossible before — roots were stepped over —
    so this is the other half of the fix: the root row must reach
    `focus_agent_terminal` with its own id, its own harness and NO root of its
    own (`root_session_id` is "" for a root).
    """
    monkeypatch.setenv("HERDR_ENV", "1")
    calls = _fake_focus_agent_terminal(monkeypatch)
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})

    _run_tui_capture(monkeypatch, [DOWN, ENTER, QUIT], model=_model(opener))

    # One DOWN adopts the root, so the root's own session and harness go through.
    assert calls == [("root", "claude-native", "")], calls


def test_enter_focuses_a_worker_whose_listing_carried_no_root(monkeypatch):
    """A worker with no `root_session_id` is still focused by its OWN id.

    The old path resolved a worker to its root and declined when there was none;
    Enter now focuses the row's own session, so a missing root no longer blocks
    anything — `""` is simply what is passed for it.
    """
    monkeypatch.setenv("HERDR_ENV", "1")
    calls = _fake_focus_agent_terminal(monkeypatch)
    # `worker()` leaves `root_session_id` empty: a worker whose listing carried no
    # root. It is still its OWN agent, so it is still focusable.
    orphan = worker("w1", "idle", text="one")
    assert a._selectable(orphan), "the row must be a selectable agent row"

    class OneRow:
        def refresh(self, **kwargs):
            return a.Frame(rows=[orphan])

    screen = _run_tui_capture(monkeypatch, [DOWN, ENTER, QUIT], model=OneRow())

    # Enter no longer needs the root to resolve anything: it focuses the row's own
    # session, passing "" for the root the row does not have.
    assert calls == [("w1", "", "")], calls
    assert not any("focus:" in text for text in screen.painted), screen.painted


def test_enter_on_a_worker_row_resolves_the_spec_harness_for_the_callee(
        spec_home, monkeypatch):
    """REGRESSION for the v0.20.0 report: Enter on a WORKER row.

    `build_rows` gives every worker row NO harness (`""`) and an agent name
    parsed from its title; the row's MARK resolves the harness the agent's spec
    declares. Enter must resolve it the SAME way, or `focus_agent_terminal` is
    handed "" and declines EVERY worker as an unknown harness — even though the
    worker's tab is visibly on screen, which is exactly what the user reported.
    Built through `build_rows`, so the row is shaped as the pane really shapes it.
    """
    _write_spec(spec_home, "Hivemind", "coder_zen", "opencode-native")
    listing = [listed("root", status="running", title="conversation"),
               listed("w1", parent="root", status="running",
                      title="coder_zen:do-it")]
    row = next(r for r in a.build_rows(listing, ["root"], now=NOW)
               if r.session_id == "w1")
    # Exactly what a worker row carries: no harness of its own, agent `zen`.
    assert row.harness == "" and row.agent == "zen"
    calls = _fake_focus_agent_terminal(monkeypatch)

    assert a.focus_agent(row) is None
    # The spec's harness — not the row's blank — reaches the callee.
    assert calls == [("w1", "opencode-native", "root")], calls


def test_enter_on_an_unresolvable_worker_degrades_to_the_callee_notice(
        spec_home, monkeypatch):
    """A worker whose agent has no installed spec either still degrades cleanly.

    The resolution yields "" (there is nothing to guess), so Enter hands that
    through and shows whatever the callee answers — its honest "unknown harness"
    — rather than raising on the keypress.
    """
    listing = [listed("root", status="running", title="conversation"),
               listed("w1", parent="root", status="running",
                      title="coder_ghost:do-it")]
    row = next(r for r in a.build_rows(listing, ["root"], now=NOW)
               if r.session_id == "w1")
    assert row.harness == "" and row.agent == "ghost"
    notice = og_herdr_launch.NOTICE_FOCUS_UNKNOWN_HARNESS.format("this agent")
    calls = _fake_focus_agent_terminal(monkeypatch, notice=notice)

    assert a.focus_agent(row) == notice
    assert calls == [("w1", "", "root")], calls


def test_focus_resolution_adds_no_call_beyond_agent_focus():
    """The one-outbound-call guarantee survives the root resolution: resolving a
    TARGET is not a second call, and this path adds none (see
    `_assert_only_agent_focus`)."""
    _assert_only_agent_focus(Path(a.__file__).read_text())


def test_enter_outside_herdr_paints_the_callee_notice(monkeypatch):
    """The "not inside herdr" answer the callee returns is shown in the pane.

    The gate lives in `focus_agent_terminal` now (this branch does not have it, so
    the stub returns it): the TUI's job is to paint whatever notice comes back
    through the ONE existing notice path — the footer row.
    """
    monkeypatch.delenv("HERDR_ENV", raising=False)
    calls = _fake_focus_agent_terminal(monkeypatch,
                                       notice=a.NOTICE_FOCUS_OUTSIDE_HERDR)

    screen = _run_tui_capture(monkeypatch, [DOWN, ENTER, QUIT])

    # The callee was reached with the selected row's own id (the root, on a
    # single DOWN) and its notice was painted.
    assert calls == [("root", "claude-native", "")], calls
    assert any(a.NOTICE_FOCUS_OUTSIDE_HERDR in text for text in screen.painted), \
        screen.painted


def test_focus_degrades_when_og_herdr_launch_cannot_be_imported(monkeypatch):
    # `None` in sys.modules makes `from og_herdr_launch import ...` raise
    # ImportError: the lazy import `focus_agent` does. The view still has to run,
    # degrading to the "unavailable" notice rather than a traceback.
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setitem(sys.modules, "og_herdr_launch", None)

    screen = _run_tui_capture(monkeypatch, [DOWN, ENTER, QUIT])

    assert any(a.NOTICE_FOCUS_UNAVAILABLE in text for text in screen.painted), \
        screen.painted


def test_a_focus_that_cannot_reach_herdr_shows_a_notice_and_the_loop_runs_on(
        monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    calls = _fake_focus_agent_terminal(
        monkeypatch, notice="focus: connect: no such socket")

    # Enter clears the selection (undims), so a SECOND Enter needs a second DOWN
    # to re-adopt a row: two presses, two calls, and the loop still takes keys.
    screen = _run_tui_capture(monkeypatch, [DOWN, ENTER, DOWN, ENTER, QUIT])

    # One call per press: the failure neither swallows the key nor retries.
    assert len(calls) == 2, calls
    assert any("focus: " in text for text in screen.painted), screen.painted


# ---- the focus notice takes the FOOTER row (see `_pane`) -------------------

def _notice_frame(notice=None, truncated=False):
    rows = [header(), rail(), worker("w1", "working", text="one")]
    return a.Frame(rows=rows, notice=notice, truncated=truncated)


def test_a_notice_takes_the_bottom_row_in_place_of_the_footer():
    lines = a.render(_notice_frame(a.NOTICE_FOCUS_OUTSIDE_HERDR), width=80)
    assert lines[-1] == a.NOTICE_FOCUS_OUTSIDE_HERDR
    assert a._footer(80) not in lines, "the footer gives its row to the notice"


def test_without_a_notice_the_bottom_row_is_still_the_footer():
    lines = a.render(_notice_frame(), width=80)
    assert lines[-1] == _footer_text(80)


def test_a_notice_longer_than_the_pane_is_clipped_not_overflowed():
    long = "focus: " + "x" * 200
    lines = a.render(_notice_frame(long), width=20)
    assert lines[-1] == a._clip(long, 20)
    assert len(lines[-1]) <= 20, "the row is cut to the pane, never run past it"


def test_the_pane_is_the_same_height_with_and_without_a_notice():
    without = a.render(_notice_frame(), width=80)
    with_notice = a.render(_notice_frame(a.NOTICE_FOCUS_UNAVAILABLE), width=80)
    assert len(without) == len(with_notice)


def test_a_notice_paints_bright_where_the_footer_paints_dim():
    # An error painted DIM is easy to miss: the footer is chrome and dim, the
    # notice reports a failure and is painted at NORMAL brightness.
    with_notice = a._pane(_notice_frame(a.NOTICE_FOCUS_UNAVAILABLE), 80)
    assert with_notice.items[-1][1] == a.NORMAL
    assert a._pane(_notice_frame(), 80).items[-1][1] == a.DIM


def test_a_failed_focus_paints_the_notice_on_the_bottom_row(monkeypatch):
    # End to end through the loop: after a failed Enter the notice IS the pane's
    # last row and the footer's line is gone from that frame.
    monkeypatch.delenv("HERDR_ENV", raising=False)
    _fake_focus_agent_terminal(monkeypatch,
                               notice=a.NOTICE_FOCUS_OUTSIDE_HERDR)

    screen = _run_tui_capture(monkeypatch, [DOWN, ENTER, QUIT])

    rows = _rows_with_attr(screen.frame_writes[-1])
    bottom = rows[max(rows)][0]
    assert bottom == a.NOTICE_FOCUS_OUTSIDE_HERDR, rows
    assert a._footer(80) not in [text for text, _attr in rows.values()], \
        "the footer does not share the pane with the notice"


def test_the_selection_follows_a_selected_worker_that_changes_group_across_a_refresh(
        monkeypatch):
    """THE REGRESSION the reviewer caught, driven through the real loop.

    A selected worker changes state, so it moves to another STATE GROUP and the
    rows reorder underneath the selection. The view used to hold the INDEX across
    the refresh, so the poll after the move left the selection on whichever worker
    now occupied it — a different session, and Enter would have focused that one.

    Here `w2` is selected at index 4 in the idle group, turns `working`, and its
    box moves to index 3 in the working group while its sibling `w1` takes index
    4. The selection follows the SESSION, so it lands on `w2`.
    """
    monkeypatch.delenv("HERDR_ENV", raising=False)
    before = [
        listed("root", status="running", title="conversation"),
        listed("w1", parent="root", status="running", title="coder_zen:one"),
        listed("w2", parent="root", status="idle", title="coder_zen:two"),
    ]
    after = [
        listed("root", status="running", title="conversation"),
        listed("w1", parent="root", status="running", title="coder_zen:one"),
        listed("w2", parent="root", status="running", title="coder_zen:two"),
    ]
    opener = RoutingOpener([before, after],
                           details={"root": ROOT_DETAIL,
                                    "w1": detail("w1"), "w2": detail("w2")})
    model = _model(opener)

    # The pane opens UNTOUCHED, so the caret has to be ADOPTED before it can be
    # moved: the first `j` takes the root, the second w1, the third w2. `r` forces
    # the refresh that reorders the rows; `q` ends the loop after that frame is
    # painted.
    screen = _run_tui_capture(monkeypatch, [DOWN, DOWN, DOWN, ord("r"), ord("q")],
                              model=model)

    # The box the caret lands on points at w2 — its task says "two" — not at w1's
    # box, which is what the old index-preserving snap would have chosen. Selection
    # is a paint ATTRIBUTE now (bright vs DIM), so read it off the painted attrs: a
    # line is written span by span, so reassemble the rows first, and the bright
    # (non-DIM) worker content row is w2's.
    lines = _rows_with_attr(screen.frame_writes[-1])
    dim = lines[max(lines)][1]     # the footer is pinned to the bottom row, DIM
    node_rows = [(text, attr) for text, attr in lines.values()
                 if text.startswith(a.NODE)]
    bright = [text for text, attr in node_rows if attr != dim]
    dimmed = [text for text, attr in node_rows if attr == dim]
    assert len(bright) == 1 and "two" in bright[0], node_rows
    assert len(dimmed) == 1 and "one" in dimmed[0], node_rows


def test_the_footer_names_its_bindings():
    # The FULL form labels every binding it lists. Esc is bound but deliberately
    # UNLISTED (see the footer comment); `c cli`, `␣ expand` and `r` are gone.
    assert "↑/↓ move" in a.FOOTER, "the footer must name the move binding"
    assert "⏎ focus" in a.FOOTER, "the footer must name the Enter binding"
    assert "a access" in a.FOOTER, "the existing bindings must survive"
    assert "esc" not in a.FOOTER, "Esc is bound but unlisted on purpose"


def _cjk_width(text: str) -> int:
    """Display columns if AMBIGUOUS is two — the CJK-locale rule.

    A footer FORM must fit under BOTH this and the Latin rule, and `_display_width`
    charges Ambiguous two, so `_footer` choosing with it is what guarantees that.
    The footer's move arrows ↑/↓ ARE Ambiguous, so both rulers charge them two and
    agree on every form; this stays the independent check that keeps that true. See
    `test_the_footer_is_measured_the_same_in_a_cjk_locale`.
    """
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
               for ch in text)


def test_the_footer_names_every_key_at_every_width():
    # The regression this pins: the full footer is 50 columns and the agents pane
    # is a ~46-column split, so a FIXED footer was clipped mid-word — "a access"
    # and "q quit" fell off the end.
    #
    # The FORM is measured with the pessimistic ruler, so the form a width gets
    # fits that width under BOTH locale rules and no key can be clipped in either.
    # The painted ROW is that form padded with the strip out to the pane, and it is
    # composed under the pane's own (western) ruler — measured with `len`. See
    # `test_the_footer_is_measured_the_same_in_a_cjk_locale`.
    for width in (46, 52, 64, 80):
        row = a.render(a.Frame(), width=width)[-1]
        form = a._footer(width)
        assert a._display_width(form) <= width, (width, form)
        assert _cjk_width(form) <= width, (width, form)
        assert row.rstrip() == form and len(row) == width, (width, row)
        for key in ("q", "a", "t"):
            assert key in row, (width, key, row)
        assert ("↑/↓" in row or "↑↓" in row), (width, row)
        # Enter is named by its key glyph; Esc is bound but deliberately UNLISTED —
        # it must never appear.
        assert ("enter" in row or "⏎" in row), (width, row)
        assert "esc" not in row, (width, row)


def test_the_footer_chooses_a_form_by_measured_width():
    # The threshold is each form's own width, measured — not a magic number
    # that can drift out of step with the strings.
    #
    # THE NUMBERS MOVED REPEATEDLY. `t theme` first cost COMPACT the word `refresh`
    # (it shed a label rather than growing past the ~46-column split). Then a spaced
    # separator was added and its glyph — U+00B7 MIDDLE DOT, East Asian AMBIGUOUS —
    # cost TWO measured columns each, which `_display_width` (the ruler `_footer`
    # chooses with) charged in full: the full form grew to 85, so a plain 80-column
    # terminal showed COMPACT and the ~46-column split fell all the way to the bare
    # KEYS form. THE SEPARATOR IS NOW U+2219 BULLET OPERATOR, NARROW — one column
    # under both rulers — and COMPACT carries none at all (its entries are joined by
    # a single space, its pre-separator spelling). A key is still in all three forms:
    # only labels degrade.
    #
    # THE ARROWS COST TWO COLUMNS EACH, AND THAT IS MEASURED, NOT ASSUMED. `^/v`
    # was swapped for `↑/↓` (and `^v` for `↑↓`): U+2191/U+2193 are East Asian
    # AMBIGUOUS, so `_display_width` charges each TWO and the move entry grows from
    # 3 measured columns to 5 (2 to 4 in COMPACT/KEYS). Both rulers still agree on
    # every form, so the CHOICE stays safe in both locales. The same move also
    # dropped `c cli`, `␣ expand` and `r` entirely. So the three forms measure
    # 50 / 30 / 20, and `esc` — bound but deliberately unlisted — is in NONE.
    assert a._footer(79) == a.FOOTER
    assert a._footer(80) == a.FOOTER, "the full form fits the classic 80 columns again"
    assert a._footer(45) == a.FOOTER_COMPACT
    assert a._footer(29) == a.FOOTER_KEYS
    assert a._display_width(a.FOOTER) == 50
    assert a._display_width(a.FOOTER_COMPACT) == 30
    assert a._display_width(a.FOOTER_KEYS) == 20
    # And the boundary is the MEASUREMENT, checked from both sides of each form,
    # so a future edit to the strings cannot quietly move a threshold. FOOTER_KEYS
    # is excluded from the lower side on purpose: it is the FLOOR, and `_footer`
    # returns it unconditionally below COMPACT rather than measuring it.
    for form in (a.FOOTER, a.FOOTER_COMPACT):
        exact = a._display_width(form)
        assert a._footer(exact) == form, form
        assert a._footer(exact - 1) != form, form
    floor = a._display_width(a.FOOTER_KEYS)
    assert a._footer(floor) == a.FOOTER_KEYS
    assert all(a._footer(w) == a.FOOTER_KEYS for w in range(0, floor + 1))
    # Only labels degrade: every form names every key it lists, and `esc` — bound
    # but deliberately unlisted — is in NONE of them.
    for form in (a.FOOTER, a.FOOTER_COMPACT, a.FOOTER_KEYS):
        assert "q" in form and "a" in form and "t" in form, form
        assert "esc" not in form, form
        assert ("↑/↓" in form or "↑↓" in form), form
    # THE SEPARATOR IS SPACED IN THE TWO LABELLED FORMS AND ABSENT FROM COMPACT:
    # COMPACT is the degraded form and has already given up words, so it carries no
    # separator glyph of its own.
    for form in (a.FOOTER, a.FOOTER_KEYS):
        assert a.FOOTER_SEP in form, form
    assert a.FOOTER_SEP not in a.FOOTER_COMPACT, a.FOOTER_COMPACT


def test_the_footer_is_measured_the_same_in_a_cjk_locale():
    # The regression this pins, in the locale that re-created it: ↑ (U+2191) and
    # ↓ (U+2193) are East Asian AMBIGUOUS — one column in a Latin locale, TWO in a
    # CJK one. The FIRST time they were in the footer the ruler charged them one, so
    # `_footer` measured the full form at 64 and chose it for a 64-column pane while
    # a CJK terminal painted 66 and `_clip` cut keys off. They are BACK now, and what
    # keeps them safe is that BOTH rulers charge them two: `_display_width` and this
    # test's CJK rule agree on every form, so the form chosen for a width fits it in
    # either locale. That agreement is the invariant — NOT "no Ambiguous glyph".
    #
    # THE SAME TRAP WAS ALSO SET BY THE SEPARATOR — ` · ` (U+00B7) is AMBIGUOUS,
    # charged two, which pushed the full form to 85 and cost the pane its labels —
    # and it is U+2219 BULLET OPERATOR, NARROW. The only disagreement left is against
    # `len`, the ruler the whole pane is composed for (the box borders are Ambiguous
    # and composed the same way), so the trailing strip reaches the pane edge in the
    # western terminal this ships to.
    import unicodedata

    # The separator is NARROW, so both rulers charge it the same three columns.
    assert unicodedata.east_asian_width("\u2219") == "N"
    assert a._display_width(a.FOOTER_SEP) == _cjk_width(a.FOOTER_SEP) == 3
    for form in (a.FOOTER, a.FOOTER_COMPACT, a.FOOTER_KEYS):
        assert a._display_width(form) == _cjk_width(form), form
        # The MOVE ARROWS are the only Ambiguous glyph the footer carries, and both
        # rulers charge them two — that agreement is the whole point, so any OTHER
        # Wide/Fullwidth/Ambiguous glyph reaching a form is the bug this catches.
        for ch in form:
            if unicodedata.east_asian_width(ch) in ("W", "F", "A"):
                assert ch in ("↑", "↓"), (ch, form)

    # The arrows ARE Ambiguous — the fact that makes the two-column charge load-
    # bearing, and the reason they cost 5 measured columns, not 3. ⏎ and ∙ stay
    # Narrow — checked, not assumed.
    for glyph in ("↑", "↓"):
        assert unicodedata.east_asian_width(glyph) == "A", glyph
    for glyph in ("⏎", "∙"):
        assert unicodedata.east_asian_width(glyph) == "N", glyph

    # Every form `_footer` picks for a pane at least as wide as the KEYS floor
    # fits that pane under the CJK rule too, so no key is ever clipped.
    floor = _cjk_width(a.FOOTER_KEYS)
    for width in range(floor, 90):
        assert _cjk_width(a._footer(width)) <= width, width


def test_the_footer_separator_is_not_ambiguous():
    """THE TEST THAT STOPS THIS RECURRING: the separator glyph must not be East
    Asian Ambiguous.

    An Ambiguous glyph is one column in a Latin locale and TWO in a CJK one, and
    `_display_width` — the ruler `_footer` chooses a form with — charges it two.
    That is not hypothetical: the separator was U+00B7 MIDDLE DOT, "A", so the full
    form measured 85 and the compact form 63, past both the classic 80-column
    terminal and the ~46-column agents split, which fell all the way to the bare
    KEYS form. The labels — the whole point of the footer — disappeared at the
    width the operator actually runs. U+2219 BULLET OPERATOR is "N", one column
    under both rules. This asserts the property directly, so a future edit that
    swaps in another Ambiguous glyph fails HERE rather than silently eating the
    labels in a CJK terminal.
    """
    import unicodedata
    glyph = a.FOOTER_SEP.strip()
    assert len(glyph) == 1, a.FOOTER_SEP
    width = unicodedata.east_asian_width(glyph)
    assert width != "A", \
        "an Ambiguous separator silently costs the pane its labels (see the footer comment)"
    assert width not in ("W", "F"), "a Wide/Fullwidth separator would shear the row"


def test_the_footer_key_is_prominent_and_its_label_recedes():
    """1a. Each entry is TWO spans, so the KEY reads as the prominent half: the key
    takes the theme's `text` slot AND is exempt from the footer line's DIM, while
    the label and the ` ∙ ` separator take `meta` and dim with the chrome."""
    spans = a._footer_spans(120)          # wide enough for the full form

    key_text = "".join(t for t, role in spans
                       if a._bare_role(role) == a.FOOTER_KEY)
    label_text = "".join(t for t, role in spans
                         if a._bare_role(role) == a.FOOTER_LABEL)
    # The full form labels every binding it lists, naming the move keys `↑/↓` and
    # Enter by its key glyph (`⏎`); Esc is bound but deliberately UNLISTED.
    for key in ("↑/↓", "⏎", "t", "a", "q"):
        assert key in key_text, (key, key_text)
    for label in ("move", "focus", "theme", "access", "quit"):
        assert label in label_text, (label, label_text)
    # THE SEPARATOR IS THE DIMMER OF THE TWO ROLES, and there is exactly one per
    # join between entries.
    assert label_text.count(a.FOOTER_SEP) == len(a.FOOTER_ENTRIES) - 1
    # ONLY the key is exempt from the line's brightness: the exemption and the role
    # are the same fact, and nothing else carries it.
    for _text, role in spans:
        assert role.startswith(a.UNDIM) == (a._bare_role(role) == a.FOOTER_KEY), role
    # And the two roles name the theme slots the task asked for.
    assert a._theme_role(a.FOOTER_KEY) == "text"
    assert a._theme_role(a.FOOTER_LABEL) == a.META == "meta"
    # The key is the `text` slot at FULL brightness; the label is `meta` on the
    # line's DIM — so the pairing is prominence, not merely two colours.
    assert a._role_brightness(a.undim(a.FOOTER_KEY)) == a.NORMAL
    assert a._role_brightness(a.FOOTER_LABEL) is None


def test_the_footer_row_is_padded_to_the_pane_with_the_strip(monkeypatch):
    """1c. `paint_line` colours only the cells a span covers, so without padding the
    strip stops at the last key and the window's own background shows between it
    and the pane edge. The row is padded to `width` with the STRIP's role — never
    PLAIN, which would paint no strip at all — and the strip really does reach
    `init_pair` under a theme."""
    for width in (26, 46, 80, 120):
        assert a._spans_width(a._footer_spans(width)) == width, width
    # Where there is padding to place, its span carries the strip's role.
    for width in (46, 80, 120):
        assert a._footer_spans(width)[-1][1] == a.FOOTER_LABEL, width

    frame = a.Frame(rows=[header()])
    theme = _theme("catppuccin-mocha")
    fake, _writes = _colour_paint(monkeypatch, frame, width=120, theme=theme)
    assert any(bg == a.hex_to_ansi256(a.footer_strip(theme))
               for _p, _fg, bg in fake.init_pair_calls), fake.init_pair_calls


def test_the_footer_roles_take_the_strip_background_under_a_theme():
    """The footer's two roles name their theme slots for the FOREGROUND and the
    derived strip for the BACKGROUND — one span, two answers from the same role."""
    theme = _theme("nord")
    strip = a.footer_strip(theme)
    assert strip is not None and strip != theme.colour("bg")
    assert a._role_background(a.FOOTER_KEY, theme) == strip
    assert a._role_background(a.FOOTER_LABEL, theme) == strip
    # The `undim:` modifier is a brightness, not a colour: it changes nothing here.
    assert a._role_background(a.undim(a.FOOTER_KEY), theme) == strip


# ---------------------------------------------------------------------------
# 22. THEMES: the background decision the `_VENDOR_COLOURS` comment deferred
# ---------------------------------------------------------------------------
#
# The comment above `_VENDOR_COLOURS` said, in as many words: "That is a
# background decision, not a fourteen-colour retune, so it is not made here." It
# is made here. These tests pin the decision on both sides of it:
#
#   * under a real theme the theme's `bg` reaches `init_pair` as the SECOND
#     argument, and the window background is filled so the pane is not stripes;
#   * under `og` — and `og` only — nothing changes at all, which is what the
#     golden render at the bottom of this section exists to prove.
#
# The COLOUR-ROLE tests above all paint the `og` theme (see `_colour_paint`), so
# every one of them still asserts today's indices. That is the point of the row.

DRACULA_BG = a.hex_to_ansi256("#282a36")


def test_the_themes_background_reaches_init_pair_as_the_second_argument(monkeypatch):
    """THE DECISION. `init_pair(n, fg, -1)` gave every colour the TERMINAL's own
    background; on a light terminal that inverted the whole logo table into the
    failure the table's own floor exists to prevent. A theme supplies the
    background, and it has to arrive as the second argument to be any use."""
    frame = a.Frame(rows=[header(),
                          worker("w1", "idle", harness="claude-native",
                                 meta="12s")], selected=1)
    fake, writes = _colour_paint(monkeypatch, frame, theme=_theme("dracula"))
    assert fake.init_pair_calls, "a coloured frame must spend a pair"
    theme = _theme("dracula")
    selected_bg = a.hex_to_ansi256(theme.colour("sel_bg"))
    # The footer's strip is a THIRD background the pane spends — a shade of `bg`
    # again, on its own row (see `_role_background`). On dracula it lands on a cell
    # clear of both `bg` and `sel_bg` (the strip's guard requires it), so it is
    # named here explicitly rather than relied on to coincide with either.
    strip_bg = a.hex_to_ansi256(a.footer_strip(theme))
    allowed = {DRACULA_BG, selected_bg, strip_bg}
    for _pair, fg, bg in fake.init_pair_calls:
        assert bg in allowed, (fg, bg)
        assert bg != -1, "the -1 background is exactly what this change replaced"
    assert any(bg == DRACULA_BG for _p, _fg, bg in fake.init_pair_calls)
    # And the body's own colour comes from the theme too, not from pair 0.
    meta = _writes_for(writes, "12s")
    assert meta and all(attr & 0xFF00 for attr in meta)


def _background_calls(scr):
    """The attributes the view handed to the window background, in order."""
    return [attr for _name, _ch, attr in scr.bkgd_calls]


def test_the_window_background_is_filled_for_an_opaque_theme(monkeypatch):
    """`bkgdset`, or the background shows in STRIPES. Every cell no `addnstr`
    covers keeps the window background, so an opaque theme that does not set it
    paints coloured text floating on the terminal's own background.

    A CALL, not an attribute. `scr.bkgd_calls` records what was handed to
    `window.bkgdset(ch, attr)`; the previous version of this test read an
    assignable `scr.bkgd`, which the real `_curses.window` rejects outright.
    """
    frame = a.Frame(rows=[header()], selected=0)
    fake = FakeCurses(colors=256, color_pairs=256)
    monkeypatch.setitem(sys.modules, "curses", fake)
    scr = _WriteScreen(24, 80)
    fake.wrapper = lambda loop: loop(scr)
    a._run_tui(_StubModel(frame), a.DEFAULT_INTERVAL, "http://x",
               theme=_theme("dracula"))
    assert scr.bkgd_calls, "an opaque theme must SET the window background"
    name, ch, attr = scr.bkgd_calls[-1]
    assert ch == a.BKGD_CHAR, ("the background is a blank painted in a colour, "
                               "never a glyph", ch)
    assert attr & 0xFF00, attr


def test_the_window_background_is_left_alone_for_the_og_theme(monkeypatch):
    """AND NOT FOR `og`. Setting the background to a pair allocated for some
    other role would put a colour on the pane nobody chose; the whole of what
    this row promises is that the terminal's own background keeps coming
    through. NO CALL AT ALL is the claim, so that is what is asserted."""
    frame = a.Frame(rows=[header()], selected=0)
    fake = FakeCurses(colors=256, color_pairs=256)
    monkeypatch.setitem(sys.modules, "curses", fake)
    scr = _WriteScreen(24, 80)
    fake.wrapper = lambda loop: loop(scr)
    a._run_tui(_StubModel(frame), a.DEFAULT_INTERVAL, "http://x",
               theme=_theme("og"))
    assert scr.bkgd_calls == [], "og leaves the window background untouched"
    # Every background is the terminal's own (-1). The footer's label/meta spans
    # spend a pair of their own now, so this is a SET of backgrounds, not one —
    # what `og` promises is that none of them is a colour.
    assert set(bg for _p, _fg, bg in fake.init_pair_calls) == {-1}


def test_the_window_background_is_set_by_a_CALL_a_terminal_will_accept():
    """The test that would have caught the defect without a terminal.

    Three claims, all checkable in a suite that never opens a pty:

    1. On the REAL `_curses.window`, `bkgd`/`bkgdset` are callable methods, so
       the view has to CALL one. It was assigned instead, which cannot work.
    2. The real window has `__dictoffset__ == 0` — no instance dict — which is
       the MECHANISM that makes (1) unbinding impossible rather than merely
       discouraged, and is what `FakeScreen.__setattr__` mirrors.
    3. The fake's own background setter is callable too, and carries the same
       `(ch, attr)` parameter names the real one takes, so the two cannot drift
       into a shape one of them accepts and the other rejects.

    THE PRINCIPLE, and the reason this exists: a fake that is EASIER TO SATISFY
    THAN THE REAL OBJECT WILL CERTIFY CODE THAT CANNOT RUN. `FakeScreen.bkgd` was
    an assignable attribute on a plain Python object, so an assignment the real
    window refuses passed 1213 tests and did nothing on every terminal.
    """
    import curses
    import inspect

    real = curses.window
    assert callable(getattr(real, "bkgd")) \
        and callable(getattr(real, "bkgdset")), \
        "the window background is a METHOD on curses.window, never an attribute"
    assert real.__dictoffset__ == 0, \
        "curses.window carries no instance dict, which is why its methods are " \
        "read-only on an instance -- the reason FakeScreen.__setattr__ guards"

    scr = FakeScreen()
    assert callable(scr.bkgd) and callable(scr.bkgdset)
    for name in ("bkgd", "bkgdset"):
        # The real one is read off the CLASS (so `self` is still in it), the
        # fake off an INSTANCE (so `self` is already bound away).
        real_params = list(inspect.signature(getattr(real, name)).parameters)[1:]
        fake_params = list(inspect.signature(getattr(scr, name)).parameters)
        assert real_params == fake_params, (name, real_params, fake_params)

    # And the guard is live, not just documented: an assignment that curses
    # refuses must be refused here too.
    with pytest.raises(AttributeError):
        scr.bkgd = 256


def test_a_background_the_terminal_refuses_does_not_stop_the_view(monkeypatch):
    """A terminal that rejects the background call still has to show the pane,
    so `curses.error` — and ONLY `curses.error` — is survivable.

    The companion is the real point: a `TypeError` from the same line is a BUG IN
    THE CALL, and a blanket `except Exception` around it is exactly what let the
    `stdscr.bkgd = attr` assignment fail silently on every terminal.
    """
    fake = FakeCurses(colors=256, color_pairs=256)
    monkeypatch.setitem(sys.modules, "curses", fake)

    class _Refuses(FakeScreen):
        def bkgdset(self, ch, attr):
            raise fake.error("bkgdset: ERR")

    scr = _Refuses(24, 80)
    fake.wrapper = lambda loop: loop(scr)
    a._run_tui(_StubModel(a.Frame(rows=[header()], selected=0)),
               a.DEFAULT_INTERVAL, "http://x", theme=_theme("dracula"))
    assert scr.painted, "the frame was painted, not skipped"

    class _MisCalled(FakeScreen):
        def bkgdset(self, ch, attr):
            raise TypeError("bkgdset() takes exactly 2 arguments (1 given)")

    boom = _MisCalled(24, 80)
    fake.wrapper = lambda loop: loop(boom)
    with pytest.raises(TypeError):
        a._run_tui(_StubModel(a.Frame(rows=[header()], selected=0)),
                   a.DEFAULT_INTERVAL, "http://x", theme=_theme("dracula"))


def _picker_backgrounds(scr):
    """The background attributes the view set during a run driven by the picker."""
    return [attr for _name, _ch, attr in scr.bkgd_calls]


def test_a_theme_change_moves_the_window_background_with_it(monkeypatch, tmp_path):
    """The background is not a STARTUP decision. Previewing an opaque theme under
    the picker has to fill the pane being previewed on, and CANCELLING it has to
    hand the terminal's own background back — otherwise the operator cancels and
    is left with a pane of colours they just rejected, painted behind glyphs that
    went back to `-1` backgrounds.

    Driven from `og`, so the startup sets nothing and every call recorded here is
    one the theme CHANGE made.
    """
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "save_theme", lambda slug: None)
    monkeypatch.setattr(a, "access_info", lambda server, environ=None: None)

    fake = FakeCurses(colors=256, color_pairs=256)
    monkeypatch.setitem(sys.modules, "curses", fake)
    # t, down onto the theme after `og`, Enter, q.
    apply_scr = _PickerScreen(24, 80)
    apply_scr.keys = [ord("t"), a.PICKER_DOWN, ENTER, ord("q")]
    fake.wrapper = lambda loop: loop(apply_scr)
    a._run_tui(_StubModel(_picker_frame()), a.DEFAULT_INTERVAL, "http://x",
               theme=_theme("og"))
    applied = _picker_backgrounds(apply_scr)
    assert applied, "applying a theme must move the window background"
    assert applied[-1] & 0xFF00, applied[-1]

    # t, down onto the same opaque theme, Esc, q. Cancelling hands back pair 0,
    # which `use_default_colors` has already made the terminal's own.
    cancel_scr = _PickerScreen(24, 80)
    cancel_scr.keys = [ord("t"), a.PICKER_DOWN, 27, ord("q")]
    fake.wrapper = lambda loop: loop(cancel_scr)
    a._run_tui(_StubModel(_picker_frame()), a.DEFAULT_INTERVAL, "http://x",
               theme=_theme("og"))
    cancelled = _picker_backgrounds(cancel_scr)
    assert any(attr & 0xFF00 for attr in cancelled), "the preview filled the pane"
    assert cancelled[-1] == 0, \
        "and cancelling gave the terminal's own background back"


def test_a_default_role_spends_no_pair_at_all():
    """`"default"` is legal in EVERY role, and it means the terminal's own. The
    whole reason `og` can be expressed at all is that PLAIN, sel_bg and rail all
    take that value — a loader that only allowed it in `bg` could not describe
    what the pane painted before themes existed. META is the one role `og` still
    colours, because the pane always did."""
    og = _theme("og")
    for role in (a.PLAIN, "undim:plain", a.ORCH):
        assert a._role_colour(role, og) is None, role
    assert a._role_colour(a.META, og) == 109, "og's meta is ansi 109, as today"
    assert a._role_background(role, og) is None, role
    assert a._role_background(a.SELECTED, og) is None, role
    # ...while the theme that DOES carry them spends real pairs for them.
    dracula = _theme("dracula")
    assert a._role_colour(a.PLAIN, dracula) == a.hex_to_ansi256("#ffd7af")
    assert a._role_colour(a.META, dracula) == a.hex_to_ansi256("#6272a4")
    assert a._role_colour(a.state_role("idle"), dracula) \
        == a.hex_to_ansi256("#50fa7b")
    assert a._role_colour(a.state_role("working"), dracula) \
        == a.hex_to_ansi256("#f1fa8c")


def test_parchment_is_the_dominant_text_colour_on_the_default_theme():
    """THE OPERATOR'S ASK. dracula.text is #ffd7af, a warm parchment, and PLAIN is
    what paints agent names, titles and box borders — so routing PLAIN at the
    theme's `text` is what makes parchment DOMINANT rather than merely present.

    #ffd7af is the cell the operator picked from the 256-colour palette, and it
    is ansi 223 exactly. It replaces #f0e6d2, whose nearest cell was 254 -- a
    pure grey -- so this used to be parchment on paper and grey on the terminal."""
    dracula = _theme("dracula")
    assert dracula.colour("text") == "#ffd7af"
    assert a._role_colour(a.PLAIN, dracula) == a.hex_to_ansi256("#ffd7af") == 223
    # ORCH is still a WEIGHT and no colour, and `undim:` is still not a colour.
    assert a._role_colour(a.ORCH, dracula) is None
    assert a._role_bold(a.ORCH) is True


def test_only_the_selected_row_gets_a_background(monkeypatch):
    """`sel_bg` is the one role that is a BACKGROUND, and selection was already a
    brightness axis — DIM vs NORMAL is untouched. Under `og` it is `"default"`
    and nothing moves, which is why the golden render below still holds."""
    frame = a.Frame(rows=[header(),
                          worker("w1", "blocked", text="one"),
                          worker("w2", "blocked", text="two", meta="12s")],
                    selected=2)
    dracula = _theme("dracula")
    assert a._role_background(a.PLAIN, dracula) == dracula.colour("bg")
    assert a._role_background(a.META, dracula) == dracula.colour("bg")
    # The selected LINE, which is a brightness rather than a span role -- so the
    # painter is what knows it, and passes it down.
    assert a._role_background(a.PLAIN, dracula, on_selected=True) \
        == dracula.colour("sel_bg")
    assert dracula.colour("sel_bg") != dracula.colour("bg")
    og = _theme("og")
    assert a._role_background(a.PLAIN, og) is None
    assert a._role_background(a.PLAIN, og, on_selected=True) is None

    fake, writes = _colour_paint(monkeypatch, frame, theme=dracula, keys=[DOWN, QUIT])
    backgrounds = {bg for _p, _fg, bg in fake.init_pair_calls}
    assert DRACULA_BG in backgrounds
    assert a.hex_to_ansi256(dracula.colour("sel_bg")) in backgrounds


def test_the_footer_strip_is_a_different_cell_from_the_background_of_every_theme():
    """THE WHOLE POINT OF THE STRIP GUARD, on BOTH cells it must avoid. A strip is
    derived as a HEX, but the terminal paints a CELL — and two different hexes snap
    to the same 256-colour cell all the time. This terminal reports COLORS=256 and
    can redefine none (no `ccc`), so a strip whose shade resolved to the
    background's OWN cell would be invisible and the feature would do nothing at
    all, silently — and one that resolved to `sel_bg`'s cell would paint the footer
    exactly as a SELECTED row. Every theme the catalog ships is checked for both
    separations here, and `og` — whose `bg` is the terminal's own — is checked to
    have NO strip, because there is no colour to shade. Before the guard covered
    `sel_bg`, six shipped themes (ayu-dark, bearded-dark, dracula, github-dark,
    monokai-dark, one-dark) put the strip on the selection's own cell."""
    themes = th.ordered_themes(th.load_catalog())
    opaque = 0
    for theme in themes:
        strip = a.footer_strip(theme)
        if theme.background() is None:
            assert strip is None, theme.slug
            continue
        opaque += 1
        cell = a.hex_to_ansi256(strip)
        assert cell != a.hex_to_ansi256(theme.colour("bg")), \
            f"{theme.slug}: the strip IS the background it is meant to sit on"
        if not theme.is_default("sel_bg"):
            assert cell != a.hex_to_ansi256(theme.colour("sel_bg")), \
                f"{theme.slug}: the strip IS the selection's cell, so the footer " \
                "reads as a selected row"
    assert opaque >= 25, opaque


def test_the_strip_guard_steps_past_a_shade_that_snaps_to_the_background_cell(tmp_path):
    """THE GUARD'S OWN TEETH, on a background that really does collide.

    `#000033` is chosen because its FIRST 12% shade lands on its OWN 256-cell (17)
    — which no hex reveals, only the cell does. The derivation must notice, step
    further, and answer with a cell that differs.
    """
    theme = _hand_edited_theme(tmp_path, bg="#000033")
    base = a.hex_to_ansi256("#000033")
    first = a.rgb_to_hex(a._shade(a.hex_to_rgb("#000033"), a.STRIP_STEP))
    assert a.hex_to_ansi256(first) == base, "the fixture no longer collides at step 1"

    strip = a.footer_strip(theme)
    assert strip is not None
    assert a.hex_to_ansi256(strip) != base


def test_the_strip_falls_back_to_a_neighbour_cell_when_no_shade_separates(
        tmp_path, monkeypatch):
    """The belt-and-braces branch: when NO step separates the cells, a defined
    NEIGHBOUR of the background's own cell is used — a different cell by
    construction. Driven by zeroing the step, so every candidate IS the
    background."""
    monkeypatch.setattr(a, "STRIP_STEP", 0.0)
    theme = _hand_edited_theme(tmp_path, bg="#282a36")
    base = a.hex_to_ansi256("#282a36")

    assert a.hex_to_ansi256(a.footer_strip(theme)) == base + 1


def test_an_og_footer_has_no_strip_and_spends_no_pair_for_one():
    """`og` PROMISES THE TERMINAL'S OWN BACKGROUND, and a strip is a colour this
    process would be inventing for a pane that has no background to shade. So there
    is none, the footer's roles resolve to `default`, and the key's own span spends
    no pair at all."""
    og = _theme("og")
    assert og.background() is None
    assert a.footer_strip(og) is None
    assert a.footer_strip(None) is None
    assert a._role_background(a.FOOTER_KEY, og) is None
    assert a._role_background(a.FOOTER_LABEL, og) is None

    fake = FakeCurses(colors=256, color_pairs=256)
    colours = a._Colours(fake, og)
    assert colours.role_attr(a.undim(a.FOOTER_KEY), og) == 0, \
        "a `default` foreground on no background is pair 0, not a new pair"


def test_an_opaque_theme_spends_no_sel_bg_pair_while_the_pane_is_untouched(monkeypatch):
    """THE THEME CONSEQUENCE OF THE UNTOUCHED PANE, on `github-dark`.

    `sel_bg` is only ever reached through `attr_for`'s `on_selected`, which is
    False on every span of every line while nothing is selected — so the painter
    allocates the pair for NOTHING. This is the assertion the theming change made
    load-bearing: a phantom selection is no longer a subtle brightness
    difference, it is a coloured bar on a row the operator never chose.

    THE THEME READS A BACKGROUND HONESTLY. github-dark tints its selection and its
    strip lands on a cell of its own — clear of both `bg` and `sel_bg`, which the
    strip's guard now enforces for every theme — so a background genuinely says
    whether `sel_bg` was spent. github-dark used to put the strip on `sel_bg`'s OWN
    cell, which is why this test briefly named another theme; the extended guard
    fixed that, so it is named here again.
    """
    rows = [header(),
            rail("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]
    theme = _theme("github-dark")
    sel_bg = theme.colour("sel_bg")
    assert sel_bg != theme.colour("bg"), "the theme really does tint its selection"
    strip = a.hex_to_ansi256(a.footer_strip(theme))
    assert strip not in (a.hex_to_ansi256(theme.colour("bg")),
                         a.hex_to_ansi256(sel_bg)), "the test could not tell them apart"

    def backgrounds(keys):
        fake, _writes = _colour_paint(monkeypatch, a.Frame(rows=rows),
                                      theme=theme, keys=keys)
        return {bg for _pair, _fg, bg in fake.init_pair_calls}

    untouched = backgrounds([QUIT])
    assert a.hex_to_ansi256(theme.colour("bg")) in untouched, "the theme is on"
    # Only the pane background and the footer's strip are spent — nothing else.
    assert untouched <= {a.hex_to_ansi256(theme.colour("bg")), strip}, untouched
    assert a.hex_to_ansi256(sel_bg) not in untouched, \
        "no sel_bg pair is allocated at all"

    # One arrow key, and the pair appears — so the absence above is the selection
    # being absent, not the theme being unable to spend one.
    assert a.hex_to_ansi256(sel_bg) in backgrounds([DOWN, QUIT])


def test_a_terminal_claiming_truecolor_paints_exactly_what_a_256_one_does(monkeypatch):
    """THE TRUECOLOR BRANCH IS GONE, and the claim is not that one of the two is
    broken but that they are now the SAME paint.

    It used to answer `0xFFD7AF` and `0x282A36` here and hand them to
    `init_pair`, whose second and third arguments are colour NUMBERS. Nothing in
    this ncurses (6.0.20150808) turns a packed triple into a colour —
    `curses.init_extended_pair` and `init_extended_color` are both absent — so
    what that painted was whatever slot 0xFFD7AF happened to name, and the
    terminal it claimed to serve (`TERM=tmux-256color`, `infocmp` reporting no
    `ccc`) reports 256 colours anyway. A hex that only survives as an exact RGB
    triple is a hex that renders as an arbitrary palette cell everywhere else.

    Two runs of the SAME frame, differing only in what the fake claims `curses`
    saw, and an assertion that every pair spent is identical. That is how a
    future re-add has to argue with a test rather than with a comment.
    """
    frame = a.Frame(rows=[header(),
                          worker("w1", "idle", harness="claude-native",
                                 meta="12s")], selected=1)
    theme = _theme("dracula")
    painted = {}
    for colors in (256, 1 << 24):
        fake, _writes = _colour_paint(monkeypatch, frame, colors=colors,
                                      theme=theme)
        painted[colors] = [(fg, bg) for _pair, fg, bg in fake.init_pair_calls]
        assert painted[colors], colors
    assert painted[1 << 24] == painted[256], painted
    # And what they agree on is the palette: the parchment is the CELL the
    # operator picked (223), not the hex and not a packed triple.
    assert (223, a.hex_to_ansi256("#282a36")) in painted[256], painted[256]
    assert all(fg == -1 or 0 <= fg < 256
               for fg, _bg in painted[256]), painted[256]
    # The window background asks for the terminal's own foreground (-1) against
    # the theme's own background, so the pane fills without a foreground.
    assert (-1, a.hex_to_ansi256("#282a36")) in painted[256], painted[256]
    # And a mark is its 256 PALETTE index: `_VENDOR_COLOURS` is a table of
    # palette cells, and the guard above it is reasoned in that space.
    assert (a._VENDOR_COLOURS["anthropic"], a.hex_to_ansi256("#282a36")) \
        in painted[256], painted[256]


def test_a_pair_number_outside_the_palette_is_refused_rather_than_allocated():
    """What the old truecolor branch would have reached, at the one place that
    can refuse it. `_Colours.attr` range-checks BOTH ends against `COLORS` and
    allows `-1` — curses' "the terminal's own" — on either side of a pair, and
    nothing else below the limit. A 24-bit RGB triple is not a colour number, so
    the honest answer is to paint the glyph bare, which is what every other
    unallocatable value on this path already gets."""
    class _C:
        has_colors = staticmethod(lambda: True)
        COLORS, COLOR_PAIRS = 256, 256
        init_pair_calls = []

        @staticmethod
        def init_pair(n, fg, bg):
            _C.init_pair_calls.append((n, fg, bg))

        @staticmethod
        def color_pair(n):
            return n << 8

    colours = a._Colours(_C, _theme("dracula"))
    assert colours.attr(0x282A36, 0x282A36) == 0, "a packed triple is not a cell"
    assert colours.attr(-5, 0x282A36) == 0, "-1 is the only inherit value"
    assert colours.attr(223, -5) == 0
    assert _C.init_pair_calls == []
    # ...while a cell on either end, and -1 on either end, still allocates.
    assert colours.attr(-1, 235), "the terminal's own text on the theme's bg"
    assert colours.attr(223, -1), "the theme's text on the terminal's own bg"
    assert len(_C.init_pair_calls) == 2


def _hand_edited_theme(tmp_path: Path, **colors) -> th.Theme:
    """A theme built the way an operator builds one: written to a `themes.json`
    and read back THROUGH THE LOADER.

    `_theme` reads the shipped catalog and exists so that no test can pin a
    `Theme` object the pane could not actually resolve. This is the same rule for
    the rows the catalog cannot reach — `text: "default"` on a theme with a
    coloured background is a legal file the shipped twenty-six never contain, and
    building it any other way would test a fiction.
    """
    row = {"label": "Hand Edited", "order": 99,
           "colors": {**{role: "default" for role in th.ROLES}, **colors}}
    path = tmp_path / "themes.json"
    path.write_text(json.dumps({"version": 1, "themes": {"hand": row}}),
                    encoding="utf-8")
    return th.load_catalog(path)["hand"]


def test_a_default_foreground_still_paints_on_a_coloured_background(tmp_path):
    """`"default"` MEANS CURSES -1 IN ANY ROLE, and `-1` is a legal `init_pair`
    FOREGROUND. A span whose text is `"default"` on a themed background is still
    a span on that background — it is the one that reads as the terminal's own
    text — and returning no pair for it drops the background silently, so the
    operator asks for terminal-coloured text on a dracula pane and gets terminal
    text on whatever the pane behind it happened to be.

    The shipped catalog cannot show this: `og` is the only theme with a
    `"default"` role anywhere and its `bg` and `sel_bg` are `"default"` too, so
    the theme is written here instead. Both ends are exercised, because a fix
    that only handled the pane's own background would leave the SELECTED row —
    the one row the operator is looking straight at — unthemed.
    """
    theme = _hand_edited_theme(tmp_path, bg="#282a36", sel_bg="#44475a")
    assert theme.is_default("text") and not theme.is_default("sel_bg")
    fake = FakeCurses(colors=256, color_pairs=256)
    colours = a._Colours(fake, theme)

    pane = colours.role_attr(a.PLAIN, theme)
    selected = colours.role_attr(a.PLAIN, theme, on_selected=True)
    assert pane and selected and pane != selected
    assert [(fg, bg) for _pair, fg, bg in fake.init_pair_calls] == [
        (-1, a.hex_to_ansi256("#282a36")),
        (-1, a.hex_to_ansi256("#44475a")),
    ], fake.init_pair_calls
    # The foreground really is -1 and not the theme's text colour: this theme's
    # `text` IS "default", so there is nothing else it could be.
    assert a._role_value(a.PLAIN, theme, 256) == th.CURSES_DEFAULT


def test_a_coloured_role_still_wins_where_it_has_a_colour_to_win_with(tmp_path):
    """AND THE OTHER DIRECTION. A coloured `text` on a `"default"` background is
    the case this all has to not break: the pair is allocated as before, with the
    terminal's own background behind it."""
    theme = _hand_edited_theme(tmp_path, text="#ffd7af")
    fake = FakeCurses(colors=256, color_pairs=256)
    colours = a._Colours(fake, theme)
    assert colours.role_attr(a.PLAIN, theme)
    assert [(fg, bg) for _pair, fg, bg in fake.init_pair_calls] \
        == [(223, th.CURSES_DEFAULT)], fake.init_pair_calls


def test_og_still_spends_no_pair_where_both_ends_are_default():
    """THE NO-REGRESSION HALF. Both ends `"default"` really is the case with no
    pair to spend: `use_default_colors` has already made pair 0 the terminal's own
    colours, so allocating `init_pair(n, -1, -1)` would spend a pair from a
    256-entry table to say exactly what pair 0 already says. The `og` row is the
    one that relies on it, and it is the reason the fix above had to be written
    as "unless BOTH ends are default" rather than "always".

    `ORCH` is here too and for a different reason: it is a WEIGHT and no colour,
    so there is no foreground to contribute at all. None is reserved for that,
    which is the distinction `_role_value`'s docstring draws.
    """
    og = _theme("og")
    fake = FakeCurses(colors=256, color_pairs=256)
    colours = a._Colours(fake, og)
    for role in (a.PLAIN, "undim:plain", a.ORCH):
        assert colours.role_attr(role, og) == 0, role
        assert colours.role_attr(role, og, on_selected=True) == 0, role
    assert fake.init_pair_calls == [], \
        "og must spend nothing to say nothing, selected row included"


def test_the_pair_table_is_keyed_by_the_pair_not_the_foreground():
    """One foreground against two backgrounds is two different paints. Keying the
    cache by fg alone would hand back the wrong pair the second time a theme
    changed, and the failure would be a background nobody chose."""
    class _C:
        has_colors = staticmethod(lambda: True)
        COLORS, COLOR_PAIRS = 256, 256
        error = type("error", (Exception,), {})
        init_pair_calls = []

        @staticmethod
        def init_pair(n, fg, bg):
            _C.init_pair_calls.append((n, fg, bg))

        @staticmethod
        def color_pair(n):
            return n << 8

    colours = a._Colours(_C, _theme("dracula"))
    first = colours.attr(109, DRACULA_BG)
    second = colours.attr(109, DRACULA_BG + 1)
    assert first != second, "the same fg on two backgrounds is two allocations"
    assert colours.attr(109, DRACULA_BG) == first, "and each is cached"
    assert len(_C.init_pair_calls) == 2


# --- the theme picker -------------------------------------------------------

def _picker_frame():
    return a.Frame(rows=[header(),
                         worker("w1", "idle", text="one"),
                         worker("w2", "working", text="two")], selected=1)


def test_the_picker_lists_every_theme_in_catalog_order():
    state = a._picker(active="dracula")
    assert state.slugs == [t.slug for t in th.ordered_themes(th.load_catalog())]
    assert state.slugs[0] == "og", "order 0 puts OG first in the menu"
    assert state.current() == "dracula", "it opens on the LIVE theme, not row one"
    assert state.labels["dracula"] == "Dracula"


@pytest.mark.parametrize("down,up", [(a.PICKER_DOWN, "k"),
                                     (ord("j"), ord("k"))])
def test_the_caret_moves_with_both_the_arrows_and_jk(down, up):
    start = a._picker(active="dracula")
    next_slug = start.slugs[start.cursor + 1]
    moved = a.picker_key(start, down)
    assert moved.current() == next_slug, (down, up)
    back = a.picker_key(moved, ord(up) if isinstance(up, str) else up)
    assert back.current() == "dracula", (down, up)


def test_the_caret_clamps_rather_than_wrapping():
    """A list you can fall off the end of and come back on is a list you can get
    lost in, and this one is read by eye."""
    state = a._picker(active="og")
    for _ in range(40):
        state = a.picker_key(state, a.PICKER_UP)
    assert state.cursor == 0
    for _ in range(40):
        state = a.picker_key(state, a.PICKER_DOWN)
    assert state.cursor == len(state.slugs) - 1


def test_enter_applies_and_esc_or_q_cancels():
    state = a._picker(active="dracula")
    state = a.picker_key(state, a.PICKER_DOWN)
    state = a.picker_key(state, 10)
    assert state.closed and state.applied == state.slugs[state.cursor]

    for cancel in (27, ord("q")):
        state = a._picker(active="dracula")
        state = a.picker_key(state, a.PICKER_DOWN)
        state = a.picker_key(state, cancel)
        assert state.closed, cancel
        assert state.applied is None, "a cancel is not an apply"


def test_an_unrecognised_key_is_ignored_rather_than_falling_through_to_the_pane():
    """The overlay owns the keyboard while it is up, so a key it does not answer
    must do NOTHING — not quit the pane behind it, not move the selection."""
    state = a._picker(active="dracula")
    for key in (ord("a"), ord("r"), ord(" "), ord("z")):
        after = a.picker_key(state, key)
        assert after.cursor == state.cursor and not after.closed, key
        assert after.applied is None


def test_a_closed_picker_ignores_everything_after_it():
    state = a.picker_key(a._picker(active="dracula"), 27)
    assert a.picker_key(state, 10).applied is None


def test_the_picker_draws_a_box_with_the_active_theme_marked():
    rows = [a.line_text(line) for line in a.picker_lines(
        a._picker(active="dracula"), 80, 24)]
    assert rows[0].startswith("┌─ theme ") and rows[-1].startswith("└")
    body = rows[1:-1]
    assert any("Dracula" in row and "•" in row for row in body), body
    assert any("▸ •Dracula" in row for row in body), body
    assert a.PICKER_KEYS in "\n".join(body)


def test_the_picker_shows_fewer_entries_rather_than_overflowing_the_pane():
    """The `access_lines`-drops-its-QR precedent. Twenty-six themes do not fit a
    short pane, and an overlay with its own bottom edge off-screen takes the key
    hint that says how to close it with it."""
    state = a._picker(active="dracula")
    for height in (6, 8, 12, 24):
        rows = a.picker_lines(state, 80, height)
        assert len(rows) <= height, (height, len(rows))
        assert all(len(a.line_text(row)) <= 80 for row in rows)
    # Truncated, and it says so, and the CARET is never one of the dropped rows.
    short = [a.line_text(line) for line in a.picker_lines(state, 80, 10)]
    assert any("…" in row for row in short), short
    assert any("▸" in row for row in short), short
    # And with the whole catalog on screen there is nothing to apologise for.
    tall = [a.line_text(line) for line in a.picker_lines(state, 80, 40)]
    assert not any("…" in row for row in tall), tall


def test_the_picker_scrolls_to_keep_the_caret_visible():
    state = a._picker(active="tokyo-night-light")     # the last row
    rows = [a.line_text(line) for line in a.picker_lines(state, 40, 10)]
    assert any("▸" in row and "Tokyo Night Light" in row for row in rows), rows


def test_the_picker_never_overflows_a_narrow_pane():
    state = a._picker(active="dracula")
    for width in range(a.PICKER_MIN_WIDTH, 48):
        for height in range(1, 30):
            rows = a.picker_lines(state, width, height)
            assert len(rows) <= height, (width, height, len(rows))
            assert all(len(a.line_text(row)) <= width for row in rows), (width, height)
    # Below the width a box can close in, it draws NOTHING rather than a
    # rectangle that does not close. The agents pane is never this narrow -- its
    # own footer floor is 14 columns -- so this guards the arithmetic.
    assert a.picker_lines(state, a.PICKER_MIN_WIDTH - 1, 24) == []
    assert a.picker_lines(state, 80, 0) == []
    assert a.picker_lines(state, 0, 0) == []


def test_t_opens_the_picker_and_esc_leaves_the_previous_theme(monkeypatch, tmp_path):
    """`t` reaches the overlay, Esc leaves it, and the pane behind it is intact.

    `save_theme` is replaced with a recorder rather than merely pointed at a
    temporary home: a test that can write into the OPERATOR's real ~/.omnigent
    if a future key change reaches Enter is not a test anyone should leave
    unattended, and the recorder turns that into a visible failure.
    """
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    saved = []
    monkeypatch.setattr(a, "save_theme", lambda slug: saved.append(slug))
    fake = FakeCurses(colors=256, color_pairs=256)
    monkeypatch.setitem(sys.modules, "curses", fake)
    scr = _PickerScreen(24, 80)
    scr.keys = [ord("t"), 27, ord("q")]
    fake.wrapper = lambda loop: loop(scr)
    assert a._run_tui(_StubModel(_picker_frame()), a.DEFAULT_INTERVAL,
                      "http://x", theme=_theme("dracula")) == 0
    assert any(a.PICKER_TITLE in row for row in scr.painted), "the overlay opened"
    assert saved == [], "a cancel persists nothing"
    assert not th.theme_state_path().exists()


def test_enter_in_the_picker_applies_and_persists_the_theme(monkeypatch, tmp_path):
    """The other half: Enter applies, the pane keeps its own state, and the choice
    survives into the next launch. The live repaint is what makes applying
    VISIBLE, and it is the one thing `paint_needed` would otherwise skip — the
    lines are byte-identical under any theme, so nothing else would repaint.
    """
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "access_info", lambda server, environ=None: None)
    fake = FakeCurses(colors=256, color_pairs=256)
    monkeypatch.setitem(sys.modules, "curses", fake)
    scr = _PickerScreen(24, 80)
    # t, down (onto the theme after dracula), Enter, q.
    scr.keys = [ord("t"), a.PICKER_DOWN, 10, ord("q")]
    fake.wrapper = lambda loop: loop(scr)
    assert a._run_tui(_StubModel(_picker_frame()), a.DEFAULT_INTERVAL,
                      "http://x", theme=_theme("dracula")) == 0
    assert th.load_theme() == "github-dark", th.load_theme()
    assert not th.theme_state_path().parent.joinpath("og-install.json").exists()


# --- the CLI ----------------------------------------------------------------

def test_theme_is_a_parser_flag_and_does_not_persist(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    args = a.build_parser().parse_args(["--theme", "nord"])
    assert args.theme == "nord"
    assert a.build_parser().parse_args([]).theme is None


def test_an_unknown_theme_is_refused_by_name(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    opener = RoutingOpener([], details={})
    assert a.main(["--once", "--server", "http://x", "--theme", "nosuchtheme"],
                  opener=opener) == 2
    err = capsys.readouterr().err
    assert "nosuchtheme" in err
    for slug in ("dracula", "nord", "og"):
        assert slug in err, "the message names the valid ones"


# --- the golden -------------------------------------------------------------

GOLDEN_FRAME = [
    '╭──────────────────────────────────────────────────────────────────────────────╮',
    '│ ✳  conversation                                                   Claude Code│',
    '╰──────────────────────────────────────────────────────────────────────────────╯',
    '│',
    '│   ╭──────────────────────────────────────────────────────────────────────────╮',
    '⦿ ⠋ │    w1                                                             working│',
    '│   ╰──────────────────────────────────────────────────────────────────────────╯',
    '│   ╭──────────────────────────────────────────────────────────────────────────╮',
    '⦿ ⠋ │    w2                                                             working│',
    '│   ╰──────────────────────────────────────────────────────────────────────────╯',
    '│   ╭──────────────────────────────────────────────────────────────────────────╮',
    '⦿ ⠋ │    w3                                                             working│',
    '│   ╰──────────────────────────────────────────────────────────────────────────╯',
    '',
    # The footer row as `_pane` composes it: the form an 80-column pane gets —
    # the FULL form, back now that the separator is U+2219 (Narrow) and the form
    # measures 79 — padded with the background strip out to the pane edge.
    _footer_text(a.DEFAULT_WIDTH),
]


def _golden_frame(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    frame = _model(opener).refresh()
    frame.selected = a.snap_selection(frame.rows, None, 0)
    frame.access = None
    return frame, opener


def test_render_is_byte_identical_under_every_theme(monkeypatch, tmp_path):
    """THE STRUCTURAL FACT `og` DEPENDS ON. `render` is a pure function from
    state to text and never sees a colour, so all twenty-six themes have to
    produce the same bytes. If this ever fails, the whole "choose `og` and get
    today's pane" promise is already false."""
    frame, _opener = _golden_frame(monkeypatch, tmp_path)
    rendered = {theme.slug: a.render(frame, width=80)
                for theme in th.ordered_themes(th.load_catalog())}
    assert len(rendered) == 26
    for slug, lines in rendered.items():
        assert lines == rendered["og"], (slug, lines)
    assert rendered["og"] == GOLDEN_FRAME


def test_the_golden_frame_is_what_the_pane_has_always_rendered(monkeypatch, tmp_path):
    """A FROZEN copy, not just self-consistency. Without it "every theme renders
    the same" would happily pass on a frame that had quietly changed shape, and
    the only proof that this whole background change cost an `og` user nothing is
    that the bytes did not move."""
    frame, _opener = _golden_frame(monkeypatch, tmp_path)
    assert a.render(frame, width=80) == GOLDEN_FRAME


def test_once_with_og_selected_is_byte_identical_to_once_with_no_theme(
        monkeypatch, tmp_path, capsys):
    """THE WHOLE ARGUMENT, end to end through `main`. With `og` selected --theme
    og, and again with it merely configured -- the output is the frozen golden,
    which is what an operator who wants none of this is entitled to."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    monkeypatch.setattr(a.shutil, "get_terminal_size", lambda *a, **kw: os.terminal_size((80, 24)))

    assert a.main(["--once", "--all", "--server", "http://x", "--theme", "og"],
                  opener=opener) == 0
    assert capsys.readouterr().out.splitlines() == GOLDEN_FRAME

    monkeypatch.setenv(th.THEME_ENV, "og")
    assert a.main(["--once", "--all", "--server", "http://x"], opener=opener) == 0
    assert capsys.readouterr().out.splitlines() == GOLDEN_FRAME


def test_a_one_shot_theme_flag_does_not_write_the_stored_choice(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    monkeypatch.setattr(a.shutil, "get_terminal_size", lambda *a, **kw: os.terminal_size((80, 24)))
    a.main(["--once", "--all", "--server", "http://x", "--theme", "nord"],
           opener=opener)
    capsys.readouterr()
    assert not th.theme_state_path().exists(), \
        "--theme is a one-shot; only the picker persists"


def test_the_stored_choice_survives_into_the_next_launch(monkeypatch, tmp_path,
                                                         capsys):
    """The env beats the stored slug and the stored slug beats the default, in that
    order -- the env is set before the command and the flag is typed now."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    th.save_theme("gruvbox")
    assert th.load_theme() == "gruvbox"
    monkeypatch.setenv(th.THEME_ENV, "nord")
    assert th.load_theme() == "nord"


def test_the_picker_closes_rather_than_spinning_when_the_keyboard_breaks(
        monkeypatch, tmp_path):
    """`getch` returning ERR is a curses error, not a keypress. A dialog that
    spins forever because the keyboard is broken is worse than one that gives up
    and leaves the pane exactly as it was."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.setattr(a, "save_theme", lambda slug: pytest.fail("persisted"))
    fake = FakeCurses(colors=256, color_pairs=256)
    monkeypatch.setitem(sys.modules, "curses", fake)
    scr = _PickerScreen(24, 80)
    scr.keys = [ord("t"), -1, -1, ord("q")]     # open, then only ERR
    fake.wrapper = lambda loop: loop(scr)
    assert a._run_tui(_StubModel(_picker_frame()), a.DEFAULT_INTERVAL,
                      "http://x", theme=_theme("dracula")) == 0


def test_the_pane_scroll_offset_survives_a_theme_change(monkeypatch, tmp_path):
    """The overlay's row number must not be mistaken for the PANE's scroll
    offset. `offset` is the list's window in the enclosing loop, so a shadowing
    local in the overlay would repaint the tree at the overlay's row and scroll
    the operator's view to somewhere they never asked for."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    frame = a.Frame(rows=[header()] + [worker(f"w{i}", "idle", text=f"t{i}")
                                       for i in range(12)], selected=1)
    fake = FakeCurses(colors=256, color_pairs=256)
    monkeypatch.setitem(sys.modules, "curses", fake)
    scr = _PickerScreen(10, 46)
    scr.keys = [ord("t"), 27, ord("q")]
    fake.wrapper = lambda loop: loop(scr)
    a._run_tui(_StubModel(frame), a.DEFAULT_INTERVAL, "http://x",
               theme=_theme("dracula"))
    # The short pane forces a non-zero offset; the overlay must not change it.
    after = [row for _spans, row in
             [(l, a.line_text(l)) for l in a._pane(frame, 46, 10).items]
             if "t0" in row]
    assert after, "the tree still shows its first worker after a cancelled pick"


# ---------------------------------------------------------------------------
# 17. a failing listing DEGRADES the pane; it must not kill it
# ---------------------------------------------------------------------------
#
# `refresh` makes exactly ONE wire call `og_herdr_watch` does not already swallow
# — `fetch_listing` (every detail fetch returns None on failure, see its
# docstring) — and a transient non-200 from it used to escape into
# `curses.wrapper` and take the whole screen down. The measured failure was a 404
# at the WRONG port: `og agents` asked a hardcoded 6767 while og's own server ran
# on 6768. The pane must keep the last good rows, say so on the footer, and heal
# on the next tick. These drive the real `_run_tui` loop, because the previous
# frame the pane has to fall back to lives in the loop, not in the model.

def _listing_404():
    return HTTPError("http://x/v1/sessions", 404, "Not Found", {}, None)


def test_a_failed_listing_fetch_still_raises_out_of_refresh():
    """The watcher is RIGHT to raise; the LOOP is what copes (see below).

    `refresh` must not start swallowing wire errors — an unreachable server is a
    fact it has no previous frame to report against. Pinned so a future "fix" does
    not quietly move the swallow down here and lose the previous rows on the way.
    """
    opener = RoutingOpener([_listing_404()], details={"root": ROOT_DETAIL})
    with pytest.raises(HTTPError):
        _model(opener).refresh()


def test_a_failed_listing_keeps_the_last_rows_and_names_the_server_and_status(
        monkeypatch):
    """The pane survives a failed poll: prior rows stay, the footer explains it."""
    opener = RoutingOpener([_live_listing(), _listing_404()],
                           details={"root": ROOT_DETAIL})
    # The first poll succeeds, so there ARE prior rows to keep; the next TICK
    # polls again and fails. There is no manual-refresh key any more, so the
    # interval is shortened to zero and an inert key buys the second tick.
    screen = _run_tui_capture(monkeypatch, [ord("z"), QUIT],
                              model=_model(opener), interval=0.0)

    assert screen.frames, "the pane kept painting"
    last = "\n".join(screen.frames[-1])
    assert "w1" in last, "the last good rows are still on screen"
    assert "http://x" in last, "the notice names the server it tried"
    assert "HTTP 404" in last, "and the status, so a wrong port is diagnosable"


def test_the_pane_recovers_on_the_next_tick_once_the_server_answers(monkeypatch):
    """A server that comes back heals the pane with no restart: rows, no notice."""
    opener = RoutingOpener([_listing_404(), _live_listing()],
                           details={"root": ROOT_DETAIL})
    # The server answers again on the NEXT TICK (no manual-refresh key any more):
    # the interval is shortened to zero and an inert key buys the second tick.
    screen = _run_tui_capture(monkeypatch, [ord("z"), QUIT],
                              model=_model(opener), interval=0.0)

    assert screen.frames
    first, after = screen.frames[0], screen.frames[-1]
    assert any("HTTP 404" in t for t in first), first
    assert not any("w1" in t for t in first), "the failed poll had no rows to show"
    assert any("w1" in t for t in after), "the recovered poll paints the tree"
    assert not any("HTTP 404" in t for t in after), "and the outage notice is gone"


def test_a_healthy_empty_listing_is_empty_and_not_an_outage(monkeypatch):
    """The rule the whole change turns on: an empty pane and an unreachable server
    must not look the same. A server that answers with NO sessions renders an empty
    pane with the normal footer — no notice."""
    opener = RoutingOpener([[]], details={})
    screen = _run_tui_capture(monkeypatch, [QUIT], model=_model(opener))

    assert screen.frames, "the pane painted"
    painted = "\n".join(screen.painted)
    assert "HTTP" not in painted and "server http" not in painted, painted
    assert "╭" not in painted, "no session renders no box"


def test_the_listing_notice_names_the_url_and_the_http_status():
    err = HTTPError("http://x/v1/sessions", 404, "Not Found", {}, None)
    text = a.listing_failure_notice("http://127.0.0.1:6767", err)
    assert text == "server http://127.0.0.1:6767: HTTP 404 Not Found (retrying)"


def test_the_listing_notice_falls_back_to_a_url_error_reason():
    text = a.listing_failure_notice("http://x", URLError("Connection refused"))
    assert text == "server http://x: Connection refused (retrying)"


def test_the_listing_notice_never_raises_when_the_error_cannot_be_rendered():
    class Unrenderable(BaseException):
        reason = None

        def __str__(self):
            raise RuntimeError("no text")

    assert a.listing_failure_notice("http://x", Unrenderable()) \
        == "server unreachable: http://x"


def test_the_operator_supplied_server_flag_wins_over_bin_og_forwarding():
    """bin/og puts its `--server` FIRST (see `cmd_agents`), and argparse keeps the
    LAST occurrence — so the flag the operator typed is the one that wins. This is
    the ordering `cmd_agents`' comment relies on, pinned where it can be checked
    without a shell."""
    args = a.build_parser().parse_args(
        ["--server", "http://127.0.0.1:6768", "--server", "http://example:9999"])
    assert args.server == "http://example:9999"
