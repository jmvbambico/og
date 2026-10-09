"""Tests for installer/og_herdr_client.py — the herdr socket API client.

Safety: nothing here may touch the live herdr session. Every test that speaks
the protocol drives a threaded fake AF_UNIX server whose socket lives in its own
short-lived temp directory (see SOCKET_BASE for why not tmp_path), and an
autouse fixture scrubs $HERDR_SOCKET_PATH / $HERDR_SESSION so even an
accidental un-injected resolve cannot reach ~/.config/herdr/herdr.sock. Every
client is still constructed with an explicit socket path. No `herdr` CLI is
invoked either.
"""
from __future__ import annotations

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
    """

    def __init__(self, handler):
        self.directory = tempfile.mkdtemp(prefix="hfd-", dir=SOCKET_BASE)
        self.path = str(Path(self.directory) / SOCKET_NAME)
        self.requests = []
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
# unwraps.
RESULTS = {
    "ping": {"type": "pong"},
    "workspace.list": {"workspaces": [{"name": "dev", "active_tab": "tab_9"}]},
    "tab.create": {"tab": {"id": "tab_9", "label": "coder"},
                   "root_pane": {"id": "pane_1"}},
    "tab.close": {"closed": True},
    "pane.run": {"started": True},
    "pane.close": {"closed": True},
    "pane.rename": {"label": "coder"},
    "pane.read": {"text": "line one\nline two"},
    "pane.report_agent": {"ok": True},
    "pane.release_agent": {"ok": True},
    "pane.report_metadata": {"ok": True},
    "agent.list": {"agents": [{"name": "coder", "state": "working"}]},
    "agent.get": {"name": "coder", "state": "working"},
}


def echo_handler(request, send):
    """Reply with the canned result for the request's method.

    Returns False (keep the connection open) because that is herdr: one
    connection serves many requests, it does not hang up after each one.
    """
    send({"id": request["id"], "result": RESULTS.get(request["method"], {})})
    return False


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

def test_call_frames_request_as_ndjson_with_id_and_method(client, fake):
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


def test_request_ids_sequence_across_calls_on_one_connection(fake):
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
# result unwrapping / error frames
# --------------------------------------------------------------------------

def test_call_returns_the_result_object_not_the_envelope(client, fake):
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
def test_report_agent_accepts_valid_states(client, fake, state):
    server = fake()
    stateful = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stateful.report_agent("pane_1", "og", "coder", state)
    assert server.requests[0]["params"]["state"] == state
    stateful.close()


@pytest.mark.parametrize("state", ["busy", "", "IDLE", "done", None])
def test_report_agent_rejects_invalid_state_before_socket(client, fake, state):
    server = fake()
    stateful = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    with pytest.raises(ValueError):
        stateful.report_agent("pane_1", "og", "coder", state)
    # Rejected client-side: nothing was written to the socket.
    assert server.requests == []
    stateful.close()


def test_report_agent_drops_unset_optional_params(client, fake):
    server = fake()
    stateful = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stateful.report_agent("pane_1", "og", "coder", "working")
    params = server.requests[0]["params"]
    assert params == {"pane_id": "pane_1", "source": "og", "agent": "coder", "state": "working"}
    stateful.close()


def test_report_agent_passes_optional_params_when_given(client, fake):
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


def test_tab_create(client, fake):
    server = fake()
    tab = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    result = tab.tab_create("dev", "/repo", "coder", focus=True)
    assert result["tab"]["id"] == "tab_9"
    assert result["root_pane"]["id"] == "pane_1"
    assert server.requests[0]["params"] == {
        "workspace": "dev", "cwd": "/repo", "label": "coder", "focus": True}
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
# transport failures
# --------------------------------------------------------------------------

def test_connect_to_missing_socket_raises_herdr_error(tmp_path):
    missing = str(tmp_path / "not-there.sock")
    client = herdr.HerdrClient(socket_path=missing, timeout=1.0)
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.ping()
    assert excinfo.value.code == "connect"
    client.close()


def test_peer_close_is_reported_not_retried(fake):
    """A connection the server hung up on fails loudly instead of re-sending.

    Re-sending would risk running a non-idempotent method twice, so the client
    surfaces `disconnected` and only reconnects on the *next* call.
    """
    def hangup_handler(request, send):
        send({"id": request["id"], "result": {"type": "pong"}})
        return True  # close after every reply

    server = fake(hangup_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    assert client.ping() == {"type": "pong"}
    with pytest.raises(herdr.HerdrError) as excinfo:
        client.ping()
    assert excinfo.value.code == "disconnected"
    # The dead socket was dropped, so the next call is a clean reconnect.
    assert client.ping() == {"type": "pong"}
    # req_2 never reached the server: the failed write was not replayed.
    assert [r["id"] for r in server.requests] == ["req_1", "req_3"]
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


def test_events_subscribe_forwards_types_filter(fake):
    def subscribe_handler(request, send):
        send({"id": request["id"], "result": {"subscribed": True}})
        send({"id": "evt_1", "event": "agent.state", "params": {"state": "idle"}})
        return False

    server = fake(subscribe_handler)
    client = herdr.HerdrClient(socket_path=server.path, timeout=5.0)
    stream = client.events_subscribe(types=["pane.exit", "agent.state"])
    assert next(stream)["event"] == "agent.state"
    stream.close()
    client.close()
    assert server.requests[0]["method"] == "events.subscribe"
    assert server.requests[0]["params"] == {"types": ["pane.exit", "agent.state"]}


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