"""Tests for installer/og_herdr_watch.py.

No network, ever: every test injects a stub opener (or drives the SSE parser
directly), so nothing here can touch the developer's live server on
127.0.0.1:6767. `discover_token` reads a tmp_path fixture, never the real
~/.omnigent.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import URLError

import pytest

import og_herdr_watch as w


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    """Minimal stand-in for an http.client.HTTPResponse as urlopen returns it:
    a context manager with read(n)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class StubOpener:
    """Records every request and replies from a queue of bodies.

    `bodies` entries may be bytes (one chunk per read), a dict (JSON-encoded
    and readable in any chunk size), or an Exception instance, which is raised
    instead of replied with — that is how a dead server is simulated. The last
    body repeats if the watcher reads more than once, which keeps a `watch()`-
    style loop from running dry.
    """

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        if isinstance(body, BaseException):
            raise body
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return FakeResponse(body)


def session(sid="s1", status="running", parent=None, **extra):
    """A session row shaped like the real API's.

    Every key here is a key the listing actually returns (the captured row
    below is the reference); only identity, status and parentage vary per test.
    Building fixtures from the measured shape rather than a guessed one is the
    point: a wrong contract in a fixture hides the same bug it would hide in the
    module.
    """
    base = {
        "id": sid,
        "agent_id": "05724a49a7ad4e52a8698a8dc98a864e",
        "agent_name": "hivemind",
        "archived": False,
        "comments_count": 0,
        "created_at": 1791566251,
        "external_session_id": f"ses_{sid}",
        "labels": {},
        "owner": "cryogenix",
        "parent_session_id": parent,
        "pending_elicitations_count": 0,
        "permission_level": 4,
        "runner_id": "runner_token_b6d20a38844b57baad37dc4e61e36846",
        "status": status,
        "title": f"title-{sid}",
        "updated_at": 1791566384,
        "viewer_unread": False,
    }
    base.update(extra)
    return base


def envelope(rows, has_more=False, last_id=None):
    """A list envelope of the shape the server answers with."""
    env = {"object": "list", "data": list(rows), "has_more": has_more}
    if last_id is None and rows:
        last_id = rows[-1].get("id")
    if last_id is not None:
        env["last_id"] = last_id
        env["first_id"] = rows[0].get("id")
    return env


def urls(opener):
    return [r.full_url for r in opener.requests]


# One row exactly as it came off a live 0.17.0 server. Kept as text so it is
# parsed the same way the module parses it, and kept verbatim so a test cannot
# quietly "fix" a fixture to match the code.
VERBATIM_ROW_JSON = """
{"agent_id":"05724a49a7ad4e52a8698a8dc98a864e",
 "agent_name":"hivemind",
 "archived":false,
 "comments_count":0,
 "created_at":1791566251,
 "external_session_id":"ses_ede5462ebffe8PRK3LyKCkOka3",
 "id":"6583350e2adc4fbf8b9f8b323de0f513",
 "labels":{},
 "owner":"cryogenix",
 "parent_session_id":"e29bf406bd6d480cbc61fa6125e8d5fd",
 "pending_elicitations_count":0,
 "permission_level":4,
 "runner_id":"runner_token_b6d20a38844b57baad37dc4e61e36846",
 "status":"idle",
 "title":"coder_zen:fix-drop-types-alias",
 "updated_at":1791566384,
 "viewer_unread":false}
"""

VERBATIM_ROW = json.loads(VERBATIM_ROW_JSON)

VERBATIM_ENVELOPE_JSON = (
    '{"object":"list","data":[' + VERBATIM_ROW_JSON.strip() + '],'
    '"has_more":true,'
    '"first_id":"6583350e2adc4fbf8b9f8b323de0f513",'
    '"last_id":"6583350e2adc4fbf8b9f8b323de0f513"}'
)


# --------------------------------------------------------------------------
# herdr_state
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["running"])
def test_state_running_is_working(status):
    assert w.herdr_state({"status": status}) == "working"


@pytest.mark.parametrize("status", ["idle", "completed", "done", "closed"])
def test_state_quiesced_statuses_are_idle(status):
    assert w.herdr_state({"status": status}) == "idle"


@pytest.mark.parametrize("status", [
    "starting", "pending", "waiting", "error", "failed", "cancelled", "",
])
def test_state_unknown_statuses(status):
    assert w.herdr_state({"status": status}) == "unknown"


def test_state_case_and_whitespace_insensitive():
    assert w.herdr_state({"status": "  RUNNING "}) == "working"
    assert w.herdr_state({"status": "Idle"}) == "idle"


def test_state_elicitation_overrides_running():
    assert w.herdr_state({"status": "running", "pending_elicitations_count": 1}) == "blocked"


def test_state_elicitation_overrides_idle():
    assert w.herdr_state({"status": "idle", "pending_elicitations_count": 3}) == "blocked"


def test_state_elicitation_string_count_still_blocks():
    # A string count has shown up in client payloads; erring toward "blocked"
    # is the safe direction.
    assert w.herdr_state({"status": "running", "pending_elicitations_count": "2"}) == "blocked"


def test_state_zero_elicitation_does_not_block():
    assert w.herdr_state({"status": "running", "pending_elicitations_count": 0}) == "working"


def test_state_bool_elicitation_does_not_block():
    assert w.herdr_state({"status": "running", "pending_elicitations_count": True}) == "working"


@pytest.mark.parametrize("bad", [
    {},
    {"status": None},
    {"status": 7},
    {"pending_elicitations_count": "many"},
    {"status": "running", "pending_elicitations_count": None},
    None,
    [],
    "not a dict",
])
def test_state_malformed_never_raises(bad):
    assert w.herdr_state(bad) in {"idle", "working", "blocked", "unknown"}


def test_state_malformed_non_dict_is_unknown():
    assert w.herdr_state(None) == "unknown"
    assert w.herdr_state(["running"]) == "unknown"


# -- the measured field name ------------------------------------------------
#
# The row spells the count PLURAL. Reading the singular spelling meant the key
# never matched any real row, so "blocked" never fired — the one state a human
# most needs to see, silently dead.

def test_state_verbatim_row_with_one_elicitation_is_blocked():
    row = dict(VERBATIM_ROW, pending_elicitations_count=1)
    assert w.herdr_state(row) == "blocked"


def test_state_verbatim_row_as_captured_is_idle():
    # Unchanged, and read exactly as the server sends it: every other key here is
    # the server's, so an idle verdict means those keys were read and found
    # quiet rather than never looked at.
    assert w.herdr_state(VERBATIM_ROW) == "idle"


def test_state_plural_spelling_alone_is_enough_to_block():
    # The regression test for the slip: this row carries no singular key at all,
    # so a module reading only the singular spelling reports "idle" here.
    assert w.herdr_state({"status": "idle", "pending_elicitations_count": 1}) == "blocked"


def test_state_plural_zero_alone_does_not_block():
    assert w.herdr_state({"status": "running", "pending_elicitations_count": 0}) == "working"


def test_state_singular_mcp_spelling_still_blocks():
    # Kept as a fallback on purpose: `session_get_info` in the MCP tool set
    # reports `pending_elicitation_count`, so a caller who got its session dict
    # from there rather than from HTTP still gets a blocked pane.
    assert w.herdr_state({"status": "idle", "pending_elicitation_count": 1}) == "blocked"


def test_state_plural_decides_when_a_row_carries_both_spellings():
    # Plural is the contract; the singular only fills in for a row that has no
    # usable plural count, so it cannot overrule one that does.
    both = {"status": "running", "pending_elicitations_count": 0,
            "pending_elicitation_count": 1}
    assert w.herdr_state(both) == "working"


def test_state_singular_fills_in_when_the_plural_count_is_unusable():
    row = {"status": "running", "pending_elicitations_count": "many",
           "pending_elicitation_count": 1}
    assert w.herdr_state(row) == "blocked"


# --------------------------------------------------------------------------
# list_sessions
# --------------------------------------------------------------------------

def test_list_sessions_requests_kind_any_by_default():
    opener = StubOpener([session()])
    watcher = w.SessionWatcher(opener=opener)
    result = watcher.list_sessions()
    assert result == [session()]
    url = opener.requests[0].full_url
    assert "/v1/sessions" in url
    assert "kind=any" in url, f"watcher must ask for sub-agents: {url}"
    # An explicit page size, not the server's default of 20 — a default-sized
    # page is what loses the root session.
    assert f"limit={w.SESSION_PAGE_LIMIT}" in url


def test_list_sessions_kind_is_overridable():
    opener = StubOpener([session()])
    w.SessionWatcher(opener=opener).list_sessions(kind="sub_agent")
    assert "kind=sub_agent" in opener.requests[0].full_url


def test_list_sessions_unwraps_envelope_and_drops_non_dicts():
    opener = StubOpener({"sessions": [session(), "junk"]})
    assert w.SessionWatcher(opener=opener).list_sessions() == [session()]


def test_list_sessions_accepts_bare_list():
    opener = StubOpener([session()])
    assert w.SessionWatcher(opener=opener).list_sessions() == [session()]


def test_list_sessions_sends_bearer_token_when_present():
    opener = StubOpener([session()])
    w.SessionWatcher(token="secret-token", opener=opener).list_sessions()
    headers = {k.lower(): v for k, v in opener.requests[0].header_items()}
    assert headers["authorization"] == "Bearer secret-token"


def test_list_sessions_unauthenticated_without_token():
    opener = StubOpener([session()])
    w.SessionWatcher(opener=opener).list_sessions()
    names = {k.lower() for k, _ in opener.requests[0].header_items()}
    assert "authorization" not in names


def test_poll_once_asks_for_kind_any():
    # The regression that motivated the module: without kind=any a watcher
    # never sees a delegated worker.
    opener = StubOpener([session()])
    w.SessionWatcher(opener=opener).poll_once()
    assert "kind=any" in opener.requests[0].full_url


# --------------------------------------------------------------------------
# pagination: the listing is newest-first and paged, so one page is not the
# listing
# --------------------------------------------------------------------------

def test_list_sessions_walks_every_page_and_passes_the_cursor():
    pages = [
        envelope([session("s1"), session("s2")], has_more=True, last_id="s2"),
        envelope([session("s3"), session("s4")], has_more=True, last_id="s4"),
        envelope([session("s5")], has_more=False, last_id="s5"),
    ]
    opener = StubOpener(*pages)
    rows = w.SessionWatcher(opener=opener).list_sessions()

    assert [r["id"] for r in rows] == ["s1", "s2", "s3", "s4", "s5"]

    seen = urls(opener)
    assert len(seen) == 3, seen
    assert "after=" not in seen[0]
    assert "after=s2" in seen[1], seen[1]
    assert "after=s4" in seen[2], seen[2]
    assert all("kind=any" in u for u in seen)


def test_list_sessions_cursor_falls_back_to_the_last_row_id():
    # An envelope that omits last_id still has to make progress, or the walk
    # would stop after one page and lose the oldest rows.
    first = {"object": "list", "data": [session("s1"), session("s2")], "has_more": True}
    opener = StubOpener(first, envelope([session("s3")]))
    rows = w.SessionWatcher(opener=opener).list_sessions()
    assert [r["id"] for r in rows] == ["s1", "s2", "s3"]
    assert "after=s2" in urls(opener)[1]


def test_list_sessions_stops_when_the_server_gives_no_cursor():
    # has_more with nowhere to continue is not a reason to ask again: repeating
    # the first request would return the same page and walk in circles.
    page = {"object": "list", "data": [{"status": "running"}], "has_more": True}
    opener = StubOpener(page)
    rows = w.SessionWatcher(opener=opener).list_sessions()
    assert rows == [{"status": "running"}]
    assert len(opener.requests) == 1


def test_list_sessions_stops_a_server_that_repeats_a_cursor():
    # A server that ignores `after` and keeps claiming has_more must not be able
    # to spin the walk forever.
    opener = StubOpener(envelope([session("s1")], has_more=True, last_id="same"))
    w.SessionWatcher(opener=opener).list_sessions()
    assert len(opener.requests) == 2, urls(opener)


def test_list_sessions_caps_a_runaway_at_the_page_limit():
    # has_more true forever, page after page: the walk stops at the ceiling.
    opener = StubOpener(
        *[envelope([session(f"s{i}")], has_more=True, last_id=f"s{i}")
          for i in range(w.MAX_SESSION_PAGES * 2)]
    )
    rows = w.SessionWatcher(opener=opener).list_sessions()
    assert len(opener.requests) == w.MAX_SESSION_PAGES
    assert len(rows) == w.MAX_SESSION_PAGES


def test_list_sessions_caps_the_rows_it_returns(monkeypatch):
    # A server that ignores `limit` and sends a huge page still cannot make one
    # poll hold an unbounded number of rows.
    monkeypatch.setattr(w, "MAX_LISTED_SESSIONS", 3)
    opener = StubOpener(
        envelope([session("s1"), session("s2")], has_more=True, last_id="s2"),
        envelope([session("s3"), session("s4")], has_more=True, last_id="s4"),
        envelope([session("s5"), session("s6")], has_more=False),
    )
    rows = w.SessionWatcher(opener=opener).list_sessions()
    assert [r["id"] for r in rows] == ["s1", "s2", "s3"]


# --------------------------------------------------------------------------
# a listing that stopped short is not evidence of deletion
# --------------------------------------------------------------------------

def cap_pages(monkeypatch, pages=1):
    """Force the walk to stop after `pages` fetches — the page cap."""
    monkeypatch.setattr(w, "MAX_SESSION_PAGES", pages)


def test_a_complete_listing_is_not_truncated():
    rows, truncated = w.SessionWatcher(
        opener=StubOpener(envelope([session("s1")]))
    )._fetch_listing()
    assert ([r["id"] for r in rows], truncated) == (["s1"], False)


def test_the_page_cap_is_reported_as_truncated(monkeypatch):
    cap_pages(monkeypatch)
    opener = StubOpener(envelope([session("s1")], has_more=True, last_id="s1"))
    rows, truncated = w.SessionWatcher(opener=opener)._fetch_listing()
    assert ([r["id"] for r in rows], truncated) == (["s1"], True)


def test_a_server_that_will_not_say_where_to_continue_is_truncated():
    # has_more with no cursor to use: more exists and none of it was fetched.
    payload = {"object": "list", "data": [{"status": "running"}], "has_more": True}
    _rows, truncated = w.SessionWatcher(opener=StubOpener(payload))._fetch_listing()
    assert truncated is True


def test_a_repeated_cursor_is_reported_as_truncated():
    page = envelope([session("s1")], has_more=True, last_id="same")
    opener = StubOpener(page)
    rows, truncated = w.SessionWatcher(opener=opener)._fetch_listing()
    assert truncated is True
    assert len(rows) == 2, "both fetches landed before the walk gave up"


def test_the_row_cap_is_reported_as_truncated(monkeypatch):
    monkeypatch.setattr(w, "MAX_LISTED_SESSIONS", 1)
    opener = StubOpener(envelope([session("s1"), session("s2")]))
    rows, truncated = w.SessionWatcher(opener=opener)._fetch_listing()
    assert ([r["id"] for r in rows], truncated) == (["s1"], True)


def test_a_truncated_listing_does_not_manufacture_removals(monkeypatch):
    watcher = w.SessionWatcher(
        opener=StubOpener(envelope([session("s1"), session("s2")]))
    )
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [
        ("added", "s1"), ("added", "s2"),
    ]

    # Now past what one capped walk can fetch. s2 is beyond the page — the
    # oldest rows are the roots — and not gone.
    cap_pages(monkeypatch)
    watcher.opener = StubOpener(envelope([session("s1")], has_more=True, last_id="s1"))
    assert watcher.poll_once() == []
    assert set(watcher._seen) == {"s1", "s2"}, "the unseen session was forgotten"


def test_the_next_complete_listing_emits_the_removal_it_suppressed(monkeypatch):
    watcher = w.SessionWatcher(
        opener=StubOpener(envelope([session("s1"), session("s2")]))
    )
    watcher.poll_once()

    cap_pages(monkeypatch)
    watcher.opener = StubOpener(envelope([session("s1")], has_more=True, last_id="s1"))
    assert watcher.poll_once() == []

    # This listing really does lack s2, and having kept the record of it from
    # the truncated poll the watcher can say so — exactly once.
    watcher.opener = StubOpener(envelope([session("s1")]))
    events = watcher.poll_once()
    assert [(e.kind, e.session_id) for e in events] == [("removed", "s2")]
    assert events[0].previous["id"] == "s2"

    watcher.opener = StubOpener(envelope([session("s1")]))
    assert watcher.poll_once() == []


def test_a_truncated_listing_still_reports_added_and_changed(monkeypatch):
    cap_pages(monkeypatch)
    watcher = w.SessionWatcher(opener=StubOpener(
        envelope([session("s1"), session("s3")], has_more=True, last_id="s3")
    ))
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [
        ("added", "s1"), ("added", "s3"),
    ]

    watcher.opener = StubOpener(envelope(
        [session("s1", status="idle"), session("s3")], has_more=True, last_id="s3"
    ))
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [("changed", "s1")]


def test_a_session_beyond_the_cap_is_not_re_added_when_it_returns(monkeypatch):
    watcher = w.SessionWatcher(
        opener=StubOpener(envelope([session("s1"), session("s2")]))
    )
    watcher.poll_once()

    cap_pages(monkeypatch)
    watcher.opener = StubOpener(envelope([session("s1")], has_more=True, last_id="s1"))
    assert watcher.poll_once() == []
    assert "s2" in watcher._seen

    # s2 is back in the window and unchanged. Suppressing the removal is only
    # half the fix: if the retained state had been replaced, this would be an
    # "added" and the consumer would open a second tab for a session whose pane
    # is already on screen.
    watcher.opener = StubOpener(envelope(
        [session("s1"), session("s2")], has_more=True, last_id="s2"
    ))
    assert watcher.poll_once() == []


# -- a truncated poll is only uncertain about what it never fetched ---------

def test_a_truncated_listing_retires_a_sub_agent_it_fetched_and_found_idle(monkeypatch):
    # Suppressing removals wholesale is what left a finished worker holding a
    # stale pane. The row came back; it says the session is done; truncation
    # says nothing about that.
    cap_pages(monkeypatch)
    watcher = w.SessionWatcher(opener=StubOpener(envelope([
        session("root", parent=None),
        session("worker", status="running", parent="root"),
    ])))
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [
        ("added", "root"), ("added", "worker"),
    ]

    watcher.opener = StubOpener(envelope([
        session("root", parent=None),
        session("worker", status="idle", parent="root"),
    ], has_more=True, last_id="root"))
    events = watcher.poll_once()
    assert [(e.kind, e.session_id) for e in events] == [("removed", "worker")]
    assert events[0].previous["status"] == "running"
    assert "worker" not in watcher._seen, "a retired session stayed in _seen"
    assert "root" in watcher._seen


def test_a_truncated_listing_retires_a_session_that_came_back_archived(monkeypatch):
    cap_pages(monkeypatch)
    watcher = w.SessionWatcher(opener=StubOpener(envelope(
        [session("s1", status="running", parent="root")]
    )))
    assert [e.kind for e in watcher.poll_once()] == ["added"]

    watcher.opener = StubOpener(envelope(
        [session("s1", status="running", parent="root", archived=True)],
        has_more=True, last_id="s1",
    ))
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [("removed", "s1")]
    assert "s1" not in watcher._seen


def test_a_truncated_listing_separates_fetched_and_rejected_from_unfetched(monkeypatch):
    # Both cases in one poll: only the one the watcher actually saw retires.
    cap_pages(monkeypatch)
    watcher = w.SessionWatcher(opener=StubOpener(envelope([
        session("gone", status="running", parent="root"),
        session("beyond", status="running", parent="root"),
    ])))
    watcher.poll_once()

    watcher.opener = StubOpener(envelope([
        session("gone", status="idle", parent="root"),
        # "beyond" is simply not in this page: unfetched, so still believed in.
    ], has_more=True, last_id="gone"))
    events = watcher.poll_once()
    assert [(e.kind, e.session_id) for e in events] == [("removed", "gone")]
    assert set(watcher._seen) == {"beyond"}


def test_a_retired_session_does_not_come_back_on_the_next_truncated_poll(monkeypatch):
    # ...and it actually leaves the retained state, or the poll after this one
    # removes it all over again.
    cap_pages(monkeypatch)
    watcher = w.SessionWatcher(opener=StubOpener(envelope(
        [session("s1", status="running", parent="root")]
    )))
    watcher.poll_once()

    idle = envelope([session("s1", status="idle", parent="root")],
                    has_more=True, last_id="s1")
    watcher.opener = StubOpener(idle)
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [("removed", "s1")]

    watcher.opener = StubOpener(idle)
    assert watcher.poll_once() == []


def test_a_duplicated_id_ends_on_the_last_row_that_arrived(monkeypatch):
    # Documented policy: last row wins, for the object and for the verdict.
    # This is the one case where a truncated poll and a complete one can
    # disagree about what a row means, so it is pinned rather than left to
    # whichever copy the dict happened to keep.
    cap_pages(monkeypatch)
    live = session("s1", status="running", parent="root")
    dead = session("s1", status="idle", parent="root")
    watcher = w.SessionWatcher(opener=StubOpener(envelope([live, dead])))
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == []

    # ...and the reverse order keeps it, so the rule is the last row's and not
    # a preference for one verdict.
    watcher = w.SessionWatcher(opener=StubOpener(envelope([dead, live])))
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [("added", "s1")]


def test_a_duplicated_row_is_reported_once(monkeypatch):
    cap_pages(monkeypatch)
    watcher = w.SessionWatcher(opener=StubOpener(envelope(
        [session("s1"), session("s1", title="renamed")]
    )))
    events = watcher.poll_once()
    assert [(e.kind, e.session_id) for e in events] == [("added", "s1")]
    assert events[0].session["title"] == "renamed", "last row did not win"


def test_the_truncation_notice_is_written_once_per_transition(monkeypatch, capsys):
    cap_pages(monkeypatch)
    page = envelope([session("s1")], has_more=True, last_id="s1")
    watcher = w.SessionWatcher(opener=StubOpener(page, page, page))
    for _ in range(3):
        watcher.poll_once()
    err = capsys.readouterr().err
    assert err.count("truncated") == 1, err
    assert "removals suppressed" in err, err

    # Coming back out of it is worth a line too — the watcher is whole again
    # and removals resume — and it is a line, not a flood.
    complete = envelope([session("s1")])
    for _ in range(3):
        watcher.opener = StubOpener(complete)
        watcher.poll_once()
    err = capsys.readouterr().err
    assert err.count("complete again") == 1, err
    assert "truncated" not in err


def test_an_untruncated_watcher_writes_nothing(capsys):
    w.SessionWatcher(opener=StubOpener(envelope([session("s1")]))).poll_once()
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------
# the measured envelope and row, parsed verbatim
# --------------------------------------------------------------------------

def test_verbatim_envelope_is_parsed_into_rows():
    # The captured envelope says has_more true, so a correct walker follows the
    # cursor; the second (empty) body is the end of the walk.
    opener = StubOpener(json.loads(VERBATIM_ENVELOPE_JSON), envelope([]))
    rows = w.SessionWatcher(opener=opener).list_sessions()
    assert rows == [VERBATIM_ROW]
    assert "after=6583350e2adc4fbf8b9f8b323de0f513" in urls(opener)[1]


def test_verbatim_envelope_without_more_pages_is_one_request():
    payload = json.loads(VERBATIM_ENVELOPE_JSON)
    payload["has_more"] = False
    opener = StubOpener(payload)
    assert w.SessionWatcher(opener=opener).list_sessions() == [VERBATIM_ROW]
    assert len(opener.requests) == 1


def test_poll_once_keys_a_verbatim_row_on_its_id():
    # The event identity comes from `id`. The captured row carries no
    # `session_id`, no `kind` and no `workspace` — the three keys the old
    # contract claimed — so anything still reaching for them reads nothing.
    live = dict(VERBATIM_ROW, status="running")
    watcher = w.SessionWatcher(opener=StubOpener(envelope([live])))
    event = watcher.poll_once()[0]
    assert event.session_id == "6583350e2adc4fbf8b9f8b323de0f513"
    assert event.session == live
    assert event.state == "working"


# --------------------------------------------------------------------------
# should_project: what is worth a pane at all
# --------------------------------------------------------------------------

def test_should_project_a_root_that_is_idle():
    # A root is the conversation the human is typing into. It reads "idle" the
    # whole time it waits for them, so retiring it on idle would close the pane
    # they are typing into.
    row = dict(VERBATIM_ROW, parent_session_id=None, status="idle")
    assert w.should_project(row) is True


def test_should_project_a_root_that_is_archived_is_not():
    row = dict(VERBATIM_ROW, parent_session_id=None, archived=True)
    assert w.should_project(row) is False


def test_should_project_an_idle_sub_agent_is_not():
    # The verbatim row as captured: a sub-agent, idle, done. Projecting every
    # finished session is what made the workspace unusable.
    assert w.should_project(VERBATIM_ROW) is False


def test_should_project_a_running_sub_agent_is():
    row = dict(VERBATIM_ROW, status="running")
    assert w.should_project(row) is True


def test_should_project_a_blocked_sub_agent_is_even_when_idle():
    row = dict(VERBATIM_ROW, status="idle", pending_elicitations_count=2)
    assert w.should_project(row) is True
    assert w.herdr_state(row) == "blocked"


def test_should_project_never_projects_an_archived_sub_agent():
    for status in ("running", "idle"):
        row = dict(VERBATIM_ROW, status=status, archived=True,
                   pending_elicitations_count=1)
        assert w.should_project(row) is False, status


@pytest.mark.parametrize("bad", [None, [], "not a dict", {}, {"id": "s1"}])
def test_should_project_malformed_never_raises(bad):
    assert w.should_project(bad) in (True, False)


def test_poll_once_only_reports_projected_sessions():
    # The consumer sees the live set and nothing else: an idle sub-agent is
    # absent from the very first poll rather than being added and removed.
    opener = StubOpener(envelope([
        session("root", status="idle"),
        session("worker-running", status="running", parent="root"),
        session("worker-idle", status="idle", parent="root"),
        session("worker-failed", status="failed", parent="root"),
        session("archived", status="running", parent="root", archived=True),
    ]))
    watcher = w.SessionWatcher(opener=opener)
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [
        ("added", "root"), ("added", "worker-running"),
    ]


def test_a_sub_agent_going_idle_produces_exactly_one_removed_event():
    watcher = w.SessionWatcher(
        opener=StubOpener(envelope([session("s1", status="running", parent="root")]))
    )
    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [("added", "s1")]

    watcher.opener = StubOpener(
        envelope([session("s1", status="idle", parent="root")])
    )
    events = watcher.poll_once()
    assert [(e.kind, e.session_id) for e in events] == [("removed", "s1")]
    assert events[0].session == {}
    assert events[0].previous["status"] == "running"
    assert events[0].state == "unknown"

    # ...and once. It is gone from the set now, so nothing re-reports it.
    watcher.opener = StubOpener(
        envelope([session("s1", status="idle", parent="root")])
    )
    assert watcher.poll_once() == []


def test_a_root_that_goes_idle_is_never_removed():
    # Same status move, opposite outcome: the pane the user is typing into
    # stays.
    watcher = w.SessionWatcher(
        opener=StubOpener(envelope([session("root", status="running")]))
    )
    assert [e.kind for e in watcher.poll_once()] == ["added"]
    watcher.opener = StubOpener(envelope([session("root", status="idle")]))
    assert [e.kind for e in watcher.poll_once()] == ["changed"]


# --------------------------------------------------------------------------
# poll_once diffing
# --------------------------------------------------------------------------

def first_poll(sessions):
    opener = StubOpener(sessions)
    watcher = w.SessionWatcher(opener=opener)
    return watcher, watcher.poll_once()


def test_first_poll_reports_everything_as_added():
    _, events = first_poll([session("s1"), session("s2", status="idle")])
    assert [(e.kind, e.session_id) for e in events] == [("added", "s1"), ("added", "s2")]
    assert all(e.previous == {} for e in events)


def test_added_event_carries_current_session():
    _, events = first_poll([session("s1", status="idle")])
    assert events[0].session == session("s1", status="idle")
    assert events[0].state == "idle"


def test_identical_listing_produces_no_events():
    watcher, _ = first_poll([session("s1")])
    opener = StubOpener([session("s1")])
    watcher.opener = opener
    assert watcher.poll_once() == []


def test_new_id_is_added():
    watcher, _ = first_poll([session("s1")])
    watcher.opener = StubOpener([session("s1"), session("s2")])
    events = watcher.poll_once()
    assert [(e.kind, e.session_id) for e in events] == [("added", "s2")]


def test_vanished_id_is_removed_with_no_current_session():
    watcher, _ = first_poll([session("s1"), session("s2")])
    watcher.opener = StubOpener([session("s1")])
    events = watcher.poll_once()
    assert [(e.kind, e.session_id) for e in events] == [("removed", "s2")]
    assert events[0].session == {}
    assert events[0].previous["id"] == "s2"
    assert events[0].state == "unknown"


def test_status_change_emits_changed():
    watcher, _ = first_poll([session("s1", status="running")])
    watcher.opener = StubOpener([session("s1", status="idle")])
    events = watcher.poll_once()
    assert [e.kind for e in events] == ["changed"]
    assert events[0].previous["status"] == "running"
    assert events[0].session["status"] == "idle"


def test_elicitation_arriving_emits_changed_and_flips_state():
    watcher, _ = first_poll([session("s1", status="running")])
    watcher.opener = StubOpener([session("s1", status="running",
                                          pending_elicitations_count=1)])
    events = watcher.poll_once()
    assert [e.kind for e in events] == ["changed"]
    assert events[0].state == "blocked"


def test_title_change_emits_changed():
    watcher, _ = first_poll([session("s1")])
    watcher.opener = StubOpener([session("s1", title="renamed")])
    assert [e.kind for e in watcher.poll_once()] == ["changed"]


def test_irrelevant_churn_emits_nothing():
    # last_activity_at moves on every poll of a live session; diffing on it
    # would make the consumer thrash.
    watcher, _ = first_poll([session("s1", last_activity_at="2026-01-01T00:00:00Z")])
    watcher.opener = StubOpener([session("s1", last_activity_at="2026-01-01T00:00:09Z",
                                          workspace="/other", agent_name="coder")])
    assert watcher.poll_once() == []


def test_added_and_removed_in_one_poll():
    watcher, _ = first_poll([session("s1"), session("s2")])
    watcher.opener = StubOpener([session("s2", status="idle"), session("s3")])
    events = watcher.poll_once()
    assert {(e.kind, e.session_id) for e in events} == {
        ("removed", "s1"), ("added", "s3"), ("changed", "s2"),
    }


def test_sessions_without_id_are_skipped():
    _, events = first_poll([{"status": "running"}, session("s1")])
    assert [e.session_id for e in events] == ["s1"]


def test_a_row_whose_only_id_is_external_session_id_is_skipped():
    # `external_session_id` is a different handle from the row identity, so
    # keying on it would let one session be counted twice under two names.
    row = dict(session("s1"))
    del row["id"]
    _, events = first_poll([row, session("s2")])
    assert [e.session_id for e in events] == ["s2"]


def test_events_do_not_alias_the_watchers_retained_objects():
    # SessionEvent promises "both are copies, safe to keep" — so neither may be
    # the very dict the watcher is still holding on to. Mutating the test's own
    # listing proves nothing here: StubOpener JSON-encodes it, so poll_once
    # works on a fresh decode and the fixture dict is not the same object at all.
    # The object that matters is the one under _seen, which is what a real
    # watcher would go on to read, diff and mutate.
    watcher, _ = first_poll([session("s1")])
    stored = watcher._seen["s1"]            # what poll #1 retained
    watcher.opener = StubOpener([session("s1", status="idle")])
    event = watcher.poll_once()[0]
    retained = watcher._seen["s1"]          # what poll #2 retained

    assert event.kind == "changed"

    # The copy is a value, not a view: rewrite both retained objects and the
    # event must still report what was true when it was polled.
    stored["status"] = "tampered"
    retained["status"] = "tampered"
    assert event.previous["status"] == "running"
    assert event.session["status"] == "idle"
    assert event.state == "idle"

    # Same thing stated directly — neither may be the very dict `_seen` holds.
    assert event.previous is not stored, \
        "event.previous aliases the object retained from the last poll"
    assert event.session is not retained, \
        "event.session aliases the object the watcher is still holding"


def test_mutating_an_event_does_not_fabricate_the_next_changed_event():
    # The same invariant from the other side, and the reason the copy exists.
    # `_seen` intentionally holds the object the server returned; if the event
    # shared that object, a consumer scribbling on an event would be editing
    # the watcher's memory and the next poll would report a change that never
    # happened — or swallow one that did.
    watcher, _ = first_poll([session("s1")])
    watcher.opener = StubOpener([session("s1", status="idle")])
    event = watcher.poll_once()[0]
    assert event.kind == "changed"

    event.session["status"] = "running"
    event.session["title"] = "scribbled-over"

    watcher.opener = StubOpener([session("s1", status="idle")])
    assert watcher.poll_once() == [], \
        "the caller's edit leaked into the watcher's retained state"


# --------------------------------------------------------------------------
# SSE parsing
# --------------------------------------------------------------------------

def parse(chunks):
    parser = w._SSEParser()
    out = []
    for chunk in chunks:
        out.extend(parser.feed(chunk))
    out.extend(parser.flush())
    return out


def test_sse_single_frame():
    assert parse([b'data: {"type": "message"}\n\n']) == [{"type": "message"}]


def test_sse_multiline_data_is_joined():
    frames = parse([b'data: {"type":\ndata: "chunk",\ndata: "n": 1}\n\n'])
    assert frames == [{"type": "chunk", "n": 1}]


def test_sse_ignores_comment_keepalives():
    frames = parse([b': keepalive\n\ndata: {"a": 1}\n\n: another\n\n'])
    assert frames == [{"a": 1}]


def test_sse_skips_invalid_json_without_raising():
    frames = parse([b'data: not json at all\n\ndata: {"ok": 1}\n\n'])
    assert frames == [{"ok": 1}]


def test_sse_skips_json_that_is_not_an_object():
    assert parse([b'data: [1, 2, 3]\n\ndata: {"ok": 1}\n\n']) == [{"ok": 1}]


def test_sse_ignores_other_fields():
    frames = parse([b'event: update\nid: 7\nretry: 1000\ndata: {"a": 1}\n\n'])
    assert frames == [{"a": 1}]


def test_sse_frame_split_across_reads():
    chunks = [b'data: {"ty', b'pe": "mes', b'sage"}', b'\n', b'\n']
    assert parse(chunks) == [{"type": "message"}]


def test_sse_frame_split_mid_crlf():
    assert parse([b'data: {"a": 1}\r', b'\n\r\n']) == [{"a": 1}]


def test_sse_bare_cr_line_endings():
    # SSE permits CR, CRLF and bare LF. CR-only is not what Omnigent emits, but
    # the parser claims to speak the format, so it has to: read as one enormous
    # line otherwise, losing every frame on the stream.
    assert parse([b'data: {"a": 1}\r\r']) == [{"a": 1}]


def test_sse_bare_cr_stream_of_several_frames():
    body = b': beat\r\rdata: {"n": 1}\r\rdata: {"n": 2}\r\r'
    assert parse([body]) == [{"n": 1}, {"n": 2}]


def test_sse_bare_cr_frame_split_across_reads():
    assert parse([b'data: {"a": 1', b'}\r', b'\r']) == [{"a": 1}]


def test_sse_trailing_bare_cr_dispatches_at_flush():
    # A lone CR at the end of a buffer is held back — the next byte decides
    # whether it was a bare CR or half a CRLF — so this frame can only arrive
    # when the stream ends.
    assert parse([b'data: {"a": 1}\r\rdata: {"b": 2}\r']) == [{"a": 1}, {"b": 2}]


def test_sse_mixed_line_endings():
    body = b'data: {"n": 1}\r\n\r\ndata: {"n": 2}\n\ndata: {"n": 3}\r\r'
    assert parse([body]) == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_sse_caps_a_line_that_never_terminates():
    # A peer that never sends a newline must not be able to grow the parser in a
    # process meant to run for days.
    parser = w._SSEParser()
    chunk = b"x" * 65536
    for _ in range(64):                       # 4 MiB of one unterminated line
        assert list(parser.feed(chunk)) == []
        assert len(parser._buf) <= w.MAX_SSE_LINE_BYTES + len(chunk)

    # ...and the stream is still usable once the peer starts speaking SSE again.
    assert list(parser.feed(b"\n")) == []     # ends the discarded line
    assert list(parser.feed(b'data: {"ok": 1}\n\n')) == [{"ok": 1}]


def test_sse_drops_the_frame_a_discarded_line_belongs_to():
    parser = w._SSEParser()
    parser.feed(b'data: {"partial": ')      # start of a real frame ...
    parser.feed(b"z" * (w.MAX_SSE_LINE_BYTES + 1))   # ... then a runaway line
    assert parser._data_bytes == 0
    assert list(parser.feed(b'\ndata: {"ok": 1}\n\n')) == [{"ok": 1}]


def test_sse_caps_data_lines_that_never_dispatch():
    parser = w._SSEParser()
    chunk = b'data: {"x": "' + b"y" * 4096 + b'"}\n'
    for _ in range(2048):                     # 8 MiB of undispatched data
        assert list(parser.feed(chunk)) == []
        assert parser._data_bytes <= w.MAX_SSE_FRAME_BYTES

    assert list(parser.feed(b'\ndata: {"ok": 1}\n\n')) == [{"ok": 1}]
    assert parser._data_bytes == 0


def test_sse_drops_the_tail_of_an_oversized_frame():
    # The cap used to clear the buffer and stop there, so the rest of the frame
    # accumulated as though it were fresh and the blank line dispatched it: the
    # consumer got a fragment it cannot tell from a whole frame, and the
    # consumer is a UI that acts on what it is handed.
    parser = w._SSEParser()
    oversize = b'x' * (w.MAX_SSE_FRAME_BYTES + 1)
    assert list(parser.feed(b'data: "' + oversize + b'"\n')) == []
    assert list(parser.feed(b'data: {"ok": true}\n')) == [], "tail of the same frame"
    assert list(parser.feed(b"\n")) == [], "frame boundary"
    assert parser._data == [] and parser._data_bytes == 0

    # The frame after it is a normal frame again — including a multi-line one,
    # which a stale fragment would have corrupted.
    assert list(parser.feed(b'data: {"n":\ndata: 1}\n\n')) == [{"n": 1}]


def test_flush_cannot_dispatch_the_tail_of_an_oversized_frame():
    # The stream ends with the poisoned frame still open, part of it sitting
    # unterminated in the buffer. flush() is the last chance to hand the
    # consumer something it would take for a frame; it must not.
    # feed() is a generator: every call below is drained, or it runs nothing at
    # all and the test passes without having fed the parser.
    parser = w._SSEParser()
    oversize = b'x' * (w.MAX_SSE_FRAME_BYTES + 1)
    assert list(parser.feed(b'data: "' + oversize + b'"\n')) == []
    assert list(parser.feed(b'data: {"ok": true}')) == []   # unterminated: buffered
    assert list(parser.flush()) == []


def test_flush_after_an_oversized_frame_still_parses_the_next_one():
    parser = w._SSEParser()
    oversize = b'x' * (w.MAX_SSE_FRAME_BYTES + 1)
    assert list(parser.feed(b'data: "' + oversize + b'"\n')) == []
    assert list(parser.flush()) == []
    assert list(parser.feed(b'data: {"ok": 1}\n\n')) == [{"ok": 1}]


def test_sse_multiple_frames_in_one_read():
    assert parse([b'data: {"n": 1}\n\ndata: {"n": 2}\n\n']) == [{"n": 1}, {"n": 2}]


def test_sse_empty_payload_is_not_dispatched():
    assert parse([b'\n\ndata: {"a": 1}\n\n']) == [{"a": 1}]


def test_stream_events_reads_a_live_response():
    body = b'data: {"session_id": "s1"}\n\ndata: {"session_id": "s2"}\n\n'
    opener = StubOpener(body)
    watcher = w.SessionWatcher(opener=opener)
    assert list(watcher.stream_events("s1")) == [
        {"session_id": "s1"}, {"session_id": "s2"},
    ]
    url = opener.requests[0].full_url
    assert "/v1/sessions/s1/stream" in url
    assert "text/event-stream" in \
        {v for _, v in opener.requests[0].header_items()}


def test_stream_events_tolerates_trailing_frame_without_blank_line():
    opener = StubOpener(b'data: {"a": 1}\n\ndata: {"b": 2}\n')
    assert list(w.SessionWatcher(opener=opener).stream_events("s1")) == [
        {"a": 1}, {"b": 2},
    ]


def test_stream_events_tolerates_garbage():
    opener = StubOpener(b'data: {\n\n: beat\n\ndata: nope\n\n')
    assert list(w.SessionWatcher(opener=opener).stream_events("s1")) == []


# --------------------------------------------------------------------------
# discover_token
# --------------------------------------------------------------------------

def write_tokens(home: Path, payload) -> None:
    (home / "auth_tokens.json").write_text(json.dumps(payload))


LOCAL = "http://127.0.0.1:6767"


def test_discover_token_reads_nested_entry(tmp_path):
    write_tokens(tmp_path, {LOCAL: {
        "token": "tok-nested", "user_id": "u1", "expires_at": "2030-01-01",
    }})
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) == "tok-nested"


def test_discover_token_reads_bare_string_entry(tmp_path):
    write_tokens(tmp_path, {LOCAL: "tok-bare"})
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) == "tok-bare"


def test_discover_token_uses_the_entry_for_this_server(tmp_path):
    write_tokens(tmp_path, {
        "https://remote.example": {"token": "tok-remote"},
        LOCAL: {"token": "tok-local"},
    })
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) == "tok-local"


def test_discover_token_ignores_a_token_issued_for_another_server(tmp_path):
    # The whole point: no entry for this server means run unauthenticated, not
    # "borrow the remote one" — a bearer presented to a host it was not issued
    # for is a leak, and it is invisible because the request still succeeds.
    write_tokens(tmp_path, {"https://remote.example": {"token": "tok-remote"}})
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) is None


def test_discover_token_ignores_a_token_for_another_server_via_alias(tmp_path):
    # Same trap through a spelling that *contains* the loopback host: a
    # substring preference would have picked this up, a key match does not.
    write_tokens(tmp_path, {"https://127.0.0.1.example.com": "tok-remote"})
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) is None


def test_discover_token_ignores_an_unusable_token_value(tmp_path):
    write_tokens(tmp_path, {LOCAL: {"token": 17}})
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) is None


def test_discover_token_matches_ignoring_a_trailing_slash(tmp_path):
    write_tokens(tmp_path, {"http://127.0.0.1:6767/": {"token": "tok-slash"}})
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) == "tok-slash"


def test_discover_token_missing_file_returns_none(tmp_path):
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) is None


def test_discover_token_corrupt_file_returns_none(tmp_path):
    (tmp_path / "auth_tokens.json").write_text("{not json")
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) is None


def test_discover_token_empty_or_bogus_returns_none(tmp_path):
    write_tokens(tmp_path, {})
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) is None
    write_tokens(tmp_path, {"http://x": {"token": ""}})
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) is None
    write_tokens(tmp_path, ["nope"])
    assert w.SessionWatcher.discover_token(LOCAL, home=tmp_path) is None


# --------------------------------------------------------------------------
# safety net: nothing here may open a socket
# --------------------------------------------------------------------------

def test_no_test_reaches_the_real_server(monkeypatch):
    # If any test forgot the opener seam, urlopen would reach 127.0.0.1:6767.
    import urllib.request

    def boom(*args, **kwargs):
        raise AssertionError("a test attempted a real network call")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(w.urllib.request, "urlopen", boom)

    opener = StubOpener([session()])
    watcher = w.SessionWatcher(token="t", opener=opener)
    assert watcher.poll_once()[0].kind == "added"
    assert list(watcher.stream_events("s1")) == []


def test_watch_generator_sleeps_between_polls(monkeypatch):
    import time as time_mod

    class Done(Exception):
        pass

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise Done

    monkeypatch.setattr(time_mod, "sleep", fake_sleep)
    watcher = w.SessionWatcher(poll_interval=0.25, opener=StubOpener([session()]))
    events = []
    with pytest.raises(Done):
        for event in watcher.watch():
            events.append(event)
    assert [e.kind for e in events] == ["added"]
    assert sleeps == [0.25]


# --------------------------------------------------------------------------
# watch() resilience: the only production caller (og_herdr.py's run_forever)
# wraps nothing, so a transient error must not stop the daemon
# --------------------------------------------------------------------------

class Stop(Exception):
    """Raised by the fake sleep to end a watch() loop after N sleeps."""


def drive_watch(monkeypatch, watcher, stop_after_sleeps):
    """Run watcher.watch() to the Nth sleep. Returns (events, sleeps).

    Sleeping is stubbed, so the backoff can be asserted exactly and no test
    ever waits on a real clock.
    """
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= stop_after_sleeps:
            raise Stop

    monkeypatch.setattr(w.time, "sleep", fake_sleep)
    events = []
    with pytest.raises(Stop):
        for event in watcher.watch():
            events.append(event)
    return events, sleeps


def test_watch_survives_a_failed_poll_and_keeps_yielding(monkeypatch, capsys):
    down = URLError(ConnectionRefusedError(111, "Connection refused"))
    opener = StubOpener(
        [session("s1")],   # poll 1: s1 live
        down,              # poll 2: server restarting
        down,              # poll 3: still down
        [session("s1")],   # poll 4: back, unchanged
    )
    watcher = w.SessionWatcher(poll_interval=0.5, token="super-secret",
                               opener=opener)
    events, sleeps = drive_watch(monkeypatch, watcher, stop_after_sleeps=4)

    # s1 is announced once and never churned. If the failed polls had touched
    # _seen, recovery would diff against an empty state and emit removed+added
    # for a session that never went anywhere.
    assert [(e.kind, e.session_id) for e in events] == [("added", "s1")]
    assert watcher._seen == {"s1": session("s1")}

    # It waited rather than spun, the wait grew while the server stayed down,
    # and it went back to poll_interval once a poll succeeded.
    assert sleeps == [0.5, 0.5, 1.0, 0.5]
    assert len(opener.requests) == 4

    # The failure is visible to whoever is watching the daemon's stderr, and
    # the bearer token is not in it.
    err = capsys.readouterr().err
    assert "poll failed" in err
    assert "Connection refused" in err
    assert "super-secret" not in err


def test_watch_keeps_going_through_a_long_outage(monkeypatch):
    # Server gone for two polls, then a real change: the diff must be against
    # the last state actually seen, so only the status move is reported.
    opener = StubOpener(
        [session("s1", status="running")],
        URLError("boom"), URLError("boom"),
        [session("s1", status="idle")],
    )
    watcher = w.SessionWatcher(poll_interval=0.5, opener=opener)
    events, sleeps = drive_watch(monkeypatch, watcher, stop_after_sleeps=4)
    assert [(e.kind, e.session_id, e.state) for e in events] == [
        ("added", "s1", "working"),
        ("changed", "s1", "idle"),
    ]
    assert sleeps == [0.5, 0.5, 1.0, 0.5]


def test_watch_backoff_is_bounded(monkeypatch):
    watcher = w.SessionWatcher(poll_interval=1.0,
                               opener=StubOpener(URLError("down")))
    events, sleeps = drive_watch(monkeypatch, watcher, stop_after_sleeps=8)
    assert events == []
    # Doubles up to the cap and then stays there — unbounded backoff would be
    # its own outage, leaving the watcher asleep long after the server is back.
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0,
                      w.MAX_POLL_BACKOFF, w.MAX_POLL_BACKOFF, w.MAX_POLL_BACKOFF]


def test_watch_failure_message_scrubs_url_credentials(monkeypatch, capsys):
    # urllib puts the request URL in its error message, and a base URL written
    # with userinfo would otherwise print that password into the daemon's log.
    opener = StubOpener(
        URLError("<urlopen error http://user:hunter2@127.0.0.1:6767/v1/sessions>")
    )
    watcher = w.SessionWatcher(poll_interval=0.1, opener=opener)
    drive_watch(monkeypatch, watcher, stop_after_sleeps=1)
    err = capsys.readouterr().err
    assert "hunter2" not in err
    assert "***@" in err


def test_poll_once_still_raises():
    # The resilience belongs to the loop. poll_once() is the primitive and stays
    # strict, so a caller that wants the exception still gets it.
    watcher = w.SessionWatcher(opener=StubOpener(URLError("boom")))
    with pytest.raises(URLError):
        watcher.poll_once()


def test_watch_does_not_swallow_keyboard_interrupt(monkeypatch):
    # A daemon still has to be stoppable: Ctrl-C must not be retried forever.
    sleeps = []
    monkeypatch.setattr(w.time, "sleep", lambda s: sleeps.append(s))
    watcher = w.SessionWatcher(poll_interval=0.5,
                               opener=StubOpener(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        list(watcher.watch())
    assert sleeps == [], "Ctrl-C was treated as a poll failure and retried"
