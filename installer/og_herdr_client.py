#!/usr/bin/env python3
"""og herdr — a stdlib-only client for the herdr control socket.

Transport (verified against herdr 0.9.3)
----------------------------------------
herdr speaks **newline-delimited JSON over a Unix domain socket**: one JSON
object per line, in both directions, UTF-8, no framing header.

    request  {"id":"req_1","method":"ping","params":{}}
    success  {"id":"req_1","result":{"type":"pong"}}
    error    {"id":"req_1","error":{"code":"not_found","message":"pane not found"}}

A reply always echoes the request's ``id``. ``result`` is the payload (empty
object for void methods); ``error`` replaces it entirely on failure and carries
a machine-readable ``code`` plus a human ``message``. Both are surfaced as
:class:`HerdrError`.

Socket path resolution (highest priority first)
-----------------------------------------------
1. an explicit argument to :meth:`HerdrClient` / :meth:`HerdrClient.resolve_socket_path`
2. ``$HERDR_SOCKET_PATH``
3. ``$HERDR_SESSION=<name>`` -> ``~/.config/herdr/sessions/<name>/herdr.sock``
4. the default ``~/.config/herdr/herdr.sock``

A quirk that shapes this file
----------------------------
``events.subscribe`` turns the connection into a **push channel**: from then on
the server interleaves unsolicited event frames with anything else it sends, and
those event frames may arrive *before* the subscribe ack. A reply read loop
therefore cannot assume "the next frame I read is my answer" — it must skip
every frame whose ``id`` is not the request it is waiting for. See
:meth:`HerdrClient._await_reply` and :meth:`HerdrClient.events_subscribe`.

Everything here is stdlib (json + socket) and every socket error — refused,
timed out, closed mid-reply, malformed frame — is funnelled into
:class:`HerdrError`, so callers have exactly one exception type to catch.
"""
from __future__ import annotations

import itertools
import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# The default socket, relative to $HOME (resolution step 4).
DEFAULT_SOCKET_RELPATH = Path(".config") / "herdr" / "herdr.sock"

# $HERDR_SESSION=<name> addresses one named session under sessions/<name>/.
SESSION_RELDIR = Path(".config") / "herdr" / "sessions"
SESSION_SOCKET_NAME = "herdr.sock"

# The states `pane.report_agent` accepts. herdr renders anything else as an
# unknown-state dot, so a typo here would fail *visually* and silently rather
# than loudly — reject it client-side instead.
AGENT_STATES = ("idle", "working", "blocked", "unknown")


class HerdrError(Exception):
    """An error reply from herdr, or a transport failure talking to it.

    `code` is herdr's machine-readable string (`"not_found"`, ...) for a real
    error frame, or one of the synthetic codes this client raises when it cannot
    complete a request at all:

    ``connect``        the socket could not be reached (missing, refused)
    ``timeout``        no reply arrived within the client's timeout
    ``disconnected``   the peer closed the connection mid-conversation
    ``bad_response``   the reply was not JSON, not an object, or lacked both
                       ``result`` and ``error``, or lacked a field a wrapper
                       was told to unwrap
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__("{0}: {1}".format(code, message) if message else code)


def _drop_none(params: Dict[str, Any]) -> Dict[str, Any]:
    """Strip keys whose value is None.

    herdr treats an explicit JSON null as "set it to null"; for optional
    params (a report message, a metadata ttl) "not supplied" has to mean the
    key is absent, or the field gets cleared server-side instead of kept.
    """
    return {k: v for k, v in params.items() if v is not None}


class HerdrClient:
    """Synchronous client for the herdr control socket.

    One connection is opened lazily on the first request and reused for
    subsequent ones; :meth:`close` releases it, and a later call reconnects.
    Use it as a context manager to tie the socket to a block.
    """

    def __init__(self, socket_path: Optional[str] = None,
                 timeout: Optional[float] = 10.0) -> None:
        self.socket_path = self.resolve_socket_path(socket_path)
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._rfile = None
        # Request ids are per-client and monotonic ("req_1", "req_2", ...).
        # The id is the only thing tying a reply to its request, so it must not
        # repeat within a connection's lifetime.
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------
    # socket path resolution
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_socket_path(socket_path: Optional[str] = None,
                            env: Optional[Dict[str, str]] = None,
                            home: Optional[Any] = None) -> str:
        """Resolve the control socket: arg > $HERDR_SOCKET_PATH > $HERDR_SESSION
        > ~/.config/herdr/herdr.sock.

        `env` and `home` are injectable so callers (and tests) can resolve a
        path without touching the real environment or the real home directory.
        An empty-string env var counts as unset.
        """
        if socket_path:
            return str(socket_path)
        env = os.environ if env is None else env
        home = Path.home() if home is None else Path(home)

        from_env = env.get("HERDR_SOCKET_PATH")
        if from_env:
            return str(from_env)

        session = env.get("HERDR_SESSION")
        if session:
            return str(Path(home) / SESSION_RELDIR / session / SESSION_SOCKET_NAME)

        return str(Path(home) / DEFAULT_SOCKET_RELPATH)

    # ------------------------------------------------------------------
    # connection lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the connection. Safe to call when never connected, or twice."""
        rfile, sock = self._rfile, self._sock
        self._rfile, self._sock = None, None
        for closeable in (rfile, sock):
            if closeable is None:
                continue
            try:
                closeable.close()
            except OSError:
                pass

    def __enter__(self) -> "HerdrClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _connect(self) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except OSError as exc:
            sock.close()
            # Wrap connect failures rather than leaking OSError: a caller
            # driving herdr should not need to know whether "herdr is not
            # running" arrives as ConnectionRefusedError or FileNotFoundError.
            raise HerdrError(
                "connect",
                "cannot reach herdr at {0}: {1}".format(self.socket_path, exc),
            ) from exc
        self._sock = sock
        # Buffered reader: a reply can arrive split across TCP-sized reads, and
        # readline reassembles the newline-delimited framing. Writes go straight
        # to the socket (sendall) so they never share a buffer with the reader.
        self._rfile = sock.makefile("rb")

    # ------------------------------------------------------------------
    # framing
    # ------------------------------------------------------------------

    def _write(self, frame: Dict[str, Any]) -> None:
        assert self._sock is not None
        data = (json.dumps(frame) + "\n").encode("utf-8")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            # No silent reconnect-and-retry: a peer that has gone away may have
            # taken a *previous* request with it, and re-sending on a fresh
            # connection would run a non-idempotent method (tab.create) twice.
            # The socket is closed so the next call reconnects cleanly, and the
            # failure is reported instead of papered over.
            self.close()
            raise HerdrError("disconnected", "write failed: {0}".format(exc)) from exc

    def _read_frame(self) -> Dict[str, Any]:
        """Read one NDJSON frame. Raises HerdrError on timeout/EOF/garbage."""
        if self._rfile is None:
            self._connect()
        assert self._rfile is not None
        try:
            line = self._rfile.readline()
        except socket.timeout as exc:
            # Must be caught before the OSError arm below; socket.timeout is a
            # subclass. The buffered reader's state after a mid-line timeout is
            # not trustworthy, so the connection is left unusable — callers that
            # want to recover call close() and reconnect.
            raise HerdrError(
                "timeout",
                "no reply from herdr within {0}s".format(self.timeout),
            ) from exc
        except OSError as exc:
            self.close()
            raise HerdrError("disconnected", "read failed: {0}".format(exc)) from exc

        if not line:
            self.close()
            raise HerdrError("disconnected", "herdr closed the connection")
        try:
            frame = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise HerdrError("bad_response", "reply was not JSON: {0}".format(exc)) from exc
        if not isinstance(frame, dict):
            raise HerdrError(
                "bad_response", "reply frame was {0}, want object".format(type(frame).__name__))
        return frame

    def _send(self, method: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Write one request frame and return its id."""
        self._connect()
        req_id = "req_{0}".format(next(self._ids))
        self._write({
            "id": req_id,
            "method": method,
            # `params` is always present, empty object included: herdr reads the
            # key unconditionally for a void method like tab.close.
            "params": dict(params) if params else {},
        })
        return req_id

    @staticmethod
    def _raise_error_frame(frame: Dict[str, Any]) -> None:
        err = frame.get("error") or {}
        if not isinstance(err, dict):
            err = {"code": "unknown", "message": str(err)}
        raise HerdrError(str(err.get("code", "unknown")), str(err.get("message", "")))

    def _await_reply(self, req_id: str) -> Any:
        """Read frames until the reply carrying `req_id` arrives.

        The skip is the whole point of this method: once a subscription is
        active the connection carries unsolicited event frames, and an event
        can even overtake the ack for the subscribe request itself. Anything
        whose `id` is not `req_id` is not our answer, whatever it is.
        """
        while True:
            frame = self._read_frame()
            if frame.get("id") != req_id:
                continue
            if "error" in frame:
                self._raise_error_frame(frame)
            if "result" in frame:
                result = frame["result"]
                return {} if result is None else result
            raise HerdrError("bad_response", "reply had neither result nor error: {0}".format(frame))

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> dict:
        """Send one request and return its unwrapped `result` object.

        Raises :class:`HerdrError` on an error frame or any transport failure.
        """
        return self._await_reply(self._send(method, params))

    @staticmethod
    def _field(result: Any, key: str, method: str) -> Any:
        """Pull `key` out of a result object, loudly if it is missing.

        A wrapper that returned None here would hand a caller a None that
        explodes three frames later; surfacing `bad_response` at the call site
        names the method that misbehaved.
        """
        if isinstance(result, dict) and key in result:
            return result[key]
        raise HerdrError(
            "bad_response", "{0} reply is missing {1!r}: {2!r}".format(method, key, result))

    # ------------------------------------------------------------------
    # wrappers
    # ------------------------------------------------------------------

    def ping(self) -> dict:
        """Liveness probe; returns the result verbatim (`{"type": "pong"}`)."""
        return self.call("ping")

    def workspace_list(self) -> list:
        """The `workspaces` array of workspace.list."""
        return self._field(self.call("workspace.list"), "workspaces", "workspace.list")

    def tab_create(self, workspace: str, cwd: str, label: str, focus: bool = False) -> dict:
        """Create a tab; returns the whole result — it carries `tab` and `root_pane`."""
        return self.call("tab.create", {
            "workspace": workspace, "cwd": cwd, "label": label, "focus": focus,
        })

    def tab_close(self, tab_id: str) -> None:
        """Close a tab and its panes."""
        self.call("tab.close", {"tab_id": tab_id})

    def pane_run(self, pane_id: str, command: str) -> None:
        """Type a command into a pane and submit it."""
        self.call("pane.run", {"pane_id": pane_id, "command": command})

    def pane_close(self, pane_id: str) -> None:
        """Close one pane."""
        self.call("pane.close", {"pane_id": pane_id})

    def pane_rename(self, pane_id: str, label: str) -> None:
        """Set a pane's title/label."""
        self.call("pane.rename", {"pane_id": pane_id, "label": label})

    def pane_read(self, pane_id: str, source: str = "recent", lines: int = 40) -> str:
        """Read pane content back as text.

        `source` is herdr's pane buffer selector (`"recent"`, `"visible"`, ...).
        pane.read has shipped more than one payload spelling, so accept a bare
        string result or any of the usual text keys rather than guessing once
        and raising KeyError on the other shapes.
        """
        result = self.call("pane.read", {
            "pane_id": pane_id, "source": source, "lines": lines,
        })
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("text", "output", "content", "data"):
                value = result.get(key)
                if isinstance(value, str):
                    return value
            value = result.get("lines")
            if isinstance(value, list):
                return "\n".join(str(line) for line in value)
        raise HerdrError("bad_response", "pane.read reply carried no text: {0!r}".format(result))

    def report_agent(self, pane_id: str, source: str, agent: str, state: str,
                     message: Optional[str] = None, seq: Optional[int] = None,
                     agent_session_id: Optional[str] = None) -> None:
        """Publish agent state for a pane.

        `state` must be one of `idle|working|blocked|unknown`. It is validated
        here rather than on the wire because herdr's own reaction to an
        unrecognised state is to render an ambiguous marker — a typo would be
        invisible until someone read the wrong dot.
        """
        if state not in AGENT_STATES:
            raise ValueError(
                "unknown agent state {0!r}; expected one of {1}".format(
                    state, ", ".join(AGENT_STATES)))
        self.call("pane.report_agent", _drop_none({
            "pane_id": pane_id, "source": source, "agent": agent, "state": state,
            "message": message, "seq": seq, "agent_session_id": agent_session_id,
        }))

    def release_agent(self, pane_id: str, source: str, agent: str,
                      seq: Optional[int] = None) -> None:
        """Drop this client's claim over an agent's pane marker."""
        self.call("pane.release_agent", _drop_none({
            "pane_id": pane_id, "source": source, "agent": agent, "seq": seq,
        }))

    def report_metadata(self, pane_id: str, source: str, title: Optional[str] = None,
                        display_agent: Optional[str] = None,
                        ttl_ms: Optional[int] = None) -> None:
        """Attach pane metadata (title, agent label) with an optional expiry."""
        self.call("pane.report_metadata", _drop_none({
            "pane_id": pane_id, "source": source, "title": title,
            "display_agent": display_agent, "ttl_ms": ttl_ms,
        }))

    def agent_list(self) -> list:
        """The `agents` array of agent.list."""
        return self._field(self.call("agent.list"), "agents", "agent.list")

    def agent_get(self, target: str) -> dict:
        """Fetch one agent record by name/id."""
        return self.call("agent.get", {"target": target})

    # ------------------------------------------------------------------
    # subscriptions
    # ------------------------------------------------------------------

    def events_subscribe(self, types: Optional[List[str]] = None) -> Iterator[dict]:
        """Subscribe to pushed events; yields each event frame as it arrives.

        `types` optionally narrows the stream. The frames yielded are herdr's
        raw event objects (they carry `event`, not `result`).

        Two lifetime facts. The generator owns the connection — the
        subscription *is* the connection — so closing it (or leaving a `for`
        loop early) closes the socket. And the client's timeout applies per
        read, so an idle stream raises :class:`HerdrError` with code `timeout`
        rather than blocking forever; pass `timeout=None` for a blocking stream.
        """
        params = {"types": list(types)} if types else {}
        req_id = self._send("events.subscribe", params)
        try:
            while True:
                frame = self._read_frame()
                # The subscribe ack is identified by our request id and carries
                # no event name — consume it and keep streaming. An event frame
                # can beat the ack to the wire, which is why this cannot simply
                # read until it sees a matching id.
                if "event" not in frame and frame.get("id") == req_id:
                    if "error" in frame:
                        self._raise_error_frame(frame)
                    continue
                if "event" in frame:
                    yield frame
                # Anything else on a subscribed connection carries neither our
                # id nor an event name: drop it rather than hand the caller a
                # frame it cannot interpret.
        finally:
            self.close()


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    with HerdrClient() as client:
        print(json.dumps(client.ping(), indent=2))