#!/usr/bin/env python3
"""Observe Omnigent sessions and report them as add/remove/change events.

This module knows nothing about herdr — no panes, no tabs, no workspaces. It
only polls the Omnigent HTTP API and emits `SessionEvent`s; whatever sits above
(here: `og_herdr.py`) decides what an event means for a UI.

API facts this encodes, verified against a running server:

* Base URL defaults to http://127.0.0.1:6767.
* `GET /v1/sessions` lists sessions but **defaults to kind="default"**, which
  returns ROOT sessions only. Delegated sub-agent workers are invisible unless
  you pass `?kind=any` (or `kind=sub_agent`). This is the single easiest way to
  write a watcher that looks healthy and never sees a worker, so `kind="any"`
  is the default here and in `poll_once`.
* `GET /v1/sessions/{id}/stream` is a Server-Sent Events live tail. We parse it
  with urllib; there is deliberately no websocket dependency (CI installs only
  pytest and pyyaml).
* A session object carries at least: session_id, status, title, agent_name,
  parent_session_id, pending_elicitation_count, workspace.
* Auth may be required. `~/.omnigent/auth_tokens.json` maps base URL to
  `{token, user_id, expires_at}`. Tokens are read, never logged, never printed,
  never hardcoded; if none is found we proceed unauthenticated.

stdlib only; Python 3.10+.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

DEFAULT_BASE_URL = "http://127.0.0.1:6767"

# Statuses that mean "the agent is not producing output right now". Anything
# outside this set (plus "running") is "unknown" rather than a guess — a state
# the consumer does not recognise is safer than a wrong one.
IDLE_STATUSES = frozenset({"idle", "completed", "done", "closed"})

# Only these fields take part in diffing. The server bumps last_activity_at (and
# similar bookkeeping) constantly; if diffing keyed on the whole object, every
# poll of a working session would emit "changed" and the consumer above would
# thrash. Session identity/state, title and status are the only things anyone
# acts on.
MATERIAL_FIELDS = ("title", "status")


def herdr_state(session: dict) -> str:
    """Map an Omnigent session object to a herdr agent state.

    Returns one of "idle", "working", "blocked", "unknown".

    Rules, in priority order:

    * pending_elicitation_count > 0 -> "blocked", whatever `status` says. An
      agent waiting on a human approval is blocked even while its status reads
      "running"; that is the state a human needs to see most.
    * status "running" -> "working".
    * status in IDLE_STATUSES -> "idle".
    * anything else -> "unknown".

    Missing, None or wrongly-typed fields never raise: a malformed session
    yields "unknown" (or "blocked" if it carries a usable count).
    """
    if not isinstance(session, dict):
        return "unknown"

    pending = session.get("pending_elicitation_count")
    if isinstance(pending, bool):
        # bool is an int subclass; True meaning "1 blocked" would be nonsense.
        pending = None
    if isinstance(pending, int) and pending > 0:
        return "blocked"
    # A numeric string still counts — the field has been spelled as a string
    # by clients more than once, and blocking is the safe direction to err in.
    if isinstance(pending, str):
        try:
            if int(pending.strip()) > 0:
                return "blocked"
        except ValueError:
            pass

    status = session.get("status")
    if not isinstance(status, str):
        return "unknown"
    status = status.strip().lower()
    if status == "running":
        return "working"
    if status in IDLE_STATUSES:
        return "idle"
    return "unknown"


@dataclass
class SessionEvent:
    """One change in the observed set of sessions.

    kind is "added", "removed" or "changed". `session` is the current object
    ({} for "removed", since it is gone) and `previous` is the prior one
    ({} for "added"). Both are copies, safe to keep.
    """

    kind: str
    session_id: str
    session: dict = field(default_factory=dict)
    previous: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        """herdr state of the current object; "unknown" for a removal."""
        return herdr_state(self.session) if self.session else "unknown"


class _SSEParser:
    """Incremental Server-Sent Events parser.

    Feed it bytes; it yields one parsed JSON object per dispatched frame. Wire
    rules implemented: `data:` lines accumulate into a frame, a blank line
    dispatches it, `:` lines are keepalive comments, other fields (event:, id:,
    retry:) are ignored, and a payload that is not valid JSON is skipped rather
    than raising — one bad frame must not kill a live tail. Chunk boundaries
    are irrelevant; a frame split across reads still dispatches once.
    """

    def __init__(self) -> None:
        self._buf = b""
        self._data: list[bytes] = []

    def feed(self, chunk: bytes) -> Iterator[dict]:
        self._buf += chunk
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = self._buf[:idx]
            self._buf = self._buf[idx + 1:]
            # SSE allows CR, CRLF or bare LF line endings.
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            frame = self._line(raw)
            if frame is not None:
                yield frame

    def flush(self) -> Iterator[dict]:
        """Dispatch anything still buffered when the stream ends without a
        trailing blank line (some servers close right after a frame)."""
        if self._buf:
            raw, self._buf = self._buf, b""
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            frame = self._line(raw)
            if frame is not None:
                yield frame
        frame = self._dispatch()
        if frame is not None:
            yield frame

    def _line(self, raw: bytes) -> Optional[dict]:
        if raw == b"":
            return self._dispatch()
        if raw.startswith(b":"):
            return None  # keepalive comment
        field_name, _, value = raw.partition(b":")
        if field_name != b"data":
            return None
        if value.startswith(b" "):
            value = value[1:]
        self._data.append(value)
        return None

    def _dispatch(self) -> Optional[dict]:
        if not self._data:
            return None
        payload = b"\n".join(self._data)
        self._data = []
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None


class SessionWatcher:
    """Poll `GET /v1/sessions` (kind=any) and diff it into SessionEvents.

    `opener` is the injection seam for tests and for callers that want their
    own transport; it defaults to urllib.request.urlopen and is called as
    opener(request) returning a context manager with .read(n).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: Optional[str] = None,
        poll_interval: float = 3.0,
        opener: Optional[Any] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.poll_interval = poll_interval
        self.opener = opener or urllib.request.urlopen
        # session_id -> last seen session object. Populated by poll_once.
        self._seen: dict[str, dict] = {}

    # -- auth ---------------------------------------------------------------

    @staticmethod
    def discover_token(home=None) -> Optional[str]:
        """Best-effort read of the machine-local API token.

        Looks in `home` (default $OMNIGENT_HOME, else ~/.omnigent) for
        auth_tokens.json, whose shape is {base_url: {token, user_id,
        expires_at}} (older files stored a bare string per base URL). Entries
        for a loopback server win, since that is the server og talks to.
        Returns None when the file is missing or unreadable — the caller then
        runs unauthenticated. The token value is never logged.
        """
        if home is None:
            home = os.environ.get("OMNIGENT_HOME") or str(Path.home() / ".omnigent")
        path = Path(home).expanduser() / "auth_tokens.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        def order(item):
            key = str(item[0])
            local = ("127.0.0.1" in key) or ("localhost" in key)
            return (0 if local else 1, key)

        for _, entry in sorted(data.items(), key=order):
            if isinstance(entry, str) and entry:
                return entry
            if isinstance(entry, dict):
                token = entry.get("token")
                if isinstance(token, str) and token:
                    return token
        return None

    # -- polling ------------------------------------------------------------

    def _request(self, url: str, accept: str = "application/json"):
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", accept)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return req

    def list_sessions(self, kind: str = "any") -> list:
        """GET /v1/sessions?kind=<kind> and return the session list.

        kind defaults to "any" on purpose — see the module docstring: the
        server's own default hides every sub-agent worker.
        """
        url = f"{self.base_url}/v1/sessions?{urllib.parse.urlencode({'kind': kind})}"
        req = self._request(url)
        with self.opener(req) as resp:
            body = resp.read()
        payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        if isinstance(payload, dict):
            for key in ("sessions", "items", "data"):
                if isinstance(payload.get(key), list):
                    return [s for s in payload[key] if isinstance(s, dict)]
            return []
        if isinstance(payload, list):
            return [s for s in payload if isinstance(s, dict)]
        return []

    def poll_once(self) -> list:
        """One listing, diffed against the last, as a list of SessionEvent.

        "added" for new ids, "removed" for vanished ids, and "changed" only
        when herdr_state, title or status moved — see MATERIAL_FIELDS.
        """
        fresh = {}
        for session in self.list_sessions(kind="any"):
            sid = session.get("session_id") or session.get("id")
            if isinstance(sid, str) and sid:
                fresh[sid] = session

        events = []
        for sid, session in fresh.items():
            if sid not in self._seen:
                events.append(SessionEvent("added", sid, dict(session), {}))
            elif self._material_change(self._seen[sid], session):
                events.append(SessionEvent("changed", sid, dict(session),
                                           dict(self._seen[sid])))
        for sid, session in self._seen.items():
            if sid not in fresh:
                events.append(SessionEvent("removed", sid, {}, dict(session)))

        self._seen = fresh
        return events

    @staticmethod
    def _material_change(previous: dict, current: dict) -> bool:
        if herdr_state(previous) != herdr_state(current):
            return True
        return any(previous.get(f) != current.get(f) for f in MATERIAL_FIELDS)

    def watch(self):
        """Yield events forever, sleeping poll_interval between polls.

        Deliberately a plain generator with no exception handling: a caller
        that wants to survive a server restart can wrap it, and one that does
        not should see the failure instead of an infinite silent loop.
        """
        while True:
            for event in self.poll_once():
                yield event
            time.sleep(self.poll_interval)

    # -- live tail ----------------------------------------------------------

    def stream_events(self, session_id: str):
        """Yield parsed SSE data frames for one session as dicts.

        GET /v1/sessions/{id}/stream is a long-lived response; reading it in
        fixed-size chunks and feeding an incremental parser is what lets a
        frame split across two reads still arrive intact. Non-JSON payloads
        are dropped by the parser, never raised.
        """
        url = f"{self.base_url}/v1/sessions/{urllib.parse.quote(session_id)}/stream"
        req = self._request(url, accept="text/event-stream")
        parser = _SSEParser()
        with self.opener(req) as resp:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                for frame in parser.feed(chunk):
                    yield frame
            for frame in parser.flush():
                yield frame


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Print one line per session event. Handy for eyeballing a live server."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--once", action="store_true", help="one poll, then exit")
    args = parser.parse_args(list(argv) if argv is not None else None)

    watcher = SessionWatcher(
        base_url=args.base_url,
        token=SessionWatcher.discover_token(),
        poll_interval=args.interval,
    )
    for event in watcher.poll_once():
        print(event.kind, event.session_id, event.state)
    if not args.once:
        try:
            for event in watcher.watch():
                print(event.kind, event.session_id, event.state)
        except KeyboardInterrupt:
            print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
