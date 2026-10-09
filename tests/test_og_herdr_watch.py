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

    `bodies` entries may be bytes (one chunk per read) or a dict (JSON-encoded
    and readable in any chunk size). The last body repeats if the watcher reads
    more than once, which keeps a `watch()`-style loop from running dry.
    """

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return FakeResponse(body)


def session(sid="s1", status="running", **extra):
    base = {
        "session_id": sid,
        "status": status,
        "title": f"title-{sid}",
        "agent_name": "dev-lead",
        "parent_session_id": None,
        "pending_elicitation_count": 0,
        "workspace": "/tmp/ws",
    }
    base.update(extra)
    return base


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
    assert w.herdr_state({"status": "running", "pending_elicitation_count": 1}) == "blocked"


def test_state_elicitation_overrides_idle():
    assert w.herdr_state({"status": "idle", "pending_elicitation_count": 3}) == "blocked"


def test_state_elicitation_string_count_still_blocks():
    # A string count has shown up in client payloads; erring toward "blocked"
    # is the safe direction.
    assert w.herdr_state({"status": "running", "pending_elicitation_count": "2"}) == "blocked"


def test_state_zero_elicitation_does_not_block():
    assert w.herdr_state({"status": "running", "pending_elicitation_count": 0}) == "working"


def test_state_bool_elicitation_does_not_block():
    assert w.herdr_state({"status": "running", "pending_elicitation_count": True}) == "working"


@pytest.mark.parametrize("bad", [
    {},
    {"status": None},
    {"status": 7},
    {"pending_elicitation_count": "many"},
    {"status": "running", "pending_elicitation_count": None},
    None,
    [],
    "not a dict",
])
def test_state_malformed_never_raises(bad):
    assert w.herdr_state(bad) in {"idle", "working", "blocked", "unknown"}


def test_state_malformed_non_dict_is_unknown():
    assert w.herdr_state(None) == "unknown"
    assert w.herdr_state(["running"]) == "unknown"


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
    assert events[0].previous["session_id"] == "s2"
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
                                          pending_elicitation_count=1)])
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


def test_discover_token_reads_nested_entry(tmp_path):
    write_tokens(tmp_path, {"http://127.0.0.1:6767": {
        "token": "tok-nested", "user_id": "u1", "expires_at": "2030-01-01",
    }})
    assert w.SessionWatcher.discover_token(home=tmp_path) == "tok-nested"


def test_discover_token_reads_bare_string_entry(tmp_path):
    write_tokens(tmp_path, {"http://127.0.0.1:6767": "tok-bare"})
    assert w.SessionWatcher.discover_token(home=tmp_path) == "tok-bare"


def test_discover_token_prefers_loopback_entry(tmp_path):
    write_tokens(tmp_path, {
        "https://remote.example": {"token": "tok-remote"},
        "http://127.0.0.1:6767": {"token": "tok-local"},
    })
    assert w.SessionWatcher.discover_token(home=tmp_path) == "tok-local"


def test_discover_token_missing_file_returns_none(tmp_path):
    assert w.SessionWatcher.discover_token(home=tmp_path) is None


def test_discover_token_corrupt_file_returns_none(tmp_path):
    (tmp_path / "auth_tokens.json").write_text("{not json")
    assert w.SessionWatcher.discover_token(home=tmp_path) is None


def test_discover_token_empty_or_bogus_returns_none(tmp_path):
    write_tokens(tmp_path, {})
    assert w.SessionWatcher.discover_token(home=tmp_path) is None
    write_tokens(tmp_path, {"http://x": {"token": ""}})
    assert w.SessionWatcher.discover_token(home=tmp_path) is None
    write_tokens(tmp_path, ["nope"])
    assert w.SessionWatcher.discover_token(home=tmp_path) is None


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
