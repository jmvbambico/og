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
* The listing answers with a LIST ENVELOPE, not `{"sessions": [...]}`:

      {"object": "list", "data": [ ...rows... ], "has_more": true,
       "first_id": "<id>", "last_id": "<id>"}

  Rows are newest-first and paginated. The server's own page size is 20 and it
  honours `limit`; the next page is `?after=<last_id>` and `has_more` says
  whether one exists. Reading a single page silently drops the oldest rows —
  which is where the root session, the conversation the user is driving, lives.
* A row carries `id` (its identity here), `status`, `title`, `agent_name`,
  `parent_session_id` (null on a root), `pending_elicitations_count` —
  PLURAL, `archived`, plus `external_session_id`, `agent_id`, `labels`,
  `owner`, `permission_level`, `runner_id`, `created_at`, `updated_at`,
  `comments_count`, `viewer_unread`. There is no `session_id`, no `kind` and no
  `workspace` in a row.
* There is no server-side status filter: `status=`, `statuses=` and `state=`
  are all ignored and the server returns the same rows regardless. Any
  narrowing has to happen on this side — see `should_project`.
* `GET /v1/sessions/{id}/stream` is a Server-Sent Events live tail. We parse it
  with urllib; there is deliberately no websocket dependency (CI installs only
  pytest and pyyaml).
* Auth may be required. `~/.omnigent/auth_tokens.json` maps base URL to
  `{token, user_id, expires_at}`. Tokens are read, never logged, never printed,
  never hardcoded; if none is found we proceed unauthenticated. A token is only
  ever used for the server it was issued for: a store holding nothing but a
  remote server's token leaves us unauthenticated rather than borrowing it.

stdlib only; Python 3.10+.
"""
from __future__ import annotations

import json
import os
import re
import sys
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

# Page size asked of the listing endpoint. Measured against a 0.17.0 server:
# its own default is 20 rows, newest-first, which is fewer than a busy machine
# has sessions — a single page loses the root session every time. `limit` is
# honoured, so ask for a page big enough that ordinary installs finish in one.
SESSION_PAGE_LIMIT = 100

# Ceilings on one listing walk. A busy machine measures ~100 sessions (91 idle,
# 8 failed, 1 running), so this is order-of-magnitude headroom; the purpose is
# not to be exact but to keep a single poll bounded in requests and in memory
# when a server holds thousands of rows, or one that ignores `after` and says
# has_more forever. Truncation costs the OLDEST rows — the roots — which is why
# the ceiling is set an order of magnitude above anything real: see the note in
# list_sessions.
MAX_SESSION_PAGES = 10
MAX_LISTED_SESSIONS = 1000

# Ceiling on how long watch() waits between retries after a failed poll. Long
# enough that a server restart does not become a request storm, short enough
# that events start flowing again promptly once it is back.
MAX_POLL_BACKOFF = 30.0

# Ceilings for the SSE parser. A session event is kilobytes; a megabyte is
# already several orders of magnitude past anything real. Past these the peer is
# either broken or hostile, and in a process meant to run for days the only
# acceptable response is to drop what we cannot hold and keep the tail alive —
# a live stream that dies on one malformed frame is worse than one that skips
# it, and one that buffers forever until it is OOM-killed is worse still.
MAX_SSE_LINE_BYTES = 1 << 20    # 1 MiB for a single line with no terminator
MAX_SSE_FRAME_BYTES = 4 << 20   # 4 MiB of accumulated `data:` lines

# A watcher is a background process: its stderr goes wherever the user pointed
# it, so anything it prints must be safe to show.
_URL_USERINFO = re.compile(r"(?<=://)[^/\s:@]+(:[^/\s@]*)?@")


def _scrub(text: str) -> str:
    """Strip `user:pass@` out of anything about to be written to stderr.

    urllib puts the request URL into its error messages, so a base URL written
    as http://user:pass@host would otherwise land in a log line. The bearer
    token travels in a header and never appears in an exception, but the URL is
    cheap to scrub and this is the one function whose output nobody reads before
    it is printed.
    """
    return _URL_USERINFO.sub("***@", text)


def _server_key(url: str) -> str:
    """Canonical form of a base URL, for matching a token to its server.

    Keeps scheme/host/port and path (so `...:6767` and `...:6767/` are the same
    server), lowercases the host because hosts are case-insensitive, and drops
    any userinfo so a secret in the URL cannot reach a log line or a
    comparison. Anything unparseable falls back to the stripped string, which
    simply will not match.
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
        netloc = (parts.hostname or "").lower()
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urllib.parse.urlunsplit(
            (parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", "")
        )
    except ValueError:  # e.g. a port that is not a number
        return url.strip().rstrip("/")


def _pending_elicitation_count(session: dict) -> Optional[int]:
    """How many elicitations this session is waiting on a human to answer, or
    None when the row carries no usable count.

    The REST row spells the field `pending_elicitations_count` — PLURAL —
    measured against a live server; reading the singular spelling means the key
    never matches and `blocked` never fires at all, which is the one state a
    human most needs to see. The singular spelling is the MCP tool's
    `session_get_info` field name, so a caller that got its session dict from
    there rather than from HTTP hands us that shape; it stays as a fallback,
    never as the deciding key.

    A bool is refused even though it is an int subclass: `True` meaning "one
    elicitation pending" would be nonsense. A numeric string still counts —
    clients have spelled the count as a string, and blocking is the safe
    direction to err in.
    """
    for key in ("pending_elicitations_count", "pending_elicitation_count"):
        raw = session.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            try:
                return int(raw.strip())
            except ValueError:
                continue
    return None


def herdr_state(session: dict) -> str:
    """Map an Omnigent session object to a herdr agent state.

    Returns one of "idle", "working", "blocked", "unknown".

    Rules, in priority order:

    * pending_elicitations_count > 0 -> "blocked", whatever `status` says. An
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

    pending = _pending_elicitation_count(session)
    if pending is not None and pending > 0:
        return "blocked"

    status = session.get("status")
    if not isinstance(status, str):
        return "unknown"
    status = status.strip().lower()
    if status == "running":
        return "working"
    if status in IDLE_STATUSES:
        return "idle"
    return "unknown"


# The two herdr states in which a sub-agent is still worth a pane.
LIVE_STATES = frozenset({"working", "blocked"})


def should_project(session: dict) -> bool:
    """Whether a listed session is worth projecting to whatever sits above.

    `poll_once` filters the listing through this, so the consumer only ever sees
    sessions that pass and a "removed" event always means "this one stopped
    being worth watching".

    The rule, and why it differs by kind:

    * A ROOT session (no parent_session_id) is projected while it is listed and
      not archived. A root is the conversation the human drives, and it reads
      "idle" for the entire time it is waiting for them to type — projecting on
      activity alone would close the pane they are typing into.
    * A SUB-AGENT session is projected only while it is working or waiting:
      herdr_state "working" or "blocked", i.e. running, or holding elicitations
      a human has to answer. When it goes idle or failed it leaves the set,
      which is exactly the intended meaning of "removed". Without this, every
      finished worker on the machine (measured: 91 idle, 8 failed) would get a
      pane and the workspace would be unusable.
    * `archived` is excluded either way: archived is the server's own word for
      "do not show this".

    Note there is no server-side filter to push this into — status=, statuses=
    and state= are all ignored by the API — so this has to be decided here.
    """
    if not isinstance(session, dict):
        return False
    if session.get("archived"):
        return False
    parent = session.get("parent_session_id")
    if parent is None or parent == "":
        return True
    return herdr_state(session) in LIVE_STATES


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

    Lines end at CR, LF or CRLF, all three of which the spec allows. A lone CR
    at the very end of the buffer is held back until the next read, because the
    byte after it decides whether it was a bare CR or half of a CRLF.

    Both buffers are capped (MAX_SSE_LINE_BYTES, MAX_SSE_FRAME_BYTES). A stream
    that never terminates a line, or a run of `data:` lines that never dispatches,
    is dropped rather than buffered: this object lives inside a process meant to
    run for days, and a malformed peer must not be able to grow it without
    limit or kill the tail.
    """

    def __init__(self) -> None:
        self._buf = b""
        self._data: list[bytes] = []
        self._data_bytes = 0
        # True while we are discarding the tail of a line we already threw away,
        # so its remainder is not mistaken for a fresh frame.
        self._resync = False

    def feed(self, chunk: bytes) -> Iterator[dict]:
        self._buf += chunk
        while True:
            idx = self._next_break()
            if idx < 0:
                if len(self._buf) > MAX_SSE_LINE_BYTES:
                    self._drop_line()
                break
            raw = self._buf[:idx]
            # A CRLF is one terminator, not an empty line between two.
            width = 2 if self._buf[idx:idx + 2] == b"\r\n" else 1
            self._buf = self._buf[idx + width:]
            if self._resync:
                self._resync = False
                continue
            frame = self._line(raw)
            if frame is not None:
                yield frame

    def flush(self) -> Iterator[dict]:
        """Dispatch anything still buffered when the stream ends without a
        trailing blank line (some servers close right after a frame)."""
        rest, self._buf = self._buf, b""
        if self._resync:
            # Whatever is left belongs to a line we already discarded.
            self._resync = False
        else:
            # Normalise to LF so a trailing lone CR — which feed() held back
            # because it might have been a CRLF — terminates its line here.
            for raw in rest.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n"):
                frame = self._line(raw)
                if frame is not None:
                    yield frame
        frame = self._dispatch()
        if frame is not None:
            yield frame

    def _next_break(self) -> int:
        """Index of the next line terminator in `_buf`, or -1 if there is none
        (yet — a lone CR on the very end counts as "yet", see the class docstring).
        """
        lf = self._buf.find(b"\n")
        cr = self._buf.find(b"\r")
        if cr == len(self._buf) - 1 and lf < 0:
            # Could be half of a CRLF whose LF is still in flight. Splitting now
            # would turn one terminator into two, i.e. invent a blank line and
            # dispatch a frame the peer has not finished writing.
            return -1
        if lf < 0:
            return cr
        if cr < 0 or lf < cr:
            return lf
        return cr

    def _drop_line(self) -> None:
        """Throw away a line too big to hold, and resynchronise on its end."""
        self._buf = b""
        # The frame this line belonged to is already over budget; keeping any of
        # it would mean dispatching half a frame once the terminator arrives.
        self._data = []
        self._data_bytes = 0
        self._resync = True

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
        self._data_bytes += len(value) + 1
        if self._data_bytes > MAX_SSE_FRAME_BYTES:
            self._data = []
            self._data_bytes = 0
        return None

    def _dispatch(self) -> Optional[dict]:
        if not self._data:
            return None
        payload = b"\n".join(self._data)
        self._data = []
        self._data_bytes = 0
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None


def _read_page(payload: Any) -> tuple[list, bool, Optional[str]]:
    """One listing response -> (rows, has_more, next_cursor).

    `data` is the measured key of the list envelope, `has_more`/`last_id` its
    pagination fields; the other two key spellings and the bare-array form are
    tolerance for a payload that is not the shape this server speaks, kept
    because parsing them costs nothing and an unreadable page is worse than a
    surprising one. They are not what the tests pin.

    The cursor is the envelope's own `last_id`; the last row's `id` is the
    fallback for an envelope that omits it. Both are the row identity, which is
    what `after` takes.
    """
    if isinstance(payload, list):
        return [s for s in payload if isinstance(s, dict)], False, None
    if not isinstance(payload, dict):
        return [], False, None

    rows: Optional[list] = None
    for key in ("data", "sessions", "items"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            rows = [s for s in candidate if isinstance(s, dict)]
            break
    if rows is None:
        return [], False, None

    cursor = payload.get("last_id")
    if not isinstance(cursor, str) or not cursor:
        last = rows[-1].get("id") if rows else None
        cursor = last if isinstance(last, str) and last else None
    return rows, bool(payload.get("has_more")), cursor


def _session_id(session: dict) -> Optional[str]:
    """The row's identity, or None when it has none.

    `id` is what the listing actually returns and what every diff, event and
    pane above is keyed on. `session_id` is kept as a fallback for a payload
    from some other source that uses that name.

    `external_session_id` — the `ses_…` handle the row also carries — is
    deliberately NOT accepted here: it is a different handle from the row
    identity, so mixing the two lets one session be counted twice under two
    names, or two rows collide under one.
    """
    for key in ("id", "session_id"):
        sid = session.get(key)
        if isinstance(sid, str) and sid:
            return sid
    return None


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
        # session id -> last seen session object. Populated by poll_once.
        self._seen: dict[str, dict] = {}
        # Whether the last listing was truncated, so the notice written about
        # it is written once per transition rather than once per poll.
        self._listing_truncated = False

    # -- auth ---------------------------------------------------------------

    @staticmethod
    def discover_token(base_url: str, home=None) -> Optional[str]:
        """Read the API token issued for `base_url`, if there is one.

        Looks in `home` (default $OMNIGENT_HOME, else ~/.omnigent) for
        auth_tokens.json, whose shape is {base_url: {token, user_id,
        expires_at}} (older files stored a bare string per base URL).

        Only an entry whose key names this server is considered. A store that
        holds a token for `https://remote.example` and nothing for localhost
        must leave us unauthenticated, not send that remote bearer to the
        loopback server — a credential presented to a host it was not issued for
        is a credential leak, and it is easy to miss because it still "works".
        Returns None when the file, the entry or a usable token is missing; the
        caller then runs unauthenticated. The token value is never logged.
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

        target = _server_key(base_url)
        for key, entry in data.items():
            # Two spellings of the same URL can both be present; the tie-break
            # that matters is which one holds a usable token, so scan them all
            # rather than picking a winner on the key alone.
            if _server_key(str(key)) != target:
                continue
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

    def _get_json(self, url: str) -> Any:
        """GET one JSON document. Strict: a transport or decode error raises."""
        req = self._request(url)
        with self.opener(req) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)

    def _fetch_listing(self, kind: str = "any") -> tuple[list, bool]:
        """Walk the listing to its end -> (rows, truncated).

        `truncated` means the walk stopped with rows left unfetched — the page
        cap ran out, the row cap cut the tail off, the envelope promised more
        but gave no cursor to ask for, or the server handed back a cursor it had
        already given. A complete listing is `truncated is False`.

        The caller cannot invent the difference from the rows themselves: the
        rows of a truncated listing are indistinguishable from the rows of a
        smaller server, and that indistinguishability is exactly what makes a
        truncated listing dangerous — see poll_once.
        """
        rows: list = []
        truncated = False
        params = {"kind": kind, "limit": SESSION_PAGE_LIMIT}
        seen_cursors: set = set()
        cursor: Optional[str] = None

        for _ in range(MAX_SESSION_PAGES):
            if cursor:
                params["after"] = cursor
            payload = self._get_json(
                f"{self.base_url}/v1/sessions?{urllib.parse.urlencode(params)}"
            )
            page, has_more, next_cursor = _read_page(payload)
            rows.extend(page)
            if not has_more:
                break
            if not next_cursor or next_cursor in seen_cursors:
                # The server says there is more and will not say where to get
                # it. Stopping is the only thing that terminates the walk, and
                # whatever is past that point counts as unfetched.
                truncated = True
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            # The page cap ran out on a page that still claimed there was more.
            truncated = True

        if len(rows) > MAX_LISTED_SESSIONS:
            # A server ignoring `limit` and sending a huge page: the cap still
            # holds, and the rows it cuts are rows we did not finish reading.
            rows = rows[:MAX_LISTED_SESSIONS]
            truncated = True

        return rows, truncated

    def list_sessions(self, kind: str = "any") -> list:
        """Every listed session, walked across pages. Unfiltered — narrowing is
        `should_project`'s job, so this stays a faithful read of the API.

        kind defaults to "any" on purpose — see the module docstring: the
        server's own default hides every sub-agent worker.

        The listing is paginated and newest-first, so one page is not the
        listing: on a machine with more than a page of sessions, reading only
        the first one drops the root session — the conversation the user is
        actually driving — and makes anything that slips across the page
        boundary between two polls indistinguishable from a deletion, which the
        consumer above turns into a closed tab. So the walk follows
        `?after=<last_id>` while `has_more` is true.

        The walk is capped (MAX_SESSION_PAGES, MAX_LISTED_SESSIONS) so a poll
        stays bounded in requests and memory whatever the server holds. The cap
        drops the OLDEST rows, which are the ones this most needs — hence the
        order-of-magnitude headroom rather than a tight bound. Those rows also
        stop being evidence of deletion: `_fetch_listing` reports when the walk
        stopped short, and poll_once acts on it.
        """
        rows, _truncated = self._fetch_listing(kind)
        return rows

    def poll_once(self) -> list:
        """One listing, diffed against the last, as a list of SessionEvent.

        "added" for new ids, "removed" for vanished ids, and "changed" only
        when herdr_state, title or status moved — see MATERIAL_FIELDS.

        Only sessions `should_project` accepts reach the diff, so the consumer
        is handed the live set and nothing else; a sub-agent finishing reads as
        "removed" because that is what it is.

        A TRUNCATED listing cannot mean anything by an absence. Rows past the
        cap were never fetched, and since the listing is newest-first those are
        the oldest sessions — the roots, the panes the user is typing into — so
        reporting them as removed would close exactly the panes that must not
        close, manufactured by our own ceiling rather than by the server. So a
        truncated poll emits no removals at all, and keeps the sessions it did
        not see in `_seen`: dropping them would only move the damage, turning
        this poll's phantom removals into the next complete poll's phantom
        `added`, and the consumer would open a second tab for a session whose
        pane is already on screen. What the poll *did* see is still diffed
        normally, so `added` and `changed` keep flowing while truncated.

        The cost, stated plainly: a session that quietly finished during a
        truncated window keeps its pane until a complete listing arrives — a
        stale pane instead of a wrongly closed one — and if the listing never
        completes, removals never resume at all. One line to stderr, on the
        transition into that state and out of it, is what makes it findable.

        Strict: a transport or decode error propagates. `_seen` is updated as
        the very last statement, so a failure leaves the previously observed
        state untouched rather than half-updated.
        """
        rows, truncated = self._fetch_listing(kind="any")
        fresh = {}
        for session in rows:
            sid = _session_id(session)
            if sid is not None and should_project(session):
                fresh[sid] = session

        events = []
        for sid, session in fresh.items():
            if sid not in self._seen:
                events.append(SessionEvent("added", sid, dict(session), {}))
            elif self._material_change(self._seen[sid], session):
                events.append(SessionEvent("changed", sid, dict(session),
                                           dict(self._seen[sid])))

        if truncated:
            self._announce_listing(len(rows), truncated)
            # Merge over what we already knew rather than replacing it: the
            # sessions missing from this poll are missing from the FETCH, not
            # from the server, and the only honest record of them is the one we
            # took when we could still see them.
            self._seen.update(fresh)
            return events

        for sid, session in self._seen.items():
            if sid not in fresh:
                events.append(SessionEvent("removed", sid, {}, dict(session)))

        self._announce_listing(len(rows), truncated)
        self._seen = fresh
        return events

    def _announce_listing(self, rows: int, truncated: bool) -> None:
        """Report a change in what this watcher knows, not the state itself.

        Being on partial knowledge is something an operator has to be able to
        discover — it changes what the events above mean — but a watcher
        polling every few seconds in a permanently truncated state would
        otherwise say the same thing forever and bury everything else. So the
        line is written when the state changes and not in between: entering the
        truncated state, and coming back out of it.
        """
        if truncated == self._listing_truncated:
            return
        self._listing_truncated = truncated
        if truncated:
            sys.stderr.write(
                f"og herdr watch: session listing truncated at {rows} rows; "
                "reporting only what was fetched, removals suppressed until a "
                "complete listing arrives\n"
            )
        else:
            sys.stderr.write(
                f"og herdr watch: session listing complete again ({rows} rows); "
                "removals resume\n"
            )
        sys.stderr.flush()

    @staticmethod
    def _material_change(previous: dict, current: dict) -> bool:
        if herdr_state(previous) != herdr_state(current):
            return True
        return any(previous.get(f) != current.get(f) for f in MATERIAL_FIELDS)

    def watch(self):
        """Yield events forever, sleeping poll_interval between polls.

        poll_once() is strict — a refused connection, a 500, or a listing that
        is not JSON raises out of it — and it stays that way: a caller that
        wants the exception should get it. The *loop* is what has to be
        resilient, because the only production caller (og_herdr.py's
        `run_forever`) wraps nothing and a daemon that dies of one server
        restart has silently stopped doing its job.

        So a failed poll is reported on stderr and retried after a backoff that
        doubles up to MAX_POLL_BACKOFF and resets on the first success. Events
        resume on their own when the server comes back.

        `_seen` is deliberately not touched on the failure path: poll_once()
        updates it as its last statement, so a poll that raises leaves the last
        state actually observed in place and recovery diffs against that.
        Otherwise every live session would be reported removed and re-added on
        each outage, and the consumer would thrash rebuilding tabs it already
        has.
        """
        backoff = self.poll_interval
        while True:
            try:
                events = self.poll_once()
            except Exception as exc:  # noqa: BLE001 — surviving this IS the job
                # KeyboardInterrupt and SystemExit are BaseException and
                # GeneratorExit closes the generator, so Ctrl-C still stops it.
                sys.stderr.write(
                    f"og herdr watch: poll failed ({_scrub(repr(exc))}); "
                    f"retrying in {backoff:g}s\n"
                )
                sys.stderr.flush()
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_POLL_BACKOFF)
                continue
            for event in events:
                yield event
            time.sleep(self.poll_interval)
            backoff = self.poll_interval

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
        token=SessionWatcher.discover_token(args.base_url),
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
