#!/usr/bin/env python3
"""`og sessions` — list, hide and delete Omnigent sessions from the terminal.

The web UI can delete a session from a session's "..." menu. This is the same
operation from the shell, because the human who prunes an ever-growing space
list does it from a terminal, not a browser — and because an uncapped space
list is only livable if deleting is easy.

Three surfaces, split by how much they destroy:

  * (no flag)             list the ROOT sessions: id, title, status, runner
                         state, workspace, age. Read-only.
  * --archive / --unarchive
                         `PATCH {"archived": bool}`. Reversible, and not merely
                         gentler: `should_project` already excludes archived
                         sessions, so archiving is what takes a session out of
                         the herdr spaces without destroying anything.
  * --delete              `DELETE /v1/sessions/{id}`. IRREVERSIBLE — the server
                         tears down tasks, runner-side resources (environments,
                         terminals), session files and the conversation row.
                         Confirmation by default; `--yes` for scripts; `--force`
                         to override the safety refusals; `--delete-branch`, and
                         only that flag, opts into git worktree/branch cleanup.

Why a separate verb and not `og chat --delete <id>`: `og chat` starts and
resumes conversations. A mistyped `og chat` must never be able to destroy one,
and the future herdr per-space command needs a verb of its own to call.

API facts, read from a live server's own /openapi.json:

* `DELETE /v1/sessions/{id}` takes `delete_branch` as an OPT-IN query param; the
  worktree and branch are left alone otherwise. With it set and the host
  offline the delete is refused with 409, so a caller can retry without cleanup
  rather than be told 404. Owner-level access is required.
* `PATCH /v1/sessions/{id}` takes `UpdateSessionRequest`, which the server
  applies field-wise (`model_fields_set`) — so `{"archived": true}` touches
  nothing else. Archiving is OWNER-gated. `delete_worktree` is a field on the
  same request; this module never sends it.
* `GET /v1/sessions?kind=any` excludes ARCHIVED rows unless
  `include_archived=true`, and carries NEITHER `workspace` NOR `runner_online` —
  both are detail-only fields (measured: an idle root's detail row says
  `runner_online: false`, its listing row does not have the key at all). So the
  listing alone can neither label a row with its directory nor answer the one
  question a destructive action needs. The detail row is fetched for the roots
  on screen, and the listing is never re-implemented here.

Transport: the listing is `SessionWatcher.fetch_listing`, which owns
pagination, per-server token discovery and truncation handling. The mutations go
through this module's own `_call`, because the watcher is GET-only by contract.
`opener` is the injection seam, spelled exactly as in `SessionWatcher` and
`ProjectFiler`: tests inject a stub and never touch a server.

stdlib only; Python 3.10+.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from og_herdr_watch import (
    DEFAULT_BASE_URL,
    SessionWatcher,
    _session_id,
)

# What a human types to confirm a delete. Deliberately a word rather than the
# session id: 32 hex characters is a paste-or-fight, and the id is already on
# screen above the prompt for the paste case. Lowercased before comparison.
CONFIRM_WORD = "delete"

# A needle at least this long is treated as a whole session id when it matches
# nothing in the listing, so a session that has already fallen off the listing
# (archived, or older than the walk's cap) can still be acted on. This cannot
# delete the wrong thing: the id is sent verbatim, so a mistyped tail is a 404
# from the server, never a match against a different session.
MIN_WHOLE_ID_LEN = 16

# Statuses that mean work is in flight, from the server's own vocabulary
# (`idle`, `launching`, `running`, `waiting`, `failed`). "waiting" counts: it is
# a session holding an elicitation a human has not answered yet.
BUSY_STATUSES = frozenset({"running", "launching", "waiting"})

# What a delete takes with it, in the order it takes it. Printed above the
# prompt: a confirmation for an action whose blast radius is unstated is a
# confirmation of nothing.
DELETE_BLAST_RADIUS = (
    "this destroys the conversation row, its transcript files, its tasks and "
    "its runner-side resources (environments, terminals)"
)


class SessionAdminError(Exception):
    """A refusal or a failure to report — never a traceback.

    Everything this command can get wrong is something the human can act on, so
    every path out of `main` is a printed line, not an exception escaping.
    """


# ---------------------------------------------------------------------------
# the API, behind one seam
# ---------------------------------------------------------------------------

class SessionAdmin:
    """Everything `og sessions` asks of the server, over one injected opener."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 token: Optional[str] = None,
                 opener: Optional[Any] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token if token is not None else SessionWatcher.discover_token(
            self.base_url)
        self.opener = opener or urllib.request.urlopen
        # One watcher, so the listing walk and the detail reads share the token
        # and the transport. `_fetch_detail` is private and used here for the
        # same reason og_agents uses it: it is the detail read that already
        # folds every failure into None, and that "None means unknown" is what
        # the runner rule below is written against.
        self._watcher = SessionWatcher(base_url=self.base_url, token=self.token,
                                       opener=self.opener)

    # -- reads ---------------------------------------------------------------

    def listing(self) -> tuple[list, bool]:
        """(rows, truncated), unfiltered — the caller decides what to show.

        The walk raises on a transport or decode error (it is strict, by
        design: a poll must not mistake a failed read for a deleted session),
        so the refusal is translated HERE. Every action in this command starts
        with a listing, and "the server is not running" is the single most
        likely way to run it — that has to read as one line naming the server,
        not as a traceback out of a module the human did not ask for.

        include_archived is asked for EXPLICITLY rather than left to the
        server's default of false: an archived session is invisible otherwise,
        and one this command cannot see is one it cannot unarchive. Measured on
        a live server: 401 rows by default, 439 with the flag. The archived rows
        cost one query parameter on a request this method makes anyway.
        """
        try:
            return self._watcher.fetch_listing("any", include_archived=True)
        except (OSError, ValueError) as exc:
            raise SessionAdminError(
                f"could not list sessions on {self.base_url}: {exc}") from exc

    def detail(self, session_id: str) -> dict:
        """The session's detail row, or {} when it could not be read."""
        return self._watcher._fetch_detail(session_id) or {}

    # -- writes --------------------------------------------------------------

    def set_archived(self, session_id: str, archived: bool) -> Any:
        url = f"{self.base_url}/v1/sessions/{urllib.parse.quote(session_id)}"
        return self._call(url, "PATCH", {"archived": archived})

    def delete(self, session_id: str, delete_branch: bool = False) -> Any:
        url = f"{self.base_url}/v1/sessions/{urllib.parse.quote(session_id)}"
        if delete_branch:
            # The ONLY place `delete_branch` is ever set, and only from the
            # explicit flag: it is `git worktree remove --force` plus
            # `git branch -D`, run on the user's machine, and it is never a
            # sensible default for "delete this conversation".
            url = f"{url}?delete_branch=true"
        return self._call(url, "DELETE")

    def _call(self, url: str, method: str, payload: Optional[dict] = None) -> Any:
        """One mutating request; JSON document out, or None for an empty body.

        `HTTPError` is caught BEFORE `OSError` because it is a subclass of it,
        and it is the one that carries the server's own reason — a 404 for a
        session that is gone, a 409 for `--delete-branch` with the host
        offline, a 403 for a session this account does not own. Those are
        answers, and they are reported as answers. Everything else on the
        reachable failure surface of one request (URLError, a timeout, a body
        that is not JSON) is reported as "could not reach the server", which is
        what it means.
        """
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with self.opener(request) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            reason = _http_reason(exc)
            raise SessionAdminError(
                f"the server refused {method} (HTTP {exc.code}){reason}") from exc
        except (OSError, ValueError) as exc:
            raise SessionAdminError(
                f"could not reach {self.base_url}: {exc}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8")
                              if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError):
            return None


def _http_reason(exc: urllib.error.HTTPError) -> str:
    """`: <the server's own words>`, or nothing when it said nothing.

    FastAPI puts the human-readable part in `detail`; older shapes use `message`
    or `error`. All three are tried because the point is to relay the reason, not
    to reformat it, and the body is truncated so a stack trace in a JSON blob
    cannot reflow the terminal.
    """
    try:
        body = exc.read()
    except Exception:  # a body that cannot even be read is not a new failure
        return ""
    if not body:
        return ""
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return f": {body.decode('utf-8', 'replace')[:200]}"
    if isinstance(parsed, dict):
        for key in ("detail", "message", "error"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return f": {value[:200]}"
    return ""


# ---------------------------------------------------------------------------
# reading a session
# ---------------------------------------------------------------------------

def is_root(row: Any) -> bool:
    """Whether a listing row is a ROOT session — the conversations a human drives.

    A delegated worker always carries `parent_session_id`, so the parent test is
    the rule; `kind` is checked too because it costs nothing and a sub-agent row
    without a parent (a malformed listing) should still not be presented as a
    conversation.
    """
    if not isinstance(row, dict):
        return False
    parent = row.get("parent_session_id")
    if parent is not None and parent != "":
        return False
    return row.get("kind") != "sub_agent"


@dataclass
class SessionView:
    """One session as this command shows it. Every field has a rendered form."""

    session_id: str
    title: str
    status: str
    runner: str          # "live" / "off" / "?" (the detail row was unreadable)
    workspace: str       # "" when unknown
    created_at: Optional[float]
    archived: bool


def describe(session_id: str, row: dict, detail: dict) -> SessionView:
    """Build a view from a listing row plus its detail row.

    Each field comes from wherever it exists. `title` and `status` are listing
    fields; `workspace` and `runner_online` are detail-only (measured — the
    listing row does not carry them at all), so a session whose detail read
    failed still lists, with `?` for the runner and no directory, rather than
    disappearing.
    """
    title = row.get("title") or detail.get("title")
    status = row.get("status") or detail.get("status")
    online = detail.get("runner_online")
    if online is True:
        runner = "live"
    elif online is False:
        runner = "off"
    else:
        runner = "?"
    workspace = detail.get("workspace")
    created = row.get("created_at", detail.get("created_at"))
    return SessionView(
        session_id=session_id,
        title=title if isinstance(title, str) and title else "(untitled)",
        status=status if isinstance(status, str) and status else "?",
        runner=runner,
        workspace=workspace if isinstance(workspace, str) else "",
        created_at=created if isinstance(created, (int, float)) else None,
        archived=bool(row.get("archived") or detail.get("archived")),
    )


def has_live_runner(row: dict, detail: dict) -> bool:
    """Whether deleting this session right now would kill work in flight.

    Two signals, because each covers the other's blind spot:

    * `runner_online: true` on the detail row — measured against a live server:
      an idle root reports false, a running one true. The precise answer, and
      the reason a finished session (idle, but its host still attached) is
      still deletable, which is the whole point of the command.
    * a BUSY status on the LISTING row, which is always in hand. If the detail
      read failed, `running` must still refuse — an unreadable detail row is
      not permission.

    "waiting" is busy for the same reason "running" is: the session is holding
    an elicitation somebody has not answered yet.
    """
    if isinstance(detail, dict) and detail.get("runner_online") is True:
        return True
    status = row.get("status") or detail.get("status")
    return isinstance(status, str) and status.strip().lower() in BUSY_STATUSES


def current_session_id(env: Any = None) -> Optional[str]:
    """The session this terminal is inside, or None when that cannot be told.

    Omnigent stamps `OMNIGENT_SESSION_ID` into every agent-facing environment
    and it is inherited by every shell that agent starts — measured inside a
    live og worker: `OMNIGENT_SESSION_ID=3a352897…`. `OG_SESSION_ID` is read
    first so a wrapper (a herdr per-space command, a script) can name the
    session outright where Omnigent stamped nothing.

    Nothing else is consulted, deliberately. `OMNIGENT=1` says only that SOME
    session is up the tree, and guessing from the working directory would
    refuse deletions in any directory that merely hosts another live session.
    Unknown stays unknown — the live-runner refusal is what covers the common
    case, since a session you are talking through is running by definition.
    """
    source = os.environ if env is None else env
    for name in ("OG_SESSION_ID", "OMNIGENT_SESSION_ID"):
        value = source.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_session(rows: Iterable[Any], needle: str) -> str:
    """Turn what the human typed into one session id, or raise.

    An exact id resolves to itself. A PREFIX resolves when it is unique, which
    is what makes the eight-character id in the listing usable without a
    scrollback hunt. Anything ambiguous is refused BY NAME rather than resolved
    to a winner, because guessing which of two sessions a human meant is exactly
    the guess that must not be made before a DELETE.

    A long needle that matches nothing is passed through: the id is sent
    verbatim and the server answers 404 if it is wrong, so this stays safe while
    still reaching a session the listing has already dropped.
    """
    matches = []
    for row in rows:
        sid = _session_id(row) if isinstance(row, dict) else None
        if sid and sid.startswith(needle):
            matches.append(sid)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        shown = ", ".join(sorted(matches)[:4])
        raise SessionAdminError(
            f"{needle!r} matches {len(matches)} sessions ({shown}); "
            "give more of the id")
    if len(needle) >= MIN_WHOLE_ID_LEN:
        return needle
    raise SessionAdminError(
        f"no session matches {needle!r}; run 'og sessions' for the ids")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

DEFAULT_WIDTH = 80


def shorten_home(path: str, home: Optional[str] = None) -> str:
    """`/Users/you/projects/og` -> `~/projects/og`.

    Only the plain `$HOME` prefix is collapsed, and only on a path boundary, so
    a directory that merely starts with the same letters (`/Users/youtoo`) is
    left alone. A shortened path is for reading; the full one is printed above
    every confirmation.
    """
    root = os.path.expanduser("~") if home is None else home
    if root and root != "/" and path.startswith(root + os.sep):
        return "~" + path[len(root):]
    return path


def age_since(created_at: Optional[float], now: float) -> str:
    """`12s` / `6m` / `2h` / `9d`, or `?` when the server did not say.

    One unit, the largest that still says something. Past a day the exact hour
    stops mattering — a session nine days old is not a decision about minutes.
    A timestamp in the future (clock skew between the server and this machine)
    clamps to `0s` rather than printing a negative age, which would read as a
    bug in the listing.
    """
    if not isinstance(created_at, (int, float)):
        return "?"
    total = max(0, int(now - created_at))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h"
    return f"{total // 86400}d"


def render_table(views: Iterable[SessionView], *, width: int = DEFAULT_WIDTH,
                 now: Optional[float] = None,
                 current: Optional[str] = None) -> list[str]:
    """The listing as lines. Pure: state in, lines out, nothing printed here.

    Sized for a narrow pane — the columns are fixed and small, the title takes
    whatever is left, and the workspace trails it after a separator. The title
    is what a human prunes by, so it keeps its space when the line overflows.
    A `*` in the left gutter marks the session this terminal is inside.
    """
    stamp = time.time() if now is None else now
    fixed = 1 + 8 + 2 + 7 + 2 + 6 + 2 + 5 + 2   # gutter + id .. age + gaps
    room = max(12, width - fixed)
    lines = [f"  {'ID':<8}  {'STATUS':<7}  {'RUNNER':<6}  {'AGE':<5}  TITLE"]
    for view in views:
        marker = "*" if view.session_id == current else " "
        label = view.title
        if view.workspace:
            label = f"{label}  ·  {shorten_home(view.workspace)}"
        if len(label) > room:
            label = label[:max(1, room - 1)] + "…"
        lines.append(
            f"{marker}{view.session_id[:8]:<8}  {view.status[:7]:<7}  "
            f"{view.runner[:6]:<6}  {age_since(view.created_at, stamp):<5}  "
            f"{label}")
    return lines


# ---------------------------------------------------------------------------
# confirmation
# ---------------------------------------------------------------------------

def confirm(stdin: Any, *, assume_yes: bool = False) -> bool:
    """Whether the human agreed to the delete. False means do not proceed.

    `--yes` is the non-interactive override, and it is the ONLY way to skip this
    — there is no "destructive flag implies yes" shortcut, because that is the
    exact shape that turns a mistyped flag into a lost conversation.

    With no terminal there is nothing to confirm against, so this raises rather
    than reading a line: `echo DELETE | og sessions --delete x` must not be a
    way to delete a session without ever having asked. Ctrl-D at the prompt
    aborts (False, not an error — the human chose to stop).
    """
    if assume_yes:
        return True
    if not stdin.isatty():
        raise SessionAdminError(
            "deleting needs confirmation and this is not a terminal; "
            "pass --yes to delete non-interactively")
    sys.stderr.write(f"type {CONFIRM_WORD} to confirm, anything else to abort: ")
    sys.stderr.flush()
    line = stdin.readline()
    if not line:
        sys.stderr.write("\n")
        return False
    return line.strip().lower() == CONFIRM_WORD


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="og sessions",
        description="List, archive and delete Omnigent sessions.",
        epilog=("Deleting destroys the conversation, its files and its tasks; "
                "archiving is the reversible way to take a session out of the "
                "herdr spaces."),
    )
    parser.add_argument("--server", default=DEFAULT_BASE_URL,
                        help="Omnigent server base URL")
    parser.add_argument("--all", action="store_true", dest="show_archived",
                        help="also list archived sessions (the server hides "
                             "them by default)")
    parser.add_argument("--width", type=int, default=None,
                        help="wrap the listing to this width")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--archive", metavar="ID",
                        help="hide a session (reversible; PATCH, not DELETE)")
    action.add_argument("--unarchive", metavar="ID",
                        help="bring an archived session back")
    action.add_argument("--delete", metavar="ID",
                        help="DESTROY a session and everything attached to it")
    parser.add_argument("--delete-branch", action="store_true",
                        help="with --delete: also remove a server-created git "
                             "worktree and its branch (never implicit)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="skip the confirmation prompt (for scripts)")
    parser.add_argument("--force", action="store_true",
                        help="with --delete: proceed even if the session has a "
                             "live runner or is the one you are inside")
    return parser


def main(argv: Optional[Iterable[str]] = None, *,
         opener: Optional[Any] = None,
         stdin: Any = None,
         env: Any = None,
         now: Optional[float] = None,
         out: Any = None,
         err: Any = None) -> int:
    """`og sessions`. 0 on success, 1 on a refusal or a failure."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    stdin = sys.stdin if stdin is None else stdin
    admin = SessionAdmin(base_url=args.server, opener=opener)

    try:
        if args.delete is not None:
            return _delete(admin, args, stdin=stdin, env=env, now=now, out=out)
        if args.archive is not None or args.unarchive is not None:
            return _archive(admin, args, out=out)
        _require_delete_only(args)
        return _list(admin, args, env=env, now=now, out=out, err=err)
    except SessionAdminError as exc:
        err.write(f"og sessions: {exc}\n")
        return 1


def _require_delete_only(args: argparse.Namespace) -> None:
    """Refuse flags that only mean something beside --delete.

    `--delete-branch` without `--delete` is the dangerous one: it looks like it
    did something to a session and did not. Naming the combination is more
    useful than silently ignoring either flag.
    """
    for flag, value in (("--delete-branch", args.delete_branch),
                        ("--yes", args.yes),
                        ("--force", args.force)):
        if value and args.delete is None:
            raise SessionAdminError(f"{flag} only applies to --delete")


def _list(admin: SessionAdmin, args: argparse.Namespace, *, env: Any,
          now: Optional[float], out: Any, err: Any) -> int:
    rows, truncated = admin.listing()
    roots = [row for row in rows if is_root(row)]
    archived = sum(1 for row in roots if row.get("archived"))
    shown = [row for row in roots
             if args.show_archived or not row.get("archived")]

    current = current_session_id(env)
    views = []
    for row in shown:
        sid = _session_id(row)
        if not sid:
            continue
        views.append(describe(sid, row, admin.detail(sid)))

    width = args.width or shutil.get_terminal_size(
        (DEFAULT_WIDTH, 24)).columns
    if not views:
        out.write(f"no sessions on {admin.base_url}\n")
    else:
        for line in render_table(views, width=width, now=now, current=current):
            out.write(line + "\n")

    # Notices on stderr so the table itself stays pipeable.
    if truncated:
        err.write("og sessions: the listing was truncated; some sessions are "
                  "not shown\n")
    if archived and not args.show_archived:
        err.write(f"og sessions: {archived} archived session(s) hidden — "
                  "pass --all to show them\n")
    if current and any(v.session_id == current for v in views):
        err.write("og sessions: * marks the session this terminal is inside\n")
    return 0


def _archive(admin: SessionAdmin, args: argparse.Namespace, *, out: Any) -> int:
    archived = args.archive is not None
    needle = args.archive if archived else args.unarchive
    rows, _truncated = admin.listing()
    session_id = resolve_session(rows, needle)
    view = describe(session_id, _row_for(rows, session_id), admin.detail(session_id))
    admin.set_archived(session_id, archived)
    verb = "archived" if archived else "unarchived"
    out.write(f"{verb} {session_id} ({view.title})\n")
    if archived:
        out.write("    reversible: 'og sessions --unarchive "
                  f"{session_id}' brings it back, and it is hidden from the "
                  "herdr spaces until then\n")
    return 0


def _delete(admin: SessionAdmin, args: argparse.Namespace, *, stdin: Any,
            env: Any, now: Optional[float], out: Any) -> int:
    rows, _truncated = admin.listing()
    session_id = resolve_session(rows, args.delete)
    row = _row_for(rows, session_id)
    detail = admin.detail(session_id)
    view = describe(session_id, row, detail)

    if not args.force:
        current = current_session_id(env)
        if current and current == session_id:
            raise SessionAdminError(
                f"refusing to delete {session_id[:8]}: it is the session this "
                "terminal is inside. Deleting it ends the conversation you are "
                "talking through. Pass --force if that is what you meant")
        if has_live_runner(row, detail):
            status = (row.get("status") or detail.get("status") or "busy")
            raise SessionAdminError(
                f"refusing to delete {session_id[:8]}: it has a live runner "
                f"(status {status}). Deleting it now would kill work in "
                "flight. Stop it first, or pass --force")

    out.write(f"about to DELETE session {session_id}\n")
    out.write(f"    title      {view.title}\n")
    if view.workspace:
        out.write(f"    workspace  {view.workspace}\n")
    out.write(f"    status     {view.status}, runner {view.runner}"
              f", created {age_since(view.created_at, time.time() if now is None else now)} ago\n")
    out.write(f"    {DELETE_BLAST_RADIUS}\n")
    out.write("    git cleanup (worktree + branch): "
              + ("REQUESTED (--delete-branch)\n" if args.delete_branch
                 else "not requested — pass --delete-branch to opt in\n"))

    if not confirm(stdin, assume_yes=args.yes):
        out.write("aborted; nothing was deleted\n")
        return 1

    admin.delete(session_id, delete_branch=args.delete_branch)
    out.write(f"deleted {session_id}\n")
    return 0


def _row_for(rows: Iterable[dict], session_id: str) -> dict:
    """The listing row for one id, or {} when the listing does not have it."""
    for row in rows:
        if isinstance(row, dict) and _session_id(row) == session_id:
            return row
    return {}


if __name__ == "__main__":
    raise SystemExit(main())