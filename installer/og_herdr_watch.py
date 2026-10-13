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
* A LISTING row carries `id` (its identity here), `status`, `title`,
  `agent_name`, `parent_session_id` (null on a root),
  `pending_elicitations_count` — PLURAL, `archived`, plus
  `external_session_id`, `agent_id`, `labels`, `owner`, `permission_level`,
  `runner_id`, `created_at`, `updated_at`, `comments_count`, `viewer_unread`.
  There is no `session_id`, no `kind` and no `workspace` in a listing row.
* A listing row is NOT the whole session. `GET /v1/sessions/{id}` — the DETAIL
  endpoint, one request per session — carries `workspace` (the directory the
  session actually lives in), plus `kind`, `harness`, `git_branch`,
  `sub_agent_name`, `runner_online` and the full `pending_elicitations` LIST
  where the listing has only its count. The listing being silent about a
  directory is not evidence that the API has none: it is why every pane once
  opened wherever the bridge happened to be started. So the watcher fetches the
  detail row ONCE per session, on the `added` path, and merges the directory
  and the session's identity (`kind`, `harness`, `sub_agent_name`) into the
  session object it emits — see `_workspace_for` for what a sub-agent resolves
  to, and why that answer is close but deliberately not exact; see DETAIL_FIELDS
  for why these three come from the detail row rather than from the listing
  row's `agent_name`, which reads a different name entirely. The same fetch is
  what decides whether the session is worth projecting at all: `runner_online`
  lives on the detail row only, and `attach` refuses a session with no live
  runner — see `runner_is_offline`, which is why that rule is applied next to
  the fetch rather than inside `should_project`.
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

# Ceiling on the parent-workspace cache (see _parent_workspace_for). The map
# exists to collapse a FAN-OUT — four workers under one root must not fetch that
# root four times — so its natural size is one entry per distinct root, which is
# small. It is capped anyway: the process is meant to run for days, roots come
# and go, and an unbounded second map beside `_seen` is a slow leak that only
# shows up as memory nobody can account for. Evicting costs one re-fetch.
MAX_PARENT_WORKSPACES = 64

# Fields merged out of the DETAIL row onto the session object the consumer
# receives, alongside `workspace`. All three answer "which worker is this":
# `kind` is the server's own classification (default / sub_agent), `harness`
# names the CLI driving the agent, and `sub_agent_name` is the delegated
# worker's own name.
#
# WHY the DETAIL row and not the listing row's `agent_name`, when the listing
# row appears to carry a name for the same thing: two sources, two answers, and
# they disagree. Measured on one live server for one sub-agent, the LISTING row
# read `agent_name: "hivemind"` — the ROOT's agent, leaked into the child's row
# — while `GET /v1/sessions/{id}` read `agent_name: "coder_zen"`, the child's
# own. Every worker in a conversation would therefore have been labelled with
# the conversation's orchestrator, which is the one name guaranteed to be wrong
# for all of them. The detail row is the authoritative one, and it is already
# being fetched for the directory, so taking these three costs no request.
#
# Each is merged only when it is a non-empty STRING, so a row that reported
# `sub_agent_name: null` for a root leaves the key absent rather than teaching
# the consumer that the root's name is null.
DETAIL_FIELDS = ("kind", "harness", "sub_agent_name")

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

# The exceptions that can only mean this module is wrong, so watch() re-raises
# them instead of retrying. The test is where they come from, not how common
# they are: nothing on the transport path raises any of them. urllib raises
# URLError/HTTPError (OSError subclasses) and socket.timeout (also an OSError);
# json raises JSONDecodeError and a bad decode raises UnicodeDecodeError (both
# ValueErrors). A TypeError out of the poll path is a mistake in this code, and
# retrying broken code can never succeed — it only wastes the process, and it
# buries the bug under "poll failed (...); retrying in 30s", a line that reads
# like a network blip and will be believed.
#
# This is an EXCLUSION list on purpose, never an allow-list of expected
# failures. An allow-list would stop the daemon dead on any transport error
# nobody enumerated — an SSL class, whatever an injected opener raises — which
# is precisely the outage the retry loop exists to survive (a server restart
# silently ending the watcher is a shipped defect). Retrying the unknown is the
# safe default; only the provably unretryable is excluded. Grow it when a new
# class of bug shows up; do not shrink it to a list of known-good errors.
_BUG_EXCEPTIONS = (TypeError, AttributeError, NameError, AssertionError)


def _scrub(text: str) -> str:
    """Strip `user:pass@` out of anything about to be written to stderr.

    urllib puts the request URL into its error messages, so a base URL written
    as http://user:pass@host would otherwise land in a log line. The bearer
    token travels in a header and never appears in an exception, but the URL is
    cheap to scrub and this is the one function whose output nobody reads before
    it is printed.
    """
    return _URL_USERINFO.sub("***@", text)


def _say(line: str) -> bool:
    """Write one diagnostic line to stderr; return whether it landed.

    A watcher is a background process: its stderr is a pipe to whatever the
    user launched it into, and it can be gone (broken pipe, closed descriptor,
    full disk on a redirect). Every diagnostic this module prints goes through
    here for that reason, and the reason is not tidiness: an unguarded
    `sys.stderr.write` inside an error handler replaces the failure being
    reported with the reporting, so a full disk plus one refused connection
    would kill the daemon — arriving through the very branch whose whole job
    is to make sure a transient failure cannot do that. OSError covers broken
    pipe and ENOSPC, ValueError covers writing to a closed file; together they
    are the whole failure surface of a one-line best-effort write, which is why
    this is not a bare except.

    The return value is what `_announce_listing` needs: it records its state as
    announced only on a write that actually landed, so a transient failure is
    retried instead of losing the notice for good.

    Accepted wart: if write() succeeded and flush() then raised, the text can
    still reach the stream later and the retry prints a second copy. A
    duplicated line is strictly better than a lost one, and that is the tradeoff
    to make deliberately rather than discover.
    """
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except (OSError, ValueError):
        return False
    return True


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


def runner_is_offline(detail: Any) -> bool:
    """Whether a DETAIL row says this session has no live runner, so a pane for
    it could only ever show an error.

    `omnigent attach` joins a LIVE session on a running server and refuses
    anything else — measured against a live server:

        Error: Session cf399984… has no online runner on http://127.0.0.1:6767

    Two of the seven sessions the bridge projected had an offline runner, and
    each would have opened a tab whose entire content is that line: noise, and on
    a machine with a long history of finished root sessions, most of them.

    ONLY an explicit boolean false suppresses a projection. A missing key, a
    null, a differently-typed value, or a detail row that never arrived (a 404, a
    500, a refused connection — `_fetch_detail` reports every one of them as
    None) all mean "unknown", and unknown projects. The asymmetry IS the
    decision: projecting a session whose runner turns out to be dead costs the
    user one tab showing an error, while hiding a session on a guess costs them a
    session they never knew existed and cannot get back by waiting, because the
    refusal would be invisible. An unreachable detail endpoint must not be able
    to empty the user's workspace.

    This is deliberately NOT folded into `should_project`: that function decides
    from the LISTING row alone, and `runner_online` is not on a listing row —
    measured keys are agent_id, agent_name, archived, comments_count, created_at,
    external_session_id, id, labels, owner, parent_session_id,
    pending_elicitations_count, permission_level, runner_id, status, title,
    updated_at, viewer_unread. Deciding it there would cost a detail fetch per
    listing row per poll — every row, every poll, for good — to learn one boolean
    about the handful of sessions about to get a pane. So the rule is this one
    predicate, applied by `_enrich` where the detail row is already in hand and
    was fetched anyway.
    """
    return isinstance(detail, dict) and detail.get("runner_online") is False


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

    What this decides from a LISTING row alone is deliberately everything it can:
    `runner_online`, the other thing that makes a session unattachable, is not
    on a listing row at all, so it is `runner_is_offline`, applied where the
    detail row is already fetched. Two places holding halves of one rule would
    read as two rules.
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

    Hitting the frame cap discards the frame through to its blank-line
    boundary, not just up to the line that broke the cap. Clearing the buffer at
    the cap would leave the rest of the frame accumulating as though it were
    fresh, and the blank line would then dispatch that tail as a whole frame:
    the consumer cannot tell a fragment from data, and the consumer here is a
    UI that acts on what it is handed. A dropped frame is a gap; a fabricated
    one is worse.
    """

    def __init__(self) -> None:
        self._buf = b""
        self._data: list[bytes] = []
        self._data_bytes = 0
        # True while we are discarding the tail of a line we already threw away,
        # so its remainder is not mistaken for a fresh frame.
        self._resync = False
        # True once the frame cap has tripped: swallow every line until the
        # blank line that ends this frame, so no part of an oversized frame is
        # ever dispatched as if it were a whole one.
        self._discard_frame = False

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
        if self._discard_frame:
            # The stream ended inside a frame that had already been poisoned.
            # The lines above went through _line and were swallowed; refusing to
            # dispatch here as well means ending the stream mid-discard cannot
            # turn the remainder into a frame, whatever _line is doing later.
            return
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
            # The blank line that ends the frame. Arriving mid-discard it also
            # ends the discard, so the next frame parses normally; _data is
            # empty by then, so this dispatches nothing.
            self._discard_frame = False
            return self._dispatch()
        if self._discard_frame:
            # Past the frame cap and still inside the frame: swallow this line
            # too, whatever it is. Dispatching the tail would hand the consumer
            # a fragment it has no way to distinguish from a whole frame.
            return None
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
            # Over budget: drop what we have and mark the frame as poisoned.
            # The frame has not ended — more `data:` lines of it may still be
            # coming — so the discard has to run to the blank line rather than
            # stop here.
            self._data = []
            self._data_bytes = 0
            self._discard_frame = True
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
        # parent session id -> the directory that parent reported, for the
        # parent lookups a sub-agent needs. Bounded (MAX_PARENT_WORKSPACES);
        # see _parent_workspace_for for why it exists and why it is not a
        # second _seen.
        self._parent_workspace: dict[str, str] = {}

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

    def fetch_listing(self, kind: str = "any") -> tuple[list, bool]:
        """Walk the listing to its end -> (rows, truncated). PUBLIC.

        Returns `(rows, truncated)`, and `truncated` means the walk stopped with
        rows left unfetched. It is NOT the same as "there are no more rows": a
        complete listing is `truncated is False`, and only that turns the absence
        of a session into evidence it is gone. `list_sessions` drops the flag
        because it wants only the rows; a caller that decides anything from an
        absence (poll_once, og_agents) needs both halves and calls this.

        `truncated` is set when the page cap ran out, the row cap cut the tail
        off, the envelope promised more but gave no cursor to ask for, or the
        server handed back a cursor it had already given.

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
        stop being evidence of deletion: `fetch_listing` reports when the walk
        stopped short, and poll_once acts on it.
        """
        rows, _truncated = self.fetch_listing(kind)
        return rows

    def poll_once(self) -> list:
        """One listing, diffed against the last, as a list of SessionEvent.

        "added" for new ids, "removed" for vanished ids, and "changed" only
        when herdr_state, title or status moved — see MATERIAL_FIELDS.

        Only sessions `should_project` accepts reach the diff, so the consumer
        is handed the live set and nothing else; a sub-agent finishing reads as
        "removed" because that is what it is.

        One more session is refused, and it is the one the LISTING row cannot
        decide: a session whose detail row reports `runner_online: False` gets
        no event at all, because `omnigent attach` refuses it and its pane could
        show nothing but that error (see `runner_is_offline`). It is refused on
        the `added` path only — where the detail row is fetched for its directory
        anyway — and it is refused by NOT entering `fresh`, so it never reaches
        `_seen` and therefore never produces a `removed` either. Which also
        means no permanent negative: it is re-asked on every poll, and the moment
        its runner is online the next poll projects it and it gets its pane.

        An already-projected session whose runner later goes offline is NOT
        retired. Re-checking it would cost a detail fetch per live session per
        poll, and closing the pane of a root session that is merely between
        turns — a root reads "idle" whenever it waits for the human, which is
        most of its life — is the exact failure the root rule above exists to
        prevent. A runner coming and going is the server's state to report; it is
        not a reason to take away a pane the user is working in.

A TRUNCATED listing cannot mean anything by an absence — but only for the
        rows it never fetched. Those are past the cap, and since the listing is
        newest-first they are the oldest sessions: the roots, the panes the user
        is typing into. Reporting them as removed would close exactly the panes
        that must not close, manufactured by our own ceiling rather than by the
        server.

        So absence has to be read in three states, not two, and they are not
        equally uncertain:

        * FETCHED and no longer projectable — a sub-agent went idle, a row came
          back archived. The row is in hand and says the session is done, and a
          truncated poll is irrelevant to that: it gets its `removed` event
          like any other, or the pane for a finished worker would sit there
          until the listing fit in one walk.
        * NEVER FETCHED — beyond the cap. Absence proves nothing, so the
          removal is suppressed and the session stays in `_seen`. Dropping it
          would only move the damage: this poll's phantom removals become the
          next complete poll's phantom `added`, and the consumer opens a second
          tab for a session whose pane is already on screen.
        * PRESENT, unchanged. `added` and `changed` flow from the fetched rows
          exactly as before, truncated or not.

        The cost that remains, stated plainly: a session that finished while it
        sat past the cap keeps a stale pane until a complete listing brings it
        back into view, and if no listing is ever complete again, the never-
        fetched ones never retire at all. A stale pane instead of a wrongly
        closed one; one stderr line, on the transition into that state and out
        of it, is what makes it findable.

        Strict: a transport or decode error propagates. `_seen` is updated as
        the very last statement, so a failure leaves the previously observed
        state untouched rather than half-updated.
        """
        rows, truncated = self.fetch_listing(kind="any")

        # Every id that came back in this poll, whether or not it qualifies,
        # because "did not qualify" and "was never asked" are different facts
        # and only the second one licenses an absence. On a duplicated id the
        # last row wins — it decides both the retained object and whether the
        # id still projects, so a later archived or idle copy retires an id an
        # earlier qualifying copy had kept alive. The server should not send
        # two rows for one id; when it does, honouring the last is the same
        # rule `fresh` has always applied to the object itself, and keeping the
        # first appearance's position keeps event order equal to listing order.
        fetched: dict[str, tuple[dict, bool]] = {}
        for session in rows:
            sid = _session_id(session)
            if sid is not None:
                fetched[sid] = (session, should_project(session))

        fresh: dict[str, dict] = {}
        events = []
        for sid, (session, qualifies) in fetched.items():
            if not qualifies:
                continue
            if sid in self._seen:
                fresh[sid] = session
                if self._material_change(self._seen[sid], session):
                    events.append(SessionEvent("changed", sid, dict(session),
                                               dict(self._seen[sid])))
                continue
            # Enriched HERE, on the first sighting only. A poll must stay one
            # listing walk plus a handful of detail fetches, so the directory is
            # resolved when the session is added and never looked up again for a
            # session already in `_seen` — which is what "changed" events above
            # are: same session, no new fetch.
            enriched, projectable = self._enrich(session)
            if not projectable:
                # An offline runner: no pane is worth opening (see
                # `runner_is_offline`). Deliberately NOT recorded in `fresh`, and
                # therefore not in `_seen`, which buys both halves of what has to
                # be true here:
                #   - it is re-asked on every poll rather than remembered as
                #     "unattachable", because a runner can come back online and
                #     the session must then get its pane. The cost is one detail
                #     fetch per such session per poll, bounded by how many there
                #     are (measured: 2 of 7), and a permanent negative cache
                #     would strand those sessions for the life of the process.
                #   - it never produces a `removed`, because `removed` is
                #     derived from `_seen` below and this session was never in
                #     it. A phantom removal would close a pane that does not
                #     exist and, on a later poll, look like a session re-added.
                continue
            fresh[sid] = session
            events.append(SessionEvent("added", sid, enriched, {}))

        retired = []
        for sid, session in self._seen.items():
            if sid in fresh:
                continue
            if truncated and sid not in fetched:
                # Never fetched. Kept, not removed: the next complete listing
                # will decide, and by then the diff is against this record
                # rather than against a gap.
                continue
            retired.append(sid)
            events.append(SessionEvent("removed", sid, {}, dict(session)))

        if truncated:
            self._announce_listing(len(rows), truncated)
            # Merge over what we already knew rather than replacing it: what is
            # missing from this poll is missing from the FETCH, and the only
            # honest record of those sessions is the one taken when we could
            # still see them. The ones this poll did retire have to actually
            # leave, or the next poll retires them again.
            self._seen.update(fresh)
            for sid in retired:
                self._seen.pop(sid, None)
            return events

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

        The state is recorded as announced only if the write actually landed —
        see `_say`, and the return value below, which is the whole reason this
        method exists in its present shape.

        A failure is swallowed AND the state is deliberately left unrecorded,
        which is the part that matters. Recording it first (or moving the
        assignment after the write without checking) loses the notice for good
        on the first failure: every later poll sees no transition, returns
        early, and the watcher then runs with removals suppressed and nothing
        anywhere saying so. Leaving it unrecorded means a transient failure is
        simply retried on the next poll and the notice still lands; a
        permanently broken stderr means every poll tries, every try fails, and
        so nothing is written, nothing is raised and nothing is recorded — no
        spam, no poll-failure retry loop, cost one failed syscall per poll.
        """
        if truncated == self._listing_truncated:
            return
        if truncated:
            line = (
                f"og herdr watch: session listing truncated at {rows} rows; "
                "reporting only what was fetched, removals suppressed until a "
                "complete listing arrives\n"
            )
        else:
            line = (
                f"og herdr watch: session listing complete again ({rows} rows); "
                "removals resume\n"
            )
        if _say(line):
            self._listing_truncated = truncated

    @staticmethod
    def _material_change(previous: dict, current: dict) -> bool:
        if herdr_state(previous) != herdr_state(current):
            return True
        return any(previous.get(f) != current.get(f) for f in MATERIAL_FIELDS)

    # -- the session's directory ---------------------------------------------
    #
    # Everything in this section exists because the LISTING row is not the whole
    # session. The list endpoint carries no `workspace`, and reading it as if it
    # did is what made every pane open wherever the daemon happened to start;
    # `GET /v1/sessions/{id}` carries the field, along with `kind`,
    # `git_branch`, `sub_agent_name` and the full `pending_elicitations` list.
    #
    # The fetch lives HERE, in the watcher, and not in the bridge above: this
    # module owns every HTTP call, which is what keeps the two independently
    # testable — and an enrichment call made from the bridge's `_added` would
    # raise straight through `reconcile`, which only catches herdr's own
    # HerdrError, and take the whole batch down with it.

    def _fetch_detail(self, session_id: str) -> Optional[dict]:
        """GET /v1/sessions/{id}, or None if it could not be read.

        Returns None for every reason the fetch can fail — a 404, a 500, a
        refused connection, a timeout, a body that is not JSON — because what it
        carries is an ENRICHMENT and never a reason to lose a session. The
        caller falls back to whatever it would have used anyway, which is a
        strictly better outcome than a poll that died because an optional
        request happened to be unhealthy: a watcher that cannot survive a
        failing detail fetch is a worse bridge than one that never asks.

        None now also feeds the runner rule, and there it is read as "unknown",
        which projects — `runner_is_offline` spells out why the direction is
        that way round. Both uses agree: the same fetch that cannot tell us the
        directory must not be able to hide the session either.

        The swallow is deliberately NOT `_BUG_EXCEPTIONS`. A bug in this module
        must still stop the watcher rather than be mistaken for a flaky server,
        and the cheapest way to keep both properties is to scope the catch to
        the fetch alone — nothing of ours is inside these lines, so nothing of
        ours can hide here.
        """
        url = f"{self.base_url}/v1/sessions/{urllib.parse.quote(session_id)}"
        try:
            detail = self._get_json(url)
        except (OSError, ValueError):
            # OSError covers URLError, HTTPError and socket.timeout; ValueError
            # covers JSONDecodeError and UnicodeDecodeError. That is the whole
            # reachable failure surface of one GET.
            return None
        return detail if isinstance(detail, dict) else None

    def _parent_workspace_for(self, parent_id: str) -> Optional[str]:
        """The directory a parent session reported, cached by parent id.

        A fan-out of four workers under one root would otherwise fetch that
        root's detail four times, once per worker, to learn the same string.

        Only a directory that was actually learned is cached. A parent whose
        detail could not be read is re-asked on the next sub-agent rather than
        remembered as "has no directory" — one extra request, and the answer
        self-corrects as soon as the server is healthy again.

        The map is capped at MAX_PARENT_WORKSPACES because its natural key is
        the root set, which only holds while those roots are on screen, and
        this process is meant to run for days. `_seen` already exists and
        already holds one entry per live session; a second unbounded map beside
        it is the kind of thing that only shows up as memory nobody can
        account for.
        """
        cached = self._parent_workspace.get(parent_id)
        if cached:
            return cached
        detail = self._fetch_detail(parent_id)
        if not detail:
            return None
        workspace = detail.get("workspace")
        if not isinstance(workspace, str) or not workspace:
            return None
        if len(self._parent_workspace) >= MAX_PARENT_WORKSPACES:
            # Evict the oldest entry rather than refusing to cache: a stale
            # cache costs one request, an uncached one costs it every time.
            self._parent_workspace.pop(next(iter(self._parent_workspace)))
        self._parent_workspace[parent_id] = workspace
        return workspace

    def _workspace_for(self, session: dict,
                      detail: Optional[dict]) -> Optional[str]:
        """The directory this session belongs in, or None when nobody knows.

        `detail` is the session's OWN detail row, already fetched by `_enrich`;
        it is passed in rather than fetched here so the one request this poll
        spends on a session buys both the directory and the runner answer, and so
        a caller cannot accidentally spend a second.

        Resolution order:

        1. the session's OWN detail row. A root is simply its own directory.
        2. its PARENT's. A sub-agent's detail row carries `workspace: None` —
           measured, not assumed — while the parent's is a real path, and a
           worker of the og session belongs in the og tree. This is the best
           answer the API can give, and it is NOT exact: a delegated worker
           really runs in its own git worktree, which the API does not report
           anywhere. Guessing the root beats opening the pane in whatever
           directory the daemon happened to start in, by a wide margin — but it
           is a considered approximation, so it is written down here rather than
           left to look like a fact about the worker.
        3. None. The consumer then uses its own default, which is the honest
           outcome when neither row answered.
        """
        if detail:
            workspace = detail.get("workspace")
            if isinstance(workspace, str) and workspace:
                return workspace
        parent = session.get("parent_session_id")
        if isinstance(parent, str) and parent:
            return self._parent_workspace_for(parent)
        return None

    def _enrich(self, session: dict) -> tuple[dict, bool]:
        """The session object to emit, and whether it is attachable at all.

        Returns `(enriched, projectable)`. Both answers come from ONE detail
        fetch, which is why this is the place the runner rule lives: the
        directory and `runner_online` are both detail-only fields, so a session
        is fully decided here, on the one request already being made. See
        `runner_is_offline` for the rule and for why an unreadable detail row
        projects rather than hides.

        The same fetch carries the session's IDENTITY — `kind`, `harness` and
        `sub_agent_name` — which the consumer above needs to lay a delegated
        worker out beside its parent. Those are detail-only fields too, so this
        is one request buying five answers, not a second round trip for a label.
        See DETAIL_FIELDS for why these three and not the listing row's
        `agent_name`.

        The directory key is OMITTED when there is no directory to report,
        rather than set to None or "": `session.get("workspace")` on an absent
        key and on an empty one must mean the same thing to the consumer, which
        is "ask me for your default". The caller gets a fresh dict either way,
        so the object in `_seen` stays exactly what the server listed.
        """
        sid = _session_id(session)
        detail = self._fetch_detail(sid) if sid is not None else None
        enriched = dict(session)
        workspace = self._workspace_for(session, detail)
        if workspace:
            enriched["workspace"] = workspace
        if detail:
            for key in DETAIL_FIELDS:
                value = detail.get(key)
                if isinstance(value, str) and value:
                    enriched[key] = value
        return enriched, not runner_is_offline(detail)

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
        resume on their own when the server comes back. Both reports go through
        `_say`, so a stderr that refuses the line costs the message and nothing
        else — the retry below still happens, and the raise above still happens.

        What is deliberately NOT retried is `_BUG_EXCEPTIONS` — see the tuple for
        why those four and only those four. They are re-raised immediately, not
        retried once first: a first attempt cannot make a TypeError go away, and
        paying one backoff to learn that only delays the crash an operator needs
        to see. One stderr line says why the watcher stopped and names the
        exception, because a daemon that exits silently is exactly what this
        loop exists not to be. KeyboardInterrupt and SystemExit are
        BaseException and GeneratorExit closes the generator, so Ctrl-C still
        stops it and none of the three are affected by any of this.

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
            except _BUG_EXCEPTIONS as exc:
                # Raised, not logged and retried: see _BUG_EXCEPTIONS. The line
                # is written first because "poll failed; retrying" would be a
                # lie about why this stopped, and stopping quietly is worse.
                # `_say` cannot raise even when this stderr is gone, so a broken
                # stderr never replaces the bug it is reporting: the TypeError
                # leaving this generator is the useful thing.
                _say(
                    f"og herdr watch: poll raised {_scrub(repr(exc))}, which "
                    "does not come from the transport; this looks like a bug "
                    "in the watcher, so it is stopping rather than retrying\n"
                )
                raise
            except Exception as exc:  # noqa: BLE001 — surviving this IS the job
                # Same guard as the branch above, for the same reason: a stderr
                # that will not take the line (a full disk under
                # `og herdr > log 2>&1`, a closed pipe) must not turn an ordinary
                # transport error into the daemon's death — arriving through the
                # one path whose entire purpose is to prevent that. The retry
                # happens either way; the line is a nicety, the backoff is the
                # behaviour.
                _say(
                    f"og herdr watch: poll failed ({_scrub(repr(exc))}); "
                    f"retrying in {backoff:g}s\n"
                )
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
