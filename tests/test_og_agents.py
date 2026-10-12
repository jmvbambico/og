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
import types
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import URLError

import pytest

import og_agents as a
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


def worker(sid, state, *, text="t", snippet=None, expanded=False):
    """A worker Row assembled directly, for the renderer tests.

    Deliberately NOT built through `build_rows`: these tests are about the box
    around a row, not about the title parse or the elapsed figure, so the row is
    filled in by hand. That also keeps each of them failing for its OWN reason
    when run against a build that lacks one piece — a glyph test that failed
    because the title parser was missing would be passing for the wrong reason.
    """
    return a.Row(session_id=sid, text=text, state=state, depth=1,
                 snippet=snippet, expanded=expanded)


def header(text="Claude Code", sid="root"):
    return a.Row(sid, text, "working", 0, is_root=True)


# ---------------------------------------------------------------------------
# 1. the renderer, as a pure function
# ---------------------------------------------------------------------------

def test_renderer_shows_a_root_and_its_workers_in_listing_order():
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

    assert lines[0] == "Claude Code", "the root is a plain header, not a box"
    joined = "\n".join(lines)
    # Every worker is present, in listing order, each in its own box.
    assert [row.text for row in rows[1:]] == \
        ["space-per-root", "fix-stale-comments", "waiting-on-you"]
    assert joined.index("space-per-root") < joined.index("fix-stale-comments") \
        < joined.index("waiting-on-you")
    assert [r.state for r in rows] == ["working", "working", "idle", "blocked"]
    # ...and each box is closed: one top rule per worker, and only the last one
    # is an elbow's end.
    assert sum(1 for line in lines if line.startswith("├─")) == 2
    assert sum(1 for line in lines if line.startswith("└─")) == 1


def test_renderer_nests_a_grandchild_and_shows_an_expanded_snippet():
    root = dict(listed("root", status="running"), harness="claude-native")
    sessions = [
        root,
        listed("w1", parent="root", status="running", title="coder_zen:worker"),
        listed("w2", parent="root", status="running", title="coder_zen:sibling"),
        listed("g1", parent="w1", status="running", title="coder_zen:grandchild"),
    ]
    rows = a.build_rows(
        sessions, ["root"],
        snippets={"g1": "a child report"}, expanded_ids={"g1"}, now=NOW)
    lines = a.render(a.Frame(rows=rows))

    assert lines[0] == "Claude Code"
    # w1 has a sibling after it, so its column stays open down past its box...
    assert lines[1].startswith("├─"), lines[1]
    # ...and its grandchild hangs inside that column, two narrower.
    grandchild = lines[5]
    assert grandchild.startswith("│ └─  ┌"), grandchild
    assert len(grandchild) == a.DEFAULT_WIDTH, "two columns narrower than w1"
    # The report, expanded, is a row INSIDE the box rather than a loose line
    # that would break the spine.
    assert any("a child report" in line for line in lines[5:10])
    assert all(line.startswith("│ ") for line in lines[5:10])


def test_renderer_notes_a_truncated_listing():
    frame = a.Frame(rows=[header()], truncated=True)
    assert a.render(frame)[0].startswith("… partial listing")


# ---------------------------------------------------------------------------
# 2. the B1 shape, verbatim
# ---------------------------------------------------------------------------

B1_WIDTH = 64


def test_b1_shape_is_verbatim_tree_caret_and_aligned_boxes():
    """The approved design, line for line: `├─`/`└─`, one caret, aligned boxes.

    Built through `build_rows` from a listing rather than hand-assembled rows,
    so the names, agents and elapsed figures under test are the ones the model
    actually produces. Spinner frame 2 is `⠹`.
    """
    sessions = [
        dict(listed("root", status="running", title="conversation"),
             harness="claude-native"),
        listed("w1", parent="root", status="running",
               title="coder_zen:stock-concurrency-lock", created_at=NOW - 134),
        listed("w2", parent="root", status="running", elicit=1,
               title="cmdcode:fleet-accident-reports"),
        listed("w3", parent="root", status="failed",
               title="coder_freebuff:kg-base-unit-read-auth"),
    ]
    rows = a.build_rows(sessions, ["root"], now=NOW)

    lines = a.render(a.Frame(rows=rows, selected=1, spin=2), width=B1_WIDTH)

    rule = "─" * (B1_WIDTH - 6)

    def mid(side, text):
        return f"{side}  │" + (" " + text).ljust(B1_WIDTH - 6) + "│"

    assert lines == [
        "Claude Code",
        f"├─ ▸┌{rule}┐",
        mid("│ ", "⠹ stock-concurrency-lock"),
        mid("│ ", "  zen · 2m14s"),
        f"│   └{rule}┘",
        f"├─  ┌{rule}┐",
        mid("│ ", "! fleet-accident-reports"),
        mid("│ ", "  cmdcode · waiting on you"),
        f"│   └{rule}┘",
        f"└─  ┌{rule}┐",
        mid("  ", "x kg-base-unit-read-auth"),
        mid("  ", "  freebuff · failed"),
        f"    └{rule}┘",
        "",
        a.FOOTER,
    ]
    # Every row of every box is exactly one pane wide, and the caret is on the
    # FIRST row of its box only — repeated down the box it would read as four
    # selected things.
    assert all(len(line) == B1_WIDTH for line in lines[1:13])
    assert sum(1 for line in lines if "▸" in line) == 1


def test_every_glyph_and_the_spinner_for_working():
    # The ASCII glyphs, one per state the viewer draws...
    for state, glyph in (("idle", "+"), ("blocked", "!"), ("failed", "x"),
                         ("unknown", "?")):
        frame = a.Frame(rows=[header(), worker("w", state)])
        assert glyph in "\n".join(a.render(frame, width=48)), state
    # ...and working gets the spinner instead of a glyph.
    assert a.state_glyph("working", 0) == a.SPIN_FRAMES[0]
    working = a.Frame(rows=[header(), worker("w", "working")], spin=0)
    assert "⠋" in "\n".join(a.render(working, width=48))
    # Nothing else can leak a multi-cell or coloured glyph onto the line.
    assert all(ord(ch) < 0x2800 or ch in "⠋⠙⠹⠸⠼⠴▸" for ch in "".join(a.STATE_GLYPH.values()) + a.SPIN_FRAMES + "▸")


def test_the_spinner_uses_six_dots_so_the_row_never_jitters():
    # Braille encodes dot N as bit N-1 of the cell's offset from U+2800, and
    # dots 7 and 8 are the two low bits (0x40, 0x80) — the descenders that hang
    # below the baseline. A spinner using them changes the glyph's height every
    # frame, so the row it sits in jumps as the box is redrawn.
    assert len(a.SPIN_FRAMES) == 6
    for frame in a.SPIN_FRAMES:
        assert ord(frame) - 0x2800 < 0x40, f"{frame} uses a 7/8 descender"


def test_the_spinner_advances_with_the_frame_counter_and_is_pure():
    def lines(spin):
        frame = a.Frame(rows=[header(), worker("w", "working")], spin=spin)
        return a.render(frame, width=48)

    assert lines(0) != lines(1), "the spinner has to move"
    assert lines(3) == lines(3), "same counter, same screen — render is pure"
    assert lines(9) == lines(3), "the cycle is six frames long"
    seen = [lines(n)[2].split("│")[1].strip()[0] for n in range(6)]
    assert seen == list(a.SPIN_FRAMES)
    # The counter is the ONLY thing that moved: every other column is identical.
    assert [l[:4] for l in lines(0)] == [l[:4] for l in lines(1)]


def test_a_quiet_pane_renders_the_same_lines_and_schedules_no_repaint():
    """An `og agents` pane with nothing running must not repaint at all.

    The operator leaves these open in a pane for a whole day. Repainting on a
    timer whether or not anything changed is a CPU burner for a screen that
    never differs, so the loop compares the composed lines and skips
    `erase`/`refresh` when they are identical — and blocks on the keyboard until
    the next poll instead of waking eight times a second.
    """
    quiet = a.Frame(rows=[header(),
                          worker("w1", "idle"),
                          worker("w2", "failed")])
    assert a.frame_working(quiet) is False

    first = a._pane(quiet, B1_WIDTH).items
    assert a.paint_needed(first, None) is True, "the first frame is painted"
    painted = first
    for spin in range(12):
        quiet.spin = spin
        items = a._pane(quiet, B1_WIDTH).items
        assert items == painted, "nothing changed, so nothing moved"
        assert a.paint_needed(items, painted) is False

    # ...and it sleeps until the next data poll, not until the next spinner tick.
    assert a.next_wait(now=100.0, poll_due=103.0, working=False) == 3.0

    busy = a.Frame(rows=[header(), worker("w", "working")])
    assert a.frame_working(busy) is True
    painted = a._pane(busy, B1_WIDTH).items
    busy.spin = 1
    assert a.paint_needed(a._pane(busy, B1_WIDTH).items, painted) is True
    assert a.next_wait(now=100.0, poll_due=103.0, working=True) == pytest.approx(
        a.SPIN_TICK)
    # A poll that comes first wins the race.
    assert a.next_wait(now=100.0, poll_due=100.01, working=True) == pytest.approx(
        0.01)


def test_the_caret_moves_between_workers_and_never_onto_a_header():
    rows = [header(),
            worker("w1", "idle", text="one"),
            worker("w2", "idle", text="two")]
    assert a.snap_selection(rows, 0) == 1, \
        "the view opens on the first worker, not a header"
    frame = a.Frame(rows=rows, selected=a.snap_selection(rows, 0))

    for step, expected in ((1, 2), (1, 2), (-1, 1), (-1, 1)):
        frame.selected = a.move_selection(rows, frame.selected, step)
        assert frame.selected == expected
        lines = a.render(frame, width=48)
        assert sum(1 for line in lines if "▸" in line) == 1

    # No workers at all: the caret has nowhere to go and must not crash.
    assert a.move_selection([header()], 0, 1) == 0
    assert a.snap_selection([header()], 3) == 0


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

    assert lines[0] == "Claude Code"
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

    assert [row.session_id for row in frame.rows] == ["stale", "s1"], frame.rows
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
    assert [row.session_id for row in frame.rows] == ["stale", "s1"], frame.rows
    assert "old work" in frame.rows[1].text


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

    assert [row.session_id for row in frame.rows] == ["stale", "s1"], frame.rows


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

    assert [row.session_id for row in frame.rows] == ["mine", "m1"], frame.rows
    assert frame.rows[0].text == "Claude Code"
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

    assert any("⠋ w1" in line for line in lines), lines
    assert not any("The work" in line for line in lines)


def test_a_failed_root_detail_still_renders_the_tree():
    opener = RoutingOpener([_live_listing()], details={"root": URLError("down")})
    model = _model(opener)

    lines = a.render(model.refresh())

    # The root does not vanish: with no harness to label it by, it falls back to
    # its title, and the tree beneath it still renders.
    assert lines[0] == "conversation"
    assert any("⠋ w1" in line for line in lines)


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

def test_source_only_makes_read_only_calls():
    """AST scan: no CALLED identifier looks like a writer to a session.

    Prose and docstrings are allowed to discuss prompting and elicitations (the
    rationale lives in them); only called names are inspected. A viewer must
    never send a prompt, answer an elicitation, or POST anything.
    """
    source = Path(a.__file__).read_text()
    called = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) \
                else getattr(func, "id", "")
            called.add(name.lower())
    assert called, "expected to find call expressions in the module"
    offenders = [c for c in called
                 if any(bad in c for bad in ("send", "prompt", "resolve",
                                             "elicit", "post"))]
    assert not offenders, offenders


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
    # `--once` has no animation and no curses: it prints the lines render hands
    # the painter, at spinner frame 0, and nothing else.
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    # The listing fixture carries no timestamps, so the meta lines are clock
    # free and the two frames below are the same frame.
    expected_frame = _model(opener).refresh()
    # Row 1 is the first worker: row 0 is the group header, which draws no box.
    expected_frame.selected = 1
    expected = a.render(expected_frame)

    rc = a.main(["--once", "--all", "--server", "http://x"], opener=opener)

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == expected
    assert out[0] == "Claude Code"
    assert out[1].startswith("├─ ▸┌"), "the B1 box, with the caret on the first row"
    assert out[-1] == a.FOOTER
    assert "⠋" in out[2], "spinner frame 0"


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

    assert lines[0] == "conversation"
    assert "┌─ access" not in "\n".join(lines), "the panel starts closed"
    assert a.Frame().access is None, "the toggle starts off"
    assert lines[-1] == a.FOOTER
    assert "a access" in a.FOOTER, "the footer must advertise the binding"


def test_the_access_box_is_content_sized_flush_left_and_drops_a_narrow_qr():
    qr = ["\x1b[40;37;1m" + "█" * 27 + "\x1b[0m"] + ["\x1b[40;37;1m" + "▀▄" * 13 + "\x1b[0m"]
    acc = _access(qr=qr, note="same wifi only — not reachable outside")
    note_width = len("same wifi only — not reachable outside")

    wide = a.render(a.Frame(rows=[header()], access=acc), width=80)
    assert "┌─ access " in wide[2], wide[2]
    box = a.access_lines(acc, 80)
    assert wide[2] == box[0], "flush left: column 0, not under the tree spine"
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
    assert lines[0] == "" and lines[-1] == a._clip(a.FOOTER, 26)
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

    # Exactly the list rows, then the panel, then the footer. Not a fraction of
    # the pane, not a padded block.
    assert lines[0] == "conversation"
    assert lines[1] == "", "a blank row separates the tree from the panel"
    assert lines[2:2 + len(panel)] == panel
    assert len(panel) == 2 + 13 + 1 + 2, "2 borders + 13 QR + blank + URL + note"
    assert lines[-1] == a.FOOTER


def test_agent_list_keeps_a_floor_of_rows_when_the_panel_is_open():
    qr = [f"qr-row-{i}" for i in range(13)]   # a 19-row panel: 13 QR + blank + 2 info
    acc = _access(qr=qr)
    rows = [a.Row(f"s{i}", f"row{i}", "working", 0, is_root=True)
            for i in range(40)]
    panel = a.access_lines(acc, 80)

    short = a.render(a.Frame(rows=rows, access=acc), width=80, height=8)
    assert sum(1 for line in short if line.startswith("row")) == a.ACCESS_LIST_FLOOR
    assert any("qr-row-0" in line for line in short), \
        "the panel is still shown on a short pane"

    tall = a.render(a.Frame(rows=rows, access=acc), width=80, height=40)
    assert sum(1 for line in tall if line.startswith("row")) == \
        40 - (len(panel) + 3), "the tail is reserved whole: blank, panel, blank, footer"


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
    # A moved caret and an advanced spinner cannot change that.
    frame.selected, frame.spin = 1, 3
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