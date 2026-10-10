"""Tests for installer/og_herdr_watch.py.

No network, ever: every test injects a stub opener (or drives the SSE parser
directly), so nothing here can touch the developer's live server on
127.0.0.1:6767. `discover_token` reads a tmp_path fixture, never the real
~/.omnigent.
"""
from __future__ import annotations

import inspect
import io
import json
import urllib.parse
from pathlib import Path
from urllib.error import HTTPError, URLError

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


class RoutingOpener:
    """A stub that answers by URL instead of by call order.

    The watcher fetches `GET /v1/sessions/{id}` once per newly seen session,
    so a plain queue of bodies cannot express "the listing fails twice" — the
    enrichment fetches would eat those bodies and the test would silently be
    asserting about a different sequence than it thinks. Routing on the path
    keeps the listing queue independent of the detail lookups, so a test can
    say "the listing goes down for two polls" and mean it.

    `listing` is a queue of bodies for `/v1/sessions` (the last one repeats);
    `details` maps a session id to a body or to an exception to raise for that
    id alone. An unmapped detail id raises AssertionError rather than falling
    back to the listing body, so a lookup that was never expected fails loudly
    instead of quietly looking like a session with no directory.
    """

    def __init__(self, listing, details=None):
        self.listing = list(listing)
        self.details = dict(details or {})
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        path = urllib.parse.urlsplit(request.full_url).path
        listing_path = "/v1/sessions"
        if path == listing_path:
            body = self.listing.pop(0) if len(self.listing) > 1 else self.listing[0]
            if isinstance(body, BaseException):
                raise body
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            return FakeResponse(body)
        assert path.startswith(listing_path + "/"), path
        sid = urllib.parse.unquote(path[len(listing_path) + 1:])
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


class RecordingStderr:
    """A stderr that keeps what it was handed, and can be told to fail.

    The watcher's own diagnostics go to sys.stderr, so the only way to test
    what happens when that write fails is to hand the module a stderr that
    fails. `fail_times` failures, then it behaves; `attempts` counts every
    write the module tried, failed or not, which is how a test can tell "the
    notice was never even attempted again" from "it was attempted and nothing
    was written".
    """

    def __init__(self, fail_times=0, error=None):
        self.lines = []
        self.attempts = 0
        self.flushes = 0
        self.fail_times = fail_times
        self.error = error if error is not None else OSError(32, "Broken pipe")

    def write(self, text):
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        self.lines.append(text)

    def flush(self):
        self.flushes += 1

    @property
    def text(self):
        return "".join(self.lines)


class _Tee:
    """A stderr that appends every line to a list."""

    def __init__(self, sink):
        self.sink = sink

    def write(self, text):
        self.sink.append(text)

    def flush(self):
        pass


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
    ).fetch_listing()
    assert ([r["id"] for r in rows], truncated) == (["s1"], False)


def test_the_page_cap_is_reported_as_truncated(monkeypatch):
    cap_pages(monkeypatch)
    opener = StubOpener(envelope([session("s1")], has_more=True, last_id="s1"))
    rows, truncated = w.SessionWatcher(opener=opener).fetch_listing()
    assert ([r["id"] for r in rows], truncated) == (["s1"], True)


def test_a_server_that_will_not_say_where_to_continue_is_truncated():
    # has_more with no cursor to use: more exists and none of it was fetched.
    payload = {"object": "list", "data": [{"status": "running"}], "has_more": True}
    _rows, truncated = w.SessionWatcher(opener=StubOpener(payload)).fetch_listing()
    assert truncated is True


def test_a_repeated_cursor_is_reported_as_truncated():
    page = envelope([session("s1")], has_more=True, last_id="same")
    opener = StubOpener(page)
    rows, truncated = w.SessionWatcher(opener=opener).fetch_listing()
    assert truncated is True
    assert len(rows) == 2, "both fetches landed before the walk gave up"


def test_the_row_cap_is_reported_as_truncated(monkeypatch):
    monkeypatch.setattr(w, "MAX_LISTED_SESSIONS", 1)
    opener = StubOpener(envelope([session("s1"), session("s2")]))
    rows, truncated = w.SessionWatcher(opener=opener).fetch_listing()
    assert ([r["id"] for r in rows], truncated) == (["s1"], True)


def test_fetch_listing_is_public_and_the_private_name_is_gone():
    # `fetch_listing` is the public name the truncation flag is read through
    # (og_agents does). The old private spelling is deliberately removed rather
    # than aliased, so a straggler caller fails loudly on `AttributeError`
    # instead of silently reading a name that no longer means anything.
    assert hasattr(w.SessionWatcher, "fetch_listing")
    assert not hasattr(w.SessionWatcher, "_fetch_listing")


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
# the session's directory: the LISTING row has no `workspace`, the DETAIL row
# does. Run against a live 0.17.0 server: every projected pane opened in
# whatever directory the daemon happened to start in, because the bridge read
# an absent key and used its own cwd.
# --------------------------------------------------------------------------

def detail(sid, workspace=None, parent=None, **extra):
    """One `GET /v1/sessions/{id}` row, shaped like the real one.

    The detail row is NOT the listing row: it carries `workspace`, plus
    `kind`, `git_branch`, `sub_agent_name`, `runner_online` and the full
    `pending_elicitations` LIST where the listing has only its count. A
    sub-agent's workspace is None on a live server — that is the fact the parent
    fallback exists for. `runner_online` is here too, which is why the runner
    rule cannot live in `should_project`: no listing row carries it.
    """
    row = {
        "id": sid,
        "workspace": workspace,
        "parent_session_id": parent,
        "kind": "default" if parent is None else "sub_agent",
        "sub_agent_name": None if parent is None else f"coder_{sid}",
        "git_branch": None,
        "pending_elicitations": [],
        "runner_online": None,
        "status": "running",
        "title": f"title-{sid}",
    }
    row.update(extra)
    return row


def test_a_roots_own_directory_reaches_the_event():
    # The defect, stated in its smallest form: a root session's real workspace
    # has to arrive on the event, or every pane opens in the daemon's cwd.
    opener = RoutingOpener(
        [envelope([session("s1")])],
        details={"s1": detail("s1", workspace="/Users/cryogenix/projects/og")},
    )
    event = w.SessionWatcher(opener=opener).poll_once()[0]

    assert event.kind == "added"
    assert event.session["workspace"] == "/Users/cryogenix/projects/og"
    assert event.session["id"] == "s1"


def test_a_sub_agent_inherits_its_parents_directory():
    # A sub-agent's OWN detail is None (measured). Its parent's is a real path,
    # and a worker of the og session belongs in the og tree — which beats
    # "wherever the daemon started" by a mile. It is an approximation: the
    # worker's actual git worktree is not reported anywhere by the API.
    opener = RoutingOpener(
        [envelope([session("w1", status="running", parent="root")])],
        details={
            "w1": detail("w1", workspace=None, parent="root"),
            "root": detail("root", workspace="/Users/cryogenix/projects/og"),
        },
    )
    event = w.SessionWatcher(opener=opener).poll_once()[0]

    assert event.session_id == "w1"
    assert event.session["workspace"] == "/Users/cryogenix/projects/og"


def test_a_sub_agent_with_no_directory_anywhere_emits_no_key():
    # Neither the worker's own detail nor its parent's carried one. The key
    # must be ABSENT, not None and not "": the bridge reads it with `.get()`
    # and falls back on both, but an emitted null would be a claim that the
    # directory is unknown-null rather than unknown — and it is exactly the
    # difference the consumer's fallback exists to make.
    opener = RoutingOpener(
        [envelope([session("w1", status="running", parent="root")])],
        details={
            "w1": detail("w1", workspace=None, parent="root"),
            "root": detail("root", workspace=None),
        },
    )
    event = w.SessionWatcher(opener=opener).poll_once()[0]

    # Both rows really were asked — otherwise this would pass on a module that
    # never resolves a directory at all, which is the pre-fix state.
    fetched = [r.full_url.rsplit("/", 1)[-1] for r in opener.detail_requests]
    assert sorted(fetched) == ["root", "w1"], fetched
    assert "workspace" not in event.session, event.session


def test_the_detail_is_fetched_once_per_session_not_once_per_poll():
    # A poll must stay one listing walk plus a few fetches. Re-reading every
    # known session's detail each poll would turn a 3-second poll into a
    # request storm proportional to the session count.
    opener = RoutingOpener(
        [envelope([session("s1")]), envelope([session("s1")]),
         envelope([session("s1")])],
        details={"s1": detail("s1", workspace="/repo/og")},
    )
    watcher = w.SessionWatcher(opener=opener)

    first = watcher.poll_once()
    assert first[0].session["workspace"] == "/repo/og"
    assert watcher.poll_once() == []
    assert watcher.poll_once() == []

    assert len(opener.detail_requests) == 1, (
        "the detail was re-read on a later poll; it is meant to be fetched "
        "once, when the session is first seen"
    )
    assert len(opener.listing_requests) == 3


# --------------------------------------------------------------------------
# the session's IDENTITY: which worker is this? The consumer lays a delegated
# worker out beside its parent, and it can only do that with `kind`, `harness`
# and `sub_agent_name` — all three of which the LISTING row does not carry.
# They ride the SAME detail fetch as the directory: one request, more answers.
# --------------------------------------------------------------------------

# The two rows the task's measured data is taken from, verbatim in the fields
# that matter. The disagreement between them is the whole reason DETAIL_FIELDS
# reads the detail row: a sub-agent's listing row says `agent_name: "hivemind"`,
# which is its ROOT's agent, while its detail row says `coder_zen`.
LISTED_ROOT = {
    "id": "e29bf406", "parent_session_id": None,
    "agent_name": "hivemind", "agent_id": "a1", "status": "running",
    "title": "Omnigent Herdr Integration Feasibility", "archived": False,
    "pending_elicitations_count": 0,
}
LISTED_SUB = {
    "id": "de7c4242", "parent_session_id": "e29bf406",
    "agent_name": "hivemind", "agent_id": "a1", "status": "running",
    "title": "coder_zen:space-per-session", "archived": False,
    "pending_elicitations_count": 0,
}
DETAIL_ROOT = {
    "id": "e29bf406", "kind": "default", "sub_agent_name": None,
    "agent_name": "hivemind", "harness": "claude-native",
    "parent_session_id": None, "workspace": "/Users/cryogenix/projects/og",
    "runner_online": True,
}
DETAIL_SUB = {
    "id": "de7c4242", "kind": "sub_agent", "sub_agent_name": "coder_zen",
    "agent_name": "coder_zen", "harness": "opencode-native",
    "parent_session_id": "e29bf406", "workspace": None, "runner_online": True,
}


def test_a_roots_identity_reaches_the_event():
    opener = RoutingOpener([envelope([LISTED_ROOT])],
                           details={"e29bf406": DETAIL_ROOT})
    session = w.SessionWatcher(opener=opener).poll_once()[0].session

    assert session["kind"] == "default"
    assert session["harness"] == "claude-native"
    assert "sub_agent_name" not in session, (
        "a root's sub_agent_name is null and must stay ABSENT, not be merged "
        "as a null the consumer would have to treat as a name"
    )


def test_a_sub_agents_identity_reaches_the_event():
    opener = RoutingOpener([envelope([LISTED_SUB])],
                           details={"de7c4242": DETAIL_SUB,
                                    "e29bf406": DETAIL_ROOT})
    session = w.SessionWatcher(opener=opener).poll_once()[0].session

    assert session["kind"] == "sub_agent"
    assert session["harness"] == "opencode-native"
    assert session["sub_agent_name"] == "coder_zen"


def test_the_listing_agents_name_is_never_the_one_merged():
    # The trap this exists to avoid: a sub-agent's LISTING row reads
    # `agent_name: "hivemind"` — the ROOT's agent. Merging it would label every
    # worker in a conversation with the conversation's orchestrator, which is
    # wrong for all of them at once and looks entirely plausible on screen.
    opener = RoutingOpener([envelope([LISTED_SUB])],
                           details={"de7c4242": DETAIL_SUB,
                                    "e29bf406": DETAIL_ROOT})
    session = w.SessionWatcher(opener=opener).poll_once()[0].session

    assert session["agent_name"] == "hivemind", "the listing row is untouched"
    assert session["harness"] == "opencode-native", "the detail row is the one"
    assert "coder_zen" not in session.get("agent_name", "")


def test_the_identity_rides_the_same_fetch_as_the_directory():
    # One request, more answers. A second round trip per session, purely to ask
    # which worker this is, would double the cost of every poll that discovers
    # something — and the answer was in the object already being fetched, which
    # is why it is merged here rather than requested next door.
    opener = RoutingOpener(
        [envelope([LISTED_ROOT]), envelope([LISTED_ROOT])],
        details={"e29bf406": DETAIL_ROOT},
    )
    watcher = w.SessionWatcher(opener=opener)
    added = watcher.poll_once()[0].session
    watcher.poll_once()

    assert added["harness"] == "claude-native"
    assert added["kind"] == "default"
    assert added["workspace"] == "/Users/cryogenix/projects/og"
    assert len(opener.detail_requests) == 1, (
        [r.full_url for r in opener.detail_requests])


def test_a_sub_agents_identity_and_directory_come_from_its_own_fetch():
    opener = RoutingOpener(
        [envelope([LISTED_SUB]), envelope([LISTED_SUB])],
        details={"de7c4242": DETAIL_SUB, "e29bf406": DETAIL_ROOT},
    )
    watcher = w.SessionWatcher(opener=opener)
    added = watcher.poll_once()[0].session
    watcher.poll_once()

    assert added["sub_agent_name"] == "coder_zen"
    # Its own detail has workspace=None; the parent's directory is what the
    # watcher resolves, exactly as before this change.
    assert added["workspace"] == "/Users/cryogenix/projects/og"
    # Two sessions were asked (the worker, then its parent for the directory) and
    # neither was asked twice — the identity added no traffic to either.
    asked = [r.full_url.rsplit("/", 1)[-1] for r in opener.detail_requests]
    assert sorted(asked) == ["de7c4242", "e29bf406"], asked


def test_a_null_identity_field_is_left_out_rather_than_merged_as_none():
    opener = RoutingOpener(
        [envelope([LISTED_ROOT])],
        details={"e29bf406": dict(DETAIL_ROOT, kind=None, harness="",
                                   sub_agent_name=None)},
    )
    session = w.SessionWatcher(opener=opener).poll_once()[0].session

    for key in w.DETAIL_FIELDS:
        assert key not in session, (
            "{0} should have been omitted, not merged as {1!r}".format(
                key, session.get(key)))


def test_a_failed_detail_fetch_still_omits_the_identity_fields():
    # The identity is an enrichment, exactly like the directory: a detail
    # endpoint that is down must cost the harness and the sub-agent name, not
    # the session. The bridge falls back to the raw/default label.
    for bad in (HTTPError("http://x/v1/sessions/s1", 500, "Server Error", {}, None),
                OSError(28, "No space left on device"),
                json.JSONDecodeError("Expecting value", "{", 0)):
        opener = RoutingOpener([envelope([dict(LISTED_SUB)])],
                               details={"de7c4242": bad,
                                        "e29bf406": DETAIL_ROOT})
        event = w.SessionWatcher(opener=opener).poll_once()[0]

        assert event.kind == "added", bad
        for key in w.DETAIL_FIELDS:
            assert key not in event.session, (key, bad)
        assert event.session["title"] == "coder_zen:space-per-session"


def test_a_changed_event_does_not_refetch_the_detail():
    # The other half of "once per session": a `changed` event is a session the
    # watcher already knows, so it must not cost a request either.
    opener = RoutingOpener(
        [envelope([session("s1", status="running")]),
         envelope([session("s1", status="idle")])],
        details={"s1": detail("s1", workspace="/repo/og")},
    )
    watcher = w.SessionWatcher(opener=opener)

    assert watcher.poll_once()[0].kind == "added"
    changed = watcher.poll_once()[0]
    assert changed.kind == "changed"
    assert len(opener.detail_requests) == 1


def test_a_fan_out_of_workers_fetches_the_parent_once():
    # Four workers under one root must not fetch that root four times. This is
    # the whole reason the parent cache exists.
    rows = [session(f"w{i}", status="running", parent="root") for i in range(4)]
    opener = RoutingOpener(
        [envelope(rows)],
        details={"root": detail("root", workspace="/repo/og"),
                 **{f"w{i}": detail(f"w{i}", workspace=None, parent="root")
                    for i in range(4)}},
    )
    events = w.SessionWatcher(opener=opener).poll_once()

    assert [e.session["workspace"] for e in events] == ["/repo/og"] * 4
    fetched = [r.full_url.rsplit("/", 1)[-1] for r in opener.detail_requests]
    assert fetched.count("root") == 1, fetched


def test_the_parent_cache_is_bounded():
    # This process is meant to run for days. `_seen` already holds one entry per
    # live session; a second unbounded map beside it is memory nobody can
    # account for, so the parent cache evicts rather than growing.
    cap = w.MAX_PARENT_WORKSPACES
    roots = [f"root{i}" for i in range(cap + 5)]
    details = {r: detail(r, workspace=f"/repo/{r}") for r in roots}
    details.update({f"w{r}": detail(f"w{r}", workspace=None, parent=r)
                    for r in roots})
    rows = [session(f"w{r}", status="running", parent=r) for r in roots]
    opener = RoutingOpener([envelope(rows)], details=details)

    watcher = w.SessionWatcher(opener=opener)
    watcher.poll_once()

    assert len(watcher._parent_workspace) <= cap, (
        "the parent cache grew past its ceiling"
    )


def test_a_detail_that_raises_still_emits_the_event():
    # Enrichment is a nicety and must never cost a poll. A 404, a 500, a
    # timeout or garbage JSON must cost the DIRECTORY and nothing else — the
    # session is still reported, and the bridge still falls back to --cwd.
    for bad in (HTTPError("http://x/v1/sessions/s1", 404, "Not Found", {}, None),
                OSError(28, "No space left on device"),
                json.JSONDecodeError("Expecting value", "{", 0)):
        opener = RoutingOpener(
            [envelope([session("s1")]), envelope([session("s1")])],
            details={"s1": bad},
        )
        watcher = w.SessionWatcher(opener=opener)

        event = watcher.poll_once()[0]
        assert event.kind == "added", bad
        assert len(opener.detail_requests) == 1, (
            "the detail was never fetched, so this proves nothing about a "
            "fetch that fails"
        )
        assert "workspace" not in event.session, (
            "a failed detail fetch emitted a directory anyway"
        )
        assert event.session["id"] == "s1", "the session itself was lost"

        # And the watcher is still a watcher afterwards.
        assert watcher.poll_once() == []


def test_a_detail_failure_is_retried_for_a_later_session_not_remembered():
    # A parent whose detail failed must be re-asked on the next sub-agent, not
    # remembered as "has no directory": caching a miss would make one transient
    # 500 permanent for the rest of the process's life.
    opener = RoutingOpener(
        [envelope([session("w1", status="running", parent="root")]),
         envelope([session("w1", status="running", parent="root"),
                   session("w2", status="running", parent="root")])],
        details={"w1": detail("w1", workspace=None, parent="root"),
                 "w2": detail("w2", workspace=None, parent="root"),
                 "root": OSError(503, "Service Unavailable")},
    )
    watcher = w.SessionWatcher(opener=opener)

    assert "workspace" not in watcher.poll_once()[0].session
    events = watcher.poll_once()
    assert [(e.kind, e.session_id) for e in events] == [("added", "w2")]
    fetched = [r.full_url.rsplit("/", 1)[-1] for r in opener.detail_requests]
    assert fetched.count("root") == 2, (
        "a failed parent fetch was cached as an answer and never retried"
    )


def test_the_enrichment_does_not_swallow_a_bug():
    # The swallow in _fetch_detail is scoped to the FETCH. A TypeError raised by
    # a broken _get_json is a bug in this module, and poll_once must still let
    # it out (watch() then stops on it) rather than have it read as a flaky
    # server. Pre-fix this module never fetched, so the assertion is that the
    # exception survives the enrichment path specifically: the listing below
    # succeeds and only the DETAIL raises.
    opener = RoutingOpener(
        [envelope([session("s1")])],
        details={"s1": TypeError("broken decoder")},
    )
    watcher = w.SessionWatcher(opener=opener)
    with pytest.raises(TypeError):
        watcher.poll_once()


def test_a_bug_inside_the_enrichment_still_stops_the_watcher(monkeypatch):
    # The same guarantee from the other side: the catch is around the FETCH, so
    # a mistake in the code that reads what it fetched is still a bug and not a
    # server that would not answer. This is the line between "swallow the
    # network" and "swallow everything", and it is the reason the except clause
    # names (OSError, ValueError) rather than Exception.
    monkeypatch.setattr(w.SessionWatcher, "_fetch_detail",
                        lambda self, sid: (_ for _ in ()).throw(
                            AttributeError("broken enrichment")))
    watcher = w.SessionWatcher(opener=StubOpener(envelope([session("s1")])))
    with pytest.raises(AttributeError):
        watcher.poll_once()


def test_the_retained_row_is_not_the_enriched_one():
    # `_seen` holds what the server LISTED. The enrichment belongs to the event
    # and to nothing else — otherwise the diff would be comparing an enriched
    # object against a raw one, and a mutated event could rewrite what the
    # watcher believes it saw.
    opener = RoutingOpener(
        [envelope([session("s1")]), envelope([session("s1", status="idle")])],
        details={"s1": detail("s1", workspace="/repo/og")},
    )
    watcher = w.SessionWatcher(opener=opener)

    added = watcher.poll_once()[0]
    assert watcher._seen["s1"] == session("s1")
    assert "workspace" not in watcher._seen["s1"]
    assert added.session["workspace"] == "/repo/og"

    changed = watcher.poll_once()[0]
    assert changed.kind == "changed"
    assert changed.previous == session("s1")
    assert changed.session["status"] == "idle"


def test_the_enrichment_does_not_make_a_workspace_change_material():
    # A directory is not one of MATERIAL_FIELDS: a session whose workspace
    # moved must not be reported as "changed", because a changed event does not
    # move a pane — it only re-reports state and title.
    opener = RoutingOpener(
        [envelope([session("s1")]), envelope([session("s1")])],
        details={"s1": detail("s1", workspace="/repo/og")},
    )
    watcher = w.SessionWatcher(opener=opener)
    first = watcher.poll_once()[0]
    # The first poll really did carry a directory, so the second poll's silence
    # is about the workspace changing rather than about there never being one.
    assert first.session["workspace"] == "/repo/og"

    opener.details["s1"] = detail("s1", workspace="/somewhere/else")
    assert watcher.poll_once() == []


def test_an_enriched_session_still_diffs_and_still_projects_correctly():
    # The enrichment is a merge into a copy; it must not disturb the status
    # logic, which reads a different half of the same object.
    opener = RoutingOpener(
        [envelope([session("s1", status="running",
                           pending_elicitations_count=1)])],
        details={"s1": detail("s1", workspace="/repo/og")},
    )
    event = w.SessionWatcher(opener=opener).poll_once()[0]
    assert event.state == "blocked", "the elicitation count stopped counting"
    assert event.session["workspace"] == "/repo/og"


# --------------------------------------------------------------------------
# a session whose runner is offline gets no pane at all
#
# `omnigent attach` joins a LIVE session on a running server and refuses
# anything else — measured in a real pane:
#
#     Error: Session cf399984… has no online runner on http://127.0.0.1:6767
#
# Measured across the seven sessions the bridge projected:
#
#     f9548b99  sub_agent  running  runner_online=True
#     d1c2c91b  sub_agent  idle     runner_online=True
#     3e9a0249  sub_agent  running  runner_online=True
#     dfadeb68  default    idle     runner_online=True   host_online=True
#     e29bf406  default    running  runner_online=True   host_online=True
#     5be56e58  default    idle     runner_online=False  host_online=True
#     cf399984  default    idle     runner_online=False  host_online=True
#
# Two of seven would open a tab whose whole content is that error. On a machine
# with a long history of finished root sessions it would be most of them.
#
# `runner_online` is on the DETAIL row — no listing row carries it — so the rule
# is `runner_is_offline`, applied by `_enrich` where that row is already fetched
# for the directory, and NOT folded into `should_project`, which decides from
# listing rows alone.
# --------------------------------------------------------------------------

def test_a_session_with_an_offline_runner_is_not_projected():
    # The defect, in its smallest form: the offline session is dropped from the
    # event stream, so no pane is ever opened for a command that can only fail.
    opener = RoutingOpener(
        [envelope([session("offline"), session("online")])],
        details={"offline": detail("offline", workspace="/repo/og",
                                   runner_online=False),
                 "online": detail("online", workspace="/repo/og",
                                  runner_online=True)},
    )
    watcher = w.SessionWatcher(opener=opener)

    assert [(e.kind, e.session_id) for e in watcher.poll_once()] == [
        ("added", "online")]
    assert set(watcher._seen) == {"online"}
    # Both details really were fetched: otherwise this would pass on a module
    # that never asks, which is a different (and already-fixed) defect.
    assert len(opener.detail_requests) == 2


def test_a_session_with_an_online_runner_is_still_projected():
    # The other side of the same pair. Without it, a rule that dropped everything
    # would satisfy the test above, and a workspace that empties itself looks
    # exactly like a clean bill of health.
    opener = RoutingOpener(
        [envelope([session("s1")])],
        details={"s1": detail("s1", workspace="/repo/og", runner_online=True)},
    )
    event = w.SessionWatcher(opener=opener).poll_once()[0]

    assert event.kind == "added"
    assert event.session["id"] == "s1"
    # One fetch still bought both answers.
    assert event.session["workspace"] == "/repo/og"
    assert len(opener.detail_requests) == 1


def test_the_runner_rule_does_not_depend_on_the_session_kind():
    # Both of the measured offline sessions were roots, but `attach` refuses by
    # runner, not by kind: a worker whose runner died is equally unattachable.
    # Pinning it both ways stops the rule from being quietly narrowed to roots.
    rows = [session("root-off", parent=None),
            session("root-on", parent=None),
            session("w-off", status="running", parent="root-on"),
            session("w-on", status="running", parent="root-on")]
    opener = RoutingOpener(
        [envelope(rows)],
        details={
            "root-off": detail("root-off", workspace="/repo/og",
                               runner_online=False),
            "root-on": detail("root-on", workspace="/repo/og", runner_online=True),
            "w-off": detail("w-off", workspace=None, parent="root-on",
                            runner_online=False),
            "w-on": detail("w-on", workspace=None, parent="root-on",
                           runner_online=True),
        },
    )

    assert [e.session_id for e in w.SessionWatcher(opener=opener).poll_once()] == [
        "root-on", "w-on"]


def test_a_session_whose_runner_comes_back_is_projected_on_a_later_poll():
    # No permanent negative. A refused session must never be remembered as
    # unattachable, because a runner CAN come back — and a refusal cached for the
    # life of the process would strand that session's pane for good, invisibly.
    row = session("s1")
    opener = RoutingOpener(
        [envelope([row]), envelope([row])],
        details={"s1": detail("s1", workspace="/repo/og", runner_online=False)},
    )
    watcher = w.SessionWatcher(opener=opener)
    assert watcher.poll_once() == []

    # The runner comes back up; nothing about the refusal was sticky.
    opener.details["s1"] = detail("s1", workspace="/repo/og", runner_online=True)
    events = watcher.poll_once()
    assert [(e.kind, e.session_id) for e in events] == [("added", "s1")]
    assert events[0].session["workspace"] == "/repo/og"


def test_a_session_refused_for_an_offline_runner_never_enters_seen():
    # `_seen` is what "removed" is derived from, so staying out of it is what
    # keeps a never-projected session from manufacturing the removal of a pane
    # that was never opened. It is also what makes the refusal re-checkable.
    opener = RoutingOpener(
        [envelope([session("s1")])],
        details={"s1": detail("s1", workspace="/repo/og", runner_online=False)},
    )
    watcher = w.SessionWatcher(opener=opener)

    for poll in range(3):
        assert watcher.poll_once() == [], (
            f"poll {poll} produced an event for a session with no pane")

    assert "s1" not in watcher._seen
    # Re-asked on every poll — the cost is one detail fetch per such session per
    # poll, bounded by how many there are (measured: 2 of 7), and never a
    # cached "no".
    assert len(opener.detail_requests) == 3


def test_a_never_projected_session_that_vanishes_reports_no_removal():
    # The absence case, which is where a `_seen` mistake would show: the session
    # leaves the listing entirely having never been projected. There is nothing
    # to release and nothing to close, and a `removed` here would have the
    # bridge closing a tab handle that was never issued.
    opener = RoutingOpener(
        [envelope([session("s1")]), envelope([])],
        details={"s1": detail("s1", workspace="/repo/og", runner_online=False)},
    )
    watcher = w.SessionWatcher(opener=opener)

    assert watcher.poll_once() == []
    assert watcher.poll_once() == []
    assert watcher._seen == {}


def test_a_failed_detail_fetch_projects_rather_than_hiding():
    # THE CHOSEN DIRECTION, and why: a detail row we could not read is UNKNOWN,
    # and unknown projects. Hiding on a guess costs the user a session they never
    # knew existed and cannot get back by waiting, because nothing would say it
    # was dropped; projecting a session whose runner turns out to be dead costs
    # one tab showing an error. An unreachable detail endpoint must never be able
    # to empty the workspace, so `runner_is_offline` treats None as "no".
    failures = [
        HTTPError("http://x/v1/sessions/s1", 404, "Not Found", {}, None),
        HTTPError("http://x/v1/sessions/s1", 500, "Server Error", {}, None),
        OSError(28, "No space left on device"),
        URLError("connection refused"),
        json.JSONDecodeError("Expecting value", "{", 0),
        [session("s1")],  # a body that is not a detail row at all
    ]
    for bad in failures:
        opener = RoutingOpener(
            [envelope([session("s1")])],
            details={"s1": bad},
        )
        watcher = w.SessionWatcher(opener=opener)

        events = watcher.poll_once()
        # The fetch really was attempted, so this is a test about a FAILING fetch
        # and not about a module that never asks.
        assert len(opener.detail_requests) == 1, bad
        assert [(e.kind, e.session_id) for e in events] == [("added", "s1")], bad
        assert "workspace" not in events[0].session, bad
        # And it stays in the set, so it is a normal live session from here on.
        assert "s1" in watcher._seen, bad


def test_runner_is_offline_is_false_for_everything_but_an_explicit_false():
    # The rule itself, stated as a function of its input. None (the unreadable
    # fetch), a row without the key, a null, and a non-boolean "false" all
    # project: only a JSON false is a server telling us the runner is down.
    assert w.runner_is_offline({"runner_online": False}) is True
    for detail_row in (None, {}, [], "false",
                       {"runner_online": None},
                       {"runner_online": "false"},
                       {"runner_online": 0},
                       {"runner_online": True},
                       {"workspace": "/repo"}):
        assert w.runner_is_offline(detail_row) is False, detail_row


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
    opener = RoutingOpener(
        [envelope([session("s1")]),   # poll 1: s1 live
         down,                       # poll 2: server restarting
         down,                       # poll 3: still down
         envelope([session("s1")])],  # poll 4: back, unchanged
        details={"s1": {}},
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
    # and it went back to poll_interval once a poll succeeded. Four LISTING
    # requests for four polls — the per-session detail lookup does not turn a
    # stable listing into per-poll traffic.
    assert sleeps == [0.5, 0.5, 1.0, 0.5]
    assert len(opener.listing_requests) == 4
    assert len(opener.detail_requests) == 1

    # The failure is visible to whoever is watching the daemon's stderr, and
    # the bearer token is not in it.
    err = capsys.readouterr().err
    assert "poll failed" in err
    assert "Connection refused" in err
    assert "super-secret" not in err


def test_watch_keeps_going_through_a_long_outage(monkeypatch):
    # Server gone for two polls, then a real change: the diff must be against
    # the last state actually seen, so only the status move is reported.
    opener = RoutingOpener(
        [envelope([session("s1", status="running")]),
         URLError("boom"), URLError("boom"),
         envelope([session("s1", status="idle")])],
        details={"s1": {}},
    )
    watcher = w.SessionWatcher(poll_interval=0.5, opener=opener)
    events, sleeps = drive_watch(monkeypatch, watcher, stop_after_sleeps=4)
    assert [(e.kind, e.session_id, e.state) for e in events] == [
        ("added", "s1", "working"),
        ("changed", "s1", "idle"),
    ]
    assert sleeps == [0.5, 0.5, 1.0, 0.5]
    # The enrichment does not touch the outage path: no detail is fetched while
    # the listing is down, and none on recovery either, because s1 has been in
    # _seen since poll 1.
    assert len(opener.detail_requests) == 1, [
        r.full_url for r in opener.detail_requests]


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


def test_watch_does_not_retry_system_exit(monkeypatch):
    # Same for SystemExit: a BaseException, so it was never in scope for the
    # broad catch, and excluding _BUG_EXCEPTIONS must not have moved it there.
    sleeps = []
    monkeypatch.setattr(w.time, "sleep", lambda s: sleeps.append(s))
    watcher = w.SessionWatcher(poll_interval=0.5, opener=StubOpener(SystemExit(2)))
    with pytest.raises(SystemExit):
        list(watcher.watch())
    assert sleeps == []


# --------------------------------------------------------------------------
# a stderr that refuses the truncation notice: the notice is the only record
# that the watcher is running on partial knowledge, so losing it silently
# leaves removals suppressed with nothing in the log to explain it
# --------------------------------------------------------------------------

def test_a_notice_that_could_not_be_written_is_not_recorded_as_announced(monkeypatch):
    cap_pages(monkeypatch)
    page = envelope([session("s1")], has_more=True, last_id="s1")
    err = RecordingStderr(fail_times=1)
    monkeypatch.setattr(w.sys, "stderr", err)
    watcher = w.SessionWatcher(opener=StubOpener(page))

    watcher.poll_once()
    assert err.text == "", "a notice reached a stderr that refused it"
    assert watcher._listing_truncated is False, (
        "the state was recorded as announced even though nothing was written, "
        "so the next poll would find no transition and never retry the notice"
    )

    # The state is unchanged, so the next poll still sees a transition — and
    # this time the write lands. A transient stderr failure costs one line, not
    # the notice.
    watcher.poll_once()
    assert "truncated" in err.text, err.text
    assert "removals suppressed" in err.text, err.text
    assert err.flushes == 1


def test_a_persistently_broken_stderr_raises_nothing_and_writes_nothing(monkeypatch):
    # The other half of the same decision: a stderr that is gone for good. The
    # write is attempted every poll and fails every time, so the cost is one
    # failed syscall per poll. No exception may reach poll_once's caller (a
    # broken stderr would otherwise turn a logging problem into a poll-failure
    # retry loop) and no state may be recorded (see the test above).
    cap_pages(monkeypatch)
    page = envelope([session("s1")], has_more=True, last_id="s1")
    err = RecordingStderr(fail_times=10_000)
    monkeypatch.setattr(w.sys, "stderr", err)
    watcher = w.SessionWatcher(opener=StubOpener(page))

    events = watcher.poll_once()
    for _ in range(2):
        assert watcher.poll_once() == []

    assert [(e.kind, e.session_id) for e in events] == [("added", "s1")], (
        "the watcher stopped producing events because it could not log"
    )
    assert err.text == "", "something was written to a stderr that refuses it"
    assert err.attempts == 3, "the notice was not retried, so a recovering stderr would never get it"
    assert watcher._listing_truncated is False


def test_a_written_notice_is_recorded_so_the_state_is_not_re_announced(monkeypatch):
    # The regression guard for the fix above: recording the state on success is
    # still what stops a permanently truncated watcher from saying so forever.
    cap_pages(monkeypatch)
    page = envelope([session("s1")], has_more=True, last_id="s1")
    err = RecordingStderr()
    monkeypatch.setattr(w.sys, "stderr", err)
    watcher = w.SessionWatcher(opener=StubOpener(page))

    for _ in range(3):
        watcher.poll_once()
    assert err.text.count("truncated") == 1, err.text
    assert watcher._listing_truncated is True
    assert err.flushes == 1


def test_a_broken_stderr_is_not_mistaken_for_a_bug_by_poll_once(monkeypatch):
    # The ValueError half: "I/O operation on closed file". Only OSError+ValueError
    # are swallowed, and a stderr failure must never be escalated into the
    # stopping-for-a-bug path added below.
    cap_pages(monkeypatch)
    page = envelope([session("s1")], has_more=True, last_id="s1")
    closed = ValueError("I/O operation on closed file")
    err = RecordingStderr(fail_times=10_000, error=closed)
    monkeypatch.setattr(w.sys, "stderr", err)
    w.SessionWatcher(opener=StubOpener(page)).poll_once()
    assert err.text == ""


# --------------------------------------------------------------------------
# watch() stops on what can only be a bug, and keeps retrying everything else
# --------------------------------------------------------------------------

def test_the_bug_tuple_is_exactly_the_four_definitional_cases():
    # Pinned so that growing it is a deliberate act. Adding a class is right
    # when it cannot come from I/O; replacing this with a list of known-good
    # errors is the failure that stops the daemon on an outage.
    assert w._BUG_EXCEPTIONS == (TypeError, AttributeError, NameError, AssertionError)


@pytest.mark.parametrize("exc_type", [TypeError, AttributeError, NameError,
                                      AssertionError])
def test_watch_stops_instead_of_retrying_a_bug(monkeypatch, capsys, exc_type):
    # A broken module cannot be fixed by asking again. What it must not do is
    # look like a network blip while it burns a process at the 30s ceiling.
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        # A loop that got here is retrying the bug, which is the thing under
        # test. Ending it here makes that a failure instead of a hang.
        raise Stop

    monkeypatch.setattr(w.time, "sleep", fake_sleep)
    watcher = w.SessionWatcher(poll_interval=0.5,
                               opener=StubOpener(exc_type("boom")))

    with pytest.raises(exc_type):
        list(watcher.watch())
    assert sleeps == [], "a bug was retried instead of raised"

    err = capsys.readouterr().err
    assert "bug" in err, err
    assert exc_type.__name__ in err, "the log does not say which exception stopped it"
    assert "poll failed" not in err, (
        "the stop was reported as a transport failure, which is the reading "
        f"that hides the bug: {err}"
    )


@pytest.mark.parametrize(
    "exc", [OSError("down"), URLError("down"),
            json.JSONDecodeError("Expecting value", "{", 0)],
    ids=["OSError", "URLError", "JSONDecodeError"],
)
def test_watch_still_retries_the_errors_that_look_like_transport(
        monkeypatch, capsys, exc):
    # The exclusion is small on purpose: everything the network and the decoder
    # can plausibly raise keeps its existing backoff, unchanged.
    watcher = w.SessionWatcher(poll_interval=0.5, opener=StubOpener(exc))
    events, sleeps = drive_watch(monkeypatch, watcher, stop_after_sleeps=3)

    assert events == []
    assert sleeps == [0.5, 1.0, 2.0], "the retry backoff changed for a retried error"
    err = capsys.readouterr().err
    assert "poll failed" in err, err
    assert "retrying" in err, err


def test_watch_retries_an_exception_nobody_enumerated(monkeypatch, capsys):
    # This is the defect the broad catch exists for. A class the module has never
    # heard of — a future SSL error, whatever a caller's injected opener raises —
    # must still be retried; treating "unrecognised" as "fatal" is what silently
    # ended the daemon on a server restart.
    class Weird(Exception):
        """An exception no allow-list would have been able to enumerate."""

    watcher = w.SessionWatcher(poll_interval=0.5, opener=StubOpener(Weird("odd")))
    events, sleeps = drive_watch(monkeypatch, watcher, stop_after_sleeps=3)

    assert events == []
    assert sleeps == [0.5, 1.0, 2.0]
    assert "poll failed" in capsys.readouterr().err


# --------------------------------------------------------------------------
# a broken stderr in BOTH branches of watch(): the guard is one deliberate
# pattern, not two accidents
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stderr_error", [
    OSError(28, "No space left on device"),
    ValueError("I/O operation on closed file"),
], ids=["OSError", "ValueError"])
def test_a_broken_stderr_does_not_kill_the_retry_branch(monkeypatch, stderr_error):
    # The gap: the _BUG_EXCEPTIONS branch below guarded its stderr write and
    # the broad `except Exception` branch above it did not. Broken stderr (a
    # full disk under `og herdr > log 2>&1`) plus any ordinary transport error,
    # and the OSError from the write escapes the handler, leaves watch() and
    # kills the daemon — the silent death the broad catch exists to prevent,
    # arriving through the one path we had not covered.
    err = RecordingStderr(fail_times=10_000, error=stderr_error)
    monkeypatch.setattr(w.sys, "stderr", err)
    watcher = w.SessionWatcher(poll_interval=0.5,
                               opener=StubOpener(URLError("down")))

    # The transport error is STILL retried, with the same backoff as ever: the
    # fix must not have bought survival by swallowing the retry.
    events, sleeps = drive_watch(monkeypatch, watcher, stop_after_sleeps=3)

    assert events == []
    assert sleeps == [0.5, 1.0, 2.0], (
        "a broken stderr changed the retry backoff, so the guard cost the "
        "behaviour it was protecting"
    )
    assert err.attempts == 3, "the branch never tried to report the failure"


def test_a_broken_stderr_does_not_kill_the_bug_branch(monkeypatch):
    # The other half, now sharing one helper rather than duplicating a guard:
    # the bug must still leave the generator, and the broken stderr must not
    # replace it with an OSError that reads like a transport failure.
    err = RecordingStderr(fail_times=10_000)
    monkeypatch.setattr(w.sys, "stderr", err)
    watcher = w.SessionWatcher(poll_interval=0.5, opener=StubOpener(TypeError("boom")))

    with pytest.raises(TypeError):
        list(watcher.watch())
    assert err.attempts == 1


def test_both_branches_report_through_the_one_guarded_writer():
    # The two guards read as one pattern because they ARE one function. Pinned
    # so that a third stderr write added later without the guard is visible here
    # rather than in production, and so the helper cannot be deleted back into
    # two hand-rolled try/excepts.
    source = inspect.getsource(w.SessionWatcher.watch) + \
        inspect.getsource(w.SessionWatcher._announce_listing)
    assert source.count("sys.stderr") == 0, (
        "watch()/_announce_listing wrote to stderr directly; every diagnostic "
        "must go through _say so one guard covers all of them"
    )


def test_say_reports_whether_the_line_landed(monkeypatch):
    # _announce_listing's correctness depends on this return value: it records
    # its state as announced only on a write that landed.
    err = RecordingStderr()
    monkeypatch.setattr(w.sys, "stderr", err)
    assert w._say("hello\n") is True
    assert err.text == "hello\n"
    assert err.flushes == 1

    broken = RecordingStderr(fail_times=1)
    monkeypatch.setattr(w.sys, "stderr", broken)
    assert w._say("hello\n") is False
    assert broken.text == ""


def test_say_writes_the_line_it_was_handed(monkeypatch):
    # _say stays a pure "write or give up": the URL-credential scrub is the
    # caller's job (each formats its own line through _scrub), so a line that
    # reaches _say has already been through it.
    seen = []
    monkeypatch.setattr(w.sys, "stderr", _Tee(seen))
    assert w._say("already scrubbed\n") is True
    assert seen == ["already scrubbed\n"]
