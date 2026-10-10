"""Tests for installer/og_herdr_client.py — the herdr socket API client.

Safety: nothing here may touch the live herdr session. Every test that speaks
the protocol drives a threaded fake AF_UNIX server whose socket lives in its own
short-lived temp directory (see SOCKET_BASE for why not tmp_path), and an
autouse fixture scrubs $HERDR_SOCKET_PATH / $HERDR_SESSION so even an
accidental un-injected resolve cannot reach ~/.config/herdr/herdr.sock. Every
client is still constructed with an explicit socket path. No `herdr` CLI is
invoked either; the schema fixture was captured out of band (see
tests/fixtures/README.md) and is committed.
"""
from __future__ import annotations

import inspect
import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

import og_herdr_client as herdr


# --------------------------------------------------------------------------
# fake herdr server
# --------------------------------------------------------------------------

# Where the fake's socket directory is created. NOT tmp_path.
#
# sun_path in an AF_UNIX sockaddr is capped at 104 bytes on macOS and 108 on
# Linux, and pytest's tmp_path is long before the socket name starts: on macOS
# TMPDIR is already /private/var/folders/<..>/T/ and pytest appends
# pytest-of-<user>/pytest-N/<test-name><n>/. Parametrised names like
# test_report_agent_rejects_invalid_state_before_socket[IDLE] pushed the bind
# past the cap and every test errored with "AF_UNIX path too long" before its
# body ran — on the author's short-TMPDIR machine and on CI alike. A fresh
# mkdtemp under /tmp with a 6-char name keeps the path at ~25 bytes whatever
# the test is called.
SOCKET_BASE = "/tmp"
SOCKET_NAME = "h.sock"


class FakeHerdr:
    """Threaded AF_UNIX server speaking herdr's newline-delimited JSON.

    `handler(request, send) -> bool` decides each request's fate: return True to
    close the connection after it, False to keep it open for a further request.
    `send(frame)` writes one NDJSON frame and flushes immediately, so a handler
    can push event frames at its own pace; it also accepts raw bytes for the
    malformed-payload tests. Every decoded request is recorded in `requests`
    for framing/id assertions.

    Each instance owns a private socket directory, removed by `close()`.
    `connections` counts accepted connections, so a test can assert that
    requests went out on separate sockets rather than one reused one.
    """

    def __init__(self, handler):
        self.directory = tempfile.mkdtemp(prefix="hfd-", dir=SOCKET_BASE)
        self.path = str(Path(self.directory) / SOCKET_NAME)
        self.requests = []
        self.connections = 0
        self._handler = handler
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            # Counted here, on the single accept thread, so no locking needed.
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        def send(frame):
            if isinstance(frame, (bytes, bytearray)):
                conn.sendall(bytes(frame))
            else:
                conn.sendall((json.dumps(frame) + "\n").encode("utf-8"))

        buf = b""
        try:
            with conn:
                while not self._stop.is_set():
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        request = json.loads(line.decode("utf-8"))
                        self.requests.append(request)
                        if self._handler(request, send):
                            return
        except (OSError, ValueError):
            return

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        # The socket file outlives the socket itself, so the directory has to go
        # too or /tmp accumulates one hfd-* per fake.
        shutil.rmtree(self.directory, ignore_errors=True)


# Canned results per wire method, matching the payload shape each wrapper
# unwraps. `pane.send_text` and `pane.read` are nested exactly as
# tests/fixtures/herdr_api_schema.json (success_response) nests them — the two
# shapes this table used to get wrong are what let defect #20 (pane_read) and
# defect #18 (pane.run, a method herdr has never had) through 223 green tests.
RESULTS = {
    "ping": {"type": "pong"},
    "workspace.list": {"workspaces": [{"name": "dev", "active_tab": "tab_9"}]},
    # The real workspace.create reply, measured against a live server. All THREE
    # of workspace, tab and root_pane arrive in one call — the schema's own
    # listing of this result is lossy and describes only `type` and `workspace`,
    # which is why the wrapper returns the whole object rather than unwrapping.
    "workspace.create": {"type": "workspace_created",
                         "workspace": {"workspace_id": "w7",
                                       "name": "projects", "label": "New Alignment",
                                       "insert_index": 2, "active_tab": "w7:tA",
                                       "tab_count": 1, "group_id": None},
                         "tab": {"tab_id": "w7:tA", "workspace_id": "w7",
                                 "number": 1, "label": "New Alignment",
                                 "focused": False, "pane_count": 1,
                                 "agent_status": "unknown"},
                         "root_pane": {"pane_id": "w7:pA",
                                       "terminal_id": "term_9c1f",
                                       "workspace_id": "w7", "tab_id": "w7:tA",
                                       "cwd": "/Users/cryogenix/projects/og",
                                       "agent_status": "unknown", "revision": 0}},
    "workspace.close": {"closed": True},
    # The real tab.create reply, measured against a live herdr 0.9.1 server.
    # Note there is no generic `id` key: the tab is `tab.tab_id` and the root
    # pane is `root_pane.pane_id`, and root_pane carries its `tab_id` too.
    "tab.create": {"type": "tab_created",
                   "tab": {"tab_id": "w1:tG", "workspace_id": "w1", "number": 16,
                           "label": "og-null-ws", "focused": False,
                           "pane_count": 1, "agent_status": "unknown"},
                   "root_pane": {"pane_id": "w1:pQ",
                                 "terminal_id": "term_65d6ae6b5871712",
                                 "workspace_id": "w1", "tab_id": "w1:tG",
                                 "cwd": "/private/tmp", "agent_status": "unknown",
                                 "revision": 0}},
    "tab.close": {"closed": True},
    # pane.run is absent because it is not a request variant: `herdr pane run` is
    # a CLI subcommand. pane.send_text is what a pane actually runs a command
    # with, and returns the void `ok` result.
    "pane.send_text": {"type": "ok"},
    "pane.close": {"closed": True},
    "pane.rename": {"label": "coder"},
    # The measured pane.read reply: the text is inside `read`, one level deeper
    # than anything the old wrapper looked at. Result keys were exactly
    # ['type', 'read'] against the live server.
    "pane.read": {"type": "pane_read",
                  "read": {"format": "text", "pane_id": "w1:pQ", "revision": 3,
                           "source": "recent", "tab_id": "w1:tG",
                           "text": "line one\nline two", "truncated": False,
                           "workspace_id": "w1"}},
    "pane.report_agent": {"ok": True},
    "pane.release_agent": {"ok": True},
    "pane.report_metadata": {"ok": True},
    "agent.list": {"agents": [{"name": "coder", "state": "working"}]},
    "agent.get": {"name": "coder", "state": "working"},
}


def echo_handler(request, send):
    """Reply with the canned result, then hang up — which is what herdr does.

    Returns True (close after the reply) because a real herdr connection
    serves exactly ONE request: measured against a live 0.9.1 server,
    `workspace.list` three times on one connection gave a reply, then EOF, then
    BrokenPipeError, and `ping` followed by `agent.list` gave a pong then
    BrokenPipeError. Method did not matter.

    This handler used to keep the connection open on the belief that herdr
    multiplexes; that belief was wrong, and it hid a client bug — see
    test_four_calls_in_a_row_each_get_their_own_connection.

    `events.subscribe` is the one method that keeps its connection, because
    there the stream IS the connection; it gets an ack plus one event frame.

    An unrecognised method is REFUSED, in herdr's own words, rather than
    answered with an empty object. `RESULTS.get(method, {})` is how `pane.run`
    — a CLI subcommand, never a request variant — passed every test in this
    file: the fake agreed with whatever it was asked, so it could never
    disagree with a broken client. A fake that answers only what herdr would
    answer is the precondition for the conformance test at the bottom of this
    file meaning anything.
    """
    method = request["method"]
    if method == "events.subscribe":
        send({"id": request["id"], "result": {"type": "subscription_started"}})
        send({"id": "evt_1", "event": "pane.exit", "params": {}})
        return False  # the stream stays open; the caller owns closing it
    if method not in RESULTS:
        send({"id": request["id"], "error": {
            "code": "invalid_request",
            "message": "invalid request: unknown variant `{0}`".format(method)}})
        return True
    send({"id": request["id"], "result": RESULTS[method]})
    return True


@pytest.fixture(autouse=True)
def scrub_herdr_env(monkeypatch):
    """Guarantee no test can resolve the operator's real socket."""
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)


def test_fake_socket_path_fits_the_af_unix_sun_path_cap(fake):
    """The reason the fake ignores tmp_path, asserted so it stays true.

    104 is macOS's cap on sun_path (108 on Linux); a path that crosses it fails
    the bind and every assertion in the test with it.
    """
    server = fake()
    assert len(server.path.encode("utf-8")) <= 104
    assert server.path.startswith(SOCKET_BASE)


@pytest.fixture
def fake():
    """Factory for fake servers; every one is closed (and unlinked) at teardown."""
    servers = []

    def make(handler=echo_handler):
        server = FakeHerdr(handler)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


@pytest.fixture
def client(fake):
    """A client wired to a fresh fake server inside tmp_path."""
    server = fake()
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    yield client
    client.close()


# --------------------------------------------------------------------------
# request framing / id sequencing
# --------------------------------------------------------------------------

def test_call_frames_request_as_ndjson_with_id_and_method(fake):
    server = fake()
    framed = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    assert framed.ping() == {"type": "pong"}
    assert server.requests == [{"id": "req_1", "method": "ping", "params": {}}]
    framed.close()


def test_call_sends_an_empty_params_object_when_none_given(fake):
    # `params` is always present, empty object included: herdr reads it
    # unconditionally, so omitting the key would change the frame shape.
    server = fake()
    empty = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    empty.call("tab.close")
    assert server.requests[0] == {"id": "req_1", "method": "tab.close", "params": {}}
    empty.close()


def test_request_ids_sequence_across_calls_on_one_client(fake):
    server = fake()
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    client.ping()
    client.pane_close("pane_1")
    client.agent_list()
    assert [r["id"] for r in server.requests] == ["req_1", "req_2", "req_3"]
    client.close()


def test_request_ids_do_not_restart_after_reconnect(fake):
    server = fake()
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    client.ping()
    client.close()
    client.ping()
    # Ids stay unique for the lifetime of the client, not the connection, so a
    # stale frame from a prior connection can never be mistaken for a reply.
    assert [r["id"] for r in server.requests] == ["req_1", "req_2"]
    client.close()


# --------------------------------------------------------------------------
# one connection per request
# --------------------------------------------------------------------------

def test_four_calls_in_a_row_each_get_their_own_connection(fake):
    """Regression: herdr serves ONE request per connection, then hangs up.

    Measured against a live 0.9.1 server: workspace.list three times on one
    connection gave a reply, then EOF, then BrokenPipeError; ping followed by
    agent.list gave a pong then BrokenPipeError. With the old reuse design
    every second call therefore failed, which for the bridge's four-call
    session (tab_create, pane_run, report_agent, report_metadata) meant
    alternating failure on every event. echo_handler now closes after each
    reply so this is the shape a real server has.
    """
    server = fake()  # echo_handler: reply, then hang up
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    client.tab_create("w1", "/private/tmp", "og-null-ws")
    client.pane_run("w1:pQ", "pytest -q")
    client.report_agent("w1:pQ", "og", "coder", "working")
    client.report_metadata("w1:pQ", "og", title="coder")
    # Four requests, four separate sockets, every one of them answered.
    assert [r["method"] for r in server.requests] == [
        "tab.create", "pane.send_text", "pane.report_agent", "pane.report_metadata"]
    assert server.connections == 4
    client.close()


def test_a_call_does_not_borrow_or_close_a_live_subscription(fake):
    """A subscription owns its connection; an ordinary call must not take it."""
    def subscribe_handler(request, send):
        if request["method"] != "events.subscribe":
            send({"id": request["id"], "result": RESULTS["ping"]})
            return True  # one request per connection, like herdr
        send({"id": request["id"], "result": {"type": "subscription_started"}})
        send({"id": "evt_1", "event": "pane.exit", "params": {}})
        send({"id": "evt_2", "event": "pane.exit", "params": {}})
        return False  # the stream stays open

    server = fake(subscribe_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stream = client.events_subscribe()
    assert next(stream)["id"] == "evt_1"
    # An ordinary call in the middle of the stream gets its own socket...
    assert client.ping() == {"type": "pong"}
    # ...and the stream is still readable afterwards.
    assert next(stream)["id"] == "evt_2"
    stream.close()
    client.close()


# --------------------------------------------------------------------------
# result unwrapping / error frames
# --------------------------------------------------------------------------

def test_call_returns_the_result_object_not_the_envelope(fake):
    server = fake()
    unwrap = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    result = unwrap.call("agent.get", {"target": "coder"})
    assert result == {"name": "coder", "state": "working"}
    assert "result" not in result and "id" not in result
    unwrap.close()


def test_call_returns_empty_dict_when_result_is_null(fake):
    def null_handler(request, send):
        send({"id": request["id"], "result": None})
        return True

    server = fake(null_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    assert client.call("tab.close", {"tab_id": "tab_9"}) == {}
    client.close()


def test_error_frame_raises_herdr_error_with_code_and_message(fake):
    def error_handler(request, send):
        send({"id": request["id"],
              "error": {"code": "not_found", "message": "pane not found"}})
        return True

    server = fake(error_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.pane_close("pane_does_not_exist")
    err = excinfo.value
    assert err.code == "not_found"
    assert err.message == "pane not found"
    assert isinstance(err, Exception)
    assert "not_found" in str(err)
    client.close()


def test_error_frame_without_code_or_message_still_raises(fake):
    def bare_error_handler(request, send):
        send({"id": request["id"], "error": {}})
        return True

    server = fake(bare_error_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.ping()
    assert excinfo.value.code == "unknown"
    client.close()


def test_error_frame_with_an_empty_id_raises_the_servers_message(fake):
    """An unparseable request is answered with "id": "" — that is our answer.

    Measured against a live herdr: a request it cannot parse comes back with
    an empty id, e.g. `params {}` -> "missing field `subscriptions`". Skipping
    such a frame on the id mismatch threw the server's message away, read EOF
    and reported `disconnected`, so a malformed request looked like a dropped
    connection.
    """
    def empty_id_error_handler(request, send):
        send({"id": "", "error": {"code": "invalid_request",
                                  "message": "invalid request: missing field "
                                             "`subscriptions` at line 1 column 51"}})
        return True

    server = fake(empty_id_error_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        # The request herdr actually rejected when probed live.
        client.call("events.subscribe", {})
    client.close()
    assert excinfo.value.code == "invalid_request"
    assert "missing field `subscriptions`" in excinfo.value.message
    assert excinfo.value.code != "disconnected"


def test_frame_with_our_id_but_neither_result_nor_error_raises(fake):
    def envelope_only_handler(request, send):
        send({"id": request["id"]})
        return True

    server = fake(envelope_only_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.ping()
    assert excinfo.value.code == "bad_response"
    client.close()


def test_non_json_reply_raises_bad_response(fake):
    def non_json_handler(request, send):
        send(b"this is not json\n")
        return True

    server = fake(non_json_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.ping()
    assert excinfo.value.code == "bad_response"
    client.close()


def test_non_object_reply_raises_bad_response(fake):
    def list_handler(request, send):
        send({"id": request["id"], "result": [1, 2, 3]})
        return True

    server = fake(list_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    # The result is passed through as-is; it is the wrapper, not call(), that
    # decides what the payload must look like.
    assert client.call("tab.create", {}) == [1, 2, 3]
    client.close()


def test_wrapper_raises_bad_response_when_expected_field_missing(fake):
    def shapeless_handler(request, send):
        send({"id": request["id"], "result": {"unexpected": []}})
        return True

    server = fake(shapeless_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.workspace_list()
    assert excinfo.value.code == "bad_response"
    assert "workspaces" in excinfo.value.message
    client.close()


# --------------------------------------------------------------------------
# interleaved event frames
# --------------------------------------------------------------------------

def test_reply_loop_skips_event_frame_with_a_different_id(fake):
    """The quirk: after events.subscribe the connection carries pushed frames,
    so the first frame read is not necessarily the answer."""
    def interleave_handler(request, send):
        send({"id": "evt_1", "event": "pane.exit",
              "params": {"pane_id": "pane_9"}})
        send({"id": "evt_2", "event": "agent.state",
              "params": {"agent": "coder", "state": "working"}})
        send({"id": request["id"], "result": {"type": "pong"}})
        return True

    server = fake(interleave_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    assert client.ping() == {"type": "pong"}
    client.close()


def test_reply_loop_skips_event_frames_sent_between_the_request_and_reply(fake):
    def late_event_handler(request, send):
        send({"id": request["id"], "result": {"type": "pong"}})
        send({"id": "evt_9", "event": "pane.exit", "params": {}})
        send({"id": request["id"], "result": {"type": "pong"}})
        return True

    server = fake(late_event_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    # The first matching-id frame is the answer; the trailing event is left on
    # the wire for whoever reads next.
    assert client.ping() == {"type": "pong"}
    client.close()


def test_reply_loop_skips_an_event_frame_carrying_the_awaited_id(fake):
    """Defensive: an event is never an answer, whatever its id says.

    Ids are client-generated and monotonic, so this was never observed — the
    server would have to echo a live one. Skipping on the `event` key costs one
    condition and removes the class instead of relying on that.
    """
    def shadowing_event_handler(request, send):
        send({"id": request["id"], "event": "pane.exit", "params": {"pane_id": "pane_1"}})
        send({"id": request["id"], "result": {"type": "pong"}})
        return True

    server = fake(shadowing_event_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    assert client.ping() == {"type": "pong"}
    client.close()


# --------------------------------------------------------------------------
# socket path resolution
# --------------------------------------------------------------------------

def test_resolve_socket_path_explicit_argument_wins(tmp_path):
    resolved = herdr.HerdrClient.resolve_socket_path(
        socket_path=str(tmp_path / "explicit.sock"),
        env={"HERDR_SOCKET_PATH": str(tmp_path / "env.sock"),
             "HERDR_SESSION": "nightly"},
        home=tmp_path,
    )
    assert resolved == str(tmp_path / "explicit.sock")


def test_resolve_socket_path_env_var(tmp_path):
    resolved = herdr.HerdrClient.resolve_socket_path(
        socket_path=None,
        env={"HERDR_SOCKET_PATH": str(tmp_path / "env.sock"), "HERDR_SESSION": "nightly"},
        home=tmp_path,
    )
    assert resolved == str(tmp_path / "env.sock")


def test_resolve_socket_path_session_maps_to_sessions_dir(tmp_path):
    resolved = herdr.HerdrClient.resolve_socket_path(
        socket_path=None,
        env={"HERDR_SESSION": "nightly"},
        home=tmp_path,
    )
    assert resolved == str(tmp_path / ".config" / "herdr" / "sessions" / "nightly" / "herdr.sock")


def test_resolve_socket_path_default(tmp_path):
    resolved = herdr.HerdrClient.resolve_socket_path(
        socket_path=None, env={}, home=tmp_path)
    assert resolved == str(tmp_path / ".config" / "herdr" / "herdr.sock")


def test_resolve_socket_path_empty_env_values_count_as_unset(tmp_path):
    # An empty-string env var is not a path; it must fall through, not resolve
    # to a socket named "".
    resolved = herdr.HerdrClient.resolve_socket_path(
        socket_path=None,
        env={"HERDR_SOCKET_PATH": "", "HERDR_SESSION": ""},
        home=tmp_path,
    )
    assert resolved == str(tmp_path / ".config" / "herdr" / "herdr.sock")


def test_client_resolves_socket_path_from_env_via_constructor(tmp_path):
    # The constructor honours the same precedence; the client is never
    # connected here, so nothing touches a real socket.
    path = herdr.HerdrClient.resolve_socket_path(
        socket_path=None, env={"HERDR_SESSION": "nightly"}, home=tmp_path)
    client = herdr.HerdrClient(socket_path=path)
    assert client.socket_path == path
    client.close()


def test_resolve_socket_path_matches_priority_order(tmp_path):
    home = tmp_path / "home"
    env = {
        "HERDR_SOCKET_PATH": str(tmp_path / "p2.sock"),
        "HERDR_SESSION": "p3",
    }
    arg = str(tmp_path / "p1.sock")
    assert herdr.HerdrClient.resolve_socket_path(arg, env, home) == arg
    assert herdr.HerdrClient.resolve_socket_path(None, env, home) == env["HERDR_SOCKET_PATH"]
    assert herdr.HerdrClient.resolve_socket_path(
        None, {"HERDR_SESSION": "p3"}, home).endswith(
            str(Path(".config") / "herdr" / "sessions" / "p3" / "herdr.sock"))
    assert herdr.HerdrClient.resolve_socket_path(
        None, {}, home).endswith(str(Path(".config") / "herdr" / "herdr.sock"))


# --------------------------------------------------------------------------
# report_agent state validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["idle", "working", "blocked", "unknown"])
def test_report_agent_accepts_valid_states(fake, state):
    server = fake()
    stateful = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stateful.report_agent("pane_1", "og", "coder", state)
    assert server.requests[0]["params"]["state"] == state
    stateful.close()


@pytest.mark.parametrize("state", ["busy", "", "IDLE", "done", None])
def test_report_agent_rejects_invalid_state_before_socket(fake, state):
    server = fake()
    stateful = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(ValueError):
        stateful.report_agent("pane_1", "og", "coder", state)
    # Rejected client-side: nothing was written to the socket.
    assert server.requests == []
    stateful.close()


def test_report_agent_drops_unset_optional_params(fake):
    server = fake()
    stateful = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stateful.report_agent("pane_1", "og", "coder", "working")
    params = server.requests[0]["params"]
    assert params == {"pane_id": "pane_1", "source": "og", "agent": "coder", "state": "working"}
    stateful.close()


def test_report_agent_passes_optional_params_when_given(fake):
    server = fake()
    stateful = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stateful.report_agent("pane_1", "og", "coder", "blocked",
                          message="waiting on input", seq=7, agent_session_id="sess_1")
    params = server.requests[0]["params"]
    assert params["message"] == "waiting on input"
    assert params["seq"] == 7
    assert params["agent_session_id"] == "sess_1"
    stateful.close()


# --------------------------------------------------------------------------
# happy path per convenience wrapper
# --------------------------------------------------------------------------

def test_ping(client):
    assert client.ping() == {"type": "pong"}


def test_workspace_list(client):
    assert client.workspace_list() == [{"name": "dev", "active_tab": "tab_9"}]


def test_workspace_create_returns_the_whole_three_part_reply(fake):
    """One call opens a space AND its first tab AND that tab's root pane.

    The schema's own description of `workspace_created` lists only `type` and
    `workspace`, which reads as if a caller would then need a `tab.create` and a
    way to find the pane. The live reply carries all three; a wrapper that
    unwrapped to `workspace` alone would have thrown away the two ids the bridge
    needs and forced a second call that opens a SECOND tab, not a second pane.
    """
    server = fake()
    created = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    result = created.workspace_create("New Alignment", "/repo/og")

    assert result["type"] == "workspace_created"
    assert result["workspace"]["workspace_id"] == "w7"
    assert result["tab"]["tab_id"] == "w7:tA"
    assert result["root_pane"]["pane_id"] == "w7:pA"
    assert server.requests[0] == {
        "id": "req_1", "method": "workspace.create",
        "params": {"label": "New Alignment", "cwd": "/repo/og", "focus": False}}
    created.close()


def test_workspace_create_does_not_steal_focus_by_default(fake):
    # Several sessions arrive in one poll. A space that focused itself each time
    # would rip the operator out of whatever they were typing, once per session.
    server = fake()
    created = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    created.workspace_create("New Alignment", "/repo/og")
    assert server.requests[0]["params"]["focus"] is False
    created.close()


def test_workspace_close_sends_only_the_workspace_id(fake):
    # `close_group` is herdr's "take the whole group with it" and is not sent:
    # these are the bridge's own workspaces, not a group the operator shares.
    server = fake()
    closed = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    assert closed.workspace_close("w7") is None
    assert server.requests[0] == {"id": "req_1", "method": "workspace.close",
                                  "params": {"workspace_id": "w7"}}
    closed.close()


def test_workspace_close_propagates_not_found(fake):
    # The bridge depends on the CODE, not just on the exception: a `not_found`
    # from a close means the outcome it wanted has already happened, and it is
    # recorded as success rather than as a failed removal.
    def refuse(request, send):
        send({"id": request["id"],
              "error": {"code": "not_found", "message": "workspace not found"}})
        return True

    server = fake(refuse)
    closed = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as caught:
        closed.workspace_close("w7")
    assert caught.value.code == "not_found"
    closed.close()


def test_tab_create(fake):
    server = fake()
    tab = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    result = tab.tab_create("dev", "/repo", "coder", focus=True)
    # Real key names: herdr has no generic `id`, and root_pane carries both its
    # own pane_id and the tab_id it belongs to.
    assert result["type"] == "tab_created"
    assert result["tab"]["tab_id"] == "w1:tG"
    assert result["root_pane"]["pane_id"] == "w1:pQ"
    assert result["root_pane"]["tab_id"] == "w1:tG"
    # `workspace_id`, not `workspace`: herdr's TabCreateParams drops the latter
    # as an unknown field, which silently sent every tab to the focused
    # workspace and made `--workspace` a no-op that still read as isolation.
    assert server.requests[0]["params"] == {
        "workspace_id": "dev", "cwd": "/repo", "label": "coder", "focus": True}
    tab.close()


def test_tab_close(client):
    assert client.tab_close("tab_9") is None


def test_pane_run(client):
    assert client.pane_run("pane_1", "pytest -q") is None


def test_pane_close(client):
    assert client.pane_close("pane_1") is None


def test_pane_rename(client):
    assert client.pane_rename("pane_1", "coder") is None


def test_pane_read(client):
    assert client.pane_read("pane_1") == "line one\nline two"


def test_report_agent(client):
    assert client.report_agent("pane_1", "og", "coder", "working") is None


def test_release_agent(client):
    assert client.release_agent("pane_1", "og", "coder") is None


def test_report_metadata(client):
    assert client.report_metadata("pane_1", "og", title="coder", ttl_ms=5000) is None


def test_agent_list(client):
    assert client.agent_list() == [{"name": "coder", "state": "working"}]


def test_agent_get(client):
    assert client.agent_get("coder") == {"name": "coder", "state": "working"}


# --------------------------------------------------------------------------
# the wire-contract defects that shipped
# --------------------------------------------------------------------------

def test_pane_run_sends_text_with_a_trailing_newline(fake):
    """`pane.run` is a CLI subcommand; the method that runs text is `pane.send_text`.

    The bridge's whole purpose is this call — it is what types
    `omnigent attach <id>` into the pane — so when it was sent as `pane.run`,
    every live session failed with `unknown variant 'pane.run'` and not one pane
    started anything. The fake never noticed, because it answered any method.
    The trailing newline is what submits the line.
    """
    server = fake()
    run = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    run.pane_run("w1:pQ", "omnigent attach s_abc123")
    assert server.requests[0] == {
        "id": "req_1",
        "method": "pane.send_text",
        "params": {"pane_id": "w1:pQ", "text": "omnigent attach s_abc123\n"},
    }
    run.close()


def test_pane_run_never_emits_the_cli_only_method(fake):
    """Named separately so the regression is one grep away, not one edit away."""
    server = fake()
    run = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    run.pane_run("w1:pQ", "omnigent attach s_abc123")
    assert "pane.run" not in {r["method"] for r in server.requests}
    run.close()


def test_tab_create_sends_workspace_id_and_never_workspace(fake):
    """`tab.create` takes `workspace_id`; `workspace` is dropped, not rejected.

    serde ignores unknown fields, so the wrong key did not fail — it was
    discarded and `workspace_id` defaulted to null, putting every tab in the
    focused workspace. `--workspace` looked like it isolated a run and did not.
    """
    server = fake()
    create = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    create.tab_create("w4", "/repo", "og:abcdef12")
    params = server.requests[0]["params"]
    assert params == {"workspace_id": "w4", "cwd": "/repo",
                      "label": "og:abcdef12", "focus": False}
    assert "workspace" not in params
    create.close()


def test_tab_create_sends_a_null_workspace_id_when_none_was_asked_for(fake):
    """`workspace_id` is string|null, so null is the wire's own "no preference".

    That is the focused workspace — the same thing omitting the key means — so
    the no-flag path is unchanged by the key rename.
    """
    server = fake()
    create = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    create.tab_create(None, "/repo", "og:abcdef12")
    assert server.requests[0]["params"]["workspace_id"] is None
    create.close()


def test_pane_read_returns_the_text_from_inside_the_read_object(fake):
    """The text is at `result["read"]["text"]` — measured live.

    The old wrapper looked for `text`, `output`, `content`, `data` and a `lines`
    list at the top level and found none of them, so it raised `bad_response` on
    100% of live reads. Five guessed spellings, all wrong.
    """
    def nested_read_handler(request, send):
        send({"id": request["id"], "result": RESULTS["pane.read"]})
        return True

    server = fake(nested_read_handler)
    read = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    assert read.pane_read("w1:pQ") == "line one\nline two"
    assert server.requests[0]["params"] == {
        "pane_id": "w1:pQ", "source": "recent", "lines": 40}
    read.close()


def test_pane_read_reports_a_missing_read_object_by_name(fake):
    """A flat `{"text": …}` result is no longer accepted.

    Tolerant unwrapping is what hid defect #20: it would have kept "working" if
    any of its five guesses had been right. A shape change now names itself.
    """
    def flat_read_handler(request, send):
        send({"id": request["id"], "result": {"type": "pane_read", "text": "hi"}})
        return True

    server = fake(flat_read_handler)
    read = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        read.pane_read("w1:pQ")
    assert excinfo.value.code == "bad_response"
    assert "'read'" in excinfo.value.message
    read.close()


def test_pane_read_reports_a_read_object_without_text_by_name(fake):
    def textless_handler(request, send):
        send({"id": request["id"],
              "result": {"type": "pane_read", "read": {"pane_id": "w1:pQ"}}})
        return True

    server = fake(textless_handler)
    read = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        read.pane_read("w1:pQ")
    assert excinfo.value.code == "bad_response"
    assert "'text'" in excinfo.value.message
    read.close()


@pytest.mark.parametrize("source", ["visible", "recent", "recent_unwrapped",
                                    "detection"])
def test_pane_read_accepts_every_source_in_the_schema_enum(fake, source):
    # ReadSource, spelled as the enum spells it. `recent_unwrapped` has an
    # UNDERSCORE; the CLI's `recent-unwrapped` is in the rejected list below,
    # because that is what serde refuses.
    server = fake()
    read = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    read.pane_read("w1:pQ", source=source)
    assert server.requests[0]["params"]["source"] == source
    read.close()


@pytest.mark.parametrize("source", ["recent-unwrapped", "Recent", "screen", "", None])
def test_pane_read_rejects_an_invalid_source_before_socket(fake, source):
    server = fake()
    read = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(ValueError):
        read.pane_read("w1:pQ", source=source)
    # Rejected client-side: nothing was written to the socket. On the wire this
    # would be a serde enum error attributed to the request, not to the caller
    # that misspelled the source.
    assert server.requests == []
    read.close()


def test_fake_refuses_a_method_herdr_does_not_have(fake):
    """The fake is strict, so a wrong method name fails here too.

    `RESULTS.get(method, {})` answered whatever it was asked, which is why
    `pane.run` — a method herdr has never had — was green across this whole file.
    """
    server = fake()
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.call("pane.run", {"pane_id": "w1:pQ", "command": "echo hi"})
    assert excinfo.value.code == "invalid_request"
    assert "unknown variant `pane.run`" in excinfo.value.message
    client.close()


# --------------------------------------------------------------------------
# transport failures
# --------------------------------------------------------------------------

def test_connect_to_missing_socket_raises_herdr_error(tmp_path):
    missing = str(tmp_path / "not-there.sock")
    client = herdr.HerdrClient(socket_path=missing, timeout=1.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.ping()
    assert excinfo.value.code == "connect"
    client.close()


def test_hangup_before_the_reply_is_reported_not_retried(fake):
    """A peer that dies mid-request fails loudly instead of being re-sent.

    The failure is not retried on a fresh connection: a request that may have
    been half-processed must not be replayed, since methods like tab.create are
    not idempotent. The *next* call is a clean new connection.
    """
    def hangup_handler(request, send):
        return True  # read the request, answer nothing, hang up

    server = fake(hangup_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.ping()
    assert excinfo.value.code == "disconnected"
    # The failed request was not replayed: one request, one connection.
    assert [r["id"] for r in server.requests] == ["req_1"]
    assert server.connections == 1
    # The next call opens its own connection rather than reusing the dead one.
    with pytest.raises(herdr.HerdrError):
        client.ping()
    assert server.connections == 2
    assert [r["id"] for r in server.requests] == ["req_1", "req_2"]
    client.close()


def test_timeout_raises_herdr_error(fake):
    def stall_handler(request, send):
        time.sleep(0.3)  # accept, read, but never reply within the client's timeout
        return True

    server = fake(stall_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=0.1)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.ping()
    assert excinfo.value.code == "timeout"
    client.close()


# --------------------------------------------------------------------------
# events.subscribe
# --------------------------------------------------------------------------

def test_events_subscribe_yields_pushed_frames(fake):
    def subscribe_handler(request, send):
        send({"id": request["id"], "result": {"subscribed": True}})
        send({"id": "evt_1", "event": "pane.exit", "params": {"pane_id": "pane_1"}})
        send({"id": "evt_2", "event": "agent.state", "params": {"state": "idle"}})
        return False  # keep the connection open

    server = fake(subscribe_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stream = client.events_subscribe()
    first = next(stream)
    second = next(stream)
    assert first == {"id": "evt_1", "event": "pane.exit", "params": {"pane_id": "pane_1"}}
    assert second["event"] == "agent.state"
    stream.close()
    client.close()


def test_events_subscribe_skips_ack_before_events(fake):
    def subscribe_handler(request, send):
        # Ack first, then push — the generator must swallow the ack and yield
        # only event frames.
        send({"id": request["id"], "result": {"subscribed": True}})
        send({"event": "pane.exit", "params": {"pane_id": "pane_1"}})
        return False

    server = fake(subscribe_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stream = client.events_subscribe()
    frame = next(stream)
    assert frame == {"event": "pane.exit", "params": {"pane_id": "pane_1"}}
    stream.close()
    client.close()


def test_events_subscribe_handles_event_before_ack(fake):
    """The quirk bites the ack too: an event can overtake the subscribe reply."""
    def subscribe_handler(request, send):
        send({"id": "evt_early", "event": "workspace.changed", "params": {}})
        send({"id": request["id"], "result": {"subscribed": True}})
        send({"id": "evt_after", "event": "pane.exit", "params": {}})
        return False

    server = fake(subscribe_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stream = client.events_subscribe()
    assert next(stream)["event"] == "workspace.changed"
    assert next(stream)["event"] == "pane.exit"
    stream.close()
    client.close()


def test_events_subscribe_sends_a_subscriptions_list(fake):
    """The wire key is `subscriptions`, and entries pass through verbatim.

    Measured against a live herdr:
      {"subscriptions": []}                                  -> starts
      {"subscriptions": ["pane.agent_status_changed"]}        -> "invalid
          type: string ..., expected internally tagged enum Subscription"
      {"subscriptions": [{"type": "pane.agent_status_changed"}]}
                                                            -> "missing
          field `pane_id`"
    Each entry is an internally-tagged object, a per-type entry also needs
    `pane_id`, and the empty list is the accepted catch-all.
    """
    def subscribe_handler(request, send):
        send({"id": request["id"], "result": {"type": "subscription_started"}})
        send({"id": "evt_1", "event": "agent.state", "params": {"state": "idle"}})
        return False

    server = fake(subscribe_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stream = client.events_subscribe(
        subscriptions=[{"type": "pane.agent_status_changed", "pane_id": "w1:pQ"}])
    assert next(stream)["event"] == "agent.state"
    stream.close()
    client.close()
    assert server.requests[0] == {
        "id": "req_1",
        "method": "events.subscribe",
        "params": {"subscriptions": [{"type": "pane.agent_status_changed",
                                      "pane_id": "w1:pQ"}]},
    }


def test_events_subscribe_defaults_to_an_empty_subscriptions_list(fake):
    """`[]` is what the server accepts, so it is the default — not omitted.

    Omitting `params` (what this client used to send) is rejected outright:
    "invalid request: missing field `subscriptions` at line 1 column 51".
    """
    def subscribe_handler(request, send):
        send({"id": request["id"], "result": {"type": "subscription_started"}})
        send({"id": "evt_1", "event": "pane.exit", "params": {}})
        return False

    server = fake(subscribe_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stream = client.events_subscribe()
    assert next(stream)["event"] == "pane.exit"
    stream.close()
    client.close()
    assert server.requests[0]["params"] == {"subscriptions": []}


def test_events_subscribe_sends_entries_verbatim_and_has_no_types_alias(fake):
    """`subscriptions` is the only spelling, and entries go out untouched.

    Measured against a live herdr, a list of type *names* is refused:
      {"subscriptions": ["pane.agent_status_changed"]} -> "invalid type:
      string ..., expected internally tagged enum Subscription"
    so there is no `types=` alias to map onto it — it could only build a request
    guaranteed to be rejected on the wire, at stream time rather than at the
    call site. The supported form passes its entries through unmodelled.
    """
    def subscribe_handler(request, send):
        send({"id": request["id"], "result": {"type": "subscription_started"}})
        send({"id": "evt_1", "event": "agent.state", "params": {"state": "idle"}})
        return False

    server = fake(subscribe_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    # Argument binding fails before the generator body runs, so nothing reaches
    # the socket — the point of dropping the alias instead of keeping it.
    with pytest.raises(TypeError):
        client.events_subscribe(types=["pane.exit", "agent.state"])
    assert server.requests == []

    entries = [{"type": "pane.agent_status_changed", "pane_id": "w1:pQ"},
               {"type": "pane.exited", "pane_id": "w1:pR"}]
    stream = client.events_subscribe(subscriptions=entries)
    assert next(stream)["event"] == "agent.state"
    stream.close()
    client.close()
    assert server.requests[0]["method"] == "events.subscribe"
    assert server.requests[0]["params"] == {"subscriptions": entries}


def test_events_subscribe_raises_on_error_ack(fake):
    def error_subscribe(request, send):
        send({"id": request["id"], "error": {"code": "unsupported", "message": "no"}})
        return True

    server = fake(error_subscribe)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stream = client.events_subscribe()
    with pytest.raises(herdr.HerdrError) as excinfo:
        next(stream)
    assert excinfo.value.code == "unsupported"
    client.close()


def test_events_subscribe_raises_on_an_error_ack_with_an_empty_id(fake):
    """The refusal herdr sends for the very `subscriptions` frame we build.

    It cannot echo an id from a request it failed to parse, so the ack arrives
    with "id": "" — dropping it on the id match would hang until EOF and report
    `disconnected` instead of naming the bad parameter.
    """
    def empty_id_error_subscribe(request, send):
        send({"id": "", "error": {
            "code": "invalid_request",
            "message": "invalid request: missing field `subscriptions` "
                       "at line 1 column 51"}})
        return True

    server = fake(empty_id_error_subscribe)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stream = client.events_subscribe()
    with pytest.raises(herdr.HerdrError) as excinfo:
        next(stream)
    client.close()
    assert excinfo.value.code == "invalid_request"
    assert "missing field `subscriptions`" in excinfo.value.message


# --------------------------------------------------------------------------
# conformance against herdr's own published schema
# --------------------------------------------------------------------------
#
# Four defects shipped with the client's idea of the wire contract never checked
# against herdr's: `pane.run` (no such variant), `tab.create`'s `workspace`
# (the key is `workspace_id`, and the wrong one is dropped silently), `pane.read`'s
# five guessed payload spellings (the text is at `result["read"]["text"]`) and
# `events.subscribe`'s `types` (the key is `subscriptions`). None were visible
# here, because the fake answers whatever it is asked.
#
# So the expectation is not a list of method names someone maintains. It is the
# frames the real client emits: every public wrapper on HerdrClient is invoked
# against a recording fake, the frame it wrote is captured, and that frame is
# validated against tests/fixtures/herdr_api_schema.json. A wrapper added later
# is covered the day it is written; a hand-written list would rot exactly like the
# comments did.

# herdr's schema, vendored. See tests/fixtures/README.md for how to regenerate
# it and how to tell whether it is stale (protocol 22, schema_version 1).
SCHEMA_PATH = Path(__file__).parent / "fixtures" / "herdr_api_schema.json"

# Wrappers that send nothing, so have no frame to validate:
#   close              tears down a socket; there is no request behind it.
#   call               the raw escape hatch every wrapper goes through; its
#                      method name comes from the caller, so there is no fixed
#                      expectation to build for it. The wrappers ARE the check.
#   resolve_socket_path  pure path arithmetic, opens nothing.
NON_REQUEST_MEMBERS = frozenset({"call", "close", "resolve_socket_path"})

# A representative, schema-valid value for each parameter a wrapper may require.
# Parameters that have defaults are called with those defaults, so this table
# only has to cover the required ones. A required parameter that is missing here
# makes the test FAIL (not skip): a silently skipped wrapper would be a case the
# guard does not cover, which is the failure mode this whole file exists to stop.
#
# `source` is free-form on pane.report_agent (the reporting application's name,
# "og" here) but an enum on pane.read — whose default of "recent" is used, since
# it has one. Both spellings are correct because they are different parameters.
SAMPLE_ARGUMENTS = {
    "pane_id": "w1:pQ",
    "tab_id": "w1:tG",
    "target": "coder",
    "workspace_id": "w1",
    "cwd": "/repo",
    "label": "og:abcdef12",
    "source": "og",
    "agent": "coder",
    "state": "working",
    "text": "echo MARKER\n",
    "command": "omnigent attach s_abc123",
}


@pytest.fixture(scope="module")
def api_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _request_params_by_method(schema):
    """`{method name: params schema}` from the request schema's oneOf variants."""
    request = schema["schemas"]["request"]
    defs = request["$defs"]
    prefix = "#/schemas/request/$defs/"
    out = {}
    for variant in request["oneOf"]:
        method = variant["properties"]["method"]["const"]
        assert method not in out, "duplicate request variant for {0}".format(method)
        ref = variant["properties"]["params"]["$ref"]
        # A params schema that is inlined rather than a $ref would mean herdr
        # changed the schema's shape, and this table would quietly stop covering
        # that method. Fail instead.
        assert ref.startswith(prefix), "unhandled params $ref: " + ref
        out[method] = defs[ref[len(prefix):]]
    return out


def _resolve(schema, node):
    """Follow a `$ref` into the request `$defs`; anything else is returned as-is."""
    prefix = "#/schemas/request/$defs/"
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith(prefix), "unhandled $ref: " + ref
        node = schema["schemas"]["request"]["$defs"][ref[len(prefix):]]
    return node


def _frame_violations(schema, frame):
    """Every way `frame` disagrees with herdr's request schema, as strings.

    Three checks, one per shipped defect class:
      1. the method is a real variant          — would have caught `pane.run`
      2. every param key sent exists there     — would have caught `workspace`
      3. every required param is actually sent — catches a dropped identifier
    Plus a fourth, cheap one: a param whose schema is an inline enum must carry
    a value from it. That is the `pane.read` source enum and `pane.report_agent`'s
    state, both of which serde rejects at the request boundary.
    """
    method = frame["method"]
    variants = _request_params_by_method(schema)
    if method not in variants:
        return ["`{0}` is not a request variant in herdr's schema ({1} exist) — "
                "is it a CLI subcommand?".format(method, len(variants))]
    params_schema = variants[method]
    properties = params_schema.get("properties", {})
    required = params_schema.get("required", [])
    sent = frame.get("params", {})
    problems = []

    for key in sorted(sent):
        if key not in properties:
            problems.append(
                "{0} sends `{1}`, which is not one of its params ({2}) — serde "
                "drops an unknown key silently".format(
                    method, key, ", ".join(sorted(properties)) or "none"))

    for key in required:
        if key not in sent:
            problems.append(
                "{0} requires `{1}`, which this client does not send".format(
                    method, key))

    for key, value in sorted(sent.items()):
        enum = _resolve(schema, properties.get(key, {})).get("enum")
        if enum is not None and value is not None and value not in enum:
            problems.append("{0} param `{1}`: `{2}` is not in {3}".format(
                method, key, value, enum))

    return problems


def _client_wrappers():
    """Every public HerdrClient method that sends a request, by name."""
    members = inspect.getmembers(herdr.HerdrClient, predicate=inspect.isfunction)
    return [(name, fn) for name, fn in members
            if not name.startswith("_") and name not in NON_REQUEST_MEMBERS]


def _arguments_for(fn):
    """(args, kwargs) that exercise `fn` with a schema-valid value per parameter.

    `getmembers` hands back the plain functions off the class, so `self` is the
    first parameter and is supplied by the caller, not by this table.
    """
    args, kwargs = [], {}
    parameters = list(inspect.signature(fn).parameters.values())
    assert parameters and parameters[0].name == "self", (
        "{0}: expected an unbound method taking self".format(fn.__name__))
    for param in parameters[1:]:
        name = param.name
        if param.kind is param.KEYWORD_ONLY:
            if param.default is param.empty:
                raise AssertionError(
                    "{0}: keyword-only parameter {1!r} has no default; add it to "
                    "SAMPLE_ARGUMENTS".format(fn.__name__, name))
            kwargs[name] = param.default
            continue
        if param.default is not param.empty:
            args.append(param.default)
        elif name in SAMPLE_ARGUMENTS:
            args.append(SAMPLE_ARGUMENTS[name])
        else:
            # Fail rather than skip: an unexercised wrapper is an unchecked one.
            raise AssertionError(
                "{0}: required parameter {1!r} has no sample in "
                "SAMPLE_ARGUMENTS; add one so its frame gets validated".format(
                    fn.__name__, name))
    return args, kwargs


def _captured_frames(fake):
    """Invoke every wrapper; return `{name: (frame, refusal or None)}`.

    A refusal is recorded rather than raised. echo_handler answers a method
    herdr has never heard of with herdr's own `invalid_request`, so a wrapper
    aimed at a non-existent variant would otherwise abort the whole capture
    with a traceback and hide every OTHER frame from the report. The frame was
    still written and still worth validating, which is how `pane.run` gets
    named as a schema violation instead of as an exception from a fake.
    """
    frames = {}
    for name, fn in _client_wrappers():
        server = fake()
        client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
        args, kwargs = _arguments_for(fn)
        refusal = None
        try:
            outcome = fn(client, *args, **kwargs)
            if inspect.isgenerator(outcome):
                # events.subscribe is a generator: it sends on first pull, and
                # echo_handler pushes one event so that pull returns.
                next(outcome)
                outcome.close()
        except herdr.HerdrError as exc:
            refusal = "{0}: {1}".format(exc.code, exc.message)
        finally:
            client.close()
        assert len(server.requests) == 1, (
            "{0} wrote {1} requests, expected exactly 1".format(
                name, len(server.requests)))
        frames[name] = (server.requests[0], refusal)
    return frames


def test_the_conformance_guard_covers_every_requesting_wrapper(fake):
    """The inventory itself, so the guard cannot silently shrink to nothing."""
    names = {name for name, _ in _client_wrappers()}
    assert names == {
        "ping", "workspace_list", "workspace_create", "workspace_close",
        "tab_create", "tab_close", "pane_run",
        "pane_close", "pane_rename", "pane_read", "report_agent",
        "release_agent", "report_metadata", "agent_list", "agent_get",
        "events_subscribe",
    }


def test_every_frame_the_client_can_send_matches_herdrs_schema(fake, api_schema):
    """The systemic guard: the client's wire contract, checked against herdr's.

    Every public wrapper is invoked against a recording fake and the frame it
    emits is validated against the vendored schema. Each of the four shipped
    defects fails this test:
        pane.run            not a request variant
        {"workspace": …}    not a param of tab.create
        {"types": …}         not a param of events.subscribe
        pane.read params    validated against PaneReadParams + ReadSource
    """
    frames = _captured_frames(fake)
    offenders = {}
    for name, (frame, refusal) in sorted(frames.items()):
        problems = _frame_violations(api_schema, frame)
        if refusal is not None:
            # The strict fake already refuses what herdr would refuse; saying so
            # here too keeps the report readable when both agree.
            problems.append("the fake refused this frame too — {0}".format(refusal))
        if problems:
            offenders[name] = "\n".join("      - " + p for p in problems)
    assert not offenders, "client frames that disagree with herdr's schema:\n" + "\n".join(
        "    {0}:\n{1}".format(name, detail) for name, detail in offenders.items())


def test_the_conformance_guard_covers_the_workspace_wrappers(fake, api_schema):
    """Named rather than implied: the space-per-root model lives on these two.

    `workspace.create` is the call that opens a space together with its first
    tab, and `workspace.close` is what `--cleanup` and a root's removal use. Both
    are validated by the systemic guard above because it enumerates wrappers by
    introspection — so this test exists to make that a checked claim rather than
    an assumption: if either wrapper stopped being reachable, or started sending
    a key herdr does not have, the guard's own report would name it, but only
    someone reading the report would ever know to look.
    """
    frames = _captured_frames(fake)
    for name, method in (("workspace_create", "workspace.create"),
                         ("workspace_close", "workspace.close")):
        frame, refusal = frames[name]
        assert frame["method"] == method
        assert refusal is None, "{0}: the fake refused it too — {1}".format(
            name, refusal)
        assert _frame_violations(api_schema, frame) == []


def test_the_conformance_guard_accepts_the_schema_itself(fake, api_schema):
    """Sanity on the checker: herdr's own method/param pairs must pass.

    Without this, a checker that rejected everything would make the test above
    green for the wrong reason — the failure mode that let three of this
    session's earlier tests pass while the client was broken.
    """
    frames = {
        "ping": {"id": "r", "method": "ping", "params": {}},
        "pane.send_text": {"id": "r", "method": "pane.send_text",
                           "params": {"pane_id": "w1:pQ", "text": "hi\n"}},
        "tab.create": {"id": "r", "method": "tab.create",
                       "params": {"workspace_id": "w1", "cwd": "/repo",
                                  "label": "l", "focus": False}},
        "events.subscribe": {"id": "r", "method": "events.subscribe",
                             "params": {"subscriptions": []}},
    }
    for name, frame in frames.items():
        assert _frame_violations(api_schema, frame) == [], name


def test_the_conformance_guard_catches_a_method_herdr_does_not_have(api_schema):
    """Proof the guard bites, in the guard's own units: `pane.run` again."""
    problems = _frame_violations(api_schema, {
        "id": "req_1", "method": "pane.run",
        "params": {"pane_id": "w1:pQ", "command": "omnigent attach s1"}})
    assert any("`pane.run` is not a request variant" in p for p in problems)
    assert any("CLI subcommand" in p for p in problems)


def test_the_conformance_guard_catches_a_param_herdr_does_not_have(api_schema):
    """Proof it bites, for the dropped-key class: `workspace` on tab.create."""
    problems = _frame_violations(api_schema, {
        "id": "req_1", "method": "tab.create",
        "params": {"workspace": "w4", "cwd": "/repo", "label": "l", "focus": False}})
    assert any("sends `workspace`" in p and "drops an unknown key silently" in p
               for p in problems)


def test_the_conformance_guard_catches_the_earlier_types_defect(api_schema):
    """The fourth of the four: events.subscribe took `types`, not `subscriptions`."""
    problems = _frame_violations(api_schema, {
        "id": "req_1", "method": "events.subscribe", "params": {"types": []}})
    assert any("sends `types`" in p for p in problems)
    assert any("requires `subscriptions`" in p for p in problems)


def test_the_conformance_guard_catches_a_dropped_required_param(api_schema):
    problems = _frame_violations(api_schema, {
        "id": "req_1", "method": "pane.close", "params": {"id": "w1:pQ"}})
    assert any("sends `id`, which is not one of its params" in p for p in problems)
    assert any("requires `pane_id`" in p for p in problems)


def test_the_conformance_guard_catches_a_bad_enum_value(api_schema):
    # The CLI's hyphenated spelling: valid-looking, refused by the wire.
    problems = _frame_violations(api_schema, {
        "id": "req_1", "method": "pane.read",
        "params": {"pane_id": "w1:pQ", "source": "recent-unwrapped", "lines": 40}})
    assert any("`recent-unwrapped` is not in" in p for p in problems)


def test_the_conformance_guard_reads_a_fixture_with_all_herdrs_methods(api_schema):
    """The fixture is the contract, not a subset: 102 variants, protocol 22.

    A truncated or wrong-file fixture would still 'validate' most frames while
    silently covering far less, so the two headline numbers are pinned here.
    """
    assert api_schema["protocol"] == 22
    assert api_schema["schema_version"] == 1
    assert len(_request_params_by_method(api_schema)) == 102
    assert "pane.send_text" in _request_params_by_method(api_schema)
    assert "pane.run" not in _request_params_by_method(api_schema)
