"""Unit tests for installer/og_sessions.py.

No network, ever: every test injects a stub opener (see `RecordingOpener`), so
nothing here can touch the operator's live server on 127.0.0.1:6767 — and no
test issues a real DELETE. The two properties this file exists to pin are that
`og sessions` CAN destroy a session and that it does so only when a human said
so: `--delete` without confirmation must reach no DELETE at all, `delete_branch`
must be absent unless it was asked for by name, and archiving must be a PATCH.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

import og_sessions as s

SERVER = "http://127.0.0.1:6767"
NOW = 1_700_000_000


# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    """Minimal stand-in for an http.client.HTTPResponse: read() + context mgr."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class RecordingOpener:
    """A stub that answers by path and records every request.

    `listing` is a queue of bodies for `/v1/sessions` (the last repeats); `rows`
    maps a session id to its detail body, or to an exception to raise for that
    id alone. `fail` is an exception raised instead of answering — for every
    method, or for `fail_methods` alone, which is how the two failure points
    are covered separately (the listing walk and the mutation). Anything
    unmapped RAISES, so a request the test did not expect fails loudly instead
    of looking like an empty server.
    """

    def __init__(self, listing=None, details=None, fail=None, fail_methods=()):
        self.listing = list(listing or [{"data": [], "has_more": False}])
        self.details = dict(details or {})
        self.fail = fail
        self.fail_methods = set(fail_methods)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.fail is not None and (not self.fail_methods
                                      or request.method in self.fail_methods):
            raise self.fail
        parts = urllib.parse.urlsplit(request.full_url)
        path = parts.path
        if path == "/v1/sessions":
            body = self.listing.pop(0) if len(self.listing) > 1 else self.listing[0]
        elif request.method in ("PATCH", "DELETE"):
            body = {"id": urllib.parse.unquote(path.rsplit("/", 1)[-1]),
                    "deleted": True, "object": "conversation.deleted"}
        else:
            sid = urllib.parse.unquote(path[len("/v1/sessions/"):])
            assert sid in self.details, f"unexpected detail lookup for {sid!r}"
            body = self.details[sid]
        if isinstance(body, BaseException):
            raise body
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return FakeResponse(body)

    # -- what was asked for --------------------------------------------------

    def by_method(self, method):
        return [r for r in self.requests if r.method == method]

    @property
    def deletes(self):
        return self.by_method("DELETE")

    @property
    def patches(self):
        return self.by_method("PATCH")

    def listing_query(self):
        for request in self.requests:
            if request.method == "GET":
                return urllib.parse.parse_qs(
                    urllib.parse.urlsplit(request.full_url).query)
        return {}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def listed(sid, parent=None, status="idle", title=None, archived=False,
           **extra):
    """A session row shaped like the live listing's (which has no `workspace`)."""
    row = {"id": sid, "agent_id": "ag_1", "agent_name": "dev-lead",
           "archived": archived, "created_at": NOW - 7200,
           "labels": [], "owner": "cryogenix", "parent_session_id": parent,
           "pending_elicitations_count": 0, "permission_level": "owner",
           "runner_id": "rnr_1", "status": status, "updated_at": NOW}
    if title is not None:
        row["title"] = title
    row.update(extra)
    return row


def detail_of(workspace="/Users/cryogenix/projects/og", runner_online=False,
              title=None, status="idle", **extra):
    """A detail row shaped like the live one: `workspace` and `runner_online`."""
    body = {"id": "x", "workspace": workspace, "runner_online": runner_online,
            "host_online": True, "status": status, "harness": "opencode-native"}
    if title is not None:
        body["title"] = title
    body.update(extra)
    return body


class ScriptedStdin:
    """A stdin double: a tty (or not) plus a scripted answer."""

    def __init__(self, answer="", tty=True):
        self.answer = answer
        self._tty = tty

    def isatty(self):
        return self._tty

    def readline(self):
        line, self.answer = self.answer, ""
        return line


def run(argv, opener, *, stdin=None, env=None, now=NOW):
    """`main` with every seam injected; returns (rc, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    rc = s.main(argv, opener=opener, stdin=stdin or ScriptedStdin(),
                env={} if env is None else env, now=now, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# the listing
# ---------------------------------------------------------------------------

def test_listing_renders_id_title_status_runner_workspace_and_age(
        monkeypatch, tmp_path):
    # The listing collapses a leading `$HOME` to `~`, and `shorten_home` reads
    # it through `os.path.expanduser`, i.e. the process's HOME. Pinning the
    # fixture at the developer's real home passes only there: in CI, HOME is
    # /home/runner, nothing collapses, and the assertion fails on a raw path.
    # So own the home, and DERIVE the workspace beneath it so the path and the
    # prefix it must collapse cannot drift apart.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    sid = "3a3528974420426eb7d723df2bb0488d"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="Fix issue 502")],
                  "has_more": False}],
        details={sid: detail_of(workspace=str(home / "projects" / "og"))},
    )

    rc, out, _err = run([], opener)

    assert rc == 0
    assert "3a352897" in out                       # the id, short
    assert "Fix issue 502" in out                  # Omnigent's own title
    assert "idle" in out
    assert "off" in out                            # runner state
    assert "~/projects/og" in out                  # workspace, home collapsed
    assert "2h" in out                             # age
    assert not opener.by_method("DELETE")          # a listing deletes nothing


def test_listing_excludes_sub_agents_and_keeps_roots():
    root = "a" * 32
    worker = "b" * 32
    opener = RecordingOpener(
        listing=[{"data": [listed(root, title="root"),
                           listed(worker, parent=root, title="coder:task")],
                  "has_more": False}],
        details={root: detail_of()},
    )

    _rc, out, _err = run([], opener)

    assert "aaaa" in out
    assert "coder:task" not in out


def test_listing_falls_back_when_the_detail_row_is_unreadable():
    """A session whose detail read failed still lists.

    `workspace` and `runner_online` are detail-only fields (the listing row does
    not carry them at all), so a session that vanished between the two reads must
    degrade to `?` rather than disappear from the list a human prunes from.
    """
    sid = "c" * 32
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="still here")], "has_more": False}],
        details={sid: urllib.error.URLError("connection reset")},
    )

    rc, out, _err = run([], opener)

    assert rc == 0
    assert "still here" in out
    assert "?" in out


def test_archived_sessions_are_hidden_unless_all_is_given():
    """The server hides archived rows by default, so the walk must ask for them.

    Measured on a live server: 401 rows by default, 439 with
    `include_archived=true`. Without the flag an archived session could never be
    unarchived from here, and `--archive` would be a one-way door.
    """
    live, hidden = "d" * 32, "e" * 32
    opener = RecordingOpener(
        listing=[{"data": [listed(live, title="visible"),
                           listed(hidden, title="hidden one", archived=True)],
                  "has_more": False}],
        details={live: detail_of(), hidden: detail_of()},
    )

    _rc, out, err = run([], opener)
    assert "visible" in out
    assert "hidden one" not in out
    assert "1 archived" in err
    assert opener.listing_query().get("include_archived") == ["true"]

    _rc, out, err = run(["--all"], opener)
    assert "hidden one" in out
    assert "archived" not in err


def test_listing_warns_when_the_walk_was_truncated():
    """A short listing must say so: the absent sessions are NOT known gone."""
    sid = "f" * 32
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="page one")],
                  "has_more": True}],          # …and no cursor to continue with
        details={sid: detail_of()},
    )

    _rc, _out, err = run([], opener)

    assert "truncated" in err


def test_listing_marks_the_session_this_terminal_is_inside():
    sid = "9" * 32
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="this one")], "has_more": False}],
        details={sid: detail_of(runner_online=True)},
    )

    _rc, out, err = run([], opener,
                        env={"OMNIGENT_SESSION_ID": sid})

    assert "*99999999" in out
    assert "inside" in err


# ---------------------------------------------------------------------------
# deleting: the gate
# ---------------------------------------------------------------------------

def test_delete_without_confirmation_never_reaches_the_server():
    """No tty and no --yes: the DELETE must not be sent at all."""
    sid = "1" * 32
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="Fix issue 502")],
                  "has_more": False}],
        details={sid: detail_of()},
    )

    rc, out, err = run(["--delete", sid], opener,
                       stdin=ScriptedStdin(tty=False))

    assert rc == 1
    assert opener.deletes == []
    assert "not a terminal" in err
    # What it would have destroyed is still printed — the refusal is informed.
    assert "Fix issue 502" in out
    assert "conversation row" in out


def test_delete_aborts_when_the_confirmation_is_not_the_word():
    sid = "1" * 32
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="Fix issue 502")],
                  "has_more": False}],
        details={sid: detail_of()},
    )

    rc, out, _err = run(["--delete", sid], opener,
                        stdin=ScriptedStdin("yes\n"))

    assert rc == 1
    assert opener.deletes == []
    assert "aborted" in out


def test_delete_with_confirmation_deletes_exactly_once():
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="Fix issue 502")],
                  "has_more": False}],
        details={sid: detail_of()},
    )

    rc, out, _err = run(["--delete", sid], opener,
                        stdin=ScriptedStdin("DELETE\n"))

    assert rc == 0
    assert len(opener.deletes) == 1
    assert opener.deletes[0].full_url == f"{SERVER}/v1/sessions/{sid}"
    assert "deleted" in out


def test_yes_is_the_non_interactive_override():
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="scripted")], "has_more": False}],
        details={sid: detail_of()},
    )

    rc, _out, _err = run(["--delete", sid, "--yes"], opener,
                         stdin=ScriptedStdin(tty=False))

    assert rc == 0
    assert len(opener.deletes) == 1


def test_eof_at_the_prompt_aborts_rather_than_confirming():
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="ctrl-d")], "has_more": False}],
        details={sid: detail_of()},
    )

    rc, _out, _err = run(["--delete", sid], opener,
                         stdin=ScriptedStdin("", tty=True))

    assert rc == 1
    assert opener.deletes == []


# ---------------------------------------------------------------------------
# deleting: git cleanup is never implicit
# ---------------------------------------------------------------------------

def test_delete_branch_is_absent_unless_the_flag_is_given():
    """`git worktree remove --force` + `git branch -D` is opt-in, always."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="plain delete")],
                  "has_more": False}],
        details={sid: detail_of()},
    )

    _rc, out, _err = run(["--delete", sid, "--yes"], opener,
                         stdin=ScriptedStdin(tty=False))

    url = opener.deletes[0].full_url
    assert "delete_branch" not in url
    assert urllib.parse.urlsplit(url).query == ""
    assert "not requested" in out


def test_delete_branch_sets_the_query_param_when_asked_for():
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="with git")], "has_more": False}],
        details={sid: detail_of()},
    )

    _rc, out, _err = run(["--delete", sid, "--yes", "--delete-branch"], opener,
                         stdin=ScriptedStdin(tty=False))

    url = opener.deletes[0].full_url
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) == \
        {"delete_branch": ["true"]}
    assert "REQUESTED" in out


def test_delete_branch_without_delete_is_refused_not_ignored():
    """`--delete-branch` on its own looks like it did something. It must not."""
    opener = RecordingOpener()

    rc, _out, err = run(["--delete-branch"], opener)

    assert rc == 1
    assert "only applies to --delete" in err
    assert opener.requests == []       # not even the listing was fetched


def test_yes_without_delete_is_refused():
    opener = RecordingOpener()

    rc, _out, err = run(["--yes"], opener)

    assert rc == 1
    assert "only applies to --delete" in err


# ---------------------------------------------------------------------------
# deleting: the safety refusals
# ---------------------------------------------------------------------------

def test_delete_refuses_a_session_with_a_live_runner():
    """The web UI has no such guard, because a browser tab is not a live pane."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="busy", status="running")],
                  "has_more": False}],
        details={sid: detail_of(runner_online=True, status="running")},
    )

    rc, _out, err = run(["--delete", sid, "--yes"], opener,
                        stdin=ScriptedStdin(tty=False))

    assert rc == 1
    assert opener.deletes == []
    assert "live runner" in err
    assert "--force" in err


def test_delete_refuses_on_a_busy_status_even_when_the_detail_read_failed():
    """An unreadable detail row is not permission: the LISTING still says
    `running`, and that has to be enough to refuse."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, status="running")], "has_more": False}],
        details={sid: urllib.error.URLError("connection reset")},
    )

    rc, _out, err = run(["--delete", sid, "--yes"], opener,
                        stdin=ScriptedStdin(tty=False))

    assert rc == 1
    assert opener.deletes == []
    assert "live runner" in err


def test_force_overrides_the_live_runner_refusal():
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, status="running")], "has_more": False}],
        details={sid: detail_of(runner_online=True, status="running")},
    )

    rc, _out, _err = run(["--delete", sid, "--yes", "--force"], opener,
                         stdin=ScriptedStdin(tty=False))

    assert rc == 0
    assert len(opener.deletes) == 1


def test_delete_refuses_the_session_this_terminal_is_inside():
    """Deleting the conversation you are talking through is a foot-gun the web
    UI does not have: there, the tab you are reading is not the session."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="ours")], "has_more": False}],
        details={sid: detail_of(runner_online=False)},
    )

    rc, _out, err = run(["--delete", sid, "--yes"], opener,
                        stdin=ScriptedStdin(tty=False),
                        env={"OMNIGENT_SESSION_ID": sid})

    assert rc == 1
    assert opener.deletes == []
    assert "this terminal is inside" in err


def test_force_overrides_the_current_session_refusal():
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid)], "has_more": False}],
        details={sid: detail_of()},
    )

    rc, _out, _err = run(["--delete", sid, "--yes", "--force"], opener,
                         stdin=ScriptedStdin(tty=False),
                         env={"OG_SESSION_ID": sid})

    assert rc == 0
    assert len(opener.deletes) == 1


def test_an_idle_session_with_an_attached_runner_is_still_deletable():
    """The pruning case: a session you finished five minutes ago, host still
    up. Refusing here would make the command useless."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, status="idle")], "has_more": False}],
        details={sid: detail_of(runner_online=False, status="idle")},
    )

    rc, _out, _err = run(["--delete", sid, "--yes"], opener,
                         stdin=ScriptedStdin(tty=False))

    assert rc == 0
    assert len(opener.deletes) == 1


def test_the_current_session_guard_is_inert_when_no_marker_is_set():
    """What the guard does when it cannot know which session this terminal is
    inside: nothing at all. Pinned rather than left implicit, because the
    absence is a property of the guard and not an accident — the alternative
    (guessing from the cwd) would refuse deletions in any directory that merely
    hosts another live session. What still stands between the operator and a
    delete is the typed confirmation."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="ours, unmarked")],
                  "has_more": False}],
        details={sid: detail_of(runner_online=False)},
    )

    rc, out, err = run(["--delete", sid], opener, stdin=ScriptedStdin("DELETE\n"))

    assert rc == 0
    assert len(opener.deletes) == 1
    assert "this terminal is inside" not in err
    assert "ours, unmarked" in out           # and the delete was an informed one


def test_a_stale_session_marker_does_not_refuse_the_delete():
    """A marker naming a session that is no longer this one must not refuse: the
    guard compares for EQUALITY, so a stale value is inert — the operator is
    not inside `0000…`, and refusing here would be the guard costing a delete
    for a string left over in a wrapper's environment."""
    sid = "1234567890abcdef1234567890abcdef"
    stale = "0" * 32
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="a different session")],
                  "has_more": False}],
        details={sid: detail_of(runner_online=False)},
    )

    rc, _out, err = run(["--delete", sid, "--yes"], opener,
                        stdin=ScriptedStdin(tty=False),
                        env={"OG_SESSION_ID": stale,
                             "OMNIGENT_SESSION_ID": stale})

    assert rc == 0
    assert len(opener.deletes) == 1
    assert "this terminal is inside" not in err


def test_a_blank_session_marker_counts_as_absent():
    """A wrapper that exports an empty string is not a session. `current_session_id`
    skips blanks and falls through to the next name, and with both blank the guard
    stands down — so a blank must not accidentally match anything either."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="blank markers")],
                  "has_more": False}],
        details={sid: detail_of(runner_online=False)},
    )

    rc, _out, err = run(["--delete", sid, "--yes"], opener,
                        stdin=ScriptedStdin(tty=False),
                        env={"OG_SESSION_ID": "   ",
                             "OMNIGENT_SESSION_ID": ""})

    assert rc == 0
    assert len(opener.deletes) == 1
    assert "this terminal is inside" not in err


# ---------------------------------------------------------------------------
# archiving is a PATCH
# ---------------------------------------------------------------------------

def test_archive_is_a_patch_and_never_a_delete():
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="hide me")], "has_more": False}],
        details={sid: detail_of()},
    )

    rc, out, _err = run(["--archive", sid], opener)

    assert rc == 0
    assert opener.deletes == []
    assert len(opener.patches) == 1
    assert opener.patches[0].full_url == f"{SERVER}/v1/sessions/{sid}"
    assert json.loads(opener.patches[0].data.decode()) == {"archived": True}
    # `delete_worktree` rides the same request model; it must never be sent.
    assert "delete_worktree" not in opener.patches[0].data.decode()
    assert "archived" in out


def test_unarchive_patches_archived_false():
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="back", archived=True)],
                  "has_more": False}],
        details={sid: detail_of()},
    )

    rc, _out, _err = run(["--unarchive", sid], opener)

    assert rc == 0
    assert opener.deletes == []
    assert json.loads(opener.patches[0].data.decode()) == {"archived": False}


# ---------------------------------------------------------------------------
# resolving what the human typed
# ---------------------------------------------------------------------------

def test_an_eight_character_prefix_resolves_against_the_listing():
    """The listing shows 8 characters; that has to be enough to act on."""
    sid = "abcdef0123456789abcdef0123456789"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid, title="short id")], "has_more": False}],
        details={sid: detail_of()},
    )

    rc, _out, _err = run(["--delete", "abcdef01", "--yes"], opener,
                         stdin=ScriptedStdin(tty=False))

    assert rc == 0
    assert opener.deletes[0].full_url == f"{SERVER}/v1/sessions/{sid}"


def test_an_ambiguous_prefix_is_refused_by_name():
    first = "abc111111111111111111111111111111"
    second = "abc222222222222222222222222222222"
    opener = RecordingOpener(
        listing=[{"data": [listed(first), listed(second)], "has_more": False}],
        details={first: detail_of(), second: detail_of()},
    )

    rc, _out, err = run(["--delete", "abc", "--yes"], opener,
                        stdin=ScriptedStdin(tty=False))

    assert rc == 1
    assert opener.deletes == []
    assert "matches 2 sessions" in err


def test_an_unknown_short_id_sends_nothing():
    opener = RecordingOpener(
        listing=[{"data": [listed("a" * 32)], "has_more": False}],
        details={"a" * 32: detail_of()},
    )

    rc, _out, err = run(["--delete", "zzz"], opener)

    assert rc == 1
    assert opener.deletes == []
    assert "no session matches" in err


def test_a_full_id_the_listing_does_not_have_still_reaches_the_server():
    """A session that already fell off the walk (archived, older than the cap)
    can still be deleted: the id goes out verbatim and the server 404s or not."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(listing=[{"data": [], "has_more": False}],
                             details={sid: detail_of()})

    rc, _out, _err = run(["--delete", sid, "--yes"], opener,
                         stdin=ScriptedStdin(tty=False))

    assert rc == 0
    assert len(opener.deletes) == 1


# ---------------------------------------------------------------------------
# a truncated listing cannot resolve a prefix
# ---------------------------------------------------------------------------

def truncated_walk(rows):
    """The pages of a listing the walk cannot finish: page one carries `rows`,
    page two promises more and hands back no cursor to continue with. That is the
    exact shape that sets `fetch_listing`'s `truncated` — and it yields `rows`
    and NOTHING after them, which is the point: the walk stopped, so any further
    match the operator has in mind is a row nobody here can see."""
    return [{"data": list(rows), "has_more": True},
            {"data": [], "has_more": True}]


def test_a_truncated_listing_refuses_a_prefix_and_sends_no_delete():
    """THE regression. `has_more` with no cursor means rows were never fetched,
    and those rows are indistinguishable from the rows of a smaller server. So
    "exactly one match" here means only "exactly one match AMONG THE ROWS WE
    GOT" — and a delete built on that destroys a session the operator never
    named. Nothing may be sent."""
    fetched = "abcdef0123456789abcdef0123456789"
    opener = RecordingOpener(
        listing=truncated_walk([listed(fetched, title="the one we saw")]),
        details={fetched: detail_of()},
    )

    rc, _out, err = run(["--delete", "abcdef01", "--yes"], opener,
                        stdin=ScriptedStdin(tty=False))

    assert rc == 1
    assert opener.deletes == []
    # The walk really did stop early — it asked for a second page — so this is
    # the truncated branch and not a stale fixture that merely looks like it.
    assert len(opener.by_method("GET")) == 2
    assert "TRUNCATED" in err
    # …and it says what to do instead, because a refusal without a way forward
    # is just a wall.
    assert "full session id" in err


def test_force_does_not_buy_a_pass_on_a_truncated_listing():
    """`--force` covers the live runner and the session you are inside — two
    facts about the session. It is not a statement about the listing being
    complete, and letting it through here reopens the hole exactly."""
    sid = "abcdef0123456789abcdef0123456789"
    opener = RecordingOpener(
        listing=truncated_walk([listed(sid, title="busy", status="running")]),
        details={sid: detail_of(status="running", runner_online=True)},
    )

    rc, _out, _err = run(["--delete", "abcdef01", "--yes", "--force"], opener,
                         stdin=ScriptedStdin(tty=False))

    assert rc == 1
    assert opener.deletes == []


def test_an_exact_id_still_resolves_on_a_truncated_listing():
    """Ids are unique, so a full id cannot be shadowed by a row that was never
    fetched — and that is the escape hatch the refusal points at."""
    sid = "abcdef0123456789abcdef0123456789"
    opener = RecordingOpener(
        listing=truncated_walk([listed(sid, title="on page one")]),
        details={sid: detail_of()},
    )

    rc, _out, _err = run(["--delete", sid, "--yes"], opener,
                         stdin=ScriptedStdin(tty=False))

    assert rc == 0
    assert opener.deletes[0].full_url == f"{SERVER}/v1/sessions/{sid}"
    # No caveat is owed here: the session's own listing row came back, and the
    # id matched itself rather than a prefix.


def test_a_full_id_the_truncated_walk_never_fetched_still_reaches_the_server():
    """The second half of the escape hatch: the id is sent verbatim, so a
    session beyond the cap is reachable without ever proving anything about the
    rows behind it."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=truncated_walk([listed("a" * 32, title="page one")]),
        details={sid: detail_of()},
    )

    rc, _out, err = run(["--delete", sid, "--yes"], opener,
                        stdin=ScriptedStdin(tty=False))

    assert rc == 0
    assert opener.deletes[0].full_url == f"{SERVER}/v1/sessions/{sid}"
    assert "truncated" in err


def test_a_truncated_listing_still_refuses_a_prefix_it_can_see_is_ambiguous():
    """Two fetched matches are a fact, not a guess: that refusal must survive,
    or the truncation branch would hide the sharper one behind a vaguer line."""
    first = "abc111111111111111111111111111111"
    second = "abc222222222222222222222222222222"
    opener = RecordingOpener(
        listing=truncated_walk([listed(first), listed(second)]),
        details={first: detail_of(), second: detail_of()},
    )

    rc, _out, err = run(["--delete", "abc", "--yes"], opener,
                        stdin=ScriptedStdin(tty=False))

    assert rc == 1
    assert opener.deletes == []
    assert "matches 2 sessions" in err


def test_archive_on_a_truncated_listing_warns_and_still_patches():
    """The proportionate half. A PATCH flips one boolean and the server keeps
    every byte, and `--archive` undoes it by name — so refusing would be
    protecting a verb whose worst outcome is a re-run. It warns instead, naming
    the id it acted on and the fact that the walk could not see everything."""
    sid = "abcdef0123456789abcdef0123456789"
    opener = RecordingOpener(
        listing=truncated_walk([listed(sid, title="hide me")]),
        details={sid: detail_of()},
    )

    rc, _out, err = run(["--archive", "abcdef01"], opener)

    assert rc == 0
    assert opener.deletes == []
    assert len(opener.patches) == 1
    assert opener.patches[0].full_url == f"{SERVER}/v1/sessions/{sid}"
    assert "truncated" in err
    assert sid[:8] in err
    assert "--unarchive" in err            # and how to put it back


def test_unarchive_on_a_truncated_listing_names_archive_as_the_way_back():
    """The warning's undo flag has to follow the direction that was used."""
    sid = "abcdef0123456789abcdef0123456789"
    opener = RecordingOpener(
        listing=truncated_walk([listed(sid, archived=True, title="back")]),
        details={sid: detail_of()},
    )

    rc, _out, err = run(["--unarchive", "abcdef01"], opener)

    assert rc == 0
    assert len(opener.patches) == 1
    assert "--archive" in err


# ---------------------------------------------------------------------------
# failures are reported, not raised
# ---------------------------------------------------------------------------

def test_a_transport_failure_on_the_listing_reports_cleanly():
    """The likeliest way to run this: the server is not there.

    The walk is strict by design (a failed read must not look like a deleted
    session), so this is the one place its exception has to be translated.
    """
    opener = RecordingOpener(fail=urllib.error.URLError("Connection refused"))

    rc, _out, err = run([], opener)

    assert rc == 1
    assert "could not list sessions" in err
    assert "Traceback" not in err
    assert "og sessions:" in err


def test_a_transport_failure_on_the_delete_reports_cleanly():
    """A DELETE that never landed is the one failure a human MUST see loudly —
    as a line, not a traceback, and not as a success."""
    sid = "1234567890abcdef1234567890abcdef"
    opener = RecordingOpener(
        listing=[{"data": [listed(sid)], "has_more": False}],
        details={sid: detail_of()},
        fail=urllib.error.URLError("Connection reset by peer"),
        fail_methods={"DELETE"},
    )

    rc, out, err = run(["--delete", sid, "--yes"], opener,
                       stdin=ScriptedStdin(tty=False))

    assert rc == 1
    assert len(opener.deletes) == 1          # attempted…
    assert "deleted" not in out               # …and not reported as done
    assert "could not reach" in err
    assert "Traceback" not in err


def test_a_server_refusal_relays_the_servers_own_reason():
    """A 409 (delete_branch with the host offline) or a 403 must arrive as the
    server worded it, not as "HTTP Error 409: Conflict"."""
    sid = "1234567890abcdef1234567890abcdef"
    body = json.dumps({"detail": "host is offline; retry without cleanup"}).encode()
    opener = RecordingOpener(
        listing=[{"data": [listed(sid)], "has_more": False}],
        details={sid: detail_of()},
        fail=urllib.error.HTTPError(f"{SERVER}/v1/sessions/{sid}", 409,
                                    "Conflict", {}, io.BytesIO(body)),
        fail_methods={"DELETE"},
    )

    rc, _out, err = run(["--delete", sid, "--yes", "--delete-branch"], opener,
                        stdin=ScriptedStdin(tty=False))

    assert rc == 1
    assert "HTTP 409" in err
    assert "host is offline" in err
    assert "Traceback" not in err


def test_two_actions_at_once_are_refused_by_argparse():
    """Archive and delete are mutually exclusive — a half-applied pair of the
    two is not a state worth having."""
    opener = RecordingOpener()

    with pytest.raises(SystemExit) as excinfo:
        run(["--archive", "a" * 32, "--delete", "b" * 32], opener)

    assert excinfo.value.code == 2
    assert opener.requests == []


def test_a_bare_listing_answers_that_nothing_was_deleted():
    opener = RecordingOpener()

    rc, out, _err = run([], opener)

    assert rc == 0
    assert "no sessions" in out
    assert opener.deletes == []


# ---------------------------------------------------------------------------
# the dispatch
# ---------------------------------------------------------------------------

OG = Path(__file__).resolve().parent.parent / "bin" / "og"


def test_og_help_lists_the_sessions_command(tmp_path):
    """The help block is a fixed `sed` range over bin/og's comment header, and
    the range has rotted once already (og version went missing)."""
    import re
    import subprocess

    result = subprocess.run(
        ["bash", str(OG), "help"], capture_output=True, text=True, timeout=30,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert re.search(r"^\s*og sessions\b", result.stdout, re.M)


def test_bin_og_dispatches_sessions_to_og_sessions_py():
    """`og sessions` must reach this module, not a shell built-in of the same
    name: the dispatch arm and the script path are the two halves of the
    contract, and the help guard above only proves the name is documented."""
    import re

    text = OG.read_text()
    case = re.search(r'\ncase "\$\{1:-\}" in\n(.*?)\nesac\n', text, re.S)
    assert case and re.search(r"^  sessions\)", case.group(1), re.M)
    assert "og_sessions.py" in text