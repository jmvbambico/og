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


# ---------------------------------------------------------------------------
# 1. the renderer, as a pure function
# ---------------------------------------------------------------------------

def test_renderer_shows_a_root_and_its_workers_in_listing_order():
    root = dict(listed("root", title="Omnigent Herdr Integration", status="running"),
                harness="claude-native")
    sessions = [
        root,
        listed("w1", parent="root", status="running", title="space-per-root"),
        listed("w2", parent="root", status="idle", title="fix-stale-comments"),
        listed("w3", parent="root", status="running", elicit=1,
               title="waiting-on-you"),
    ]

    rows = a.build_rows(sessions, ["root"])
    lines = a.render(a.Frame(rows))

    assert lines == [
        "Claude Code",
        "  • ●  space-per-root",
        "  • ✓  fix-stale-comments",
        "  • ⏸  waiting-on-you",
    ]
    assert [r.state for r in rows] == ["working", "working", "idle", "blocked"]


def test_renderer_indents_a_nested_worker_and_shows_an_expanded_snippet():
    root = dict(listed("root", status="running"), harness="claude-native")
    sessions = [
        root,
        listed("w1", parent="root", status="running", title="worker"),
        listed("g1", parent="w1", status="running", title="grandchild"),
    ]
    rows = a.build_rows(
        sessions, ["root"],
        snippets={"g1": "a child report"}, expanded_ids={"g1"})
    assert a.render(a.Frame(rows)) == [
        "Claude Code",
        "  • ●  worker",
        "    • ●  grandchild",
        "      a child report",
    ]


def test_renderer_notes_a_truncated_listing():
    frame = a.Frame(rows=[a.Row("root", "Claude Code", "working", 0, is_root=True)],
                    truncated=True)
    assert a.render(frame)[0].startswith("… partial listing")


# ---------------------------------------------------------------------------
# 2. the cwd rule
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
# 3. snippet extraction
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
# 4. the budget: no detail fetch per row per poll
# ---------------------------------------------------------------------------

ROOT_DETAIL = detail("root", workspace="/repo/og", harness="claude-native")


def _live_listing():
    return [
        listed("root", status="running", title="conversation"),
        listed("w1", parent="root", status="running", title="w1"),
        listed("w2", parent="root", status="running", title="w2"),
        listed("w3", parent="root", status="running", title="w3"),
    ]


def test_three_refreshes_fetch_each_root_detail_once_not_once_per_row_per_poll():
    opener = RoutingOpener([_live_listing()] * 3, details={"root": ROOT_DETAIL})
    model = a.AgentModel(w.SessionWatcher(base_url="http://x", opener=opener),
                         cwd="/nowhere", all_roots=True)

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
    model = a.AgentModel(w.SessionWatcher(base_url="http://x", opener=opener),
                         cwd="/nowhere", all_roots=True)

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
    model = a.AgentModel(w.SessionWatcher(base_url="http://x", opener=opener),
                         cwd="/nowhere", all_roots=True)

    for _ in range(3):
        model.refresh()

    assert len(opener.detail_requests) == 2, [r.full_url for r in opener.detail_requests]
    # ...and the report it captured is there when the row is expanded.
    joined = "\n".join(a.render(model.refresh(expanded_ids={"w1"})))
    assert "The work was already committed" in joined


def test_a_quiet_root_with_no_workers_is_not_fetched():
    # An old, quiet conversation that delegated nothing: not live, so it costs
    # no detail fetch and does not appear. This is what keeps "all live roots" a
    # handful on a machine with a long history of sessions.
    listing = [listed("root", status="idle", title="old conversation")]
    opener = RoutingOpener([listing], details={})
    model = a.AgentModel(w.SessionWatcher(base_url="http://x", opener=opener),
                         cwd="/nowhere", all_roots=True)

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
    model = a.AgentModel(w.SessionWatcher(base_url="http://x", opener=opener),
                         cwd="/nowhere", all_roots=True)

    lines = a.render(model.refresh(expanded_ids={"w1"}))

    assert lines[0] == "Claude Code"
    assert any("done-worker" in line for line in lines)
    assert any("The work was already committed" in line for line in lines)


# ---------------------------------------------------------------------------
# 5. a detail fetch that fails still renders the row
# ---------------------------------------------------------------------------

def test_a_failed_snippet_fetch_still_renders_the_row():
    opener = RoutingOpener(
        [_live_listing()],
        details={"root": ROOT_DETAIL, "w1": URLError("server down")})
    model = a.AgentModel(w.SessionWatcher(base_url="http://x", opener=opener),
                         cwd="/nowhere", all_roots=True)

    frame = model.refresh(selected_id="w1", expanded_ids={"w1"})
    lines = a.render(frame)

    assert any("• ●  w1" in line for line in lines), lines
    assert not any("The work" in line for line in lines)


def test_a_failed_root_detail_still_renders_the_tree():
    opener = RoutingOpener([_live_listing()], details={"root": URLError("down")})
    model = a.AgentModel(w.SessionWatcher(base_url="http://x", opener=opener),
                         cwd="/nowhere", all_roots=True)

    lines = a.render(model.refresh())

    # The root does not vanish: with no harness to label it by, it falls back to
    # its title, and the tree beneath it still renders.
    assert lines[0] == "conversation"
    assert any("w1" in line for line in lines)


# ---------------------------------------------------------------------------
# 6. the harness label
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
# 7. read-only: the module never calls anything that could write to a session
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
# 8. no test performs a real network call
# ---------------------------------------------------------------------------

def test_no_test_reaches_the_real_server(monkeypatch):
    # If the module forgot the opener seam, urlopen would reach 127.0.0.1:6767.
    def boom(*args, **kwargs):
        raise AssertionError("a test attempted a real network call")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(w.urllib.request, "urlopen", boom)

    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})
    model = a.AgentModel(w.SessionWatcher(token="t", opener=opener),
                         cwd="/nowhere", all_roots=True)
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


def test_main_once_renders_plain_text_without_a_tty(
        monkeypatch, tmp_path, capsys):
    # The token store must never be the developer's own.
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    opener = RoutingOpener([_live_listing()], details={"root": ROOT_DETAIL})

    rc = a.main(["--once", "--all", "--server", "http://x"], opener=opener)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Claude Code" in out
    assert any("• " in line for line in out.splitlines())
