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
import json
import os
import sys
import types
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import URLError

import pytest

import og_agents as a
import og_herdr as h
import og_herdr_watch as w


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


def worker(sid, state, *, text="t", snippet=None, expanded=False,
           agent=None, meta="", harness=""):
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
                 snippet=snippet, expanded=expanded, agent=agent, meta=meta,
                 harness=harness)


def header(text="conversation", harness="claude-native", *, sid="root",
           done=0, total=0, state="working"):
    """A ROOT row: the heavy title bar and its done-gauge."""
    return a.Row(sid, text, state, 0, is_root=True, harness=harness,
                 done=done, total=total)


def rail(state="working"):
    """A breathing rail row, as `build_rows` emits it: the rail alone, opening a
    state group. It carries the group's state but draws no word — the worded group
    label is gone from the design."""
    return a.Row("", "", state, 1, rail=True)


# The worded group labels the C2 design used to draw, one per group. The pane
# dropped them — the animated glyph inside each box carries the state now — so
# every render test asserts none of these lines survives.
OLD_GROUP_LABELS = ("│ working", "│ blocked - waiting on you", "│ failed",
                    "│ unknown", "│ finished")


# ---------------------------------------------------------------------------
# 1. the renderer, as a pure function
# ---------------------------------------------------------------------------

def test_renderer_shows_a_root_title_bar_and_gauge_not_a_bare_line():
    """The operator's complaint ("the root is not boxed") is what this fixes:
    the root is now a heavy `━` title bar with its name inlaid, then a done-gauge
    with the harness label right-aligned — not the old one-line header."""
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

    assert lines[0].startswith("━━ Omnigent Herdr Integration ")
    assert set(lines[0]) == {"━", " "} | set("Omnigent Herdr Integration")
    assert "1/3 done" in lines[1], "w2 is the one idle (finished) worker"
    assert lines[1].rstrip().endswith("Claude Code"), "the harness label is right"


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
    assert sum(1 for line in lines[2:-2] if line == "│") == 3
    # The state is still told apart, by the glyph inside each box: a working
    # spinner frame, and the static `✗` and `○`.
    joined = "\n".join(lines)
    assert a.SPIN_FRAMES[0] in joined
    assert "✗" in joined and "○" in joined


def test_renderer_flattens_a_nested_worker_and_shows_an_expanded_snippet():
    """A grandchild is grouped by its OWN state like any other worker, and the
    expanded report is a row INSIDE the rounded box, not a loose line. Grouping
    by state and drawing a spine are mutually exclusive, so the tree is
    flattened."""
    root = dict(listed("root", status="running"), harness="claude-native")
    sessions = [
        root,
        listed("w1", parent="root", status="running", title="coder_zen:worker"),
        listed("g1", parent="w1", status="running", title="coder_zen:grandchild"),
    ]
    rows = a.build_rows(
        sessions, ["root"],
        snippets={"g1": "a child report"}, expanded_ids={"g1"}, now=NOW)
    lines = a.render(a.Frame(rows=rows))

    assert lines[0].startswith("━━ ")
    # The grandchild is a box of its own, in the same (working) group...
    assert any("grandchild" in line and "╭" not in line for line in lines)
    # ...and the report is drawn inside a box: the row that carries it is a body
    # row (`│…│`), never a loose line.
    report = [line for line in lines if "a child report" in line]
    assert report and report[0].startswith("│") and report[0].endswith("│")
    # Every box sits in the gutter: the rail at column 0, the rounded corner at
    # column 4, and the whole line as wide as the pane.
    tops = [line for line in lines if "╭" in line]
    assert tops and all(line.index("╭") == 4 for line in tops)
    assert all(len(line) == a.DEFAULT_WIDTH for line in tops)


def test_renderer_notes_a_truncated_listing():
    frame = a.Frame(rows=[header()], truncated=True)
    assert a.render(frame)[0].startswith("… partial listing")


# ---------------------------------------------------------------------------
# 2. the C2 shape, verbatim
# ---------------------------------------------------------------------------

C2_WIDTH = 46


def test_c2_shape_is_verbatim_title_gauge_rails_and_rounded_boxes():
    """The approved C2 template, line for line, minus the dropped labels: the
    heavy title bar, the gauge, a breathing rail opening each state group, and a
    ROUNDED box per worker BEHIND a rail+caret gutter. Built through `build_rows`
    from a listing, so the names, agents and glyphs are the ones the model
    produces at width 46. The working box's glyph is the first spinner frame.

    A box is THREE rows: top border, ONE content row, bottom border. The content
    row is the operator's order — glyph, 2-column mark slot, title flush left —
    with the meta right-aligned on the SAME row. The AGENT NAME is gone (the mark
    already says it), so nothing on a row repeats the mark.
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

    # NOTE the two spaces after ╔ (w3's Ambiguous z-ai mark) and after every
    # Narrow mark: the slot pads them all to the same two WESTERN columns, which
    # is what starts every title in the same column. One space would be the
    # sheared-title bug this pins against.
    assert lines == [
        "━━ Project Status Review ━━━━━━━━━━━━━━━━━━━━━",
        "██░░░░░░░░  1/4 done               Claude Code",
        "│",
        "│ ▸ ╭────────────────────────────────────────╮",
        "│   │ ⠋ ▯  coders                       2m14s│",
        "│   ╰────────────────────────────────────────╯",
        "│",
        "│   ╭────────────────────────────────────────╮",
        "│   │ ⏸ ⌘  reviewers           waiting on you│",
        "│   ╰────────────────────────────────────────╯",
        "│",
        "│   ╭────────────────────────────────────────╮",
        "│   │ ✗ ╔  scouts                      failed│",
        "│   ╰────────────────────────────────────────╯",
        "│",
        "│   ╭────────────────────────────────────────╮",
        "│   │ ○ ⌘  docs                   done 6m ago│",
        "│   ╰────────────────────────────────────────╯",
        "",
        a.FOOTER_COMPACT,
    ]
    # Every BOX row is exactly the pane width, and the caret is on the FIRST row
    # of its box — the `╭` border — and nowhere else.
    box_rows = [line for line in lines
                if (line.startswith("│") and line.endswith("│") and len(line) > 1)]
    assert box_rows and all(len(line) == C2_WIDTH for line in box_rows)
    # FOUR workers, ONE content row each — and FOUR tops and FOUR bottoms, so each
    # box is top border, content row, bottom border: three rows, not four.
    assert len(box_rows) == 4, box_rows
    assert sum(1 for line in lines if "╭" in line) == 4
    assert sum(1 for line in lines if "╰" in line) == 4
    assert sum(1 for line in lines if "▸" in line) == 1
    assert lines[3].startswith("│ ▸ ╭"), "the caret points at the box from outside"
    assert lines[3].index("▸") == 2, "the caret sits in its own column, after the rail"
    assert lines[3].index("╭") == 4, "the box starts after the gutter"
    # The AGENT NAME is dropped from every row: the mark carries it, so `zen`,
    # `cmdcode` and `freebuff` — the whole of the old second row — appear NOWHERE.
    for name in ("zen", "cmdcode", "freebuff"):
        assert name not in "\n".join(lines), name
    # The meta is right-aligned on the CONTENT row: each one ends at the box's
    # right edge, on the same line as the title.
    for row_index, meta in ((4, "2m14s"), (8, "waiting on you"),
                            (12, "failed"), (16, "done 6m ago")):
        assert lines[row_index].endswith(meta + "│"), lines[row_index]


def test_the_done_gauge_is_right_at_0_of_n_n_of_n_and_no_workers():
    def gauge_line(done, total):
        return a.render(a.Frame(rows=[header(done=done, total=total)]),
                         width=C2_WIDTH)[1]

    zero = gauge_line(0, 4)
    assert zero.startswith("░░░░░░░░░░  0/4 done")
    assert zero.endswith("Claude Code")
    assert len(zero) == C2_WIDTH

    full = gauge_line(4, 4)
    assert full.startswith("██████████  4/4 done")
    assert len(full) == C2_WIDTH

    # A root with no workers has nothing to be a fraction of: an empty bar, no
    # divide-by-zero.
    assert gauge_line(0, 0).startswith("░░░░░░░░░░  0/0 done")
    # Two of five fills four of ten cells, as the prototype draws it.
    assert gauge_line(2, 5).startswith("████░░░░░░  2/5 done")


def test_the_rail_runs_down_the_group_at_the_pane_left_column():
    """The rail is a column of its OWN at the pane's left edge, on the breathing
    rows, the label row and EVERY row of every box — one unbroken `│` down the
    whole group. The box has moved right, behind the caret, so it no longer
    carries that line itself; the PRE-C2 shape put the box edge in column 0 and
    the caret inside, which is exactly what this replaces."""
    rows = [header(),
            rail(),
            worker("w1", "working", text="one"),
            worker("w2", "working", text="two")]
    lines = a.render(a.Frame(rows=rows), width=C2_WIDTH)

    assert lines[0].startswith("━━ ")
    assert lines[2] == "│", "the breathing row is the rail alone"
    # Every group row — breathing and every line of both boxes — carries the rail
    # at column 0. The blank above the footer and the footer are last.
    for index, line in enumerate(lines[2:-2], start=2):
        assert line[0] == "│", (index, line)
    # The rounded corners sit AFTER the rail+caret gutter, at column 4.
    corners = [line for line in lines if "╭" in line or "╰" in line]
    assert corners and all(
        line.index("╭" if "╭" in line else "╰") == a.GUTTER for line in corners)


def test_the_caret_sits_between_the_rail_and_the_box_on_the_first_row_only():
    """`▸` is drawn in the gutter's caret column — between the rail and the box's
    `╭` — on the box's FIRST row alone. Blank on every other row of that box: a
    caret repeated down the box reads as several selected things, the rule the
    pre-C2 renderer already followed."""
    rows = [header(),
            rail(),
            worker("w1", "working", text="one"),
            worker("w2", "working", text="two")]
    lines = a.render(a.Frame(rows=rows, selected=2), width=C2_WIDTH)   # w1

    # Row 2 is the breathing rail; the box's THREE rows follow at 3..5, then the
    # next box at 6..8.
    assert lines[3][:4] == "│ ▸ ", "the caret leads the box's top border"
    assert "▸" not in lines[4] and "▸" not in lines[5], \
        "blank on the content and bottom rows of its own box"
    assert "▸" not in lines[6] and "▸" not in lines[7] and "▸" not in lines[8], \
        "the unselected box draws no caret at all"
    # The caret column (2) is strictly between the rail (0) and the box (4).
    assert lines[3].index("▸") == 2 and lines[3].index("╭") == a.GUTTER


def test_a_worker_box_is_three_rows_and_four_when_expanded():
    """The box collapsed from FOUR rows to THREE: top border, ONE content row,
    bottom border. The second content row held the agent name and is gone. SPACE
    still reveals the report as an EXTRA row INSIDE the box — now the only way to
    read a report — so an expanded box is four rows and the snippet is a box body
    row, never a loose line."""
    plain, caret_row = a.worker_box(
        worker("w", "working", text="task", agent="zen", meta="12s"),
        C2_WIDTH, True)
    assert len(plain) == 3, plain
    assert caret_row == 0, "the caret row is the box's first row"
    assert plain[0].startswith("│ ▸ ╭") and plain[2].startswith("│   ╰")
    assert all(len(line) == C2_WIDTH for line in plain)
    # ONE content row carries both halves: the title flush left, the meta flush
    # right, on the SAME line.
    assert "task" in plain[1]
    assert plain[1].rstrip("│").endswith("12s")
    # The agent name appears NOWHERE in the box: the mark carries it.
    assert "zen" not in "\n".join(plain)

    expanded, caret_row = a.worker_box(
        worker("w", "working", text="task", agent="zen", meta="12s",
               snippet="the report", expanded=True), C2_WIDTH, True)
    assert len(expanded) == 4, expanded
    assert caret_row == 0
    assert expanded[0].startswith("│ ▸ ╭") and expanded[3].startswith("│   ╰")
    assert all(len(line) == C2_WIDTH for line in expanded)
    # The report is a body row INSIDE the box, BELOW the content row.
    assert "the report" in expanded[2]
    assert expanded[2].startswith("│   │") and expanded[2].endswith("│")
    assert "task" in expanded[1] and expanded[1].rstrip("│").endswith("12s")


def test_the_title_yields_to_the_meta_on_the_shared_content_row():
    """TITLE and META share one line. The meta keeps its columns — it is the thing
    being waited on — and the TITLE is what gives way first, ELLIPSISED by `_clip`
    rather than silently blanked, so a tight row still reads the task (cut) and
    the meta (intact), and never exceeds the pane."""
    title = "fix: keep the state glyph at width one"
    row = worker("w", "blocked", text=title, agent="cmdcode",
                 meta="waiting on you")
    # Wide: the whole title is present, the meta flush right on the same line.
    wide, _ = a.worker_box(row, 78, False)
    assert title in wide[1], wide[1]
    assert wide[1].rstrip("│").endswith("waiting on you")

    # Tight: the title is ellipsised and the meta is INTACT and flush right.
    tight, _ = a.worker_box(row, 34, False)
    assert len(tight) == 3
    assert "…" in tight[1], tight[1]
    assert title not in tight[1], "the title gave way"
    assert tight[1].rstrip("│").endswith("waiting on you"), tight[1]
    assert len(tight[1]) == 34, "and the row still fits the pane"

    # A meta longer than the line can hold wins outright: the title is dropped and
    # only the meta (clipped) remains — the row still never exceeds the pane.
    over, _ = a.worker_box(worker("w", "blocked", text="task", agent="cmdcode",
                                  meta="waiting on you forever and ever"),
                           20, False)
    assert len(over) == 3 and all(len(line) == 20 for line in over)
    assert "task" not in over[1]
    # The content row is the gutter + the box edge, then the meta alone (clipped).
    assert over[1] == "│   │waiting on yo…│", over[1]


def test_the_state_glyphs_are_static_and_working_is_the_spinner():
    # The operator's static glyphs, one per state the viewer draws — and NO
    # static glyph for `working`, which the spinner owns...
    assert a.STATE_GLYPH == {"blocked": "⏸", "failed": "✗",
                             "unknown": "?", "idle": "○"}
    for state, glyph in (("idle", "○"), ("blocked", "⏸"), ("failed", "✗"),
                         ("unknown", "?")):
        frame = a.Frame(rows=[header(), worker("w", state)])
        assert glyph in "\n".join(a.render(frame, width=48)), state
        # ...and a static glyph does not move as the spinner advances.
        assert a.state_glyph(state, 0) == a.state_glyph(state, 5) == glyph
    # `working` is the SPINNER: a different frame each tick.
    assert a.STATE_GLYPH.get("working") is None
    assert a.state_glyph("working", 0) == a.SPIN_FRAMES[0]
    assert a.state_glyph("working", 1) == a.SPIN_FRAMES[1]
    assert a.state_glyph("working", 0) != a.state_glyph("working", 1)


def test_only_the_working_glyph_moves_with_the_spin_counter():
    """The spinner animates the WORKING row alone: at two spin values the working
    box differs in exactly one line, while a blocked, failed and idle box are
    byte-identical between the same two values."""
    def render_at(state, spin):
        return a.render(a.Frame(rows=[header(), rail(state), worker("w", state)],
                                spin=spin), width=C2_WIDTH)

    working0, working1 = render_at("working", 0), render_at("working", 1)
    assert working0 != working1, "a working row must visibly animate"
    # Exactly ONE line differs — the working box's content row — and the glyph
    # that changed is the working one.
    changed = [(before, after) for before, after in zip(working0, working1)
               if before != after]
    assert len(changed) == 1, changed
    assert a.SPIN_FRAMES[0] in changed[0][0]
    assert a.SPIN_FRAMES[1] in changed[0][1]

    for state, glyph in (("blocked", "⏸"), ("failed", "✗"), ("idle", "○")):
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


def test_the_caret_moves_between_workers_in_the_order_they_are_displayed():
    """THE HAZARD: grouping by state reorders the rows, so the caret must walk
    them in the order the eye reads — not in tree order. `Frame.rows` is itself
    in display order, which is what makes `move_selection` correct unchanged."""
    rows = [header(),
            rail("working"),
            worker("w1", "working", text="one"),
            rail("idle"),
            worker("w2", "idle", text="two"),
            worker("w3", "idle", text="three")]
    assert a.snap_selection(rows, None, 0) == 2, \
        "the view opens on the first WORKER, not the title bar or a rail"
    frame = a.Frame(rows=rows, selected=a.snap_selection(rows, None, 0))
    worker_text = {i: row.text for i, row in enumerate(rows) if a._is_box(row)}

    seen = []
    for step in (1, 1, -1, -1):
        frame.selected = a.move_selection(rows, frame.selected, step)
        seen.append(worker_text[frame.selected])
        lines = a.render(frame, width=48)
        assert sum(1 for line in lines if "▸" in line) == 1
    assert seen == ["two", "three", "two", "one"], "display order, not tree order"

    # No workers at all: the caret has nowhere to go and must not crash.
    assert a.move_selection([header()], 0, 1) == 0
    assert a.snap_selection([header()], None, 3) == 0


def test_the_caret_follows_its_session_across_a_state_change_reorder():
    """THE HAZARD THE GROUPING CREATES, and why the caret tracks an id.

    A selected worker that changes state moves from one group to another, so the
    rows reorder underneath it. Holding the INDEX across that refresh lands the
    caret on whichever worker now occupies it — here `w2`, selected at index 4 in
    the idle group, becomes `working` and moves to index 2, while index 4 in the
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
    # instead, so holding the index lands the caret on the wrong worker — and
    # Enter would focus w3's session.
    assert before[4].session_id == "w2"
    assert after[4].session_id == "w3" and a._is_box(after[4])
    assert a.snap_selection(after, "w2", 4) == 2, "the caret follows w2, not index 4"

    # The session GONE (it fell off the listing or the scope moved) falls back to
    # the box NEAREST the old index — so the caret stays where the operator's eye
    # is, rather than teleporting to the top of the pane.
    assert a.snap_selection(after, "no-such-session", 4) == 4
    assert a.snap_selection(after, "no-such-session", 9) == 4, "clamped, not off the end"
    assert a.snap_selection(after, "no-such-session", 0) == 2, "the first worker"
    # A tie between the box above and the box below goes to the UPPER one: index 3
    # in `before` sits between w1 (2) and w2 (4).
    assert a.snap_selection(before, "no-such-session", 3) == 2
    # An id that IS on screen but is not a BOX never captures the caret — not the
    # root's (the initial selection is index 0, the title bar) and not a rail's,
    # which is the empty string.
    assert a.snap_selection(before, before[0].session_id, 0) == 2
    assert a.snap_selection(before, "", 3) == 2
    # The no-id first frame still opens on the first worker, never the title bar.
    assert a.snap_selection(after, None, 0) == 2


def test_only_the_caret_row_of_the_selected_box_carries_the_selected_role():
    """Selecting a worker marks it with the caret, not a reverse-video block.

    `_pane` once tagged every line of the box SELECTED and `_run_tui` painted
    SELECTED reverse, so one selected worker lit a whole box. The role now
    belongs to the caret row alone; the rest of the block is NORMAL.
    """
    rows = [header(),
            rail("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]
    frame = a.Frame(rows=rows, selected=2)   # w1

    items = a._pane(frame, 48).items

    # bar, gauge, rail, box-top (caret), content, box-bottom, then w2's box...
    caret_rows = [i for i, (text, _role) in enumerate(items) if "▸" in text]
    assert caret_rows == [3], "the caret is on the box's top border"
    assert items[3][1] == a.SELECTED, "the caret row carries SELECTED"
    # Everything else is NORMAL — the rail, the selected box's content and bottom
    # rows, and the whole unselected box — or the box would read as highlighted.
    assert [role for _text, role in items[2:3]] == [a.NORMAL]
    assert [role for _text, role in items[4:9]] == [a.NORMAL] * 5
    assert sum(1 for _text, role in items if role == a.SELECTED) == 1
    # ...and it is the TITLE bar, not SELECTED, that spends REVERSE.
    assert items[0][1] == a.TITLE


def test_the_caret_is_the_only_marker_and_moves_with_the_selection():
    """The caret is the sole selection signal, and it is really drawn.

    With the reverse block gone the caret character is all that distinguishes
    the selected worker, so it has to be present in the rendered text and absent
    from the unselected box. Comparing the two selections proves nothing ELSE
    marks the row: only the caret column differs.
    """
    rows = [header(),
            rail("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]

    on_w1 = a.render(a.Frame(rows=rows, selected=2), width=48)   # caret row 3
    on_w2 = a.render(a.Frame(rows=rows, selected=3), width=48)   # caret row 6

    assert "▸" in on_w1[3] and "▸" not in on_w1[6]
    assert "▸" in on_w2[6] and "▸" not in on_w2[3]
    # The two frames differ ONLY in where the caret sits.
    blanked = [[line.replace("▸", " ") for line in frame] for frame in (on_w1, on_w2)]
    assert blanked[0] == blanked[1]


def test_a_selection_move_still_repaints_the_pane():
    """Moving the caret must still schedule a repaint.

    Every block is NORMAL except the caret row, so if the caret were not part of
    the composed text a selection move would change nothing and `paint_needed`
    would say False — the caret would appear stuck until an unrelated repaint.
    """
    rows = [header(),
            rail("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]
    frame = a.Frame(rows=rows, selected=a.snap_selection(rows, None, 0))   # w1
    before = a._pane(frame, 48).items

    frame.selected = a.move_selection(rows, frame.selected, 1)       # w2
    after = a._pane(frame, 48).items

    assert after != before, "the caret moved, so the composed lines changed"
    assert a.paint_needed(after, before) is True
    # The caret really did move to the other worker's box (w2's first row is 6).
    assert "▸" in after[6][0] and "▸" not in after[3][0]


def test_every_harness_mark_fills_the_slot_and_every_title_starts_in_one_column():
    """The box's content row carries the harness mark, and every mark spends the
    SAME two WESTERN display columns, so the glyph and the title line up down the
    list.

    THE THREE AMBIGUOUS MARKS ARE IN HERE ON PURPOSE. kilo, cognition and z-ai are
    East Asian AMBIGUOUS — a western terminal paints them ONE cell wide — and the
    slot once charged them two and left them unpadded, so their titles started one
    column LEFT of every Narrow row's. That is the shear this pins shut: one title
    column for all fourteen vendors, ambiguous or not, in the locale the operator
    runs. (Under a CJK locale those three rows shear right by one instead; that is
    the documented trade in `_slot_width`.)
    """
    harnesses = _harness_for_vendor()
    ambiguous = [harnesses[vendor] for vendor in sorted(AMBIGUOUS)]
    narrow = [harnesses[vendor] for vendor in sorted(set(MARK) - AMBIGUOUS)]
    assert len(ambiguous) == 3 and len(narrow) == 11, (ambiguous, narrow)

    title_col = {}
    for harness in narrow + ambiguous + ["totally-unknown", ""]:
        slot = a.harness_mark(harness)
        assert a._slot_width(slot) == 2, (harness, slot)
        line = a.render(a.Frame(rows=[worker("w", "working", text="ZX",
                                             harness=harness)]), 48)[1]
        assert "ZX" in line, (harness, line)
        title_col[harness] = line.index("ZX")

    assert len(set(title_col.values())) == 1, title_col
    # And the ambiguous marks are padded exactly like the Narrow ones: glyph plus
    # one space, not glyph alone.
    for vendor in AMBIGUOUS:
        assert a.harness_mark(harnesses[vendor]) == MARK[vendor] + " ", vendor


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


def test_multiple_roots_each_get_their_own_title_bar_and_gauge():
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

    bars = [line for line in lines if line.startswith("━━ ")]
    assert len(bars) == 2, "each root gets its own heavy title bar"
    assert bars[0].startswith("━━ first conv ")
    assert bars[1].startswith("━━ second conv ")
    assert "1/1 done" in joined, "the first root's own gauge"
    assert "0/1 done" in joined, "the second root's own gauge"
    assert "Claude Code" in joined and "Cline" in joined


def test_height_trimming_keeps_a_whole_group_and_never_splits_a_box():
    """The trimmer works in whole blocks (a root, or a rail and its boxes), so a
    box can never be cut in half and a group's rail never leaves its boxes. With
    the worded labels gone there is no label left to orphan, which is why the
    block is now just the rail and the boxes. A box is THREE rows, so the block
    arithmetic below is a rail + three rows."""
    rows = [header(), rail("working"),
            worker("w1", "working", text="one"),
            rail("idle"),
            worker("w2", "idle", text="two")]
    full = a.render(a.Frame(rows=rows), width=C2_WIDTH)
    joined = "\n".join(full)
    assert joined.count("╭") == 2 and joined.count("╰") == 2

    # Room for the root block (2 rows) and the first group (rail + 3-row box = 4
    # rows) but not the second group: the second group is dropped WHOLE, so its box
    # is never cut and its rail is never left behind without it.
    short = a.render(a.Frame(rows=rows), width=C2_WIDTH, height=10)
    short_joined = "\n".join(short)
    assert "one" in short_joined
    assert "two" not in short_joined, "a box must not be cut in half"
    assert short_joined.count("╭") == short_joined.count("╰") == 1
    # And no label word is left behind anywhere.
    for word in OLD_GROUP_LABELS:
        assert word not in short_joined, word


def test_height_trimming_never_splits_an_expanded_four_row_box():
    """The trim arithmetic follows the shorter box automatically, and it must
    still keep an EXPANDED box whole. A block is now a rail + a three-row box (four
    when expanded); across a range of heights a box is either absent or complete —
    its borders always come as a pair and the report row never outlives them."""
    rows = [header(), rail("working"),
            worker("w1", "working", text="one", snippet="the report",
                   expanded=True)]
    for height in range(6, 14):
        lines = a.render(a.Frame(rows=rows), width=C2_WIDTH, height=height)
        joined = "\n".join(lines)
        assert joined.count("╭") == joined.count("╰"), (height, lines)
        if "╭" in joined:
            assert "one" in joined and "the report" in joined, (height, lines)
            assert "\n".join(lines).count("╰") == 1, (height, lines)


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
                lines, caret_row = a.worker_box(
                    worker("w", state, text="a title long enough to clip",
                           agent="zen", meta="2m14s"),
                    width, selected)
                assert lines, (width, state)
                assert all(len(line) <= width for line in lines), (width, lines)
                assert caret_row == 0, (width, state)


def test_the_worker_row_degrades_in_the_documented_order():
    """The ladder: the caret column goes first, then the rail/gutter, then the
    box — the box carries the content, so it is the last thing to give up. Below
    even a minimal box, the row is one honest short line rather than a broken
    box."""
    row = worker("w", "idle", text="zx")

    # 8 and up: the shipped shape — rail, space, caret column, space, box.
    full, _ = a.worker_box(row, 8, selected=True)
    assert full[0].startswith("│ ▸ ╭"), full[0]
    assert all(len(line) == 8 for line in full)

    # 6 and 7: the caret column AND its space are gone; the rail is not.
    slim, _ = a.worker_box(row, 7, selected=True)
    assert slim[0].startswith("│ ╭"), slim[0]
    assert all(len(line) == 7 for line in slim)
    assert "▸" not in "".join(slim), "no caret column below the full gutter"

    # 4 and 5: the rail goes too; the box is the whole row.
    bare, _ = a.worker_box(row, 5, selected=True)
    assert bare[0].startswith("╭"), bare[0]
    assert all(len(line) == 5 for line in bare)

    # 1-3: no box can be drawn at all, so one honest line, state then task.
    tiny, caret_row = a.worker_box(row, 3, selected=True)
    assert len(tiny) == 1 and tiny[0] == "○ …", tiny
    assert caret_row == 0, "the SELECTED role still has a row to sit on"


def test_the_narrowest_worker_row_keeps_the_state_glyph():
    """Below a box the STATE is what survives — never a bare ellipsis.

    Rung 4 of the ladder promises one honest clipped line that still carries the
    state glyph, because the state is the one column worth keeping. `_clip`
    alone breaks that promise at width 1: handed the whole line it spends the
    single column on its own ellipsis (`…`) and the state is gone. Composing the
    glyph FIRST is what makes the promise true at 1, 2 and 3.
    """
    cases = (("working", a.SPIN_FRAMES[2]), ("blocked", "⏸"),
             ("failed", "✗"), ("idle", "○"))
    for state, glyph in cases:
        for width in (1, 2, 3):
            lines, caret_row = a.worker_box(
                worker("w", state, text="a title long enough to clip"),
                width, selected=False, spin=2)
            assert len(lines) == 1, (state, width, lines)
            assert glyph in lines[0], (state, width, lines)
            assert len(lines[0]) <= width, (state, width, lines)
            assert caret_row == 0, (state, width)
    # Width 1 is the glyph ALONE: there is no column left for the task.
    assert a.worker_box(worker("w", "idle", text="zx"), 1, True)[0] == ["○"]
    # 2 and 3 follow the glyph with as much of the clipped task as fits.
    assert a.worker_box(worker("w", "idle", text="zx"), 2, True)[0] == ["○…"]
    assert a.worker_box(worker("w", "idle", text="zx"), 3, True)[0] == ["○ …"]


def test_a_whole_rendered_frame_fits_any_pane_at_least_one_column_wide():
    # The end-to-end ladder check: title bar, gauge, rails, boxes and footer all
    # fit at every width from 1 up. A wrapped pane is what this rules out.
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
# 4. snippet extraction
# ---------------------------------------------------------------------------

def test_snippet_from_the_verbatim_assistant_message():
    assert a.snippet_from_detail({"items": [ASSISTANT_MESSAGE]}) == \
        "The work was already committed (`573414d`) before the interruption…"


def test_snippet_is_the_last_assistant_message():
    first = {"type": "message", "data": {"role": "assistant",
             "content": [{"type": "output_text", "text": "first"}]}}
    last = {"type": "message", "data": {"role": "assistant",
            "content": [{"type": "output_text", "text": "last"}]}}
    user = {"type": "message", "data": {"role": "user",
            "content": [{"type": "input_text", "text": "do it"}]}}
    assert a.snippet_from_detail({"items": [first, user, last]}) == "last"


def test_snippet_none_without_an_assistant_message():
    user = {"type": "message", "data": {"role": "user",
            "content": [{"type": "input_text", "text": "do it"}]}}
    assert a.snippet_from_detail({"items": [user]}) is None
    assert a.snippet_from_detail({"items": []}) is None
    assert a.snippet_from_detail({}) is None


def test_snippet_none_when_the_message_has_no_output_text():
    tool = {"type": "message", "data": {"role": "assistant",
             "content": [{"type": "tool_use", "id": "x"}]}}
    assert a.snippet_from_detail({"items": [tool]}) is None


def test_snippet_none_when_content_is_not_a_list():
    bad = {"type": "message", "data": {"role": "assistant", "content": "hello"}}
    assert a.snippet_from_detail({"items": [bad]}) is None


def test_snippet_tolerates_junk_without_raising():
    assert a.snippet_from_detail({"items": "nope"}) is None
    assert a.snippet_from_detail(None) is None
    assert a.snippet_from_detail({"items": [None, 7, "x"]}) is None


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


def test_a_selected_worker_is_fetched_once_not_every_refresh():
    opener = RoutingOpener(
        [_live_listing()] * 3,
        details={"root": ROOT_DETAIL,
                 "w1": detail("w1", items=[ASSISTANT_MESSAGE])})
    model = _model(opener)

    model.refresh()
    model.refresh(selected_id="w1")
    model.refresh(selected_id="w1")

    # root once, and w1 once even though it stayed selected across two refreshes.
    assert len(opener.detail_requests) == 2, [r.full_url for r in opener.detail_requests]


def test_an_idle_worker_is_read_once_when_it_finishes():
    listing = [
        listed("root", status="running", title="conversation"),
        listed("w1", parent="root", status="idle", title="done-worker"),
    ]
    opener = RoutingOpener([listing] * 3,
                           details={"root": ROOT_DETAIL,
                                    "w1": detail("w1", items=[ASSISTANT_MESSAGE])})
    model = _model(opener)

    for _ in range(3):
        model.refresh()

    assert len(opener.detail_requests) == 2, [r.full_url for r in opener.detail_requests]
    # ...and the report it captured is there when the row is expanded.
    joined = "\n".join(a.render(model.refresh(expanded_ids={"w1"})))
    assert "The work was already committed" in joined


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
    # The case the whole lazy "first idle" snippet exists for: the orchestrator
    # has answered, the worker is done, and its report is what the operator
    # wants to read. The root is live because it has a worker.
    listing = [
        listed("root", status="idle", title="conversation"),
        listed("w1", parent="root", status="idle", title="done-worker"),
    ]
    opener = RoutingOpener([listing],
                           details={"root": ROOT_DETAIL,
                                    "w1": detail("w1", items=[ASSISTANT_MESSAGE])})
    model = _model(opener)

    lines = a.render(model.refresh(expanded_ids={"w1"}))

    assert lines[0].startswith("━━ conversation ")
    assert any("done-worker" in line for line in lines)
    assert any("The work was already committed" in line for line in lines)


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
    # including the finished worker whose report is the tempting thing on screen.
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
    # liveness is free (the listing row already says idle-with-no-workers). What
    # IS fetched is `stale`'s own label and `s1`'s report — both pre-existing,
    # both for the worker the lazy "first idle" snippet exists to read.
    assert [r.full_url for r in opener.detail_requests] == [
        "http://x/v1/sessions/stale",
        "http://x/v1/sessions/s1",
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
    # The root's harness label resolves in the gauge line of its header.
    assert frame.rows[0].is_root
    assert a._root_lines(frame.rows[0], 60)[1][0].endswith("Claude Code")
    # `other` is MORE recently active than `mine` and is still not the answer:
    # recency breaks a tie between roots in `$PWD`, it does not outrank the cwd.
    assert opener.detail_requests, "both live roots were read"


# ---------------------------------------------------------------------------
# 7. a detail fetch that fails still renders the row
# ---------------------------------------------------------------------------

def test_a_failed_snippet_fetch_still_renders_the_row():
    opener = RoutingOpener(
        [_live_listing()],
        details={"root": ROOT_DETAIL, "w1": URLError("server down")})
    model = _model(opener)

    frame = model.refresh(selected_id="w1", expanded_ids={"w1"})
    lines = a.render(frame)

    assert any(a.SPIN_FRAMES[0] in line and "w1" in line for line in lines), lines
    assert not any("The work" in line for line in lines)


def test_a_failed_root_detail_still_renders_the_tree():
    opener = RoutingOpener([_live_listing()], details={"root": URLError("down")})
    model = _model(opener)

    lines = a.render(model.refresh())

    # The root does not vanish: with no harness to label it by, it falls back to
    # its title, and the tree beneath it still renders.
    assert lines[0].startswith("━━ conversation ")
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
# `_HARNESS_MARKS` is exactly what this is meant to catch.
MARK = {
    "anthropic": "\u2733",
    "opencode-zen": "\u25AF",
    "google": "\u2726",
    "openai": "\u2B21",
    "deepseek": "\U0001319E",
    "kilo": "\U0001F13A",
    "aws-kiro": "\U0001680C",
    "cursor": "\u21D7",
    "goose": "\U0001316F",
    "hermes": "\u2624",
    "xai": "\U0001D54F",
    "cognition": "\u26EF",
    "z-ai": "\u2554",
    "moonshotai": "\u2318",
}
# The three East Asian AMBIGUOUS picks: the slot charges them two cells and so
# pads them not at all. The other eleven are Narrow and get one trailing space.
AMBIGUOUS = {"kilo", "cognition", "z-ai"}


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
        # with the SLOT's ruler: the general `_display_width` would charge the
        # three Ambiguous marks an extra column and so disagree with the slot's
        # contract on purpose (see `_slot_width`).
        assert a._slot_width(got) == 2, (vendor, got)


def test_ambiguous_marks_are_padded_like_narrow_ones():
    # The slot's western ruler charges Ambiguous ONE cell, so those three are
    # padded exactly like a Narrow mark — glyph plus one space — and their titles
    # therefore start in the same column. This was the other way round (pessimistic
    # charge, no pad, sheared titles) and must not go back: see `_slot_width`.
    harnesses = _harness_for_vendor()
    for vendor, mark in MARK.items():
        got = a.harness_mark(harnesses[vendor])
        assert got == mark + " ", (vendor, repr(got))
    # And the classes are what the test claims them to be, not assumed.
    for vendor in AMBIGUOUS:
        assert unicodedata.east_asian_width(MARK[vendor]) == "A", vendor
    for vendor in set(MARK) - AMBIGUOUS:
        assert unicodedata.east_asian_width(MARK[vendor]) == "N", vendor


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
    # two WESTERN columns, and a general-ruler check would reject the three
    # Ambiguous marks the slot deliberately pads.
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
    # updated_at IS when an idle worker stopped, so "done N ago" is exact.
    assert a.worker_meta({"status": "idle", "updated_at": NOW - 360},
                         "idle", NOW) == "done 6m ago"
    assert a.worker_meta({}, "blocked", NOW) == "waiting on you"
    # A failure reason is used when the row carries one, and is never invented.
    assert a.worker_meta({"status": "failed"}, "failed", NOW) == "failed"
    assert a.worker_meta({"status": "failed", "error_message": "boot  failed"},
                         "failed", NOW) == "boot failed"
    # No timestamp: a word, not a figure derived from the epoch.
    assert a.worker_meta({}, "working", NOW) == "working"
    assert a.worker_meta({}, "idle", NOW) == "idle"


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
    # The caret opens on the first WORKER: rows 0-1 are the title bar and gauge
    # and row 2 is the group's breathing rail, none of which draw a box.
    expected_frame.selected = a.snap_selection(expected_frame.rows, None, 0)
    expected = a.render(expected_frame)

    rc = a.main(["--once", "--all", "--server", "http://x"], opener=opener)

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == expected
    assert out[0].startswith("━━ conversation "), "the root's title bar"
    assert out[1].endswith("Claude Code"), "the gauge, with the harness label"
    assert out[2] == "│", "a breathing rail opens the group"
    for word in OLD_GROUP_LABELS:
        assert word not in out, word
    assert any("╭" in line for line in out), "each worker is a rounded box"
    assert out[-1] == a.FOOTER


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

    assert lines[0].startswith("━━ conversation ")
    assert "┌─ access" not in "\n".join(lines), "the panel starts closed"
    assert a.Frame().access is None, "the toggle starts off"
    assert lines[-1] == a.FOOTER
    assert "a access" in a.FOOTER, "the footer must advertise the binding"


def test_the_access_box_is_content_sized_flush_left_and_drops_a_narrow_qr():
    qr = ["\x1b[40;37;1m" + "█" * 27 + "\x1b[0m"] + ["\x1b[40;37;1m" + "▀▄" * 13 + "\x1b[0m"]
    acc = _access(qr=qr, note="same wifi only — not reachable outside")
    note_width = len("same wifi only — not reachable outside")

    wide = a.render(a.Frame(rows=[header()], access=acc), width=80)
    box = a.access_lines(acc, 80)
    # The root block is two rows, then a blank, then the panel.
    assert "┌─ access " in wide[3], wide[3]
    assert wide[3] == box[0], "flush left: column 0, not under the rail"
    assert box[0].startswith("┌─ access ") and box[0].endswith("┐")
    assert len(box[0]) == note_width + a.BOX_PADDING, "sized to its content"
    assert all(len(line) == note_width + a.BOX_PADDING for line in box), \
        "a box whose rows differ in width is not a box"
    assert box[0] == "┌─ access " + "─" * (note_width - 7) + "┐"
    assert box[-1] == "└" + "─" * (note_width + 2) + "┘"
    assert any("█" * 27 in line for line in box), "27 columns + padding fits"
    assert acc.url in "\n".join(box)

    # The same code in a pane narrower than the code: dropped, address kept
    # (clipped by the pane, which is the only honest way to fit a 27-column URL
    # in 26 columns).
    narrow = a.access_lines(acc, 30)
    assert not any("█" in line for line in narrow), \
        "a wrapped code is unreadable noise, not a code"
    assert any(line.strip("│ ").startswith("http://192.168.100.176")
               for line in narrow), narrow
    assert narrow[-1].endswith("┘") and narrow[0].startswith("┌─ access ")
    assert all(len(line) == 30 for line in narrow), "still a box"


def test_the_access_panel_shows_the_qr_and_url_when_wide_enough():
    qr = ["\x1b[40;37;1m" + "█" * 27 + "\x1b[0m"]
    acc = _access(qr=qr, url="http://192.168.100.176:6767")

    lines = a.render(a.Frame(access=acc), width=80)

    assert any("█" * 27 in line for line in lines)
    assert "http://192.168.100.176:6767" in "\n".join(lines)
    assert lines[-2] == "" and lines[-1] == a.FOOTER, "blank, then the footer"


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
    # 26 columns is far below any footer form: it falls to the bare key list
    # rather than being clipped mid-word (see `_footer`).
    assert lines[0] == "" and lines[-1] == a._footer(26)
    assert a._display_width(lines[-1]) <= 26
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

    # The root block (bar + gauge), then a blank row, then the panel, then the
    # footer. Not a fraction of the pane, not a padded block.
    assert lines[0].startswith("━━ conversation ")
    assert "0/0 done" in lines[1], "the root is a title bar and a gauge"
    assert lines[2] == "", "a blank row separates the list from the panel"
    assert lines[3:3 + len(panel)] == panel
    assert len(panel) == 2 + 13 + 1 + 2, "2 borders + 13 QR + blank + URL + note"
    assert lines[-1] == a.FOOTER


def test_the_list_keeps_a_floor_of_rows_and_the_panel_is_never_squeezed():
    qr = [f"qr-row-{i}" for i in range(13)]   # a 19-row panel: 13 QR + blank + 2 info
    acc = _access(qr=qr)
    # The root block (2 rows) plus one-row rail blocks, so the floor is visible.
    rows = [header()] + [rail() for _ in range(6)]

    pane = a._pane(a.Frame(rows=rows, access=acc), width=80, height=8)

    # The tail (blank + panel + footer) is reserved whole and the list above it
    # keeps at least ACCESS_LIST_FLOOR rows.
    assert pane.list_rows == a.ACCESS_LIST_FLOOR
    # ...and the panel is still shown whole rather than squeezed away.
    assert any("qr-row-0" in text for text, _role in pane.items)
    # A block is never cut: the kept list is the root's two rows and whole rails.
    assert pane.items[2] == ("│", a.NORMAL)
    assert all(text != "" or role == a.NORMAL for text, role in pane.items[:3])


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
    assert not any("\x1b" in text for text, _role in a._pane(frame, 80).items)
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
    left is one frame's worth of writes.
    """

    def __init__(self, height, width):
        self.height, self.width = height, width
        self.writes: list[tuple[int, str, int]] = []

    def keypad(self, flag): pass
    def getmaxyx(self): return (self.height, self.width)
    def erase(self): self.writes.clear()
    def refresh(self): pass
    def timeout(self, ms): pass
    def getch(self): return ord("q")

    def addnstr(self, row, col, text, n, attr):
        self.writes.append((row, text[:n], n))


class _StubModel:
    """A model whose refresh always returns the same frame."""

    def __init__(self, frame):
        self._frame = frame

    def refresh(self, selected_id=None, expanded_ids=(), force=False):
        return self._frame


def _paint(frame, width, height, monkeypatch):
    """Run one real paint of `frame` and return the `addnstr` writes."""
    import curses

    scr = _FakeStdscr(height, width)
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "wrapper", lambda fn, *a, **k: fn(scr))
    a._run_tui(_StubModel(frame), 3.0, "http://x")
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
    exactly (and no more) is what replaces the old blanket `width - 1`.
    """
    import curses

    scr = _EdgeStdscr(5, 10, curses.error)

    # A normal row is written at the FULL width.
    a.paint_line(scr, 0, "╭────────╮", 10, 5, 0)
    assert scr.writes == [(0, 0, "╭────────╮", 10)], scr.writes

    # The bottom-right cell is attempted at full width and refused — swallowed,
    # never raised, and never pre-empted by writing a column short.
    a.paint_line(scr, 4, "╰────────╯", 10, 5, 0)

    # Out of range or a zero width is a no-op, not an error.
    a.paint_line(scr, 9, "x", 10, 5, 0)
    a.paint_line(scr, 0, "x", 0, 5, 0)
    assert scr.writes == [(0, 0, "╭────────╮", 10)], scr.writes


def test_the_painter_writes_a_boxes_full_width_so_its_border_survives(
        monkeypatch):
    """A box closes in the pane's last column; `width - 1` clipped it off.

    The old writer passed `width - 1` to `addnstr`, so every row lost its last
    column — exactly the `╮`, `│` and `╯` of a box's right edge — and every box
    rendered open on the right. This asserts on the recorded writes, which is
    where the truncation happened and where a pure `render` test cannot look.
    """
    frame = a.Frame(rows=[worker("w1", "working")], selected=0)
    writes = _paint(frame, 60, 20, monkeypatch)

    # A worker draws a guttered, rounded box; its rows are the only writes that
    # begin with the group rail, and a collapsed worker's box is three of them
    # (top border, one content row, bottom border).
    box = [text for _row, text, _n in writes if text.startswith(a.RAIL)]
    assert len(box) == 3, "one rounded box: top, content, bottom"
    assert all(len(line) == 60 for line in box), "each box row is the pane width"
    assert box[0].endswith(a.ROUND_CORNERS[2]), "the top rule closes on the right"
    assert box[-1].endswith(a.ROUND_CORNERS[5]), "the bottom rule closes on the right"
    assert all(n == 60 for _row, _text, n in writes), \
        "no row is written one column short"


def test_a_short_list_paints_its_footer_once(monkeypatch):
    """With the list shorter than the window the footer was painted twice.

    The window ran over the whole of `items`, tail included, and landed on the
    footer just above where the pinned tail drew it again — two footers, one
    under the last box and one at the pane bottom.
    """
    frame = a.Frame(rows=[header(), worker("w1", "working")], selected=1)
    writes = _paint(frame, 60, 24, monkeypatch)

    # A 60-column pane selects the compact footer form, so the expected line is
    # whatever form `_footer` picks for the width — not the full one.
    footers = [row for row, text, _n in writes if text == a._footer(60)]
    assert len(footers) == 1, "the footer is painted exactly once, not twice"
    assert footers[0] == 23, "and once means pinned to the pane's bottom row"


def test_a_long_list_keeps_one_footer_pinned_at_the_bottom(monkeypatch):
    """A list that overflows the window still shows exactly one footer.

    Here the window stops before the tail, so the pinned one is the only one —
    the regression guard that the short-list fix did not drop the tail.
    """
    rows = [header()] + [worker(f"w{i}", "working") for i in range(16)]
    frame = a.Frame(rows=rows, selected=1)
    writes = _paint(frame, 60, 12, monkeypatch)

    footers = [row for row, text, _n in writes if text == a._footer(60)]
    assert len(footers) == 1, "the footer is painted exactly once"
    assert footers[0] == 11, "and stays pinned to the bottom while the list scrolls"


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
    """A curses stand-in that records `use_default_colors`, and can refuse it.

    `refuse=True` makes `use_default_colors` raise the way a terminal with no
    colour support does — the call has to stay survivable, because a view that
    cannot start colour still has to paint.
    """

    def __init__(self, refuse=False):
        super().__init__("curses")
        self.refuse = refuse
        self.use_default_colors_calls = 0
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

    def wrapper(self, loop):
        loop(FakeScreen())


class FakeScreen:
    """A stdscr that answers one paint and then hands back `q`.

    Written as a plain class rather than a stub with attribute records: the
    view's only contract with the window is the method set below, and a screen
    that records its own calls would assert less about the fix than the call it
    exists to make possible.
    """

    def __init__(self, height=24, width=80):
        self.height, self.width = height, width
        self.painted: list[str] = []
        self.keys = [ord("q")]

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


def _fake_curses(monkeypatch, refuse=False):
    fake = FakeCurses(refuse=refuse)
    monkeypatch.setitem(sys.modules, "curses", fake)
    return fake


def _tui_model():
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    return _model(opener)


def test_the_view_takes_the_terminals_own_colours_so_the_pane_is_transparent(
        monkeypatch):
    # `curses.wrapper` calls start_color() before `loop` runs, and that is what
    # made every cell this view paints opaque black: pair 0 is the curses
    # default, not the terminal's. Nothing here sets a colour pair, so taking
    # the default for pair 0 is the whole fix.
    fake = _fake_curses(monkeypatch)

    assert a._run_tui(_tui_model(), a.DEFAULT_INTERVAL, "http://x") == 0

    assert fake.use_default_colors_calls == 1, \
        "pair 0 must be the terminal's own colours, once, at startup"


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
# 16. Enter focuses the selected worker in herdr; Space expands its snippet
# ---------------------------------------------------------------------------
#
# `focus_in_herdr` imports `og_herdr_client` LAZILY, so a fake module dropped
# into sys.modules IS the client it reaches — the same trick the curses stub
# above uses, and here it is what keeps every test off a real socket. HERDR_ENV
# is set or deleted per test so the "inside herdr" question is never answered by
# whatever environment the suite happens to run in (an operator running pytest
# from inside a herdr pane has HERDR_ENV=1 for real).

ENTER = 13          # what the view treats as Enter (curses.KEY_ENTER, 10 or 13)
SPACE = 32
QUIT = ord("q")


class _KeyScreen(FakeScreen):
    """`FakeScreen` with a settable key sequence and per-frame paint capture.

    `painted` (inherited) is every line ever written; `frames` is the same lines
    grouped into one list per `erase()`, which is what lets a test ask what the
    LAST frame looked like after a key changed the view.
    """

    def __init__(self, keys, height=24, width=80):
        super().__init__(height, width)
        self.keys = list(keys)
        self.frames: list[list[str]] = []

    def erase(self):
        self.frames.append([])

    def addnstr(self, row, col, text, n, attr):
        self.painted.append(text)
        if self.frames:
            self.frames[-1].append(text)


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


def _run_tui_capture(monkeypatch, keys, model=None):
    """Run the live view with a faked curses; return the screen it painted.

    Every Enter/Space test needs the frame the loop actually wrote, which the
    stock `FakeScreen` stub does not expose. `model` defaults to the shared
    `_tui_model()` (root + three running workers, `w1` selected).
    """
    fake = _fake_curses(monkeypatch)
    screens: list[_KeyScreen] = []

    def wrapper(loop):
        screen = _KeyScreen(keys)
        screens.append(screen)
        loop(screen)
    fake.wrapper = wrapper

    a._run_tui(model if model is not None else _tui_model(),
               a.DEFAULT_INTERVAL, "http://x")
    return screens[0]


def test_enter_focuses_the_selected_worker_with_that_rows_session_id(monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    calls = _fake_herdr_client(monkeypatch)
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    model = _model(opener)

    screen = _run_tui_capture(monkeypatch, [ENTER, QUIT], model=model)

    # Row 1 is the first worker (row 0 is the group header), so w1 was selected
    # and its session id is what went on the wire.
    assert calls == [("agent.focus", {"target": "w1"})], calls
    # Focus is not a reason to refetch: exactly the one listing walk for the
    # first paint, and none from the keypress.
    assert len(opener.listing_requests) == 1, "Enter must not trigger a refetch"
    # A successful focus leaves no notice behind.
    assert screen.painted, "the pane was painted"
    assert not any("focus:" in text for text in screen.painted)


def test_enter_outside_herdr_makes_no_call_and_shows_a_notice(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    calls = _fake_herdr_client(monkeypatch)

    screen = _run_tui_capture(monkeypatch, [ENTER, QUIT])

    assert calls == [], "no herdr pane, no call"
    assert any(a.NOTICE_FOCUS_OUTSIDE_HERDR in text for text in screen.painted), \
        screen.painted


def test_focus_degrades_when_the_herdr_client_cannot_be_imported(monkeypatch):
    # `None` in sys.modules makes `from og_herdr_client import ...` raise
    # ImportError: a client present in name only. The view still has to run.
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setitem(sys.modules, "og_herdr_client", None)

    screen = _run_tui_capture(monkeypatch, [ENTER, QUIT])

    assert any(a.NOTICE_FOCUS_UNAVAILABLE in text for text in screen.painted), \
        screen.painted


def test_a_focus_that_cannot_reach_herdr_shows_a_notice_and_the_loop_runs_on(
        monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    calls = _fake_herdr_client(monkeypatch,
                               error=RuntimeError("connect: no such socket"))

    screen = _run_tui_capture(monkeypatch, [ENTER, ENTER, QUIT])

    # One Enter per press: the failure neither swallows the key nor retries, and
    # the loop is still taking keys after the first failure.
    assert [method for method, _params in calls] == ["agent.focus", "agent.focus"]
    assert any("focus: " in text for text in screen.painted), screen.painted


def test_space_expands_and_collapses_the_selected_snippet(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)

    def model():
        return _model(RoutingOpener(
            [_live_listing()],
            details={"root": ROOT_DETAIL,
                     "w1": detail("w1", items=[ASSISTANT_MESSAGE])}))

    report = "The work was already committed"

    expanded = _run_tui_capture(monkeypatch, [SPACE, QUIT], model=model())
    assert any(report in text for text in expanded.frames[-1]), \
        "space expanded the selected snippet"

    # A second space collapses it: the report is in the frame the first space
    # produced and gone from the last one.
    collapsed = _run_tui_capture(monkeypatch, [SPACE, SPACE, QUIT], model=model())
    assert any(report in text for text in collapsed.frames[1]), \
        "the first space expanded the snippet"
    assert not any(report in text for text in collapsed.frames[-1]), \
        "the second space collapsed it"


def test_the_caret_follows_a_selected_worker_that_changes_group_across_a_refresh(
        monkeypatch):
    """THE REGRESSION the reviewer caught, driven through the real loop.

    A selected worker changes state, so it moves to another STATE GROUP and the
    rows reorder underneath the caret. The view used to hold the INDEX across the
    refresh, so the poll after the move left the caret on whichever worker now
    occupied it — a different session, and Enter would have focused that one.

    Here `w2` is selected at index 4 in the idle group, turns `working`, and its
    box moves to index 3 in the working group while its sibling `w1` takes index
    4. The caret follows the SESSION, so it lands on `w2`.
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
    inner = _model(opener)
    seen: list = []

    class Recording:
        def refresh(self, **kwargs):
            seen.append(kwargs.get("selected_id"))
            return inner.refresh(**kwargs)

    # `j` moves the caret down onto w2; `r` forces the refresh that reorders the
    # rows; `q` ends the loop after that frame is painted.
    screen = _run_tui_capture(monkeypatch, [ord("j"), ord("r"), ord("q")],
                              model=Recording())

    # The refresh was handed the SESSION under the caret, not an index.
    assert seen == [None, "w2"], seen
    # And the caret it resolved points at w2's box — its task says "two" — not at
    # w1's, which is what the old index-preserving snap would have chosen.
    final = screen.frames[-1]
    caret = [i for i, text in enumerate(final) if "▸" in text]
    assert len(caret) == 1, final
    box = "\n".join(final[caret[0]:caret[0] + 3])
    assert "two" in box and "one" not in box, box


def test_the_footer_names_the_focus_and_expand_bindings():
    assert "enter focus" in a.FOOTER, "the footer must name the Enter binding"
    assert "space expand" in a.FOOTER, "the footer must name the Space binding"
    assert "a access" in a.FOOTER, "the existing bindings must survive"


def _cjk_width(text: str) -> int:
    """Display columns if AMBIGUOUS is two — the CJK-locale rule.

    A footer form must fit under BOTH this and the Latin rule; the only way to
    guarantee that is for the form to contain no Ambiguous glyph, which is what
    `test_the_footer_is_measured_the_same_in_a_cjk_locale` pins.
    """
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
               for ch in text)


def test_the_footer_names_every_key_at_every_width():
    # The regression this pins: the full footer is 64 columns and the agents pane
    # is a ~46-column split, so a FIXED footer was clipped mid-word — "space
    # expand" and "a access" fell off the end, leaving the operator without the
    # two keys they most needed (Space, which had just inherited expand from
    # Enter). At every width the footer must FIT the pane UNDER BOTH LOCALE RULES
    # and still name q, r, the move keys, enter, space and a.
    for width in (46, 52, 64, 80):
        footer = a.render(a.Frame(), width=width)[-1]
        assert a._display_width(footer) <= width, (width, footer)
        assert _cjk_width(footer) <= width, (width, footer)
        for letter in ("q", "r", "a"):
            assert letter in footer, (width, letter, footer)
        assert ("^/v" in footer or "^v" in footer), (width, footer)
        # Enter and Space are words at full width and glyphs when compact, but a
        # key must be named EITHER way.
        assert ("enter" in footer or "⏎" in footer), (width, footer)
        assert ("space" in footer or "␣" in footer), (width, footer)


def test_the_footer_chooses_a_form_by_measured_width():
    # The threshold is each form's own width, measured — not a magic number
    # that can drift out of step with the strings.
    assert a._footer(80) == a.FOOTER
    assert a._footer(64) == a.FOOTER, "the full form fits a 64-column pane"
    assert a._footer(63) == a.FOOTER_COMPACT
    assert a._footer(45) == a.FOOTER_COMPACT, "the compact form fits 46"
    assert a._footer(44) == a.FOOTER_KEYS
    assert a._display_width(a.FOOTER) == 64
    assert a._display_width(a.FOOTER_COMPACT) == 45
    # Only labels degrade: every form names every key.
    for form in (a.FOOTER, a.FOOTER_COMPACT, a.FOOTER_KEYS):
        assert "q" in form and "r" in form and "a" in form, form
        assert ("^/v" in form or "^v" in form), form


def test_the_footer_is_measured_the_same_in_a_cjk_locale():
    # The regression this pins, in the locale that re-created it: ↑ (U+2191) and
    # ↓ (U+2193) are East Asian AMBIGUOUS — one column in a Latin locale, TWO in a
    # CJK one. While they were in the footer, `_footer` measured the full form as
    # 64 and chose it for a 64-column pane, but a CJK terminal painted 66 and
    # `_clip` cut "a access" off. The footer now carries no Ambiguous or Wide
    # glyph, so its measured width is identical under both rules and no form can
    # overflow in either.
    import unicodedata

    for form in (a.FOOTER, a.FOOTER_COMPACT, a.FOOTER_KEYS):
        for ch in form:
            assert unicodedata.east_asian_width(ch) not in ("W", "F", "A"), (ch, form)
        # One honest width rule: no glyph's width depends on the locale.
        assert a._display_width(form) == _cjk_width(form) == len(form), form

    # ⏎ and ␣ stay Narrow, and the wider keys still fit under the CJK rule.
    for glyph in ("⏎", "␣"):
        assert unicodedata.east_asian_width(glyph) == "N", glyph
    assert _cjk_width(a.FOOTER) == 64
    assert _cjk_width(a.FOOTER_COMPACT) <= 46

    # Every form `_footer` picks for a pane at least as wide as the KEYS floor
    # fits that pane under the CJK rule too.
    floor = _cjk_width(a.FOOTER_KEYS)
    for width in range(floor, 90):
        assert _cjk_width(a._footer(width)) <= width, width
