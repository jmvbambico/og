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


def group(state, word=None):
    """A group LABEL row, as `build_rows` emits it (`state` is a display state)."""
    return a.Row("", word or a.GROUP_WORD.get(state, state), state, 1, group=True)


def rail():
    """A breathing rail row, as `build_rows` emits it."""
    return a.Row("", "", "working", 1, rail=True)


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


def test_workers_are_grouped_by_state_with_a_label_row_each():
    """Workers appear grouped by state, each group under its own label — the
    order on screen is the group order (working, blocked, failed, finished), not
    listing order."""
    sessions = [
        dict(listed("root", title="conv", status="running"), harness="claude-native"),
        listed("w1", parent="root", status="idle", title="coder_zen:done-already"),
        listed("w2", parent="root", status="running", title="coder_claude:busy"),
        listed("w3", parent="root", status="failed", title="coder_freebuff:broke"),
    ]
    rows = a.build_rows(sessions, ["root"], now=NOW)

    # The rows themselves are in display order, which is what the caret indexes.
    boxes = [row.text for row in rows if not row.is_root
             and not row.group and not row.rail]
    assert boxes == ["busy", "broke", "done-already"]

    lines = a.render(a.Frame(rows=rows))
    joined = "\n".join(lines)
    assert "│ working" in lines
    assert "│ failed" in lines
    assert "│ finished" in lines
    # Each label sits above the box it heads, never apart from it.
    for label, task in (("working", "busy"), ("failed", "broke"),
                        ("finished", "done-already")):
        assert joined.index(label) < joined.index(task)


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


def test_c2_shape_is_verbatim_title_gauge_groups_and_rounded_boxes():
    """The approved C2 template, line for line: the heavy title bar, the gauge,
    a state label per group, and a ROUNDED box per worker BEHIND a rail+caret
    gutter. Built through `build_rows` from a listing, so the names, agents and
    glyphs are the ones the model produces at width 46."""
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

    lines = a.render(a.Frame(rows=rows, selected=3), width=C2_WIDTH)

    assert lines == [
        "━━ Project Status Review ━━━━━━━━━━━━━━━━━━━━━",
        "██░░░░░░░░  1/4 done               Claude Code",
        "│",
        "│ working",
        "│ ▸ ╭────────────────────────────────────────╮",
        "│   │ ● ▯  coders                            │",
        "│   │ zen                               2m14s│",
        "│   ╰────────────────────────────────────────╯",
        "│",
        "│ blocked - waiting on you",
        "│   ╭────────────────────────────────────────╮",
        "│   │ ⏸ ⌘  reviewers                         │",
        "│   │ cmdcode                  waiting on you│",
        "│   ╰────────────────────────────────────────╯",
        "│",
        "│ failed",
        "│   ╭────────────────────────────────────────╮",
        "│   │ ✗ ╔ scouts                             │",
        "│   │ freebuff                         failed│",
        "│   ╰────────────────────────────────────────╯",
        "│",
        "│ finished",
        "│   ╭────────────────────────────────────────╮",
        "│   │ ○ ⌘  docs                              │",
        "│   │ cmdcode                     done 6m ago│",
        "│   ╰────────────────────────────────────────╯",
        "",
        a.FOOTER_COMPACT,
    ]
    # Every BOX row is exactly the pane width (the label rows are short by
    # design — the rail is one glyph and the word follows it), and the caret is on
    # the FIRST row of its box — the `╭` border — and nowhere else.
    box_rows = [line for line in lines
                if (line.startswith("│") and line.endswith("│") and len(line) > 1)]
    assert box_rows and all(len(line) == C2_WIDTH for line in box_rows)
    assert sum(1 for line in lines if "▸" in line) == 1
    assert lines[4].startswith("│ ▸ ╭"), "the caret points at the box from outside"
    assert lines[4].index("▸") == 2, "the caret sits in its own column, after the rail"
    assert lines[4].index("╭") == 4, "the box starts after the gutter"


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
            rail(), group("working"),
            worker("w1", "working", text="one"),
            worker("w2", "working", text="two")]
    lines = a.render(a.Frame(rows=rows), width=C2_WIDTH)

    assert lines[0].startswith("━━ ")
    assert lines[2] == "│", "the breathing row is the rail alone"
    assert lines[3].startswith("│ "), "the label keeps the rail in column 0"
    # Every group row — breathing, label, and every line of both boxes — carries
    # the rail at column 0. The blank above the footer and the footer are last.
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
            rail(), group("working"),
            worker("w1", "working", text="one"),
            worker("w2", "working", text="two")]
    lines = a.render(a.Frame(rows=rows, selected=3), width=C2_WIDTH)   # w1

    # Row 0 is the group label; the box's four rows follow at 4..7.
    assert lines[4][:4] == "│ ▸ ", "the caret leads the box's top border"
    assert "▸" not in lines[5] and "▸" not in lines[6] and "▸" not in lines[7]
    assert "▸" not in lines[8], "the unselected box draws no caret at all"
    # The caret column (2) is strictly between the rail (0) and the box (4).
    assert lines[4].index("▸") == 2 and lines[4].index("╭") == a.GUTTER


def test_every_state_glyph_comes_from_the_chosen_vocabulary():
    # The operator's four glyphs, one per state the viewer draws...
    assert a.STATE_GLYPH == {"working": "●", "blocked": "⏸", "failed": "✗",
                             "unknown": "?", "idle": "○"}
    for state, glyph in (("idle", "○"), ("blocked", "⏸"), ("failed", "✗"),
                         ("unknown", "?"), ("working", "●")):
        frame = a.Frame(rows=[header(), worker("w", state)])
        assert glyph in "\n".join(a.render(frame, width=48)), state
    # ...and there is no spinner left: the glyph is a pure function of the state.
    assert a.state_glyph("working") == "●"


def test_a_quiet_pane_renders_the_same_lines_and_schedules_no_repaint():
    """An `og agents` pane with nothing changing must not repaint at all.

    The operator leaves these open in a pane for a whole day. C2 does not
    animate, so the render is byte-identical between polls and the loop skips
    `erase`/`refresh` — and blocks on the keyboard until the next poll.
    """
    quiet = a.Frame(rows=[header(), rail(), group("idle"),
                          worker("w1", "idle"), worker("w2", "failed")])

    first = a._pane(quiet, C2_WIDTH).items
    assert a.paint_needed(first, None) is True, "the first frame is painted"
    painted = first
    for _ in range(12):
        items = a._pane(quiet, C2_WIDTH).items
        assert items == painted, "nothing changed, so nothing moved"
        assert a.paint_needed(items, painted) is False

    # ...and it sleeps until the next data poll, with no spinner tick beside it.
    assert a.next_wait(now=100.0, poll_due=103.0) == 3.0


def test_the_caret_moves_between_workers_in_the_order_they_are_displayed():
    """THE HAZARD: grouping by state reorders the rows, so the caret must walk
    them in the order the eye reads — not in tree order. `Frame.rows` is itself
    in display order, which is what makes `move_selection` correct unchanged."""
    rows = [header(),
            rail(), group("working"),
            worker("w1", "working", text="one"),
            rail(), group("idle"),
            worker("w2", "idle", text="two"),
            worker("w3", "idle", text="three")]
    assert a.snap_selection(rows, 0) == 3, \
        "the view opens on the first WORKER, not the title bar or a label"
    frame = a.Frame(rows=rows, selected=a.snap_selection(rows, 0))
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
    assert a.snap_selection([header()], 3) == 0


def test_only_the_caret_row_of_the_selected_box_carries_the_selected_role():
    """Selecting a worker marks it with the caret, not a reverse-video block.

    `_pane` once tagged every line of the box SELECTED and `_run_tui` painted
    SELECTED reverse, so one selected worker lit a whole box. The role now
    belongs to the caret row alone; the rest of the block is NORMAL.
    """
    rows = [header(),
            rail(), group("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]
    frame = a.Frame(rows=rows, selected=3)   # w1

    items = a._pane(frame, 48).items

    # bar, gauge, rail, label, box-top (caret), content, agent, box-bottom, ...
    caret_rows = [i for i, (text, _role) in enumerate(items) if "▸" in text]
    assert caret_rows == [4], "the caret is on the box's top border"
    assert items[4][1] == a.SELECTED, "the caret row carries SELECTED"
    # The rows around the caret — the label above, and the box's content, agent
    # and bottom rows — are NORMAL, or the whole box would read as highlighted.
    assert [role for _text, role in items[3:4]] == [a.NORMAL]
    assert [role for _text, role in items[5:8]] == [a.NORMAL] * 3
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
            rail(), group("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]

    on_w1 = a.render(a.Frame(rows=rows, selected=3), width=48)   # caret row 4
    on_w2 = a.render(a.Frame(rows=rows, selected=4), width=48)   # caret row 8

    assert "▸" in on_w1[4] and "▸" not in on_w1[8]
    assert "▸" in on_w2[8] and "▸" not in on_w2[4]
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
            rail(), group("idle"),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]
    frame = a.Frame(rows=rows, selected=a.snap_selection(rows, 0))   # w1
    before = a._pane(frame, 48).items

    frame.selected = a.move_selection(rows, frame.selected, 1)       # w2
    after = a._pane(frame, 48).items

    assert after != before, "the caret moved, so the composed lines changed"
    assert a.paint_needed(after, before) is True
    # The caret really did move to the other worker's box.
    assert "▸" in after[8][0] and "▸" not in after[4][0]


def test_every_harness_mark_fills_the_slot_and_narrow_marks_align_the_title():
    """The box's content row carries the harness mark, and every mark spends the
    SAME two display columns, so the glyph and the title line up down the list."""
    harnesses = ("claude-native", "acp:cline", "acp:command-code",
                 "opencode-native", "acp:freebuff", "totally-unknown", "")
    title_col = {}
    for harness in harnesses:
        slot = a.harness_mark(harness)
        assert a._display_width(slot) == 2, (harness, slot)
        line = a.render(a.Frame(rows=[worker("w", "working", text="ZX",
                                             harness=harness)]), 48)[1]
        assert "ZX" in line, (harness, line)
        title_col[harness] = line.index("ZX")

    # A blank slot and a NARROW mark both spend two LAYOUT columns, so the title
    # starts in one column for all of them. (An Ambiguous mark is charged two for
    # the slot but one for the layout, so it lands one column early — deliberate,
    # see `_mark_slot`.)
    two = [h for h in harnesses if len(a.harness_mark(h)) == 2]
    assert len(two) >= 5, "the narrow/blank marks must dominate the table"
    assert len(set(title_col[h] for h in two)) == 1


def test_a_rendered_group_label_and_its_box_line_up_in_the_same_columns():
    rows = [header(), rail(), group("working"),
            worker("w1", "working", text="one")]
    lines = a.render(a.Frame(rows=rows), width=C2_WIDTH)
    # The rail is column 0 on the breathing row, the label and the box top alike;
    # the label's word and the box's rounded corner both follow the rail.
    assert lines[2] == "│"
    assert lines[3][0] == "│" and lines[3][1] == " "
    assert lines[4][0] == "│" and lines[4].index("╭") == a.GUTTER


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


def test_height_trimming_never_orphans_a_label_or_splits_a_box():
    """The trimmer works in whole blocks, so a group label can never be left
    without its box and a box can never be cut in half."""
    rows = [header(), rail(), group("working"),
            worker("w1", "working", text="one"),
            rail(), group("idle"),
            worker("w2", "idle", text="two")]
    full = a.render(a.Frame(rows=rows), width=C2_WIDTH)
    joined = "\n".join(full)
    assert joined.count("╭") == 2 and joined.count("╰") == 2
    assert "working" in joined and "finished" in joined

    # Room for the root block (2 rows) and the first group (rail + label + box =
    # 6 rows) but not the second group: the second group is dropped WHOLE — its
    # rail and its label go with its box, so no orphan label is left behind.
    short = a.render(a.Frame(rows=rows), width=C2_WIDTH, height=10)
    short_joined = "\n".join(short)
    assert "working" in short_joined and "one" in short_joined
    assert "finished" not in short_joined, "an orphaned label would be noise"
    assert "two" not in short_joined, "a box must not be cut in half"
    assert short_joined.count("╭") == short_joined.count("╰") == 1


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
# 7. a detail fetch that fails still renders the row
# ---------------------------------------------------------------------------

def test_a_failed_snippet_fetch_still_renders_the_row():
    opener = RoutingOpener(
        [_live_listing()],
        details={"root": ROOT_DETAIL, "w1": URLError("server down")})
    model = _model(opener)

    frame = model.refresh(selected_id="w1", expanded_ids={"w1"})
    lines = a.render(frame)

    assert any("●" in line and "w1" in line for line in lines), lines
    assert not any("The work" in line for line in lines)


def test_a_failed_root_detail_still_renders_the_tree():
    opener = RoutingOpener([_live_listing()], details={"root": URLError("down")})
    model = _model(opener)

    lines = a.render(model.refresh())

    # The root does not vanish: with no harness to label it by, it falls back to
    # its title, and the tree beneath it still renders.
    assert lines[0].startswith("━━ conversation ")
    assert any("●" in line and "w1" in line for line in lines), \
        "the worker box survives, under its glyph"


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
        # ...and whatever its own width, the slot is exactly two columns.
        assert a._display_width(got) == 2, (vendor, got)


def test_ambiguous_marks_are_not_padded_but_narrow_ones_are():
    # The pessimistic ruler charges Ambiguous two cells, so those three fill the
    # slot unpadded; a Narrow mark gets the single trailing space that brings it
    # to two. This IS the slot's contract — assert it on both sides.
    harnesses = _harness_for_vendor()
    for vendor, mark in MARK.items():
        got = a.harness_mark(harnesses[vendor])
        if vendor in AMBIGUOUS:
            assert got == mark, (vendor, repr(got))
        else:
            assert got == mark + " ", (vendor, repr(got))
    # And the classes are what the test claims them to be, not assumed.
    for vendor in AMBIGUOUS:
        assert unicodedata.east_asian_width(MARK[vendor]) == "A", vendor


def test_the_blank_slot_is_exactly_two_spaces():
    # A row that names NO agent identity draws the blank slot — a mark there would
    # be meaningless. This is the path the atom must never take.
    assert a.harness_mark(None) == "  "
    assert a.harness_mark("") == "  "
    assert a.harness_mark("   ") == "  "
    assert a.agent_mark(None) == "  "
    assert a.agent_mark("") == "  "
    assert a._display_width(a.harness_mark(None)) == 2


def test_every_return_is_exactly_two_columns_including_the_blank():
    for row in _catalog()["agents"]:
        assert a._display_width(a.harness_mark(row["harness"])) == 2, row["id"]
        assert a._display_width(a.agent_mark(row["id"])) == 2, row["id"]
    for degenerate in ("brand-new-native", "", None, 7, "  "):
        assert a._display_width(a.harness_mark(degenerate)) == 2, degenerate
        assert a._display_width(a.agent_mark(degenerate)) == 2, degenerate


def test_an_unresolvable_agent_gets_the_atom_never_a_blank():
    # A harness that NAMES an agent whose vendor we cannot resolve draws the atom
    # — "an agent whose vendor I could not identify" — rather than a silent blank.
    # It must not raise: a pane has to render with no marks rather than not render.
    assert a.harness_mark("brand-new-native") == a.ATOM + " "
    assert a.agent_mark("reviewer") == a.ATOM + " "
    # A non-string is NO identity, not an unresolvable one: blank.
    assert a.harness_mark(7) == "  "
    assert a.agent_mark(7) == "  "


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
        monkeypatch.setattr(h, "_REGISTRY_AGENT_VENDORS", None)
        path = tmp_path / name
        if content is not None:
            path.write_text(content)
        monkeypatch.setattr(h, "REGISTRY_PATH", path)
        assert h.harness_vendors() == {}, name
        assert h.agent_vendors() == {}, name
        assert a.harness_mark("") == "  ", name
        assert a.harness_mark("claude-native") == a.ATOM + " ", name
        assert a.agent_mark("zen") == a.ATOM + " ", name


# ---------------------------------------------------------------------------
# 8c. the agent NAME -> vendor hop (the fix for the blank worker marks)
# ---------------------------------------------------------------------------

def test_the_agent_name_resolves_through_the_registry():
    # A worker's LISTING row carries no `harness`; its mark comes from the agent
    # name the title gave. The name -> vendor hop is a registry fact, read through
    # the same catalog the labels and harness vendors come from.
    assert h.agent_vendor("cmdcode") == "moonshotai"
    assert h.agent_vendor("freebuff") == "z-ai"
    assert h.agent_vendor("kilo") == "kilo"
    assert h.agent_vendor("codex") == "openai"
    assert h.agent_vendor("claude") == "anthropic"


def test_the_agent_name_resolution_tolerates_case_and_the_coder_prefix():
    # The names arrive as `coder_zen` in a worker title (split_title strips the
    # prefix) and may arrive in any case; both are absorbed.
    for name in ("zen", "coder_zen", "Coder_Zen", "ZEN", "Coder_Zen"):
        assert h.agent_vendor(name) == "opencode-zen", name
    assert h.agent_vendor("coder_cmdcode") == "moonshotai"
    for nothing in (None, "", "   ", 7):
        assert h.agent_vendor(nothing) is None, nothing


def test_the_zen_alias_comes_from_the_registry_worker_field():
    # `zen` is not an `id`; it is the opencode row's `worker: "coder_zen"` with the
    # prefix stripped, so the alias is derivable from the catalog alone and NO
    # hardcoded alias map was needed.
    rows = _catalog()["agents"]
    opencode = next(r for r in rows if r["id"] == "opencode")
    assert opencode["worker"] == "coder_zen"
    assert "zen" not in {r["id"] for r in rows}
    assert h.agent_vendors()["zen"] == "opencode-zen"


def test_a_worker_box_marks_by_agent_name_because_the_row_has_no_harness():
    """The fix for the blank worker marks: a worker's LISTING row carries no
    `harness`, so the box resolves its mark from the agent name the title gave."""
    def content_line(**row_kwargs):
        return a.render(a.Frame(rows=[worker("w", "working", text="ZX",
                                             **row_kwargs)]), 48)[1]

    # `zen` (the opencode alias) and `cmdcode` resolve to their vendor's mark...
    assert a.agent_mark("zen") == a._HARNESS_MARKS["opencode-zen"] + " "
    assert a._HARNESS_MARKS["opencode-zen"] in content_line(agent="zen")
    assert a._HARNESS_MARKS["moonshotai"] in content_line(agent="cmdcode")
    # ...an unresolvable agent shows the atom, NOT a blank...
    assert a.ATOM in content_line(agent="reviewer")
    # ...and a row naming no agent at all is the genuinely-blank case.
    assert a.agent_mark(None) == "  "
    assert a.ATOM not in content_line(agent=None)
    # A real `harness` on the row still wins over the agent name.
    assert a._HARNESS_MARKS["anthropic"] in content_line(
        agent="cmdcode", harness="claude-native")


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
    # The caret opens on the first WORKER: rows 0-2 are the title bar, the rail
    # and the group label, which draw no box.
    expected_frame.selected = a.snap_selection(expected_frame.rows, 0)
    expected = a.render(expected_frame)

    rc = a.main(["--once", "--all", "--server", "http://x"], opener=opener)

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == expected
    assert out[0].startswith("━━ conversation "), "the root's title bar"
    assert out[1].endswith("Claude Code"), "the gauge, with the harness label"
    assert "│ working" in out, "the workers are grouped under a label"
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
