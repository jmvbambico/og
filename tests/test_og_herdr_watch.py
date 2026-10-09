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
