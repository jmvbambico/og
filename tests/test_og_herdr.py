"""Unit tests for installer/og_herdr.py.

The sibling modules the bridge talks to (og_herdr_client, og_herdr_watch) live
on other branches and are ABSENT here, so every test injects fakes: a recording
fake client, a fake watcher, and monkeypatched lazy seams. Nothing here opens a
socket, touches ~/.config/herdr/herdr.sock, HTTPs to the Omnigent server, or
runs a real `herdr` / `omnigent` command.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import shlex
import threading
from pathlib import Path

import pytest

import og_herdr as m


class FakeHerdrError(Exception):
    def __init__(self, code="boom", message="boom"):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass
class Ev:
    """Stands in for og_herdr_watch.SessionEvent."""
    kind: str
    session_id: str
    session: dict
    previous: dict = dataclasses.field(default_factory=dict)


class RecordingClient:
    """Records every call. `workspace_create` and `tab_create` share one counter
    so the ids a test sees read like a real session's layout."""

    def __init__(self, fail=None, create_results=None, spaces=None):
        self.calls = []
        self.fail = fail or {}
        # Failures that do NOT fire once: a herdr that is still broken when the
        # bridge retries, which is the case a one-shot `fail` cannot express.
        self.fail_always = {}
        # Queued create returns, so a test can hand back a malformed reply
        # (missing ids) that the default well-formed shape never produces.
        self.create_results = list(create_results) if create_results else []
        # What workspace_list reports, for the operator's own workspaces.
        self.spaces = list(spaces) if spaces else []
        # Workspaces THIS fake created and has not closed, which herdr would
        # also list. Without this the fake answers "no workspaces" about a space
        # it just made, and the bridge's re-validation would — correctly — read
        # that as "your space is gone" and re-project into a second one.
        self._live: dict[str, dict] = {}
        # Every pane herdr would list, so the agents-pane split's new pane can be
        # found by DIFFERENCE against the tab (`pane.split` names no pane, so the
        # shared split falls back to `pane.list`). Seeded by the creates below.
        self._panes: list[dict] = []
        self._seq = 0

    def _call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        # One-shot: a queued failure fires once, so a later event's call to the
        # same method succeeds — which is exactly what "does not abort the loop"
        # is meant to demonstrate.
        exc = self.fail.pop(name, None)
        if exc is None:
            exc = self.fail_always.get(name)
        if exc is not None:
            raise exc

    def _next(self):
        self._seq += 1
        return self._seq

    def call(self, method, params=None):
        """The generic seam the agents-pane split uses: `pane.split`/`pane.list`.

        The shared split reaches for `pane.split` through the raw `call` (herdr
        names no new pane in its reply), so this records it here and remembers
        the pane it made — which is how the split's fallback `pane.list` finds
        the new pane by difference. Mirrors the launcher test's FakeClient.
        """
        params = params or {}
        self.calls.append((method, params))
        if method == "pane.split":
            src = next((p for p in self._panes
                        if p["pane_id"] == params.get("target_pane_id")), {})
            n = self._next()
            self._panes.append({
                "pane_id": f"split{n}",
                "tab_id": src.get("tab_id", f"tab{n}"),
                "workspace_id": src.get("workspace_id", f"ws{n}")})
            return {"type": "ok"}
        if method == "pane.list":
            ws = params.get("workspace_id")
            return {"type": "pane_list",
                    "panes": [p for p in self._panes
                              if p["workspace_id"] == ws]}
        return {"type": "ok"}

    def workspace_create(self, label, cwd, focus=False, env=None):
        """The MEASURED workspace.create reply: workspace, tab AND root_pane, all
        three in one call — the schema's own result listing is lossy.

        `env` is recorded, not sent: it is the pane-shell environment map
        `og_herdr_client.HerdrClient.workspace_create` takes, so a test can see
        what the bridge installed in the pane it just asked for.
        """
        self._call("workspace_create", label, cwd=cwd, focus=focus, env=env)
        if self.create_results:
            return self.create_results.pop(0)
        n = self._next()
        self._live[f"ws{n}"] = {"workspace_id": f"ws{n}", "label": label,
                                "pane_count": 1, "tab_count": 1}
        self._panes.append({"pane_id": f"pane{n}", "tab_id": f"tab{n}",
                            "workspace_id": f"ws{n}"})
        return {"type": "workspace_created",
                "workspace": {"workspace_id": f"ws{n}", "label": label},
                "tab": {"tab_id": f"tab{n}", "workspace_id": f"ws{n}"},
                "root_pane": {"pane_id": f"pane{n}", "tab_id": f"tab{n}"}}

    def workspace_close(self, workspace_id):
        self._call("workspace_close", workspace_id)
        self._live.pop(workspace_id, None)
        self._panes = [p for p in self._panes
                       if p["workspace_id"] != workspace_id]

    def tab_create(self, workspace_id, cwd, label, focus=False, env=None):
        self._call("tab_create", workspace_id, cwd=cwd, label=label, env=env)
        if self.create_results:
            return self.create_results.pop(0)
        n = self._next()
        # The REAL herdr 0.9.3 shape: `tab_id` / `pane_id`, never a generic `id`.
        self._panes.append({"pane_id": f"pane{n}", "tab_id": f"tab{n}",
                            "workspace_id": workspace_id})
        return {"tab": {"tab_id": f"tab{n}"},
                "root_pane": {"pane_id": f"pane{n}"}}

    def tab_close(self, tab_id):
        self._call("tab_close", tab_id)

    def pane_run(self, pane_id, command):
        self._call("pane_run", pane_id, command)

    def pane_send_text(self, pane_id, text):
        """The unsubmitted sibling of `pane_run`: same wire method, no newline.

        A history space's pane is given this — the resume line left as text, not
        run — so no session is resurrected. Kept separate from `pane_run` here
        so a test can tell the two apart by the recorded method name.
        """
        self._call("pane_send_text", pane_id, text)

    def pane_close(self, pane_id):
        self._call("pane_close", pane_id)

    def workspace_list(self):
        self._call("workspace_list")
        return list(self.spaces) + list(self._live.values())

    def operator_closed(self, workspace_id):
        """The operator closes a space by hand, in their own herdr window.

        The space leaves the listing and NO call is recorded — the bridge did
        not do it and must not be credited with it. This is the event the whole
        re-validation rule exists for, so it is a first-class way to set one up
        rather than something a test reaches into `_live` to do.
        """
        self._live.pop(workspace_id, None)

    def operator_renamed(self, workspace_id, label):
        """The operator renames a space by hand, in their own herdr window.

        No call is recorded — the bridge did not do it — but the new label is
        what `workspace.list` reports from here on, which is exactly what the
        bridge compares against when Omnigent's session title changes. Like
        `operator_closed`, this is a first-class way to set up the operator's
        own action rather than a test reaching into `_live` directly.
        """
        self._live[workspace_id]["label"] = label

    def report_agent(self, pane_id, source, agent, state, message=None,
                     seq=None, agent_session_id=None):
        self._call("report_agent", pane_id, source, agent=agent, state=state,
                   agent_session_id=agent_session_id)

    def release_agent(self, pane_id, source, agent, seq=None):
        self._call("release_agent", pane_id, source, agent=agent)

    def report_metadata(self, pane_id, source, title=None, display_agent=None,
                        ttl_ms=None):
        self._call("report_metadata", pane_id, source, title=title)

    def workspace_rename(self, workspace_id, label):
        """The client's NAMED `workspace.rename` wrapper, matching
        `og_herdr_client.HerdrClient`. The bridge routes the launcher handoff
        through it, so the frame is covered by the introspection guard in
        tests/test_og_herdr_client.py rather than by the generic `call`."""
        self._call("workspace_rename", workspace_id, label)

    def tab_rename(self, tab_id, label):
        """The client's NAMED `tab.rename` wrapper, matching
        `og_herdr_client.HerdrClient`. The bridge routes the first-tab label
        through it, so the frame is covered by the introspection guard in
        tests/test_og_herdr_client.py rather than by the generic `call`."""
        self._call("tab_rename", tab_id, label)


class FakeWatcher:
    def __init__(self, batches):
        self.batches = list(batches)
        self.polls = 0

    def poll_once(self):
        self.polls += 1
        return self.batches.pop(0) if self.batches else []

    def watch(self):
        while self.batches:
            yield from self.batches.pop(0)


@pytest.fixture
def seams(monkeypatch):
    """Replace the two lazy seams so no real sibling module is imported."""
    monkeypatch.setattr(m, "_state", lambda s: s.get("state", "idle"))
    monkeypatch.setattr(m, "_error_cls", lambda: FakeHerdrError)


@pytest.fixture(autouse=True)
def isolated_omnigent_home(tmp_path, monkeypatch):
    """Point $OMNIGENT_HOME at a scratch directory for every test here.

    The bridge records which herdr workspaces it created there, so without this
    a single test that projected a session would append to the developer's real
    ~/.omnigent/og-herdr.json — and the next `--cleanup` would then close live
    workspaces in the operator's terminal.
    """
    home = tmp_path / ".omnigent"
    home.mkdir()
    monkeypatch.setenv("OMNIGENT_HOME", str(home))
    return home


# The measured shape of a delegated worker: a LISTING row (which carries
# `parent_session_id` and `agent_name`, and NOT `harness`) with the DETAIL row's
# identity merged in by the watcher.
def root_session(title="Omnigent Herdr Integration Feasibility", **extra):
    session = {"title": title, "state": "working", "parent_session_id": None,
               "harness": "claude-native", "kind": "default"}
    session.update(extra)
    return session


def sub_session(parent, title="coder_zen:space-per-root", **extra):
    session = {"title": title, "state": "working",
               "parent_session_id": parent, "harness": "opencode-native",
               "kind": "sub_agent", "sub_agent_name": "coder_zen"}
    session.update(extra)
    return session


def _names(client):
    return [c[0] for c in client.calls]


def _call(client, name):
    return next(c for c in client.calls if c[0] == name)


# ---------------------------------------------------------------------------
# action sequences
# ---------------------------------------------------------------------------

def test_added_sequence(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, source="og-bridge",
                      cwd="/tmp/ws")
    session = root_session("Fix bug")
    lines = bridge.reconcile([Ev("added", "s1", session)])

    # A ROOT opens a workspace, and the one call brings the space, its first tab
    # and that tab's root pane back together.
    assert _names(client) == ["workspace_create", "tab_rename", "pane_run",
                              "report_agent", "report_metadata"]
    wc = _call(client, "workspace_create")
    assert wc[1] == ("Fix bug",)
    assert wc[2]["cwd"] == "/tmp/ws"
    assert wc[2]["focus"] is False
    assert _call(client, "pane_run")[1] == (
        "pane1", "omnigent attach --server http://127.0.0.1:6767 s1")
    ra = _call(client, "report_agent")
    assert ra[1] == ("pane1", "og-bridge")
    assert ra[2] == {"agent": "Claude Code", "state": "working",
                     "agent_session_id": "s1"}
    assert _call(client, "report_metadata")[2]["title"] == "Fix bug"
    # The space's first tab is labelled with the space's own title:
    # `workspace.create` cannot label the tab it makes, so without this it shows
    # herdr's tab number.
    assert _call(client, "tab_rename")[1] == ("tab1", "Fix bug")
    assert len(lines) == 1 and "add s1" in lines[0]


def test_cwd_flag_is_honoured_for_new_panes(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/srv/repo")
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    assert _call(client, "workspace_create")[2]["cwd"] == "/srv/repo"


def test_added_defaults_cwd_to_process_cwd_and_label_to_session_id(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "abcdef123456", {})])
    # No --cwd: the pane opens in the bridge's own cwd, not a hardcoded path.
    assert _call(client, "workspace_create")[2]["cwd"] == os.getcwd()
    # And a session with no title is named by its id — bare, NOT "og:<id>". The
    # space label IS the session title the operator reads; a prefix on it would
    # wreck the one display they asked for.
    assert _call(client, "workspace_create")[1] == ("abcdef12",)
    assert len(lines) == 1
    # ...and the space's first tab is labelled the SAME way, so a space opened
    # before its title exists does not show herdr's bare tab number in the
    # meantime — the `1` the operator was looking at.
    assert _call(client, "tab_rename")[1] == ("tab1", "abcdef12")


def test_realistic_listing_row_without_workspace_key_uses_bridge_cwd(seams):
    # The complete live LISTING row — the exact key set a live server returned
    # from `GET /v1/sessions`. It has no directory, and the watcher merges one
    # in from the detail endpoint before the bridge sees it; a row that reaches
    # `_added` still bare (a detail fetch that failed, or any other source) must
    # fall back to the bridge's own cwd.
    session = {
        "agent_id": "a1", "agent_name": "opencode", "archived": False,
        "comments_count": 0, "created_at": "2026-01-01T00:00:00Z",
        "external_session_id": "ext-1", "id": "s1", "labels": [],
        "owner": "me", "parent_session_id": None,
        "pending_elicitations_count": 0, "permission_level": "default",
        "runner_id": "r1", "status": "running", "title": "Fix bug",
        "updated_at": "2026-01-01T00:00:01Z", "viewer_unread": False,
    }
    assert "workspace" not in session
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/under/test")
    bridge.reconcile([Ev("added", "s1", session)])
    assert _call(client, "workspace_create")[2]["cwd"] == "/repo/under/test"


# ---------------------------------------------------------------------------
# the pane's directory is the SESSION's when the watcher supplied one
# ---------------------------------------------------------------------------

def test_a_session_workspace_wins_over_the_bridge_cwd(seams):
    # The defect: run against a live server, every projected pane opened in
    # whatever directory the daemon happened to start in, because the bridge
    # read an absent key and used its own cwd — for a session about a different
    # repository entirely.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/wherever/the/daemon/started")
    bridge.reconcile([Ev("added", "s1", root_session(
        "Fix bug", workspace="/Users/cryogenix/projects/og"))])

    assert _call(client, "workspace_create")[2]["cwd"] == \
        "/Users/cryogenix/projects/og"


def test_a_workers_workspace_wins_over_the_bridge_cwd(seams):
    # What the watcher sends for a worker is its PARENT's directory (the
    # worker's own detail row is None). An approximation, and the best the API
    # offers — but a far better one than the daemon's cwd.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/daemon/cwd")
    bridge.reconcile([Ev("added", "root", root_session("Fix"))])
    bridge.reconcile([Ev("added", "w1", sub_session(
        "root", "coder_zen:fix", workspace="/Users/cryogenix/projects/og"))])

    assert _call(client, "tab_create")[2]["cwd"] == \
        "/Users/cryogenix/projects/og"


@pytest.mark.parametrize("session_workspace", [None, ""],
                         ids=["absent_key", "empty_string"])
def test_a_missing_or_empty_workspace_falls_back_to_the_bridge_cwd(
        seams, session_workspace):
    # The watcher omits the key when it knows nothing, so an empty value should
    # not arrive — but `.get()` must treat both the same anyway, because an
    # empty string as a cwd is a pane that opens nowhere.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/under/test")
    session = root_session("T", state="idle")
    if session_workspace is not None:
        session["workspace"] = session_workspace
    bridge.reconcile([Ev("added", "s1", session)])

    assert _call(client, "workspace_create")[2]["cwd"] == "/repo/under/test"


def test_a_session_workspace_is_used_even_alongside_the_fallback(seams):
    # The other side of the same pair: with a real directory present, --cwd is
    # ignored. The two tests together pin "prefer the session, else --cwd",
    # which a single one-sided assertion would not.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/under/test")
    bridge.reconcile([Ev("added", "s1", root_session(
        "T", state="idle", workspace="/repo/from/session"))])

    assert _call(client, "workspace_create")[2]["cwd"] == "/repo/from/session"


def test_the_dry_run_line_reports_the_session_directory(seams):
    # The dry-run output is how this was noticed at all ("every line showed the
    # same cwd"), so it has to name the directory the pane would really open in.
    bridge = m.Bridge(FakeWatcher([]), RecordingClient(), dry_run=True,
                      cwd="/daemon/cwd")
    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix bug", workspace="/Users/cryogenix/projects/og"))])

    assert lines == ["dry-run: add 'Fix bug' → space "
                     "(/Users/cryogenix/projects/og) → omnigent attach "
                     "--server http://127.0.0.1:6767 s1 [working]"]


def test_the_bridge_makes_no_http_call_of_its_own():
    # The bridge talks to herdr and to the watcher, and to nothing else. The
    # directory rides on the session dict because the WATCHER owns every HTTP
    # call: an enrichment fetch made from _added would raise through reconcile,
    # which only catches herdr's HerdrError, and abort the whole batch.
    source = Path(m.__file__).read_text()
    names = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
    assert "urllib" not in names and "urlopen" not in names, (
        "the bridge reached for the network directly; the session directory "
        "must arrive on the event, not be fetched here"
    )


# ---------------------------------------------------------------------------
# filing a root session into the install's project folder
#
# The bridge holds the TRIGGER and the POLICY (roots only, once, warned once on
# failure); the HTTP is the injected `og_projects.ProjectFiler`'s. These tests
# drive that real filer through a fake transport (the same opener seam the
# watcher uses), so the merge and the "leave it alone" rules are exercised, and
# no test touches a server.
# ---------------------------------------------------------------------------

class _FilerResponse:
    """Minimal stand-in for an http.client.HTTPResponse: read() + context mgr."""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class FilerOpener:
    """Fake transport for `og_projects.ProjectFiler`.

    Answers the labels GET with `labels` and records every PATCH body, so a test
    can see exactly what was merged and written. `fail` (an exception instance)
    is raised on the PATCH to pin the warn path.
    """

    def __init__(self, labels=None, fail=None):
        self.labels = dict(labels or {})
        self.fail = fail
        self.gets = []
        self.patches = []

    def __call__(self, request):
        if request.get_method() == "PATCH":
            if self.fail is not None:
                raise self.fail
            self.patches.append(json.loads(request.data.decode("utf-8")))
            return _FilerResponse(json.dumps({"id": "s1"}))
        self.gets.append(request.full_url)
        return _FilerResponse(json.dumps({"id": "s1", "labels": self.labels}))


def _plan(home, name="hivemind"):
    """Write the install plan whose `agent_name` the bridge must read."""
    (Path(home) / "og-install.json").write_text(json.dumps({"agent_name": name}))


def _filer(opener):
    from og_projects import ProjectFiler
    return ProjectFiler("http://127.0.0.1:6767", opener=opener)


def test_a_new_root_is_filed_under_the_plans_agent_name(
        seams, isolated_omnigent_home):
    # The project name comes from the PLAN, not a constant: this install's
    # agent_name is "hivemind", so that is the folder the session lands in.
    _plan(isolated_omnigent_home, "hivemind")
    opener = FilerOpener()
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, filer=_filer(opener))

    lines = bridge.reconcile([Ev("added", "s1", root_session("Fix bug"))])

    assert opener.patches == [{"labels": {"omni_project": "hivemind"}}]
    assert any("filed s1" in ln and "hivemind" in ln for ln in lines), lines
    # Filing is beside the pane, never instead of it.
    assert "workspace_create" in _names(client)


def test_existing_labels_survive_the_filing(seams, isolated_omnigent_home):
    # `labels` is a WHOLE map, so the write has to be a read-merge-write. A
    # blind PATCH would strip the user's own labels off the session.
    _plan(isolated_omnigent_home)
    opener = FilerOpener(labels={"topic": "perf", "env": "local"})
    bridge = m.Bridge(FakeWatcher([]), RecordingClient(), filer=_filer(opener))

    bridge.reconcile([Ev("added", "s1", root_session("T"))])

    assert opener.patches == [
        {"labels": {"topic": "perf", "env": "local", "omni_project": "hivemind"}}]


def test_a_session_already_in_a_project_is_not_refiled(
        seams, isolated_omnigent_home):
    # Filed under a DIFFERENT project by the user: not ours to re-file. No PATCH,
    # and no line claiming one happened.
    _plan(isolated_omnigent_home)
    opener = FilerOpener(labels={"omni_project": "someone-elses"})
    bridge = m.Bridge(FakeWatcher([]), RecordingClient(), filer=_filer(opener))

    lines = bridge.reconcile([Ev("added", "s1", root_session("T"))])

    assert opener.patches == []
    assert not any("filed s1" in ln for ln in lines), lines


def test_a_sub_agent_session_is_not_filed(seams, isolated_omnigent_home):
    # A worker nests under its root already; filing it would sprout a folder per
    # worker. Only the root's labels are ever read or written.
    _plan(isolated_omnigent_home)
    opener = FilerOpener()
    bridge = m.Bridge(FakeWatcher([]), RecordingClient(), filer=_filer(opener))

    bridge.reconcile([
        Ev("added", "root", root_session("T")),
        Ev("added", "w1", sub_session("root")),
    ])

    assert opener.gets == ["http://127.0.0.1:6767/v1/sessions/root/labels"]
    assert opener.patches == [{"labels": {"omni_project": "hivemind"}}]


def test_a_failing_filing_warns_once_and_still_opens_the_pane(
        seams, isolated_omnigent_home):
    # Filing is a convenience, not the job: a dead server warns ONE line, never
    # raises, and never keeps a session from getting its pane. The second failing
    # session adds no second warning.
    import urllib.error

    _plan(isolated_omnigent_home)
    opener = FilerOpener(fail=urllib.error.URLError("down"))
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, filer=_filer(opener))

    lines = bridge.reconcile([
        Ev("added", "s1", root_session("A")),
        Ev("added", "s2", root_session("B")),
    ])

    warns = [ln for ln in lines if ln.startswith("warn:")]
    assert len(warns) == 1, lines
    assert _names(client).count("workspace_create") == 2


def test_a_dry_run_files_nothing(seams, isolated_omnigent_home):
    # A dry run makes zero calls, filing included: no labels read, no PATCH.
    _plan(isolated_omnigent_home)
    opener = FilerOpener()
    bridge = m.Bridge(FakeWatcher([]), None, dry_run=True, filer=_filer(opener))

    bridge.reconcile([Ev("added", "s1", root_session("T"))])

    assert opener.gets == [] and opener.patches == []


# ---------------------------------------------------------------------------
# the pane's command: `omnigent attach --server <url> <session_id>`
#
# Measured in a real pane on a live server:
#   $ omnigent attach cf3999846e1f45fe8c2e6f835a7e905a
#   Error: No server to attach to. `attach` joins a LIVE session on a running
#   server — start one with `omnigent run`…
# `--help` says the server "defaults to the configured server, or a local server
# already running in the background", and neither default resolves from a plain
# shell inside a herdr pane. With the URL named, the same session answers a
# DIFFERENT error ("has no online runner on http://127.0.0.1:6767"), which is
# the server being found — and that second condition is the watcher's business
# (og_herdr_watch.runner_is_offline). Every pane the bridge opened without
# `--server` was a pane that could only ever show "No server to attach to".
# ---------------------------------------------------------------------------

ATTACH = "omnigent attach --server http://127.0.0.1:6767 s1"


def test_the_pane_command_names_the_server_and_the_session(seams):
    # The exact string, not a containment check: the command is TYPED INTO A
    # SHELL, so a flag in the wrong place is not a cosmetic difference.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo")
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])

    assert _call(client, "pane_run")[1] == ("pane1", ATTACH)


def test_a_non_default_server_reaches_the_pane_command(seams):
    # `--server` is the user's choice of server, and the pane runs in a shell
    # with no way to learn it: a hardcoded default here would attach a remote
    # user's session to their own loopback.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo",
                      server="https://og.example:8443")
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])

    assert _call(client, "pane_run")[1] == (
        "pane1", "omnigent attach --server https://og.example:8443 s1")


def test_the_bridge_defaults_to_the_documented_server_url(seams):
    # The default is what the CLI documents and what the watcher polls, so a
    # Bridge built without --server must not name some other server.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    assert _call(client, "pane_run")[1] == ("pane1", ATTACH)
    assert m.DEFAULT_SERVER == "http://127.0.0.1:6767"


def test_the_dry_run_line_shows_the_whole_command(seams):
    # The dry run is worth exactly what this line is worth: it is how a command
    # that cannot work is spotted before a pane runs it. Printing a shortened
    # form here would make the dry run lie about the only thing it reports.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, dry_run=True, cwd="/daemon/cwd",
                      server="https://og.example:8443")
    lines = bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])

    assert lines == ["dry-run: add 'T' → space (/daemon/cwd) → omnigent "
                     "attach --server https://og.example:8443 s1 [idle]"]
    assert client.calls == []


def test_a_server_url_with_shell_metacharacters_still_parses_as_one_command():
    # The URL is operator input typed into a shell, not a trusted constant, so
    # each part is quoted separately. The proof is not that quotes appear — it is
    # that the line splits back into exactly the five arguments it was built
    # from: a URL carrying a space, a separator or a command substitution can
    # then neither break the line nor run anything.
    nasty = "http://h:6767/a b;rm -rf /$(whoami)"
    command = m.attach_command(nasty, "s1")

    assert shlex.split(command) == [
        "omnigent", "attach", "--server", nasty, "s1"]
    # ...and the ordinary case stays unquoted, so the pane and the dry-run line
    # read as the command rather than as an escaped one.
    assert m.attach_command("http://127.0.0.1:6767", "s1") == ATTACH


def test_resume_command_is_the_og_chat_resume_line():
    # The line a HISTORY space's pane is handed, and the one a fresh `og start`
    # never uses. `og chat` — not a bare `omnigent run` — is what brings the
    # local server and the agent bundle with it, exactly as for a fresh session
    # in a launcher pane; `og chat` forwards to `omnigent run -r/--resume <id>`,
    # documented to attach to a prior conversation. It deliberately names no
    # `--server`: `og chat` has no such flag.
    assert m.resume_command("dead1") == "og chat --resume dead1"


def test_a_resume_session_id_with_metacharacters_still_parses_as_one_command():
    # A session id is operator input typed into a shell, so each part is quoted
    # separately. The proof is not that quotes appear — it is that the line
    # splits back into exactly the four arguments it was built from: an id
    # carrying a space, a separator or a command substitution can then neither
    # break the line nor run anything.
    nasty = "a b;rm -rf /$(whoami)"
    assert shlex.split(m.resume_command(nasty)) == [
        "og", "chat", "--resume", nasty]
    assert m.resume_command("abc") == "og chat --resume abc"


def test_main_threads_the_cli_server_into_the_bridge(monkeypatch, tmp_path):
    # The wiring is the defect's other half: a --server that reached the watcher
    # but not the Bridge would leave every pane on the default while the bridge
    # correctly polled somewhere else.
    import sys
    import types

    captured = {}

    # main() reads the real token store; point it at an empty directory so this
    # test never touches the developer's ~/.omnigent.
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))

    class StubWatcher:
        def __init__(self, base_url=None, token=None):
            captured["base_url"] = base_url

        def poll_once(self):
            return []

    # The sibling modules may legitimately be absent from the tree (they live on
    # other branches), so main() is driven against stubs in sys.modules rather
    # than against whatever happens to be importable here.
    stub_module = types.ModuleType("og_herdr_watch")
    stub_module.SessionWatcher = StubWatcher
    monkeypatch.setitem(sys.modules, "og_herdr_watch", stub_module)

    real_bridge = m.Bridge

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_bridge(FakeWatcher([]), None, dry_run=True)

    monkeypatch.setattr(m, "Bridge", spy)

    assert m.main(["--once", "--dry-run", "--server", "https://og.example:8443"]) == 0
    assert captured["base_url"] == "https://og.example:8443"
    assert captured["server"] == "https://og.example:8443"


def test_main_threads_the_agents_pane_setting_into_the_bridge(
        monkeypatch, tmp_path):
    # The install's `herdr_agents_pane` must reach the bridge the same way
    # `herdr_session_space_cap` does; a bridge that decided its own answer would
    # ignore the operator's choice for every resumed conversation.
    import sys
    import types

    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    (tmp_path / "og-install.json").write_text(
        json.dumps({"herdr_agents_pane": False}))

    class StubWatcher:
        def __init__(self, base_url=None, token=None):
            pass

        def poll_once(self):
            return []

    stub_module = types.ModuleType("og_herdr_watch")
    stub_module.SessionWatcher = StubWatcher
    monkeypatch.setitem(sys.modules, "og_herdr_watch", stub_module)

    captured = {}
    real_bridge = m.Bridge

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_bridge(FakeWatcher([]), None, dry_run=True)

    monkeypatch.setattr(m, "Bridge", spy)

    assert m.main(["--once", "--dry-run"]) == 0
    assert captured["agents_pane"] is False


def test_main_returns_zero_when_the_forever_loop_is_interrupted(
        monkeypatch, tmp_path):
    # A daemon the operator stops with Ctrl-C must exit cleanly: the forever
    # path catches KeyboardInterrupt and returns 0 rather than letting it
    # propagate out of main. `--once` and `--cleanup` are the other two exits
    # and are covered by the two tests around this one.
    import sys
    import types

    # main() reads the real token store; keep it off the developer's ~/.omnigent.
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))

    class StubWatcher:
        def __init__(self, base_url=None, token=None):
            pass

    stub_module = types.ModuleType("og_herdr_watch")
    stub_module.SessionWatcher = StubWatcher
    monkeypatch.setitem(sys.modules, "og_herdr_watch", stub_module)

    class Interrupted:
        def run_forever(self):
            raise KeyboardInterrupt()

    monkeypatch.setattr(m, "Bridge", lambda *args, **kwargs: Interrupted())

    # No --once, no --cleanup: this is the forever path. --dry-run keeps main()
    # from constructing the herdr client (and so touching a socket).
    assert m.main(["--dry-run"]) == 0


def test_changed_sequence_reports_state_and_title(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("Old", state="idle"))])
    client.calls.clear()

    lines = bridge.reconcile([
        Ev("changed", "s1", root_session("New", state="working"),
           previous=root_session("Old", state="idle"))])

    # The title change also renames the SPACE, not just the pane's metadata — a
    # space opened before the conversation had a name would otherwise keep its
    # `sid[:8]` label for good (see `_rename_own_space`). The label to compare
    # against comes from the per-pass listing, hence the one `workspace_list`.
    assert _names(client) == ["report_agent", "report_metadata",
                              "workspace_list", "workspace_rename",
                              "tab_rename"]
    assert _call(client, "report_agent")[2]["state"] == "working"
    # The STORED label, not one re-derived from this event: a `changed` carries
    # the listing row, which has no `harness` to derive from, and reporting a
    # different agent string would make herdr show two markers for one pane.
    assert _call(client, "report_agent")[2]["agent"] == "Claude Code"
    assert _call(client, "report_metadata")[2]["title"] == "New"
    assert _call(client, "workspace_rename")[1] == ("ws1", "New")
    # ...and the space's FIRST TAB follows the space. The root's tab was labelled
    # `Old` at creation; it must converge onto `New` here, or the operator is left
    # with a correctly-named space holding a tab still called by the old label.
    assert _call(client, "tab_rename")[1] == ("tab1", "New")
    assert "update s1" in lines[0]


def test_report_agent_carries_the_omnigent_session_id(seams):
    """A create's report names the OMNIGENT session id, not a pane or tab id.

    The point of sending it is that `agent.focus {target}` can later match this
    record back to the session, and the `og agents` pane aims its target at
    `Row.session_id` — the event's `sid`, the same value that keys every record
    in the bridge. A pane id here would focus nothing.
    """
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, source="og-bridge",
                      cwd="/tmp/ws")
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    assert _call(client, "report_agent")[2]["agent_session_id"] == "s1"


def test_changed_report_repeats_the_same_session_id(seams):
    """A `changed` re-sends the id, so the record keeps the handle `agent.focus`
    matches on across every state update, not just the create."""
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    client.calls.clear()

    bridge.reconcile([Ev("changed", "s1", root_session("T", state="working"),
                         previous=root_session("T", state="idle"))])
    assert _call(client, "report_agent")[2]["agent_session_id"] == "s1"


def test_sub_agent_report_carries_its_own_session_id(seams):
    """A worker's pane reports the WORKER's id, not its root's — focus must
    reach the worker's tab, and the two sessions are matched by their own ids."""
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([
        Ev("added", "root", root_session("R", state="idle")),
        Ev("added", "worker", sub_session("root", state="idle"))])
    reports = [c for c in client.calls if c[0] == "report_agent"]
    assert [r[2]["agent_session_id"] for r in reports] == ["root", "worker"]


def test_changed_without_title_change_skips_metadata(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("Same", state="idle"))])
    client.calls.clear()

    bridge.reconcile([Ev("changed", "s1", root_session("Same", state="working"),
                         previous=root_session("Same", state="idle"))])
    assert _names(client) == ["report_agent"]


def test_removed_sequence(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    client.calls.clear()

    lines = bridge.reconcile([Ev("removed", "s1", {})])
    # The root's own pane is released, then the WORKSPACE goes — which takes its
    # tabs with it, so there is no tab to close separately.
    assert _names(client) == ["release_agent", "workspace_close"]
    ra = _call(client, "release_agent")
    assert ra[1] == ("pane1", "og-bridge")
    assert ra[2] == {"agent": "Claude Code"}
    assert _call(client, "workspace_close")[1] == ("ws1",)
    assert "remove s1" in lines[0]

    # The mapping is dropped, so a repeat of the same removal is a no-op.
    client.calls.clear()
    assert bridge.reconcile([Ev("removed", "s1", {})]) == []
    assert client.calls == []


# ---------------------------------------------------------------------------
# idempotence and unmapped events
# ---------------------------------------------------------------------------

def test_duplicate_added_yields_one_tab(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    session = root_session("T", state="idle")
    first = bridge.reconcile([Ev("added", "s1", session)])
    second = bridge.reconcile([Ev("added", "s1", session)])
    assert len(first) == 1
    assert second == []
    assert _names(client).count("workspace_create") == 1
    assert _names(client).count("tab_create") == 0


def test_duplicate_added_of_a_sub_agent_yields_one_tab(seams):
    # The sub-agent half of the same guard: a redelivered `added` for a worker
    # must not open a second TAB either, and — the part that is new with a space
    # per root — must not open a second workspace.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "root", root_session())])
    session = sub_session("root")
    first = bridge.reconcile([Ev("added", "w1", session)])
    second = bridge.reconcile([Ev("added", "w1", session)])

    assert len(first) == 1
    assert second == []
    assert _names(client).count("tab_create") == 1
    assert _names(client).count("workspace_create") == 1


# ---------------------------------------------------------------------------
# THE LAYOUT: a workspace per ROOT session, its sub-agents as tabs inside it.
#
# This is the operator's list:
#
#     space  "Omnigent Herdr Integration Feasibility"
#       tab  Claude Code
#       tab  coder_zen:space-per-root
#       tab  coder_cmdcode:fix-stale-comments
#
# A sub-agent never gets a space of its own while its parent has one.
# ---------------------------------------------------------------------------

def test_a_root_opens_a_space_labelled_with_its_title(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "e29bf406", root_session(
        "Omnigent Herdr Integration Feasibility",
        workspace="/Users/cryogenix/projects/og"))])

    wc = _call(client, "workspace_create")
    assert wc[1] == ("Omnigent Herdr Integration Feasibility",), wc
    # cwd is the SESSION's directory, not the bridge's --cwd.
    assert wc[2]["cwd"] == "/Users/cryogenix/projects/og"
    # focus=False, always: several sessions appear per poll and each stealing
    # focus would rip the operator out of what they were typing.
    assert wc[2]["focus"] is False
    assert _names(client).count("workspace_create") == 1
    assert _names(client).count("tab_create") == 0


def test_a_sub_agent_opens_a_tab_in_its_parents_space(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.calls.clear()

    bridge.reconcile([Ev("added", "w1", sub_session(
        "root", "coder_zen:space-per-root"))])

    tc = _call(client, "tab_create")
    assert tc[1] == ("ws1",), "the tab went somewhere other than the root's space"
    assert tc[2]["label"] == "coder_zen:space-per-root"
    assert tc[2]["cwd"] == "/repo/og"
    # ...and no second workspace: the whole point of the layout.
    assert _names(client).count("workspace_create") == 0
    assert _names(client).count("tab_create") == 1


def test_a_worker_emitted_before_its_root_still_lands_in_its_space(seams):
    # THE ORDERING TRAP. The listing is NEWEST-FIRST and a sub-agent is newer
    # than the root that spawned it, so the child is emitted FIRST — on the very
    # first poll and on every poll where a new worker appears. Handled in
    # arrival order the child finds no space for a parent that has not been
    # created yet, and gets one of its own: the operator's conversation split in
    # two, which is exactly what the layout exists to prevent.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    lines = bridge.reconcile([
        Ev("added", "w1", sub_session("root", "coder_zen:space-per-root")),
        Ev("added", "root", root_session("New Alignment")),
    ])

    assert _names(client).count("workspace_create") == 1
    # The root went first, so its space existed when the child looked.
    assert _call(client, "tab_create")[1] == ("ws1",)
    assert lines[0].startswith("add root"), lines


def test_the_reordering_is_stable_within_each_group(seams):
    # Roots first, but the listing's own order survives inside each group — so
    # the log still reads the way the operator expects, and a batch of five roots
    # is still five roots in listing order.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([
        Ev("added", "w2", sub_session("r2")),
        Ev("added", "r1", root_session("A")),
        Ev("added", "w1", sub_session("r1")),
        Ev("added", "r2", root_session("B")),
    ])
    # The two spaces were opened for r1 then r2 (listing order among roots), and
    # the two tabs went into their own parents' spaces, children in arrival
    # order (w2 before w1).
    creates = [c for c in client.calls if c[0] == "workspace_create"]
    assert [c[1] for c in creates] == [("A",), ("B",)]
    tabs = [c for c in client.calls if c[0] == "tab_create"]
    assert [t[1] for t in tabs] == [("ws2",), ("ws1",)]


def test_a_worker_whose_parent_has_no_space_gets_one_of_its_own(seams):
    # Reachable: the parent's runner is offline, so the watcher never projected
    # it (runner_is_offline). Dropping the worker would hide an agent that is
    # actively working, so it gets its own space labelled from its own title.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    lines = bridge.reconcile([
        Ev("added", "w1", sub_session("never-seen", "coder_zen:fix"))])

    assert _names(client).count("workspace_create") == 1
    assert _names(client).count("tab_create") == 0
    assert _call(client, "workspace_create")[1] == ("coder_zen:fix",)
    assert "add w1" in lines[0]


def test_a_roots_removal_closes_the_space_and_drops_its_workers(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([
        Ev("added", "root", root_session("New Alignment")),
        Ev("added", "w1", sub_session("root", "coder_zen:fix")),
    ])
    client.calls.clear()

    lines = bridge.reconcile([Ev("removed", "root", {})])

    # Closing the workspace takes its tabs down with it, so there is one close
    # and it is the workspace.
    assert _names(client) == ["release_agent", "workspace_close"]
    assert _call(client, "workspace_close")[1] == ("ws1",)
    # The worker's record went with it: its pane died with the space.
    assert "w1" not in bridge._recs
    assert any("remove w1" in ln for ln in lines)
    # ...and the space is no longer recorded as ours, or `--cleanup` would chase
    # a workspace this bridge closed itself.
    assert bridge._owned == {}


def test_a_later_worker_removal_after_its_roots_is_a_safe_no_op(seams):
    # The half that has to hold: without the records being dropped, this removal
    # would answer `not_found` against a tab closed some time ago — true, but it
    # reads as a fresh fault on every worker of every finished conversation.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([
        Ev("added", "root", root_session("New Alignment")),
        Ev("added", "w1", sub_session("root", "coder_zen:fix")),
    ])
    bridge.reconcile([Ev("removed", "root", {})])
    client.calls.clear()

    assert bridge.reconcile([Ev("removed", "w1", {})]) == []
    assert client.calls == []


def test_a_workers_removal_closes_only_its_tab(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([
        Ev("added", "root", root_session("New Alignment")),
        Ev("added", "w1", sub_session("root", "coder_zen:fix")),
        Ev("added", "w2", sub_session("root", "coder_cmdcode:fix-stale")),
    ])
    client.calls.clear()

    lines = bridge.reconcile([Ev("removed", "w1", {})])

    assert _names(client) == ["release_agent", "tab_close"]
    # tab2 is the worker's own tab: the root's workspace.create took tab1.
    assert _call(client, "tab_close")[1] == ("tab2",)
    assert "remove w1" in lines[0]
    # The root's space and its other worker are untouched.
    assert bridge._recs["root"]["workspace_id"] == "ws1"
    assert "w2" in bridge._recs
    assert "root" in bridge._recs
    assert bridge._owned == {"ws1": "root"}


def test_a_nested_worker_lands_beside_its_own_parent(seams):
    # The same lookup by parent_session_id serves a grandchild, so the layout
    # does not need a second index and does not go flat at depth two.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([
        Ev("added", "root", root_session("New Alignment")),
        Ev("added", "w1", sub_session("root", "coder_zen:fix")),
        Ev("added", "g1", sub_session("w1", "coder_zen:sub-task"))])
    tabs = [c for c in client.calls if c[0] == "tab_create"]
    assert [t[1] for t in tabs] == [("ws1",), ("ws1",)]


# ---------------------------------------------------------------------------
# PAST conversations: every non-archived ROOT gets a space, live or not.
#
# The web sidebar lists every conversation the operator has had; this is the
# herdr half. A root whose runner is gone gets a space labelled with its title
# and a pane holding the resume line UNSENT — visible and resumable, with
# nothing running. A sub-agent is NOT extended this (a dead worker is noise),
# and a live root is unchanged.
# ---------------------------------------------------------------------------

def test_a_dead_root_gets_a_space_labelled_with_its_title(seams, tmp_path):
    # The title plumbing is the same one a live root uses: the space is labelled
    # with the session's own title, so the operator sees the conversation's name
    # exactly as Omnigent auto-named it.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path))
    bridge.reconcile([Ev("added", "dead1", root_session(
        "Fix issue 502", runner_online=False, workspace=str(tmp_path)))])

    wc = _call(client, "workspace_create")
    assert wc[1] == ("Fix issue 502",)
    assert wc[2]["cwd"] == str(tmp_path)
    assert wc[2]["focus"] is False
    assert _names(client).count("workspace_create") == 1


def test_a_dead_roots_pane_holds_the_resume_line_unsubmitted(seams, tmp_path):
    # THE HARD CONSTRAINT. A space for a past conversation must not resurrect it.
    # The CHAT pane is a plain shell — one `workspace.create` — holding the resume
    # line as UNSENT text: `pane_send_text` with no trailing newline, and NOT
    # `pane_run`, which would append the newline that starts it. The agents pane
    # the same create now splits runs `og agents` in a DIFFERENT pane, so this
    # pins the two apart: no pane_run ever carries the resume line, and the chat
    # pane only ever receives it as unsent text.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path))
    bridge.reconcile([Ev("added", "dead1", root_session(
        "New Alignment", runner_online=False, workspace=str(tmp_path)))])

    _name, args, _kwargs = _call(client, "pane_send_text")
    assert args == ("pane1", "og chat --resume dead1")
    assert not args[1].endswith("\n"), "the resume line must not be submitted"
    runs = [c[1] for c in client.calls if c[0] == "pane_run"]
    assert ("pane1", "og chat --resume dead1") not in runs, (
        "the resume line must never be submitted, on the chat pane or any other")


def test_a_history_spaces_first_tab_is_labelled_too(seams, tmp_path):
    # A history space is opened by the SAME `workspace.create` as a live one, so
    # it starts with the same unlabelled first tab. It is given the space's label
    # — the past conversation's title — for the same reason.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path))
    bridge.reconcile([Ev("added", "dead1", root_session(
        "New Alignment", runner_online=False, workspace=str(tmp_path)))])

    assert _call(client, "workspace_create")[1] == ("New Alignment",)
    assert _call(client, "tab_rename")[1] == ("tab1", "New Alignment")


def test_a_history_space_opens_the_agents_pane_when_enabled(seams, tmp_path):
    # THE BUG. Resuming a past conversation from the spaces list did not open
    # the `og agents` pane a fresh `og start` opens, because the history/resume
    # path never consulted `herdr_agents_pane`. With it on (the default), the
    # split happens with the SAME command and ratio a fresh launch uses — one
    # implementation, so the two cannot disagree about either.
    import og_herdr_launch
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path),
                      pane_env={"BROWSER": "true"})
    bridge.reconcile([Ev("added", "dead1", root_session(
        "Fix issue 502", runner_online=False, workspace=str(tmp_path)))])

    assert "pane.split" in _names(client)
    assert _call(client, "pane.split")[1] == {
        "target_pane_id": "pane1", "direction": "right", "focus": False,
        "cwd": str(tmp_path), "ratio": og_herdr_launch.AGENTS_PANE_RATIO,
        "env": {"BROWSER": "true"}}
    assert og_herdr_launch.AGENTS_PANE_RATIO == 0.7
    # ...and `og agents` is STARTED, in the new pane rather than the chat pane.
    runs = {c[1][0]: c[1][1] for c in client.calls if c[0] == "pane_run"}
    assert "og agents" in runs.values()
    assert "pane1" not in runs, "og agents must not run in the chat pane"


def test_a_history_space_honours_agents_pane_off(seams, tmp_path):
    # The other half of the pair: `herdr_agents_pane: false` must reach the
    # history/resume path too. A Bridge with it off opens NO split and starts
    # nothing — the operator asked for a chat pane only.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path),
                      agents_pane=False)
    bridge.reconcile([Ev("added", "dead1", root_session(
        "Fix issue 502", runner_online=False, workspace=str(tmp_path)))])

    assert "pane.split" not in _names(client)
    assert "pane_run" not in _names(client)
    # The resume line is still there, still unsubmitted: turning the split off
    # must not turn the space's own job off.
    assert _call(client, "pane_send_text")[1] == ("pane1",
                                                  "og chat --resume dead1")


def test_the_history_split_is_the_launchers_one_implementation(
        seams, tmp_path, monkeypatch):
    # ONE implementation, called by both. If the bridge ever grew its own copy of
    # the pane-split logic, this monkeypatched function would not be reached and
    # the two would drift — which is exactly how a fresh launch and a resumed
    # conversation would start disagreeing about the agents pane.
    import og_herdr_launch
    seen = {}

    def spy(client, ws_id, tab_id, root_pane, cwd, pane_env):
        seen.update(ws_id=ws_id, tab_id=tab_id, root_pane=root_pane, cwd=cwd,
                    pane_env=pane_env)
        return ["the shared split degraded"]

    monkeypatch.setattr(og_herdr_launch, "split_agents_pane", spy)
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path),
                      pane_env={"BROWSER": "true"})
    lines = bridge.reconcile([Ev("added", "dead1", root_session(
        "T", runner_online=False, workspace=str(tmp_path)))])

    assert seen == {"ws_id": "ws1", "tab_id": "tab1", "root_pane": "pane1",
                    "cwd": str(tmp_path), "pane_env": {"BROWSER": "true"}}
    # The bridge did not reach for pane.split itself; the shared function owns it.
    assert "pane.split" not in _names(client)
    # ...and the degradation the shared function returned reaches reconcile's
    # lines, so a broken split is reported, never swallowed.
    assert any("the shared split degraded" in ln for ln in lines), lines


def test_a_live_root_still_gets_todays_exact_treatment(seams):
    # No regression, and the sharpest form of it: a root with a live runner takes
    # the path it always did — a SUBMITTED attach, the agent marker, the title
    # metadata — and never the unsubmitted resume line.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "s1", root_session(
        "Live", runner_online=True, workspace="/repo/og"))])

    assert _names(client) == ["workspace_create", "tab_rename", "pane_run",
                              "report_agent", "report_metadata"]
    assert _call(client, "pane_run")[1] == (
        "pane1", "omnigent attach --server http://127.0.0.1:6767 s1")
    assert "pane_send_text" not in _names(client)


def test_a_root_with_no_runner_key_is_treated_as_live(seams):
    # Backwards compatibility, and the direction that matters: only an explicit
    # `runner_online: False` means "past conversation". A session dict from any
    # other source — an old event, a detail fetch that failed — has no such key
    # and must keep taking today's path rather than becoming a history space.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "s1", root_session("Live"))])

    assert _names(client) == ["workspace_create", "tab_rename", "pane_run",
                              "report_agent", "report_metadata"]


def test_a_dead_root_whose_workspace_is_gone_gets_no_space(seams, tmp_path):
    # A space is a shell in a directory, and a shell whose cwd does not exist is
    # broken. A past conversation whose checkout was a temporary worktree — which
    # is deleted when it finishes — names a directory that is simply not there,
    # so it gets nothing rather than a dead shell.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path))
    gone = tmp_path / "deleted-worktree"
    lines = bridge.reconcile([Ev("added", "dead1", root_session(
        "Fix issue 502", runner_online=False, workspace=str(gone)))])

    assert client.calls == []
    assert any("dead1" in ln and "gone" in ln for ln in lines), lines


def test_a_sub_agent_is_never_given_a_history_space(seams):
    # Sub-agent behaviour is unchanged: a dead worker is not a conversation, and
    # the watcher does not even emit one. If one reaches the bridge anyway (a
    # `runner_online: False` on a session that has a parent), the parent check
    # keeps the history path to roots.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    session = sub_session("root", "coder_zen:fix", runner_online=False,
                          workspace="/repo/og")
    bridge.reconcile([Ev("added", "w1", session)])

    # It is NOT a history space (no unsubmitted resume line); it falls to the
    # ordinary sub-agent path, which opens a space when the parent has none.
    assert "pane_send_text" not in _names(client)
    assert _names(client).count("workspace_create") == 1


def test_every_non_archived_root_gets_a_space_by_default(seams, tmp_path):
    # NO CAP BY DEFAULT: the operator wants the web sidebar's contents, all of
    # it, and prunes by hand. So a Bridge built with no explicit cap (the
    # shipping default, 0) opens a space for every root it is handed.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path))
    assert bridge.space_cap == 0
    events = [Ev("added", f"d{i}", root_session(
        f"past-{i}", runner_online=False, workspace=str(tmp_path)))
        for i in range(5)]

    bridge.reconcile(events)

    titles = [c[1][0] for c in client.calls if c[0] == "workspace_create"]
    assert titles == [f"past-{i}" for i in range(5)]


def test_the_history_space_cap_is_honoured_when_one_is_set(seams, tmp_path):
    # A cap that IS set is still honoured: the listing is NEWEST-FIRST, so the
    # roots arrive newest-first and the cap keeps the ones that arrive FIRST —
    # the recent conversations — and leaves the rest alone, with a line saying why.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path), space_cap=2)
    events = [Ev("added", f"d{i}", root_session(
        f"past-{i}", runner_online=False, workspace=str(tmp_path)))
        for i in range(3)]

    lines = bridge.reconcile(events)

    titles = [c[1][0] for c in client.calls if c[0] == "workspace_create"]
    assert titles == ["past-0", "past-1"]
    assert any("past-2" in ln and "cap" in ln for ln in lines), lines


def test_lowering_the_cap_governs_new_spaces_and_closes_none_that_are_open(seams,
                                                                        tmp_path):
    # Lowering `herdr_session_space_cap` in og-install.json does NOT retroactively
    # close the history spaces already on screen. It says what may open from here
    # on, and nothing here reaches for a live herdr space to enforce the number:
    # a space the operator may be reading a past conversation in is not ours to
    # close behind their back. Deliberate, and pinned here so the next reader
    # does not have to guess whether it was an oversight.
    client = RecordingClient()
    first = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path), space_cap=5)
    first.reconcile([Ev("added", f"d{i}", root_session(
        f"past-{i}", runner_online=False, workspace=str(tmp_path)))
        for i in range(2)])
    assert _names(client).count("workspace_create") == 2
    client.calls.clear()

    # The restart a lowered config actually produces: same state file, same
    # herdr, a lower cap.
    second = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path), space_cap=1)
    lines = second.reconcile([Ev("added", "d2", root_session(
        "past-2", runner_online=False, workspace=str(tmp_path)))])

    # The two already-open spaces are untouched and still counted…
    assert not {"workspace_close", "tab_close", "pane_close"} & set(_names(client))
    assert second._history_space_count() == 2
    # …and the new one is refused, because the cap governs what opens next.
    assert any("past-2" in ln and "cap" in ln for ln in lines), lines
    assert _names(client).count("workspace_create") == 0


def test_a_live_root_is_never_subject_to_the_history_cap(seams, tmp_path):
    # The cap is on PAST conversations only: a live session always gets its
    # space, even when the cap is already full — otherwise a machine at its cap
    # would stop showing the conversation someone is actually driving.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path), space_cap=1)
    bridge.reconcile([Ev("added", "d0", root_session(
        "past-0", runner_online=False, workspace=str(tmp_path)))])
    bridge.reconcile([Ev("added", "live1", root_session(
        "Live", runner_online=True, workspace=str(tmp_path)))])

    assert _names(client).count("workspace_create") == 2
    assert "pane_run" in _names(client)


def test_a_dead_root_does_not_get_a_second_space_on_a_later_pass(seams,
                                                                 tmp_path):
    # "One pane per session" holds for history spaces too: a redelivered `added`
    # for a root that already has one is a no-op, not a second space.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path))
    session = root_session("New Alignment", runner_online=False,
                           workspace=str(tmp_path))
    bridge.reconcile([Ev("added", "dead1", session)])
    bridge.reconcile([Ev("added", "dead1", session)])

    assert _names(client).count("workspace_create") == 1


def test_a_dead_root_adopts_its_recorded_space_after_a_restart(seams, tmp_path):
    # The other half of "no second space": a NEW process (empty `_recs`, same
    # state file and same herdr) adopts the recorded space instead of opening
    # another. This is also what keeps the cap stable across a restart — the
    # adopted spaces are counted, not re-created.
    session = root_session("New Alignment", runner_online=False,
                           workspace=str(tmp_path))
    client = RecordingClient()
    m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path)).reconcile(
        [Ev("added", "dead1", session)])
    client.calls.clear()

    m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path)).reconcile(
        [Ev("added", "dead1", session)])

    assert "workspace_create" not in _names(client)


def test_a_repaired_history_space_still_leaves_its_resume_line_unsubmitted(
        seams, tmp_path):
    # The REPAIR path must not quietly undo the constraint. A dead root whose
    # space the operator closed is re-projected — and re-projected as a HISTORY
    # space, with the resume line UNSENT — not as a live one that submits an
    # attach on a session nobody is watching.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path))
    bridge.reconcile([Ev("added", "dead1", root_session(
        "New Alignment", state="idle", runner_online=False,
        workspace=str(tmp_path)))])
    client.operator_closed("ws1")
    client.calls.clear()
    # Only the pane is reported gone; the space is gone too, so the repair has to
    # open a fresh one (the `_reproject` create branch, not the `_add_tab` one).
    client.fail = {"report_agent": FakeHerdrError(
        "pane_not_found", "pane ws1:p1 not found")}

    bridge.reconcile([Ev("changed", "dead1", root_session(
        "New Alignment", state="idle"), previous=root_session(
            "New Alignment", state="idle"))])

    # The resume line is UNSENT — `pane_send_text`, no newline — and no
    # `pane_run` ever carries it. (A repaired history space also opens the agents
    # pane, whose `og agents` runs in its OWN pane; see
    # `test_a_history_space_opens_the_agents_pane_when_enabled`.) The pane is read
    # off the record rather than hardcoded: the agents pane consumes an id, so the
    # repair's create is not the number it would be without a split.
    repair_pane = bridge._recs["dead1"]["pane_id"]
    _name, args, _kwargs = _call(client, "pane_send_text")
    assert args == (repair_pane, "og chat --resume dead1")
    assert not args[1].endswith("\n")
    runs = [c[1] for c in client.calls if c[0] == "pane_run"]
    assert (repair_pane, "og chat --resume dead1") not in runs, (
        "a repaired history space must never submit a runner")
    # ...and it is still recorded as a history space the cap counts.
    assert bridge._recs["dead1"]["owns_space"] is True


# ---------------------------------------------------------------------------
# the pane's environment: how a BRIDGE pane keeps the browser shut
# ---------------------------------------------------------------------------
# The same rule og_herdr_launch applies, and now stated ONCE in this module so
# the two cannot drift: a pane og opens under a multiplexer runs an omnigent
# command that can open the live conversation in a browser, and the only place
# the suppression can be installed is the pane's own shell. herdr takes an env
# map on the CREATING calls (`workspace.create`, `tab.create`, `pane.split`)
# and not on `pane.send_text` — see pane_env_for.
#
# The bridge had none of it, so every space and tab it created started with
# $BROWSER unset. The launcher threaded `ctx.pane_env` through all three of its
# own creates and the bridge threaded nothing through any of its three, which is
# how a `og start herdr` could leave a pane that opens a browser.

def test_a_space_the_bridge_opens_carries_the_browser_suppression(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/tmp/ws",
                      pane_env={"BROWSER": "true"})

    bridge.reconcile([Ev("added", "s1", root_session("Fix bug"))])

    assert _call(client, "workspace_create")[2]["env"] == {"BROWSER": "true"}


def test_a_sub_agents_tab_carries_the_browser_suppression(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/tmp/ws",
                      pane_env={"BROWSER": "true"})

    bridge.reconcile([Ev("added", "root", root_session("Fix bug")),
                      Ev("added", "w1", sub_session("root", "coder:task"))])

    assert _call(client, "tab_create")[2]["env"] == {"BROWSER": "true"}


def test_a_history_space_carries_the_browser_suppression(seams, tmp_path):
    """The one case where the bug is INVISIBLE until a human acts.

    A history space's pane holds `og chat --resume <id>` as unsubmitted text:
    nothing runs, so nothing opens a browser, and the pane looks identical to a
    suppressed one. The moment the operator presses Enter — which is the entire
    point of the space — an unsuppressed pane runs it and a browser opens. So
    the create is asserted here rather than inferred from the live cases.
    """
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path),
                      pane_env={"BROWSER": "true"})

    bridge.reconcile([Ev("added", "dead1", root_session(
        "Fix issue 502", runner_online=False, workspace=str(tmp_path)))])

    assert _call(client, "workspace_create")[2]["env"] == {"BROWSER": "true"}
    assert _call(client, "pane_send_text")[1] == ("pane1",
                                                  "og chat --resume dead1")


def test_the_bridge_defaults_an_unset_browser_rather_than_opening_one(
        seams, monkeypatch):
    """`pane_env` left out means "derive it from this process", NOT "send none".

    The bug being fixed was a path that FORGOT the map, so the default has to be
    the safe one: a Bridge built by anything — `og herdr`, `--once`, a caller
    that never heard of pane_env — suppresses unless it is told otherwise.
    """
    monkeypatch.delenv("BROWSER", raising=False)
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/tmp/ws")

    bridge.reconcile([Ev("added", "s1", root_session("Fix bug"))])

    assert _call(client, "workspace_create")[2]["env"] == {"BROWSER": "true"}


def test_an_operator_browser_setting_reaches_a_bridge_pane(seams, monkeypatch):
    """`$BROWSER` is a PREFERENCE, so the bridge passes it through, not over it.

    Measured rather than assumed: bin/og starts the bridge as
    `nohup env PYTHONUNBUFFERED=1 python3 og_herdr.py ...`, and `env` without
    `-i` passes the caller's environment through with one variable added. So the
    daemon does inherit an exported `$BROWSER` — and must honour it, exactly as
    the launcher does, rather than silently replacing it with `true`.
    """
    monkeypatch.setenv("BROWSER", "/usr/bin/firefox")
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/tmp/ws")

    bridge.reconcile([Ev("added", "s1", root_session("Fix bug"))])

    assert _call(client, "workspace_create")[2]["env"] == {
        "BROWSER": "/usr/bin/firefox"}


def test_the_suppression_is_installed_at_creation_and_never_typed_after(
        seams, tmp_path):
    """herdr has no per-send env, so creation is the ONLY place it can go.

    `pane.send_text` carries `pane_id` and `text` and nothing else (herdr's
    published PaneSendTextParams), so a line typed into an existing pane cannot
    carry an environment: it runs in the shell herdr started when the pane was
    created. That is why every create is covered above and nothing is done about
    `pane_run`/`pane_send_text` — asserted here so the next reader does not go
    looking for an env to add to a send.
    """
    monkeypatched = {"env": {"BROWSER": "true"}}
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd=str(tmp_path),
                      pane_env=monkeypatched["env"])

    bridge.reconcile([Ev("added", "s1", root_session("Live"))])

    # The fake client accepts only what the real one does: `pane_run` and
    # `pane_send_text` take (pane_id, command) — a third argument would be a
    # TypeError, which is the assertion.
    assert _call(client, "pane_run")[1] == (
        "pane1", "omnigent attach --server http://127.0.0.1:6767 s1")
    assert "env" not in _call(client, "pane_run")[2]


def test_the_launcher_and_the_bridge_share_one_pane_env_rule():
    """One statement of the rule, one implementation, no second copy.

    `og_herdr_launch` imports `pane_env_for` from here; it must be the SAME
    object, not a re-spelling that happens to agree today. Two copies of a
    suppression rule is how this bug comes back: the bridge's panes opened
    browsers for exactly as long as nobody checked the other module.
    """
    import og_herdr_launch

    assert og_herdr_launch.pane_env_for is m.pane_env_for


# ---------------------------------------------------------------------------
# the agent column: a TOOL name, not an id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("harness,expected", [
    ("claude-native", "Claude Code"),
    ("opencode-native", "OpenCode (Zen)"),
])
def test_the_reported_agent_is_the_registrys_product_name(harness, expected):
    assert m.agent_label({"harness": harness}) == expected


def test_an_unlisted_harness_falls_back_to_its_raw_id():
    # Ugly, but honest: it names exactly what ran. And it must not raise — a
    # harness og has no catalog row for is a new tool, not a broken pane.
    assert m.agent_label({"harness": "brand-new-native"}) == "brand-new-native"


@pytest.mark.parametrize("session", [{}, {"harness": None}, {"harness": ""},
                                      {"harness": 7}, "not a dict", None])
def test_a_session_naming_no_harness_gets_the_default_label(session):
    assert m.agent_label(session) == m.DEFAULT_AGENT_LABEL
    assert m.DEFAULT_AGENT_LABEL, "herdr requires a non-empty agent string"


def test_the_bridge_reports_the_registry_label_and_releases_the_same_one(seams):
    # herdr matches a claim by (source, agent): a release naming anything other
    # than what was reported leaves our marker sitting on a closed pane.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, source="og-bridge")
    bridge.reconcile([Ev("added", "s1", root_session(
        "New Alignment", harness="claude-native"))])
    assert _call(client, "report_agent")[2]["agent"] == "Claude Code"

    bridge.reconcile([Ev("removed", "s1", {})])
    assert _call(client, "release_agent")[2]["agent"] == "Claude Code"


def test_the_session_title_goes_to_metadata_not_the_agent_column(seams):
    # Together they give the operator's list: the tool on one side, the worker
    # and its task on the other.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session(
        "Omnigent Herdr Integration Feasibility", harness="claude-native"))])

    assert _call(client, "report_agent")[2]["agent"] == "Claude Code"
    assert _call(client, "report_metadata")[2]["title"] == \
        "Omnigent Herdr Integration Feasibility"


def test_the_registry_labels_are_read_from_this_repos_catalog():
    # Not from a second hard-coded table here. If a harness is renamed in
    # installer/registry.json, the pane must follow without an edit here.
    catalog = json.loads(Path(m.REGISTRY_PATH).read_text())
    rows = {row["harness"]: row["label"] for row in catalog["agents"]}
    for harness, label in rows.items():
        assert m.harness_labels()[harness] == label


def test_a_broken_registry_costs_the_label_not_the_pane(monkeypatch, tmp_path):
    # A missing or malformed catalog degrades the agent column to raw harness
    # ids. It must never raise: the bridge cannot tell a broken catalog from an
    # unfamiliar harness, and a pane lost over a cosmetic failure is a far worse
    # outcome than a pane labelled `claude-native`.
    #
    # Every case redirects REGISTRY_PATH at a scratch file. Writing over the
    # real installer/registry.json to test a failure mode would be a test that
    # deletes a source file when it is interrupted.
    for name, content in (("missing.json", None),
                          ("broken.json", "{ not json"),
                          ("wrong-shape.json", '{"agents": "nope"}'),
                          ("a-list.json", "[]")):
        monkeypatch.setattr(m, "_REGISTRY_LABELS", None)
        path = tmp_path / name
        if content is not None:
            path.write_text(content)
        monkeypatch.setattr(m, "REGISTRY_PATH", path)
        assert m.harness_labels() == {}, name
        assert m.agent_label({"harness": "claude-native"}) == "claude-native", name


def test_unmapped_changed_is_ignored(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("changed", "ghost", {"state": "working"})])
    assert lines == []
    assert client.calls == []


def test_unmapped_removed_is_ignored(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("removed", "ghost", {})])
    assert lines == []
    assert client.calls == []


# ---------------------------------------------------------------------------
# recovery from a herdr call that fails part-way through one event
# ---------------------------------------------------------------------------

def test_partial_setup_failure_retries_on_the_same_pane_without_a_second_space(
        seams):
    # pane_run fails once. The space, its tab and the pane already exist, so the
    # redelivered `added` must resume setup on them rather than open a second
    # workspace — which for a root would leave the operator with a duplicate
    # space and two panes running the same session.
    client = RecordingClient(fail={"pane_run": FakeHerdrError("boom", "pane gone")})
    bridge = m.Bridge(FakeWatcher([]), client)
    session = root_session("T", state="idle")

    first = bridge.reconcile([Ev("added", "s1", session)])
    assert any("error s1" in ln for ln in first)
    # The mapping was recorded after workspace_create, so the pane stays tracked.
    assert bridge._recs["s1"]["workspace_id"] == "ws1"
    assert bridge._recs["s1"]["tab_id"] == "tab1"
    assert bridge._recs["s1"]["pane_id"] == "pane1"
    assert _names(client).count("workspace_create") == 1
    client.calls.clear()

    second = bridge.reconcile([Ev("added", "s1", session)])
    assert _names(client).count("workspace_create") == 0
    assert _names(client) == ["pane_run", "report_agent", "report_metadata"]
    assert _call(client, "pane_run")[1] == (
        "pane1", "omnigent attach --server http://127.0.0.1:6767 s1")
    assert "resume add s1" in second[0]
    assert bridge._recs["s1"]["ready"] is True


def test_added_with_no_usable_ids_records_error_and_never_reports_on_none(
        seams):
    client = RecordingClient(create_results=[{"workspace": {}, "tab": {},
                                              "root_pane": {}}])
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([
        Ev("added", "bad", root_session("bad", state="idle")),
        Ev("added", "good", root_session("good", state="idle")),
    ])
    assert any("error bad" in ln for ln in lines)
    assert any("add good" in ln for ln in lines)
    # Nothing usable was stored for the malformed create, and none of the
    # follow-up calls were made with a None pane.
    assert "bad" not in bridge._recs
    assert _names(client).count("pane_run") == 1
    assert _call(client, "report_agent")[1][0] == "pane1"

    # A later `changed` for the rejected session must not report against a None
    # pane: there is no mapping, so it is ignored.
    client.calls.clear()
    assert bridge.reconcile([Ev("changed", "bad", {"state": "working"})]) == []
    assert client.calls == []


def test_a_rejected_workspace_create_closes_the_orphan_space(seams):
    # The space came back without its tab or pane. There IS a handle to close —
    # the workspace — and closing it takes the tab and pane with it, so there is
    # nothing further to close and no second call to fail.
    client = RecordingClient(create_results=[
        {"workspace": {"workspace_id": "wsZ"}, "tab": {}, "root_pane": {}}])
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])

    assert any("error s1" in ln for ln in lines)
    assert _names(client) == ["workspace_create", "workspace_close"]
    assert _call(client, "workspace_close")[1] == ("wsZ",)
    assert "s1" not in bridge._recs
    # ...and it was never recorded as ours, so `--cleanup` will not chase it.
    assert bridge._owned == {}


def test_failed_orphan_space_close_is_recorded_not_raised(seams):
    client = RecordingClient(
        create_results=[{"workspace": {"workspace_id": "wsZ"}, "tab": {},
                         "root_pane": {}}],
        fail={"workspace_close": FakeHerdrError("boom", "cannot close")})
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    assert sum("error s1" in ln for ln in lines) == 2
    assert _names(client) == ["workspace_create", "workspace_close"]
    assert "s1" not in bridge._recs


def test_added_with_tab_id_but_no_pane_id_closes_the_orphan_tab(seams):
    # No workspace id in the reply at all: the tab is the outermost thing we can
    # still name, so it is what gets closed.
    client = RecordingClient(create_results=[{"tab": {"tab_id": "tabZ"},
                                              "root_pane": {}}])
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    assert any("error s1" in ln for ln in lines)
    assert _names(client) == ["workspace_create", "tab_close"]
    assert _call(client, "tab_close")[1] == ("tabZ",)
    assert "s1" not in bridge._recs


def test_failed_orphan_tab_close_is_recorded_not_raised(seams):
    client = RecordingClient(
        create_results=[{"tab": {"tab_id": "tabZ"}, "root_pane": {}}],
        fail={"tab_close": FakeHerdrError("boom", "cannot close")})
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    # Both the malformed-reply line and the failed cleanup line are recorded.
    assert sum("error s1" in ln for ln in lines) == 2
    assert _names(client) == ["workspace_create", "tab_close"]
    assert "s1" not in bridge._recs


def test_added_with_pane_id_but_no_tab_id_closes_the_orphan_pane(seams):
    # No tab id means there is no tab handle to close, but the pane was created;
    # close it directly so it is not left behind.
    client = RecordingClient(create_results=[
        {"tab": {}, "root_pane": {"pane_id": "paneZ"}}])
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    assert any("error s1" in ln for ln in lines)
    assert _names(client) == ["workspace_create", "pane_close"]
    assert _call(client, "pane_close")[1] == ("paneZ",)
    assert "s1" not in bridge._recs


def test_failed_orphan_pane_close_is_recorded_not_raised(seams):
    client = RecordingClient(
        create_results=[{"tab": {}, "root_pane": {"pane_id": "paneZ"}}],
        fail={"pane_close": FakeHerdrError("boom", "cannot close")})
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    assert sum("error s1" in ln for ln in lines) == 2
    assert _names(client) == ["workspace_create", "pane_close"]
    assert "s1" not in bridge._recs


def test_failed_removal_keeps_the_mapping_for_a_retry(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    client.calls.clear()

    client.fail = {"release_agent": FakeHerdrError("boom", "cannot release")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])
    assert any("error s1" in ln for ln in lines)
    # Cleanup failed, so the mapping survives and a later `removed` can retry.
    assert "s1" in bridge._recs
    client.calls.clear()

    again = bridge.reconcile([Ev("removed", "s1", {})])
    assert _names(client) == ["release_agent", "workspace_close"]
    assert "remove s1" in again[0]
    assert "s1" not in bridge._recs


def test_removal_where_herdr_says_not_found_is_treated_as_done(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    client.calls.clear()

    client.fail = {"release_agent": FakeHerdrError("not_found", "pane not found")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])
    # Already-gone is success for us: drop the mapping and do not retry.
    assert "s1" not in bridge._recs
    assert any("remove s1" in ln for ln in lines)
    client.calls.clear()

    assert bridge.reconcile([Ev("removed", "s1", {})]) == []
    assert client.calls == []


def test_release_agent_not_found_still_attempts_the_close(seams):
    # A `not_found` from release_agent means only "there was no marker to
    # release" — it does NOT mean the space is gone (setup may have failed before
    # reporting the marker, leaving it wide open). So the close must still run.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    client.calls.clear()

    client.fail = {"release_agent": FakeHerdrError("not_found", "no marker")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])

    assert _names(client) == ["release_agent", "workspace_close"]
    assert _call(client, "workspace_close")[1] == ("ws1",)
    # The close succeeded, so only now is the mapping dropped.
    assert "s1" not in bridge._recs
    assert any("remove s1" in ln for ln in lines)


def test_release_agent_not_found_then_close_failure_keeps_the_mapping(seams):
    # release_agent says "no marker" (swallowed), but the close then fails with a
    # real error: the space's fate is unknown, so the mapping must survive and a
    # later `removed` must retry the whole cleanup.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    client.calls.clear()

    client.fail = {"release_agent": FakeHerdrError("not_found", "no marker"),
                   "workspace_close": FakeHerdrError("boom", "cannot close")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])
    assert _names(client) == ["release_agent", "workspace_close"]
    assert any("error s1" in ln for ln in lines)
    assert "s1" in bridge._recs
    client.calls.clear()

    again = bridge.reconcile([Ev("removed", "s1", {})])
    assert _names(client) == ["release_agent", "workspace_close"]
    assert "remove s1" in again[0]
    assert "s1" not in bridge._recs


def test_a_close_not_found_is_treated_as_already_gone(seams):
    # Only a `not_found` from the close proves the thing itself is gone (closing
    # a tab's only pane removes the tab, so a later tab.close answers
    # tab_not_found, and a closed workspace answers the same way): drop the
    # mapping and never retry.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    client.calls.clear()

    client.fail = {"workspace_close": FakeHerdrError("not_found", "not_found")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])
    assert _names(client) == ["release_agent", "workspace_close"]
    assert "already gone" in lines[0]
    assert "s1" not in bridge._recs
    client.calls.clear()

    assert bridge.reconcile([Ev("removed", "s1", {})]) == []
    assert client.calls == []


def test_a_space_closed_by_hand_stops_being_recorded_as_ours(seams):
    # `not_found` means somebody closed it, which is the outcome this bridge
    # wanted — so the ownership record is pruned too. Leaving it would make the
    # next `--cleanup` answer `not_found` for a space this bridge closed itself.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    assert bridge._owned == {"ws1": "s1"}

    client.fail = {"workspace_close": FakeHerdrError("not_found", "gone")}
    bridge.reconcile([Ev("removed", "s1", {})])
    assert bridge._owned == {}
    assert json.loads(Path(bridge.state_path).read_text())["spaces"] == {}


def test_underlying_id_lookup_is_key_explicit_not_order_dependent():
    # The live `root_pane` object carries BOTH keys, so an order-sensitive lookup
    # would silently return the wrong one; the caller must name the key.
    root_pane = {"pane_id": "w1:pQ", "terminal_id": "term",
                 "workspace_id": "w1", "tab_id": "w1:tG"}
    tab = {"tab_id": "w1:tG"}
    workspace = {"workspace_id": "w1"}
    assert m._id_of(root_pane, "pane_id") == "w1:pQ"
    assert m._id_of(root_pane, "tab_id") == "w1:tG"
    assert m._id_of(workspace, "workspace_id") == "w1"
    assert m._id_of(tab, "tab_id") == "w1:tG"
    assert m._id_of(tab, "pane_id") is None


def test_fake_client_returns_the_real_api_key_names():
    client = RecordingClient()
    result = client.tab_create("ws", cwd="/tmp", label="x")
    assert set(result) == {"tab", "root_pane"}
    assert "tab_id" in result["tab"] and "id" not in result["tab"]
    assert "pane_id" in result["root_pane"] and "id" not in result["root_pane"]


def test_workspace_create_returns_workspace_tab_and_root_pane_together():
    # The measured reply. The schema's own result listing describes only `type`
    # and `workspace`, so a wrapper that unwrapped to the workspace alone would
    # throw away the two ids the bridge needs and force a second call — which
    # opens a SECOND TAB, not a second pane.
    result = RecordingClient().workspace_create("New Alignment", "/repo")
    assert set(result) == {"type", "workspace", "tab", "root_pane"}
    assert result["workspace"]["workspace_id"] == "ws1"
    assert result["tab"]["tab_id"] == "tab1"
    assert result["root_pane"]["pane_id"] == "pane1"


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------

def test_herdr_error_is_recorded_and_not_fatal(seams):
    client = RecordingClient(fail={"workspace_create": FakeHerdrError(
        "no-sock", "cannot reach herdr")})
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([
        Ev("added", "bad", root_session("bad", state="idle")),
        Ev("added", "good", root_session("good", state="idle")),
    ])
    assert any("error bad" in ln and "no-sock" in ln for ln in lines)
    # the good session still got its space despite the earlier failure
    assert any("add good" in ln for ln in lines)
    assert _names(client).count("workspace_create") == 2


def test_herdr_error_on_update_does_not_abort_remaining_events(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "a", root_session(state="idle"))])
    client.calls.clear()
    # Arm the update's report_agent to fail once; the following add must still go
    # through, proving one bad call does not abort the rest of the batch.
    client.fail = {"report_agent": FakeHerdrError("x", "y")}
    lines = bridge.reconcile([
        Ev("changed", "a", root_session(state="working")),
        Ev("added", "c", root_session(state="idle")),
    ])
    assert any("error a" in ln for ln in lines)
    assert any("add c" in ln for ln in lines)


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------

def test_dry_run_makes_zero_client_calls(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, dry_run=True)
    lines = bridge.reconcile([
        Ev("added", "s1", root_session("T", state="idle")),
        Ev("changed", "s1", root_session("U", state="working"),
           previous=root_session("T", state="idle")),
        Ev("removed", "s1", {}),
    ])
    assert client.calls == []
    assert len(lines) == 3
    assert all("dry-run:" in ln for ln in lines)


def test_dry_run_is_still_idempotent(seams):
    bridge = m.Bridge(FakeWatcher([]), None, dry_run=True)
    first = bridge.reconcile([Ev("added", "s1", root_session("T"))])
    second = bridge.reconcile([Ev("added", "s1", root_session("T"))])
    assert len(first) == 1
    assert second == []


def test_a_dry_run_reports_a_worker_in_its_parents_space(seams):
    # The dry run is the only place this layout can be checked before it exists,
    # so it must describe the layout a real run WOULD produce — including a
    # worker landing beside its parent rather than in a space of its own.
    bridge = m.Bridge(FakeWatcher([]), None, dry_run=True, cwd="/repo")
    lines = bridge.reconcile([
        Ev("added", "w1", sub_session("root", "coder_zen:space-per-root")),
        Ev("added", "root", root_session("New Alignment")),
    ])
    # Roots first, whatever order the listing emitted them in.
    assert lines[0].startswith("dry-run: add 'New Alignment' → space (/repo)")
    assert "→ tab in space dry-run-space:root (/repo)" in lines[1]


# ---------------------------------------------------------------------------
# no elicitation resolution — by contract the bridge only reports `blocked`
# ---------------------------------------------------------------------------

def test_blocked_is_reported_not_resolved(seams, monkeypatch):
    monkeypatch.setattr(m, "_state", lambda s: "blocked")
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T"))])
    ra = _call(client, "report_agent")
    assert ra[2]["state"] == "blocked"
    # only reporting calls happened; nothing that could resolve an elicitation
    assert _names(client) == ["workspace_create", "tab_rename", "pane_run",
                              "report_agent", "report_metadata"]


def test_source_never_calls_an_elicitation_resolver():
    """AST scan: no function the module CALLS is an elicitation resolver.

    Comments and docstrings are allowed to discuss elicitation (the rationale
    lives in one), so this inspects only called identifiers, not prose.
    """
    src = Path(m.__file__).read_text()
    called = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) \
                else getattr(func, "id", "")
            called.add(name.lower())
    assert called, "expected to find call expressions in the module"
    assert not [c for c in called if "elicit" in c or "resolve" in c]


# ---------------------------------------------------------------------------
# watcher wiring
# ---------------------------------------------------------------------------

def test_run_once_polls_and_reconciles(seams):
    client = RecordingClient()
    watcher = FakeWatcher([[Ev("added", "s1", root_session("T"))]])
    bridge = m.Bridge(watcher, client)
    lines = bridge.run_once()
    assert watcher.polls == 1
    assert len(lines) == 1 and "add s1" in lines[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_build_parser_defaults():
    args = m.build_parser().parse_args([])
    assert args.once is False
    assert args.dry_run is False
    assert args.cleanup is False
    assert args.server == "http://127.0.0.1:6767"
    assert args.socket is None
    assert args.source == "og-bridge"


def test_build_parser_overrides():
    args = m.build_parser().parse_args([
        "--once", "--dry-run", "--server", "http://example:1",
        "--socket", "/tmp/s.sock", "--source", "me",
    ])
    assert args.once and args.dry_run
    assert args.server == "http://example:1"
    assert args.socket == "/tmp/s.sock"
    assert args.source == "me"


def test_workspace_flag_is_gone():
    # There is no shared target workspace any more: a root's space is its own,
    # and a worker's tab is its parent's. A `--workspace` here could only be
    # ignored, and an ignored flag that reads like isolation is the exact defect
    # this project already shipped once (tab.create's dropped `workspace` key).
    parser = m.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--workspace", "w1"])
    assert not hasattr(parser.parse_args([]), "workspace")


# ---------------------------------------------------------------------------
# --cleanup: with a space per root there is no single scratch workspace to
# close, so without this a test run leaves the operator closing spaces by hand.
#
# Ownership is recorded in a small state file under $OMNIGENT_HOME, keyed by the
# workspace id herdr minted — never by the label, which is the operator's session
# title verbatim and the one display they asked for.
# ---------------------------------------------------------------------------

PROJECTS = {"workspace_id": "w1", "label": "projects"}


def test_a_created_space_is_recorded_as_ours_by_id(seams):
    bridge = m.Bridge(FakeWatcher([]), RecordingClient())
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])

    recorded = json.loads(Path(bridge.state_path).read_text())
    assert recorded["version"] == m.STATE_VERSION
    assert recorded["pending"] == {}
    # Keyed by SESSION id, not workspace id: `--cleanup` can work either way,
    # but only this way answers "does this session already have a space?", which
    # is what stops a second space for one conversation.
    assert list(recorded["spaces"]) == ["root"], recorded
    entry = recorded["spaces"]["root"]
    assert entry["workspace_id"] == "ws1"
    assert entry["tab_id"] == "tab1" and entry["pane_id"] == "pane1"
    assert entry["owner"] == "bridge"
    # cwd is INFORMATIONAL — cached, never the thing a decision rests on — so it
    # is only written when there was one to cache.
    assert entry["cwd"] == os.getcwd()
    # Under $OMNIGENT_HOME, beside og-quota.json.
    assert bridge.state_path.name == "og-herdr.json"
    assert bridge.state_path.parent == Path(os.environ["OMNIGENT_HOME"])


def test_a_sub_agents_space_is_not_recorded_because_it_created_none(seams):
    bridge = m.Bridge(FakeWatcher([]), RecordingClient())
    bridge.reconcile([
        Ev("added", "root", root_session("New Alignment")),
        Ev("added", "w1", sub_session("root"))])
    assert bridge._owned == {"ws1": "root"}


def test_cleanup_closes_only_what_it_recorded(seams):
    # The operator's own `projects` workspace is in the listing and is not ours.
    # Closing it would take real work with it, and it was never recorded because
    # this bridge never opened it.
    client = RecordingClient(spaces=[PROJECTS, {"workspace_id": "ws1",
                                               "label": "New Alignment"}])
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.calls.clear()

    lines = bridge.cleanup()

    assert _names(client).count("workspace_close") == 1
    assert _call(client, "workspace_close")[1] == ("ws1",)
    assert not [c for c in client.calls
                if c[0] == "workspace_close" and c[1] == ("w1",)], \
        "the operator's projects workspace was closed"
    assert any("closed workspace ws1" in ln for ln in lines)
    # It says what was left, so "the cleanup finished" is checkable rather than
    # something the operator takes on faith.
    assert any("left workspace 'w1'" in ln and "projects" in ln for ln in lines), lines


def test_cleanup_is_idempotent_because_the_record_is_pruned(seams):
    client = RecordingClient(spaces=[PROJECTS])
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])

    bridge.cleanup()
    client.calls.clear()
    lines = bridge.cleanup()

    assert "workspace_close" not in _names(client), (
        "a second cleanup re-closed a space the first one already closed")
    assert any("nothing to close" in ln for ln in lines)


def test_dry_run_cleanup_closes_nothing_and_makes_no_call_at_all(seams):
    client = RecordingClient(spaces=[PROJECTS])
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.calls.clear()

    dry = m.Bridge(FakeWatcher([]), None, dry_run=True)
    assert dry.state_path == bridge.state_path
    lines = dry.cleanup()

    assert client.calls == [], "the dry run reached the live client"
    assert not bridge.state_path.exists() or \
        json.loads(bridge.state_path.read_text())["spaces"] == {
            "root": bridge._spaces["root"]}, \
        "a dry-run cleanup rewrote the ownership record"
    assert any(ln.startswith("dry-run cleanup: close workspace ws1")
               for ln in lines), lines
    assert any("would be closed" in ln for ln in lines), lines


def test_cleanup_with_no_state_file_says_so_and_closes_nothing(seams, tmp_path):
    # A missing file is the normal case — nothing has been created, or
    # everything already was. It is not an error, and it is said out loud so a
    # silent no-op is distinguishable from a clean run.
    client = RecordingClient(spaces=[PROJECTS])
    bridge = m.Bridge(FakeWatcher([]), client, state_path=tmp_path / "absent.json")

    lines = bridge.cleanup()

    assert _names(client) == ["workspace_list", "workspace_list"]
    assert any("no state file" in ln and "absent.json" in ln for ln in lines), lines
    assert any("nothing to close" in ln for ln in lines), lines


@pytest.mark.parametrize("name,content,expected", [
    ("stale.json", json.dumps({"version": 0, "spaces": {}}), "is not a v2 state"),
    ("corrupt.json", "{ nope", "not valid JSON"),
    ("wrong.json", json.dumps(["w1"]), "is not a v2 state"),
])
def test_a_stale_or_unreadable_state_file_is_not_an_error(
        seams, tmp_path, name, content, expected):
    # A stale file closes nothing rather than closing everything: an answer we
    # cannot read must not become permission to guess at a workspace id. The
    # expectation is the BEHAVIOUR ("reads as nothing, says so") and not one
    # version number spelled out — the note names the version the module wants,
    # which is exactly the thing a bump changes and nothing else.
    client = RecordingClient(spaces=[PROJECTS])
    path = tmp_path / name
    path.write_text(content)
    bridge = m.Bridge(FakeWatcher([]), client, state_path=path)

    lines = bridge.cleanup()

    assert _names(client) == ["workspace_list", "workspace_list"]
    assert any(expected in ln for ln in lines), lines
    assert any("nothing to close" in ln for ln in lines), lines
    assert bridge._spaces == {} and bridge._pending == {}


def test_a_state_file_of_wrong_shaped_entries_keeps_only_usable_ones(
        seams, tmp_path):
    client = RecordingClient(spaces=[PROJECTS])
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps({"version": m.STATE_VERSION, "spaces": {
        "s1": {"workspace_id": "ws1"},
        "": {"workspace_id": "wsZ"},             # no session key
        "s2": {"workspace_id": None},            # no usable workspace id
        "s3": "ws3",                             # not a record at all
        "s4": {"workspace_id": "ws4", "tab_id": 7},   # non-string id kept out
    }}))
    bridge = m.Bridge(FakeWatcher([]), client, state_path=path)
    # Read back BEFORE the cleanup, which prunes what it closes.
    assert bridge._spaces == {"s1": {"workspace_id": "ws1"},
                              "s4": {"workspace_id": "ws4"}}, bridge._spaces

    bridge.cleanup()
    # Only the two well-formed records survive, and the close order is the
    # report's order — deterministic, so an operator reading the log knows
    # which close is which.
    assert [c[1] for c in client.calls if c[0] == "workspace_close"] == \
        [("ws1",), ("ws4",)]
    assert bridge._spaces == {}


def test_a_cleanup_failure_keeps_the_record_for_a_later_run(seams):
    # A transient failure must not turn one space into a space nobody ever
    # closes — the operator would have no way to tell which one it was.
    client = RecordingClient(fail={"workspace_close": FakeHerdrError(
        "boom", "cannot close")})
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])

    lines = bridge.cleanup()
    assert any("error root" in ln and "boom" in ln for ln in lines), lines
    assert bridge._owned == {"ws1": "root"}


def test_a_cleanup_of_an_already_gone_space_counts_as_done(seams):
    client = RecordingClient(fail={"workspace_close": FakeHerdrError(
        "not_found", "workspace_not_found")})
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])

    lines = bridge.cleanup()
    assert any("already gone" in ln for ln in lines), lines
    assert bridge._owned == {}


def test_an_unreadable_workspace_list_does_not_skip_the_cleanup(seams):
    # The listing only makes the REPORT readable. A failure there must not stop
    # the closes, which are the part that matters.
    client = RecordingClient(fail={"workspace_list": FakeHerdrError(
        "boom", "no such thing")})
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.calls.clear()

    lines = bridge.cleanup()
    assert _call(client, "workspace_close")[1] == ("ws1",)
    assert any("cannot list workspaces" in ln for ln in lines), lines


def test_cleanup_runs_without_a_watcher_or_a_poll(monkeypatch, capsys):
    # `og herdr --cleanup` is about herdr. Constructing a SessionWatcher would
    # mean an HTTP round trip whose only possible effect is to start projecting
    # the sessions we are in the middle of closing.
    import sys
    import types

    boom = types.ModuleType("og_herdr_watch")

    def refuse(*args, **kwargs):
        raise AssertionError("--cleanup must not build a watcher")

    boom.SessionWatcher = refuse
    monkeypatch.setitem(sys.modules, "og_herdr_watch", boom)

    client = RecordingClient(spaces=[PROJECTS])
    client_module = types.ModuleType("og_herdr_client")
    client_module.HerdrClient = lambda socket_path=None: client
    client_module.HerdrError = FakeHerdrError
    monkeypatch.setitem(sys.modules, "og_herdr_client", client_module)

    home = Path(os.environ["OMNIGENT_HOME"]) / "og-herdr.json"
    m.save_state(home, {"spaces": {"root": {"workspace_id": "ws1",
                                            "owner": "bridge"}},
                        "pending": {}})

    assert m.main(["--cleanup"]) == 0
    out = capsys.readouterr().out
    assert [c[1] for c in client.calls if c[0] == "workspace_close"] == [("ws1",)]
    assert "closed workspace ws1" in out
    assert "left workspace 'w1'" in out
    assert json.loads(home.read_text())["spaces"] == {}


def test_build_parser_exposes_cwd_with_process_cwd_default():
    args = m.build_parser().parse_args([])
    # The documented default IS the bridge's own cwd, so assert against the same
    # source rather than a hardcoded path.
    assert args.cwd == os.getcwd()
    overridden = m.build_parser().parse_args(["--cwd", "/srv/repo"])
    assert overridden.cwd == "/srv/repo"


def test_siblings_are_imported_lazily_not_at_module_load():
    """og_herdr.py must import cleanly even when a sibling module is missing.

    The siblings live on other branches, so at any moment one or both may be
    absent from the tree — the bridge therefore must not import them at module
    load, only inside the functions that use them. Asserting the files are
    ABSENT on disk is not evidence of that (and inverts the moment the branches
    batch together), and checking sys.modules is order-dependent because other
    test files import the siblings. So parse og_herdr.py and split its imports
    by where they execute: an import reachable only through a function/lambda
    body runs lazily, a module-level one runs on first import.
    """
    def imports(node, inside_function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inside_function = True
        found = []
        if isinstance(node, ast.Import):
            found += [(a.name, inside_function) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", inside_function))
        for child in ast.iter_child_nodes(node):
            found += imports(child, inside_function)
        return found

    def is_sibling(name):
        return name in ("og_herdr_client", "og_herdr_watch") or \
            name.startswith(("og_herdr_client.", "og_herdr_watch."))

    tree = ast.parse(Path(m.__file__).read_text())
    found = imports(tree, False)
    eager = {name for name, inside in found if not inside}
    lazy = {name for name, inside in found if inside}

    assert not [name for name in eager if is_sibling(name)], \
        "module load must not pull in the siblings"
    # Positive control: the lazy imports are actually there, so a refactor that
    # simply deleted them cannot make this test pass vacuously.
    assert "og_herdr_watch" in lazy
    assert "og_herdr_client" in lazy


# ---------------------------------------------------------------------------
# the state file at v2: spaces keyed by SESSION, pending keyed by CWD
#
# v1 was `{"version": 1, "workspaces": {workspace_id: session_id}}`, which
# answers "is this space ours?" — all `--cleanup` needs — but not "does this
# session already have a space?", which is the whole of adoption. Once `og start
# <mux>` exists, a space may be on screen for a session the bridge has no memory
# of: the launcher made it, or the bridge restarted and its map died with the
# process. Without the reverse lookup the bridge opens a second space for a
# conversation that already has one.
#
# Every fake here is local: `RecordingClient` records calls and answers from its
# own `spaces` list. Nothing in this section reaches a socket.
# ---------------------------------------------------------------------------

def space_info(ws_id, label, pane_count=1, tab_count=1):
    """A `workspace.list` row, in the shape the vendored schema requires.

    `pane_count` is what confirms a record's pane can still be addressed: a
    workspace reporting no panes cannot hold the pane a record names.
    """
    return {"workspace_id": ws_id, "label": label, "pane_count": pane_count,
            "tab_count": tab_count, "active_tab_id": ws_id + ":t1"}


def write_state(home, **halves):
    """Write a v2 state file by hand and return its path."""
    path = Path(home) / m.STATE_FILENAME
    path.write_text(json.dumps({"version": m.STATE_VERSION, **halves}))
    return path


# --- 1. v1 upgrades in place, and --cleanup still closes what it described ----

def test_a_v1_state_file_is_upgraded_in_place_and_still_closed(seams, tmp_path):
    # THE REGRESSION THIS SHAPE HAD TO AVOID. A v1 file can describe spaces that
    # are live on the operator's screen right now. Discarding it would leave
    # those spaces untracked — no `--cleanup` would ever close them, and the
    # operator closes them by hand, one per conversation, in a multiplexer where
    # a mis-click loses their work.
    v1 = Path(os.environ["OMNIGENT_HOME"]) / m.STATE_FILENAME
    v1.write_text(json.dumps({"version": 1, "workspaces": {"w9": "s9", "wA": "sA"}}))

    client = RecordingClient(spaces=[PROJECTS, space_info("w9", "nine"),
                                     space_info("wA", "A")])
    bridge = m.Bridge(FakeWatcher([]), client)

    # Migrated at READ time, so even a run that only reads migrates it — and the
    # file is v2 on disk before anything is closed.
    upgraded = json.loads(v1.read_text())
    assert upgraded["version"] == m.STATE_VERSION
    assert upgraded["spaces"] == {"s9": {"workspace_id": "w9",
                                         "owner": "bridge"},
                                  "sA": {"workspace_id": "wA",
                                         "owner": "bridge"}}
    assert upgraded["pending"] == {}
    # Nothing was invented: v1 recorded no pane and no tab, and a guessed pane
    # id is exactly what a stale adoption hands to every later report_agent.
    for record in upgraded["spaces"].values():
        assert "tab_id" not in record and "pane_id" not in record

    lines = bridge.cleanup()

    # The workspace ids survived the transpose — they are the only thing in a
    # v1 record, and `--cleanup` closes by id.
    assert [c[1] for c in client.calls if c[0] == "workspace_close"] == \
        [("w9",), ("wA",)]
    assert any("upgraded 2 record(s) to v2" in ln for ln in lines), lines
    # ...and nothing was left behind for a second cleanup to chase.
    assert json.loads(v1.read_text())["spaces"] == {}
    assert bridge._spaces == {}


def test_an_upgraded_v1_file_is_closed_only_once(seams):
    v1 = Path(os.environ["OMNIGENT_HOME"]) / m.STATE_FILENAME
    v1.write_text(json.dumps({"version": 1, "workspaces": {"w9": "s9"}}))
    bridge = m.Bridge(FakeWatcher([]), RecordingClient(spaces=[space_info("w9", "n")]))
    bridge.cleanup()
    # The migrated record is pruned, so a second cleanup has nothing to chase.
    assert json.loads(v1.read_text())["spaces"] == {}


# --- 2. a session the file already has a space for is ADOPTED -----------------

def test_a_session_with_a_space_record_is_adopted_not_duplicated(seams):
    # What a bridge restart looks like: the file remembers the space, this
    # process does not. Opening a second one would give the operator two spaces
    # for one conversation, which is the entire problem adoption exists to fix.
    client = RecordingClient(spaces=[space_info("wA", "New Alignment")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={
        "s1": {"workspace_id": "wA", "tab_id": "wA:t1", "pane_id": "wA:p1",
               "owner": "bridge", "cwd": "/repo/og"}}, pending={})
    bridge = m.Bridge(FakeWatcher([]), client)

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "New Alignment", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert _names(client).count("tab_create") == 0
    assert _names(client) == ["workspace_list"]
    assert lines[0].startswith("adopt s1 → space wA tab wA:t1 pane wA:p1"), lines

    # The in-memory record was rebuilt FROM THE FILE, not re-derived: same
    # three ids, and `owns_space` follows the recorded owner.
    rec = bridge._recs["s1"]
    assert (rec["workspace_id"], rec["tab_id"], rec["pane_id"]) == \
        ("wA", "wA:t1", "wA:p1")
    assert rec["owns_space"] is True and rec["parent"] is None
    assert rec["ready"] is True


def test_adoption_does_not_retype_the_attach_command_into_a_live_pane(seams):
    # The pane is live and already running whatever it should run. Re-running
    # setup would type `omnigent attach` a second time — and for a launcher's
    # space that is a SECOND co-drive client on one session, which is the race
    # `_state` exists to avoid: Omnigent parks a single Future for an
    # elicitation, so the first resolver wins and every other client gets
    # `not_found`.
    client = RecordingClient(spaces=[space_info("wA", "New Alignment")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={
        "s1": {"workspace_id": "wA", "tab_id": "wA:t1", "pane_id": "wA:p1",
               "owner": "bridge"}}, pending={})
    m.Bridge(FakeWatcher([]), client).reconcile(
        [Ev("added", "s1", root_session("New Alignment"))])

    for name in ("pane_run", "report_agent", "report_metadata"):
        assert name not in _names(client), \
            f"adoption re-ran {name} on a pane that already has it"


def test_adoption_refreshes_the_cached_cwd_from_the_session(seams):
    # `cwd` in the file is INFORMATIONAL; the session's own `workspace` from the
    # API is authoritative. The claim is made against the LIVE value, and the
    # cache is refreshed from it — never the other way round.
    client = RecordingClient(spaces=[space_info("wB", "Moved")])
    write_state(os.environ["OMNIGENT_HOME"], pending={
        "/old/path": {"workspace_id": "wB", "tab_id": "wB:t1",
                      "pane_id": "wB:p1", "owner": "launcher",
                      "cwd": "/old/path"}}, spaces={})
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("Moved",
                                                    workspace="/new/path"))])

    assert bridge._spaces["s1"]["cwd"] == "/new/path"


# --- 3. a stale spaces record is NOT adopted ---------------------------------

def test_a_spaces_record_naming_a_dead_workspace_is_dropped_not_adopted(seams):
    # THE GUARD. Adopting this would hand the bridge a pane id that does not
    # exist, and every later `report_agent` would fail against it — a failure
    # the operator sees as a session with no badge rather than as a stale file.
    # A duplicate space is the cheaper mistake: it is visible and closable.
    client = RecordingClient(spaces=[space_info("w1", "Fresh")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={
        "s1": {"workspace_id": "wGone", "tab_id": "wGone:t1",
               "pane_id": "wGone:p1", "owner": "bridge"}}, pending={})
    bridge = m.Bridge(FakeWatcher([]), client)

    lines = bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])

    assert _names(client).count("workspace_create") == 1
    assert _call(client, "workspace_create")[1] == ("T",)
    assert any("wGone" in ln and "stale" in ln for ln in lines), lines
    # The stale record is REPLACED, not merely shadowed: the fresh space is
    # recorded under the same session id, and the dead id is nowhere in the file
    # or in memory — otherwise the next restart would make the same decision.
    assert bridge._spaces == {"s1": bridge._spaces["s1"]}
    assert bridge._spaces["s1"]["workspace_id"] == "ws1"
    assert "wGone" not in json.dumps(bridge._spaces)
    assert "wGone" not in Path(bridge.state_path).read_text()
    assert bridge._recs["s1"]["workspace_id"] == "ws1"


def test_an_unreadable_workspace_list_adopts_nothing_and_prunes_nothing(seams):
    # "Cannot tell" is not "absent". Treating an unreadable listing as an empty
    # one would turn one transient herdr hiccup into a duplicate space for every
    # live session — and into the silent loss of every pending record.
    client = RecordingClient(
        fail={"workspace_list": FakeHerdrError("boom", "cannot list")})
    write_state(os.environ["OMNIGENT_HOME"],
                spaces={"s1": {"workspace_id": "wA", "tab_id": "wA:t1",
                               "pane_id": "wA:p1", "owner": "bridge"}},
                pending={"/repo": {"workspace_id": "wB", "tab_id": "wB:t1",
                                   "pane_id": "wB:p1", "owner": "launcher"}})
    bridge = m.Bridge(FakeWatcher([]), client)

    lines = bridge.reconcile([Ev("added", "s1", root_session("T"))])

    assert any("cannot list workspaces" in ln for ln in lines), lines
    assert not any("pruned" in ln for ln in lines), lines
    # Both records survive untouched: nothing was proven dead.
    assert set(bridge._spaces) == {"s1"}
    assert set(bridge._pending) == {"/repo"}


# --- 4/5. the launcher handshake: a pending record is CLAIMED -----------------

def test_a_pending_record_for_this_directory_is_claimed_not_duplicated(seams):
    # `og start <mux>` case (A): the launcher opens a space and types `og chat`
    # into it BEFORE any session exists, so it has nothing to key a record by.
    # It writes `pending[cwd]` and execs herdr. This is the session that space
    # belongs to — and the old code would have opened a second space for it.
    client = RecordingClient(spaces=[space_info("wB", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1", "pane_id": "wB:p1",
                     "owner": "launcher", "cwd": "/repo/og"}})
    bridge = m.Bridge(FakeWatcher([]), client)

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert _names(client).count("tab_create") == 0
    assert lines[0].startswith("claim s1 → space wB"), lines

    # MOVED, not copied: the pending half is empty and the record now hangs off
    # the session id, which is the only key adoption can look up.
    saved = json.loads(Path(bridge.state_path).read_text())
    assert saved["pending"] == {}
    assert saved["spaces"]["s1"]["workspace_id"] == "wB"
    # owner stays `launcher`: claiming transfers nothing, and a record claiming
    # otherwise would misreport who to blame for a stray space.
    assert saved["spaces"]["s1"]["owner"] == "launcher"
    # ...and a launcher-made space is closed by TAB on removal, not by space.
    assert bridge._recs["s1"]["owns_space"] is False


def test_a_pending_record_for_a_different_directory_is_never_claimed(seams):
    # The match is EXACT, deliberately. A prefix or substring match would hand
    # this session the space of a sibling checkout — and that is the one error
    # here that no close or rename undoes.
    client = RecordingClient(spaces=[space_info("wB", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1", "pane_id": "wB:p1",
                     "owner": "launcher", "cwd": "/repo/og"}})
    bridge = m.Bridge(FakeWatcher([]), client)

    bridge.reconcile([Ev("added", "s1", root_session("T", workspace="/repo/og-2"))])

    assert _names(client).count("workspace_create") == 1
    assert set(bridge._pending) == {"/repo/og"}


# --- 4/5. the SAME handshake, but the launcher writes the file LATER ----------
#
# `og start herdr` starts the bridge BEFORE it execs the launcher (see `bin/og`),
# and the launcher writes its record and exits. So on a fresh `og start` the
# launcher's record is, by construction, newer than anything the bridge has read
# — and a bridge that reads the file once, at construction, cannot see it. The
# dedupe misses, the bridge concludes the session has no space, and opens a
# second one: one operator's file held three spaces for one directory, two of
# them the bridge's own. Adoption only ever worked across a bridge RESTART,
# which is exactly why the duplicate looked like it came and went.

def test_a_pending_record_the_launcher_wrote_after_the_bridge_started_is_claimed(
        seams):
    # The live failure, in the order it happens. No state file at all when the
    # bridge starts; the launcher writes one while the bridge is already
    # running, exactly as `og start` does it.
    client = RecordingClient(spaces=[space_info("wB", "og")])
    bridge = m.Bridge(FakeWatcher([]), client)
    assert not Path(bridge.state_path).exists()
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1", "pane_id": "wB:p1",
                     "owner": "launcher", "cwd": "/repo/og"}})

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    # The whole point: one space for the directory, the launcher's, and no
    # `workspace_create` anywhere in the pass.
    assert _names(client).count("workspace_create") == 0
    assert _names(client).count("tab_create") == 0
    assert lines[0].startswith("claim s1 → space wB"), lines
    saved = json.loads(Path(bridge.state_path).read_text())
    assert saved["pending"] == {}
    assert saved["spaces"]["s1"]["workspace_id"] == "wB"
    assert saved["spaces"]["s1"]["owner"] == "launcher"
    assert bridge._recs["s1"]["owns_space"] is False


def test_a_spaces_record_the_launcher_wrote_after_the_bridge_started_is_adopted(
        seams):
    # The resume path writes `spaces`, not `pending` (og_herdr_launch opens the
    # space itself once the session id is known), so the same staleness made a
    # second space there too — the `w13 owner=history` half of the same file.
    client = RecordingClient(spaces=[space_info("wA", "og")])
    bridge = m.Bridge(FakeWatcher([]), client)
    write_state(os.environ["OMNIGENT_HOME"], spaces={
        "s1": {"workspace_id": "wA", "tab_id": "wA:t1", "pane_id": "wA:p1",
               "owner": "launcher", "cwd": "/repo/og"}}, pending={})

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("adopt s1 → space wA tab wA:t1 pane wA:p1"), lines


def test_pruning_a_dead_record_does_not_delete_the_one_written_after_it(seams):
    # WHY the reload sits where it does. `_prune_pending` SAVES, and it saves
    # this process's whole copy of the file. Run against the construction-time
    # snapshot, a prune of one dead record would write out a `pending` map that
    # never contained the launcher's — deleting a record no process would then
    # hold, so the session that follows opens a duplicate and the next restart
    # finds nothing to adopt either.
    client = RecordingClient(spaces=[space_info("wNEW", "og")])
    # The bridge starts holding a record for a space the operator has closed.
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/gone": {"workspace_id": "wGone", "tab_id": "wGone:t1",
                       "pane_id": "wGone:p1", "owner": "launcher"}})
    bridge = m.Bridge(FakeWatcher([]), client)
    # The launcher then writes a fresh space for this directory.
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/gone": {"workspace_id": "wGone", "tab_id": "wGone:t1",
                       "pane_id": "wGone:p1", "owner": "launcher"},
        "/repo/og": {"workspace_id": "wNEW", "tab_id": "wNEW:t1",
                     "pane_id": "wNEW:p1", "owner": "launcher",
                     "cwd": "/repo/og"}})

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    assert any("pruned 1 pending" in ln for ln in lines), lines
    assert _names(client).count("workspace_create") == 0
    assert lines[-1].startswith("claim s1 → space wNEW"), lines
    saved = json.loads(Path(bridge.state_path).read_text())
    assert saved["pending"] == {}
    assert saved["spaces"]["s1"]["workspace_id"] == "wNEW"


def test_the_bridges_own_record_outranks_a_file_that_has_lost_it(seams):
    # MERGE, not replace. Memory is allowed to be AHEAD of the file — `save_state`
    # swallows a write that cannot complete, and the launcher's whole-file writes
    # land last-writer-wins — and a replacing reload would then hand this bridge
    # a session it had already put a live pane on, which is a duplicate space
    # for a conversation the operator is already watching.
    client = RecordingClient(spaces=[space_info("w1", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={})
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge._remember("s1", "w1", tab_id="w1:t1", pane_id="w1:p1",
                     cwd="/repo/og")
    # The file no longer carries the record the bridge just wrote.
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={})

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("adopt s1 → space w1 tab w1:t1 pane w1:p1"), lines


def test_a_state_file_that_vanished_changes_nothing(seams):
    # A reload has to fail TOWARD the view the bridge already holds. A file it
    # cannot read is not evidence that a space is recorded nowhere, and believing
    # that is precisely what opens the second space.
    client = RecordingClient(spaces=[space_info("w1", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={
        "s1": {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1",
               "owner": "bridge", "cwd": "/repo/og"}}, pending={})
    bridge = m.Bridge(FakeWatcher([]), client)
    Path(bridge.state_path).unlink()

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("adopt s1 → space w1 tab w1:t1 pane w1:p1"), lines
    # ...and the save at the end of the adopt path put the file back.
    assert json.loads(Path(bridge.state_path).read_text())["spaces"]["s1"][
        "workspace_id"] == "w1"


def test_a_corrupt_state_file_does_not_erase_what_the_bridge_already_knew(seams):
    # The other unreadable case. `load_state` reads a file that is not JSON as
    # "we recorded nothing" — correct for `--cleanup`, which then closes nothing
    # rather than closing the operator's own workspaces. For a LIVE bridge the
    # same answer must not be allowed to reach its maps, and the additive merge
    # is what guarantees it: two empty maps merged in change nothing at all.
    client = RecordingClient(spaces=[space_info("w1", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={
        "s1": {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1",
               "owner": "bridge", "cwd": "/repo/og"}}, pending={})
    bridge = m.Bridge(FakeWatcher([]), client)
    Path(bridge.state_path).write_text("{ this is not json at all")

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("adopt s1 → space w1 tab w1:t1 pane w1:p1"), lines


def test_an_empty_state_file_changes_nothing(seams):
    # Truncated to nothing by whatever wrote it last. Same answer as missing.
    client = RecordingClient(spaces=[space_info("w1", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={
        "s1": {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1",
               "owner": "bridge", "cwd": "/repo/og"}}, pending={})
    bridge = m.Bridge(FakeWatcher([]), client)
    Path(bridge.state_path).write_text("")

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("adopt s1 → space w1 tab w1:t1 pane w1:p1"), lines


# --- 4/6. two writers on ONE key: who decides, and how old is the answer -----
#
# The fold is per key, and a key is not one writer's. The launcher keys
# `pending` by DIRECTORY and `_record` replaces whatever is under that key
# (og_herdr_launch._record) — so a second `og start herdr` in a directory whose
# first conversation has not appeared yet lands a second launcher record on top
# of the first, under the identical key, with the first space still open and
# still listed. "I already hold this key" is not a reason to keep what I hold.

def test_a_pending_record_replaced_under_the_same_directory_is_not_the_old_one(
        seams):
    # The double `og start`, in the order it happens. The operator starts a
    # conversation in /repo/og; `og chat` takes a moment to register the session,
    # and they start again — so the launcher's second space is the one whose
    # record survives, and the first space is now a record nobody will claim.
    client = RecordingClient(spaces=[space_info("wOld", "og"),
                                     space_info("wNew", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wOld", "tab_id": "wOld:t1",
                     "pane_id": "wOld:p1", "owner": "launcher",
                     "cwd": "/repo/og"}})
    bridge = m.Bridge(FakeWatcher([]), client)
    # `_record` writes the whole map: the SECOND record replaces the first under
    # the same key, it does not join it.
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wNew", "tab_id": "wNew:t1",
                     "pane_id": "wNew:p1", "owner": "launcher",
                     "cwd": "/repo/og"}})

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    # The space the operator is SITTING in, not the one whose record it
    # replaced. Claiming wOld would have opened a second client on one session in
    # a pane they are not looking at, which is the same co-drive race
    # `_existing_space` refuses to enter anywhere else.
    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("claim s1 → space wNew tab wNew:t1"), lines
    saved = json.loads(Path(bridge.state_path).read_text())
    assert saved["spaces"]["s1"]["workspace_id"] == "wNew"


def test_a_record_this_process_could_not_write_out_outranks_the_file(
        seams, monkeypatch):
    # THE OTHER SIDE of the same rule, and the reason it is not "the file wins".
    # `save_state` swallows a write it could not complete, so memory can be
    # ahead of the file — a record here names the space this bridge has already
    # typed an attach command into, and the file's copy is the one that lost the
    # race. Rolling back to it would put the session on a workspace the operator
    # never asked for.
    #
    # `_write_state` is stubbed to report a write that did not land, which is the
    # only honest way to reach this: `_save_state` re-snapshots precisely when it
    # is told the write completed, and `_write_state` is where every write now
    # lands (`save_state` and `state_update` only decide and lock around it).
    client = RecordingClient(spaces=[space_info("wOld", "og"),
                                     space_info("wNew", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={
        "s1": {"workspace_id": "wOld", "tab_id": "wOld:t1", "pane_id": "wOld:p1",
               "owner": "bridge", "cwd": "/repo/og"}}, pending={})
    bridge = m.Bridge(FakeWatcher([]), client)
    monkeypatch.setattr(m, "_write_state", lambda path, state: False)
    # The bridge adopts a fresh space for this session. Nothing reaches the file.
    bridge._remember("s1", "wNew", tab_id="wNew:t1", pane_id="wNew:p1",
                     cwd="/repo/og")

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("adopt s1 → space wNew tab wNew:t1"), lines
    # ...and the file still says what it said before, which is the point: memory
    # is ahead of it and knows that it is.
    assert json.loads(Path(bridge.state_path).read_text())["spaces"]["s1"][
        "workspace_id"] == "wOld"


def test_a_record_the_launcher_writes_mid_pass_is_still_claimed(seams):
    # THE RACE INSIDE A PASS. The fold at the pass boundary is read before
    # `_prune_pending`'s `workspace.list` round trip and before every event ahead
    # of this one; a launcher that writes inside that gap is invisible to it. In
    # production that gap is the whole of `run_forever`'s single-event pass
    # (og_herdr_watch.watch yields one event at a time), so "the batch" is one
    # event and the window is the pass's own herdr call.
    #
    # The write is therefore fired from the fake's `workspace_list` — the exact
    # instant the launcher would land it, which is after the bridge has read the
    # file and before it has decided.
    class LateWrite(RecordingClient):
        def workspace_list(self):
            write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
                "/repo/other": {"workspace_id": "wOther", "tab_id": "wOther:t1",
                                "pane_id": "wOther:p1", "owner": "launcher",
                                "cwd": "/repo/other"},
                "/repo/og": {"workspace_id": "wNew", "tab_id": "wNew:t1",
                             "pane_id": "wNew:p1", "owner": "launcher",
                             "cwd": "/repo/og"}})
            return super().workspace_list()

    client = LateWrite(spaces=[space_info("wOther", "other"),
                               space_info("wNew", "og")])
    # The bridge starts already holding one pending record — so `_prune_pending`
    # asks herdr for the listing at all, which is what fires the write.
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/other": {"workspace_id": "wOther", "tab_id": "wOther:t1",
                        "pane_id": "wOther:p1", "owner": "launcher",
                        "cwd": "/repo/other"}})
    bridge = m.Bridge(FakeWatcher([]), client)

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("claim s1 → space wNew tab wNew:t1"), lines
    saved = json.loads(Path(bridge.state_path).read_text())
    assert saved["spaces"]["s1"]["workspace_id"] == "wNew"
    # The record that was already there is untouched by the pass that read a
    # later version of the file.
    assert saved["pending"]["/repo/other"]["workspace_id"] == "wOther"


# --- 4/7. a CLAIM is not an unsaved edit --------------------------------------
#
# The claim MOVES `pending[cwd]` into `spaces[sid]`, so for that key memory
# stops holding what `_synced` says it held — exactly what an unflushed local
# edit looks like from the other side of `Bridge._memory_decides`. If that
# difference is read as "protect my edit", the bridge cannot honour the
# launcher's next record for a directory it has already claimed the first record
# of, and the session for that launch opens a second space. The write that would
# have told the file about the claim is what separates the two situations, so
# these tests break the write on purpose: a claim whose save never landed is
# indistinguishable from an edit unless the rule looks at the RECORD.

FIRST_LAUNCH = {"workspace_id": "wOld", "tab_id": "wOld:t1",
                "pane_id": "wOld:p1", "owner": "launcher", "cwd": "/repo/og"}
SECOND_LAUNCH = {"workspace_id": "wNew", "tab_id": "wNew:t1",
                 "pane_id": "wNew:p1", "owner": "launcher", "cwd": "/repo/og"}


def test_a_launcher_replacement_after_a_claim_that_could_not_be_saved_is_claimed(
        seams, monkeypatch):
    # The bridge claims the first record for /repo/og; the write that would say so
    # fails (a read-only home, a full disk), so `_synced` still lists that key.
    # The operator starts a SECOND conversation in the same directory, and
    # `_record` REPLACES the record under the identical key.
    client = RecordingClient(spaces=[space_info("wOld", "og"),
                                     space_info("wNew", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={},
                pending={"/repo/og": dict(FIRST_LAUNCH)})
    bridge = m.Bridge(FakeWatcher([]), client)
    real_write = m._write_state
    failing = {"on": True}
    monkeypatch.setattr(m, "_write_state",
                        lambda path, state: False if failing["on"]
                        else real_write(path, state))

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "First", workspace="/repo/og"))])

    assert lines[0].startswith("claim s1 → space wOld"), lines
    # The claim is complete in memory and the file never heard about it.
    assert bridge._pending == {}
    assert "/repo/og" in bridge._synced["pending"]
    assert json.loads(Path(bridge.state_path).read_text())["pending"][
        "/repo/og"]["workspace_id"] == "wOld"

    # The second launch, with writes working again.
    failing["on"] = False
    write_state(os.environ["OMNIGENT_HOME"], spaces={},
                pending={"/repo/og": dict(SECOND_LAUNCH)})
    lines = bridge.reconcile([Ev("added", "s2", root_session(
        "Second", workspace="/repo/og"))])

    # wNew is what the operator is sitting in. Refusing it because the key is
    # "already dealt with" opens a second space for a directory that has one.
    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("claim s2 → space wNew"), lines


def test_a_claimed_record_whose_save_never_landed_is_not_offered_again(
        seams, monkeypatch):
    # THE OTHER SIDE, and the reason the rule cannot simply say "a key memory no
    # longer holds belongs to whoever wrote the file last". Here the file's copy
    # IS the record this process just claimed: memory dropped it because it moved
    # it into `spaces[s1]`, and the save that would have said so failed. Folding
    # it back would put the space s1 now holds into `pending` again, where the
    # next session in this directory claims it — a second client on one session,
    # which is the co-drive race `_existing_space` refuses to enter anywhere.
    client = RecordingClient(spaces=[space_info("wOld", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={},
                pending={"/repo/og": dict(FIRST_LAUNCH)})
    bridge = m.Bridge(FakeWatcher([]), client)
    monkeypatch.setattr(m, "_write_state", lambda path, state: False)

    bridge.reconcile([Ev("added", "s1", root_session("First",
                                                     workspace="/repo/og"))])

    lines = bridge.reconcile([Ev("added", "s2", root_session(
        "Second", workspace="/repo/og"))])

    # s2 gets a space of its own rather than s1's.
    assert lines[0].startswith("add s2 → space ws1"), lines
    assert bridge._pending == {}
    assert bridge._spaces["s1"]["workspace_id"] == "wOld"


def test_a_launcher_record_written_after_a_claimed_one_is_still_claimable(seams):
    # The same rule with the write landing, which is the ordinary shape: the
    # claim's save succeeds, `_synced` no longer lists the key, and the
    # launcher's next record for the directory is simply a new key to this
    # process. Written so the two tests above cannot be satisfied by a rule that
    # only ever folds fresh keys — the case above needs the memory-lacks-key
    # branch to look at the record, and this one needs it to let go.
    client = RecordingClient(spaces=[space_info("wOld", "og"),
                                     space_info("wNew", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={},
                pending={"/repo/og": dict(FIRST_LAUNCH)})
    bridge = m.Bridge(FakeWatcher([]), client)

    bridge.reconcile([Ev("added", "s1", root_session("First",
                                                     workspace="/repo/og"))])
    write_state(os.environ["OMNIGENT_HOME"], spaces={},
                pending={"/repo/og": dict(SECOND_LAUNCH)})
    lines = bridge.reconcile([Ev("added", "s2", root_session(
        "Second", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("claim s2 → space wNew"), lines


def test_a_launcher_write_landing_inside_a_claim_is_not_erased_by_it(seams,
                                                                    monkeypatch):
    # THE WINDOW BETWEEN THE RE-READ AND THE POP. Re-reading before the lookup
    # does not close it, it moves it: a launcher that replaces `pending[cwd]`
    # after the claim's read has still lost, because the claim then pops the key
    # it read and its save writes a `pending` map with no record for that
    # directory at all. The space the operator was just given a pane in belongs
    # to nothing, and the next session there opens a second one.
    #
    # The launcher here is a THREAD rather than a process, which is the same
    # thing as far as the lock goes: `flock` rides the open file description, and
    # two opens exclude each other inside one process exactly as they do across
    # two (measured). The write is fired from the claim's own READ — the only
    # instant in the pass that reproduces the window, and the one a mid-pass
    # `workspace_list` write cannot reach.
    attempted = threading.Event()
    writer = []
    client = RecordingClient(spaces=[space_info("wOld", "og"),
                                     space_info("wNew", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={},
                pending={"/repo/og": dict(FIRST_LAUNCH)})
    bridge = m.Bridge(FakeWatcher([]), client)
    reads = {"n": 0, "raced_at": None}
    real_load = m.load_state

    def load_and_race(path, note=None):
        state = real_load(path, note)
        reads["n"] += 1
        if reads["n"] == 2:
            # The second read of the pass is the claim's; the first is the pass
            # boundary's. Start the launcher and wait until it has ATTEMPTED its
            # write — not until it finishes, which under the lock it cannot do
            # until this pass lets it, and waiting for that here is the deadlock
            # the lock exists to prevent.
            reads["raced_at"] = reads["n"]

            def replace(state):
                state.setdefault("pending", {})["/repo/og"] = dict(SECOND_LAUNCH)
                return state

            def launch():
                attempted.set()
                m.state_update(Path(bridge.state_path), replace)

            thread = threading.Thread(target=launch, daemon=True)
            writer.append(thread)
            thread.start()
            assert attempted.wait(5), "the launcher never reached its write"
            if not getattr(m._state_lock_depth, "depth", 0):
                # The bridge is NOT holding the file here, so the launcher can
                # and does finish its write inside the window — which is what
                # makes the interleaving a fact rather than a race. When the
                # bridge DOES hold it, this join would deadlock (it cannot pass
                # the lock the hook is inside), so it is not taken: the launcher
                # is then queued behind the lock and lands on top of the
                # committed claim, which is the outcome under test.
                thread.join(timeout=5)
        return state

    monkeypatch.setattr(m, "load_state", load_and_race)

    lines = bridge.reconcile([Ev("added", "s1", root_session(
        "First", workspace="/repo/og"))])

    assert reads["raced_at"] == 2, reads
    assert lines[0].startswith("claim s1 → space wOld"), lines
    # Joined, not slept on: what is under assertion is the file's FINAL state,
    # and the launcher's write is entitled to land after the pass it raced.
    writer[0].join(timeout=10)
    assert not writer[0].is_alive(), "the launcher's write never completed"
    # The launcher's record SURVIVED the claim. Whoever commits first, the second
    # writer reads the first's file and lands on top of it.
    saved = json.loads(Path(bridge.state_path).read_text())
    assert saved["pending"]["/repo/og"]["workspace_id"] == "wNew", saved

    # And the consequence, which is a space an operator has to notice and close
    # by hand: the next session in that directory claims the launcher's second
    # space instead of opening a third one for a directory that already has two.
    lines = bridge.reconcile([Ev("added", "s2", root_session(
        "Second", workspace="/repo/og"))])

    assert _names(client).count("workspace_create") == 0
    assert lines[0].startswith("claim s2 → space wNew"), lines


def test_a_claimed_space_is_renamed_to_the_session_title(seams):
    # The launcher's whole reason for labelling a space from the DIRECTORY is
    # that no session existed when it made one. The rename is the handoff.
    client = RecordingClient(spaces=[space_info("wB", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1", "pane_id": "wB:p1",
                     "owner": "launcher", "cwd": "/repo/og"}})
    bridge = m.Bridge(FakeWatcher([]), client)

    bridge.reconcile([Ev("added", "s1", root_session(
        "Fix the bridge", workspace="/repo/og"))])

    # The client's named wrapper: (workspace_id, label).
    assert _call(client, "workspace_rename")[1] == ("wB", "Fix the bridge")


def test_a_claimed_space_whose_label_already_matches_is_not_renamed(seams):
    # Not cosmetics: a `workspace.rename` on every adoption of an already-correct
    # space is noise in a log the operator reads to work out what happened.
    #
    # The positive control is IN this test, because the negative alone is
    # vacuous: a module that never renames anything satisfies it for the wrong
    # reason. Run both labels through the same setup and require exactly one
    # rename out of the two.
    def claim(label):
        client = RecordingClient(spaces=[space_info("wB", label)])
        write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
            "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1",
                         "pane_id": "wB:p1", "owner": "launcher",
                         "cwd": "/repo/og"}})
        m.Bridge(FakeWatcher([]), client).reconcile(
            [Ev("added", "s1", root_session("Fix the bridge",
                                            workspace="/repo/og"))])
        return [c for c in client.calls if c[0] == "workspace_rename"]

    assert claim("og"), "control: a DIFFERENT label must be renamed"
    assert claim("Fix the bridge") == [], \
        "a space already labelled with the session title was renamed anyway"


def test_the_workspace_rename_frame_matches_herdrs_schema(seams):
    # Now that the client has a named `workspace_rename` wrapper, the wire frame
    # is ALSO covered by the introspection conformance guard in
    # tests/test_og_herdr_client.py. This stays as the bridge-side specific case:
    # it pins that the bridge passes exactly the workspace id and the session
    # title — the two params `WorkspaceRenameParams` requires, and no others —
    # so a change at the bridge (a dropped label, a stray key) is named here
    # rather than only in the client's report.
    client = RecordingClient(spaces=[space_info("wB", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1", "pane_id": "wB:p1",
                     "owner": "launcher", "cwd": "/repo/og"}})
    m.Bridge(FakeWatcher([]), client).reconcile(
        [Ev("added", "s1", root_session("Fix the bridge", workspace="/repo/og"))])

    # The wrapper maps its two arguments onto the wire params one-for-one.
    ws_id, title = _call(client, "workspace_rename")[1]
    method = "workspace.rename"
    params = {"workspace_id": ws_id, "label": title}

    schema = json.loads((Path(__file__).parent / "fixtures"
                         / "herdr_api_schema.json").read_text())
    request = schema["schemas"]["request"]
    variants = {v["properties"]["method"]["const"]: v["properties"]["params"]
                for v in request["oneOf"]}
    assert method in variants, "herdr has no workspace.rename"

    allowed = variants[method]
    while "$ref" in allowed:
        allowed = request["$defs"][allowed["$ref"].rsplit("/", 1)[1]]

    # Every required param present, and nothing extra: herdr's `Workspace.
    # RenameParams` requires both `workspace_id` and `label` and knows no other
    # key, and serde DROPS an unknown one silently — the failure mode that put
    # every tab in the focused workspace once already.
    assert set(allowed["required"]) <= set(params), params
    assert set(params) <= set(allowed["properties"]), params
    assert set(params) == set(allowed["required"]), params
    assert all(isinstance(v, str) for v in params.values()), params

    # The negative control: a param herdr does not have must be caught by the
    # comparison above, or every assertion here could pass for a typo'd key.
    assert set(allowed["properties"]) != {"workspace_id", "name"}


# ---------------------------------------------------------------------------
# the space label follows the title when it ARRIVES, not only at adoption
#
# A fresh `og chat` conversation has no title until its first message, so a
# space opened for it is labelled `sid[:8]` — the "hash id" the operator saw —
# and, before this, kept that string for the life of the session. The title
# arrives on the `changed` event after that first message, and THAT is where the
# space has to be renamed. Not a race, and not the adoption path: the ordinary
# path for every new session.
# ---------------------------------------------------------------------------

def test_a_titleless_space_is_renamed_when_the_session_title_arrives(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    # No title yet: the space is opened as the first eight characters of the id.
    bridge.reconcile([Ev("added", "abcd1234efgh", root_session(
        None, state="idle"))])
    assert _call(client, "workspace_create")[1] == ("abcd1234",)
    client.calls.clear()

    # The first message gives the conversation a name; the space follows it.
    bridge.reconcile([Ev("changed", "abcd1234efgh", root_session(
        "First question", state="working"), previous=root_session(
            None, state="idle"))])

    assert _call(client, "workspace_rename")[1] == ("ws1", "First question")


def test_a_second_title_change_renames_the_space_again(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "abcd1234efgh", root_session(
        None, state="idle"))])
    bridge.reconcile([Ev("changed", "abcd1234efgh", root_session(
        "First question", state="working"), previous=root_session(
            None, state="idle"))])
    client.calls.clear()

    bridge.reconcile([Ev("changed", "abcd1234efgh", root_session(
        "A better name", state="working"), previous=root_session(
            "First question", state="working"))])

    assert [c[1] for c in client.calls if c[0] == "workspace_rename"] == \
        [("ws1", "A better name")]


def test_a_batch_of_title_changes_lists_the_workspaces_once(seams):
    # The listing is a per-PASS cost, not a per-event one: `workspace.list` is
    # fetched once and reused for every title change in the batch, which is the
    # constraint `_workspace_index` exists to hold. Two title changes in one
    # pass must not mean two listings.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([
        Ev("added", "aaaa1111bb", root_session(None, state="idle")),
        Ev("added", "cccc2222dd", root_session(None, state="idle")),
    ])
    client.calls.clear()

    bridge.reconcile([
        Ev("changed", "aaaa1111bb", root_session("First", state="working"),
           previous=root_session(None, state="idle")),
        Ev("changed", "cccc2222dd", root_session("Second", state="working"),
           previous=root_session(None, state="idle")),
    ])

    assert _names(client).count("workspace_list") == 1, _names(client)
    assert [c[1] for c in client.calls if c[0] == "workspace_rename"] == \
        [("ws1", "First"), ("ws2", "Second")]


def test_a_workers_title_change_never_renames_the_space_or_tab_it_sits_in(seams):
    # A sub-agent gets a TAB in its parent's space, and its record names the
    # PARENT's workspace. Renaming that to the worker's title would rename the
    # operator's whole conversation to `fix-space-rename` every time a worker
    # spawned — and relabelling the parent's FIRST TAB would be the same mistake
    # one level down. The positive control — the ROOT's own title DOES rename the
    # space AND its first tab — is in the same test, because the negative alone
    # is satisfied by a module that renames nothing at all.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "root", root_session("Old name"))])
    bridge.reconcile([Ev("added", "w1", sub_session("root", "coder_zen:fix"))])

    # The worker's tab was created WITH its own title, and is never the root's.
    assert _call(client, "tab_create")[2]["label"] == "coder_zen:fix"

    # The ROOT's title arriving renames its own space AND its first tab...
    bridge.reconcile([Ev("changed", "root", root_session("New name"),
                         previous=root_session("Old name"))])
    assert _call(client, "workspace_rename")[1] == ("ws1", "New name")
    assert [c[1] for c in client.calls if c[0] == "tab_rename"] == \
        [("tab1", "Old name"), ("tab1", "New name")], client.calls

    # ...but the WORKER's never touches that space or that tab.
    client.calls.clear()
    bridge.reconcile([Ev("changed", "w1", sub_session("root", "coder_zen:v2"),
                         previous=sub_session("root", "coder_zen:fix"))])
    assert "workspace_rename" not in _names(client), _names(client)
    assert "tab_rename" not in _names(client), _names(client)


def test_a_failed_space_rename_leaves_the_pass_intact_and_the_metadata_reported(
        seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session(None, state="idle"))])
    client.calls.clear()
    client.fail = {"workspace_rename": FakeHerdrError(
        "invalid_label", "label must not be empty")}

    lines = bridge.reconcile([
        Ev("changed", "s1", root_session("First question", state="working"),
           previous=root_session(None, state="idle")),
        Ev("added", "s2", root_session("Second", state="idle")),
    ])

    # The metadata report is made BEFORE the rename, so a rename that fails
    # cannot lose it.
    assert _call(client, "report_metadata")[2]["title"] == "First question"
    # The failure is absorbed LOCALLY as an ordinary `error:` line: the rename is
    # an addition to `_changed`'s work, so it must not discard the bookkeeping of
    # the work that already succeeded. That bookkeeping is `rec["title"]` —
    # persisted and read back across a restart — and the event's own audit line.
    assert any("error s1" in ln and "invalid_label" in ln for ln in lines), lines
    assert bridge._recs["s1"]["title"] == "First question"
    assert "update s1 → state=working (title)" in lines, lines
    # ...and the batch carries on: one bad space rename aborts nothing.
    assert any("add s2" in ln for ln in lines), lines


def test_a_failed_tab_rename_is_absorbed_like_the_space_rename(seams):
    # A cosmetic label must not fail the event or drive a repair. The SPACE
    # rename already absorbs this way (the test above); the tab rename keeps the
    # SAME shape, so a `tab_not_found` — the failure mode when the operator has
    # closed the space's first tab — is a recorded line, never a raised error that
    # `reconcile` would read as a stale record and repair by re-opening a pane.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("Old", state="idle"))])
    client.calls.clear()
    client.fail["tab_rename"] = FakeHerdrError("tab_not_found", "gone")

    lines = bridge.reconcile([
        Ev("changed", "s1", root_session("New", state="working"),
           previous=root_session("Old", state="idle"))])

    # The space rename still went through and the title bookkeeping still landed.
    assert _call(client, "workspace_rename")[1] == ("ws1", "New")
    assert bridge._recs["s1"]["title"] == "New"
    # The tab failure is an ordinary `error:` line, and no repair was driven by
    # it — the record is untouched.
    assert any("error s1" in ln and "tab_not_found" in ln for ln in lines), lines
    assert _names(client).count("workspace_create") == 0, _names(client)
    assert not any("re-project" in ln for ln in lines), lines


def test_a_failed_space_rename_is_not_retried_on_a_later_poll(seams):
    # ONE attempt, never a retry — and this is the test a reader needs, because
    # the property is easy to misread. `title_changed` comes from the watcher's
    # `previous`, not from `rec`, so once the watcher has seen the new title the
    # event does not recur: a failed rename is reported once and then the space
    # simply keeps its `sid[:8]` label for the life of the session, rather than
    # the bridge repeating the same failing `workspace.rename` on every poll
    # forever (the lesson the dead-workspace repair already learned).
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "abcd1234efgh", root_session(
        None, state="idle"))])
    client.calls.clear()
    client.fail_always = {"workspace_rename": FakeHerdrError(
        "invalid_label", "nope")}

    first = bridge.reconcile([Ev("changed", "abcd1234efgh", root_session(
        "First question", state="working"), previous=root_session(
            None, state="idle"))])
    assert sum("invalid_label" in ln for ln in first) == 1, first

    # The next poll carries no title change, so the rename is not re-attempted.
    client.calls.clear()
    second = bridge.reconcile([Ev("changed", "abcd1234efgh", root_session(
        "First question", state="working"), previous=root_session(
            "First question", state="working"))])
    assert "workspace_rename" not in _names(client), _names(client)
    assert not any("invalid_label" in ln for ln in second), second


def test_a_hand_renamed_space_is_renamed_back_to_the_session_title(seams):
    # The judgement call, pinned. The operator renames a space BY HAND; Omnigent
    # then names the conversation, and the TITLE wins — the same rule the
    # adoption path already applies to a manual label. The space is a projection
    # of the conversation and the conversation's name is authoritative, and
    # across a restart the bridge cannot tell a hand-set label from its own
    # stale one, so honouring it would make the space's name depend on the order
    # events happened to arrive in.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "abcd1234ef", root_session(
        None, state="idle"))])
    client.operator_renamed("ws1", "My Own Name")
    client.calls.clear()

    bridge.reconcile([Ev("changed", "abcd1234ef", root_session(
        "First question", state="working"), previous=root_session(
            None, state="idle"))])

    assert _call(client, "workspace_rename")[1] == ("ws1", "First question")


# --- 6. pending records are pruned by EXISTENCE, not by a clock ----------------

def test_a_pending_record_whose_workspace_is_gone_is_pruned_and_claims_nothing(
        seams):
    # Existence is the whole rule — no TTL, no clock. A pending record names a
    # space; if the operator closed that space, keeping the record means the
    # next session in that directory claims a workspace that is not there.
    client = RecordingClient(spaces=[space_info("wLive", "live")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/gone": {"workspace_id": "wGone", "tab_id": "wGone:t1",
                       "pane_id": "wGone:p1", "owner": "launcher"},
        "/repo/live": {"workspace_id": "wLive", "tab_id": "wLive:t1",
                       "pane_id": "wLive:p1", "owner": "launcher"}})
    bridge = m.Bridge(FakeWatcher([]), client)

    lines = bridge.reconcile([
        Ev("added", "s1", root_session("T", workspace="/repo/gone")),
        Ev("added", "s2", root_session("U", workspace="/repo/live")),
    ])

    assert any("pruned 1 pending" in ln and "/repo/gone" in ln
               for ln in lines), lines
    # Both were resolved in this one pass: the dead one pruned, the live one
    # claimed by the session in its directory. What must never happen is the
    # dead one being claimed, which would hand a pane to a workspace that is not
    # there — so the two sessions took DIFFERENT paths, and that is the proof.
    assert bridge._pending == {}
    saved = json.loads(Path(bridge.state_path).read_text())
    assert saved["pending"] == {}
    assert [r["workspace_id"] for r in saved["spaces"].values()] == \
        ["ws1", "wLive"]
    assert saved["spaces"]["s2"]["workspace_id"] == "wLive"
    assert saved["spaces"]["s2"]["owner"] == "launcher"
    assert _names(client).count("workspace_create") == 1
    assert bridge._recs["s1"]["workspace_id"] == "ws1"
    assert bridge._recs["s2"]["workspace_id"] == "wLive"


def test_internal_bookkeeping_never_reaches_the_state_file(seams):
    # The adopt path needs to know WHICH action it took and whether the pane was
    # confirmed. Those are answers about this pass, not facts about a space, and
    # a record that carried them would leave the next reader unable to tell
    # which of its keys are real.
    client = RecordingClient(spaces=[space_info("wB", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1", "pane_id": "wB:p1",
                     "owner": "launcher", "cwd": "/repo/og"}})
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T",
                                                    workspace="/repo/og"))])

    saved = json.loads(Path(bridge.state_path).read_text())
    for record in saved["spaces"].values():
        assert set(record) <= {"workspace_id", "tab_id", "pane_id", "owner",
                               "cwd"}, record


def test_pruning_does_not_run_when_there_is_nothing_pending(seams):
    # `workspace.list` costs a socket round trip, and a bridge that has been up
    # for a week with an empty file must not pay it on every poll forever.
    #
    # The control is here for the same reason as everywhere else in this file: a
    # bridge that never listed anything would satisfy the first half for the
    # wrong reason. With a pending record present, the listing MUST happen.
    client = RecordingClient(spaces=[space_info("wLive", "live")])
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T"))])
    assert "workspace_list" not in _names(client)
    assert bridge._pending == {}

    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wLive", "tab_id": "wLive:t1",
                     "pane_id": "wLive:p1", "owner": "launcher"}})
    client2 = RecordingClient(spaces=[space_info("wLive", "live")])
    bridge2 = m.Bridge(FakeWatcher([]), client2)
    bridge2.reconcile([Ev("added", "s2", root_session("T",
                                                     workspace="/repo/og"))])
    assert "workspace_list" in _names(client2)


# --- 7. --cleanup over v2 closes both halves and nothing else -----------------

def test_cleanup_closes_both_spaces_and_pending_and_leaves_the_operators_own(
        seams):
    # A pending record is a space the LAUNCHER opened for a directory, before
    # any session existed. It is still a space on the operator's screen that
    # something opened on their behalf; leaving it behind means `og start <mux>`
    # has no way back and the operator closes spaces by hand.
    client = RecordingClient(spaces=[PROJECTS, space_info("wA", "bridge"),
                                     space_info("wB", "og")])
    write_state(os.environ["OMNIGENT_HOME"],
                spaces={"s1": {"workspace_id": "wA", "tab_id": "wA:t1",
                               "pane_id": "wA:p1", "owner": "bridge"}},
                pending={"/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1",
                                     "pane_id": "wB:p1",
                                     "owner": "launcher", "cwd": "/repo/og"}})
    bridge = m.Bridge(FakeWatcher([]), client)

    lines = bridge.cleanup()

    assert [c[1] for c in client.calls if c[0] == "workspace_close"] == \
        [("wA",), ("wB",)]
    assert not [c for c in client.calls
                if c[0] == "workspace_close" and c[1] == ("w1",)], \
        "the operator's projects workspace was closed"
    # The two halves are told apart in the report, because "which of these did
    # og open?" is the question an operator asks when a space is left over.
    assert any("closed workspace wA" in ln and "session s1" in ln
               for ln in lines), lines
    assert any("closed workspace wB" in ln and "no session yet" in ln
               for ln in lines), lines
    assert any("left workspace 'w1'" in ln and "projects" in ln
               for ln in lines), lines
    saved = json.loads(Path(bridge.state_path).read_text())
    assert saved["spaces"] == {} and saved["pending"] == {}


def test_a_dry_run_cleanup_lists_both_halves_and_closes_nothing(seams):
    client = RecordingClient()
    write_state(os.environ["OMNIGENT_HOME"],
                spaces={"s1": {"workspace_id": "wA", "owner": "bridge"}},
                pending={"/repo": {"workspace_id": "wB", "owner": "launcher"}})
    dry = m.Bridge(FakeWatcher([]), None, dry_run=True)

    lines = dry.cleanup()

    assert client.calls == []
    assert any(ln.startswith("dry-run cleanup: close workspace wA")
               for ln in lines), lines
    assert any(ln.startswith("dry-run cleanup: close workspace wB")
               for ln in lines), lines
    assert any("would be closed" in ln and "2 " in ln for ln in lines), lines
    # Nothing was pruned, so the NEXT real cleanup still has both.
    assert set(dry._spaces) == {"s1"} and set(dry._pending) == {"/repo"}


# --- 8. a broken file reads as empty, without raising -------------------------

@pytest.mark.parametrize("body", [
    "{ not json at all",
    json.dumps(["not", "a", "dict"]),
    json.dumps({"version": 99, "spaces": {"s1": {"workspace_id": "w1"}}}),
])
def test_a_corrupt_or_unknown_version_state_file_reads_as_empty(seams,
                                                                tmp_path, body):
    # A REGRESSION GUARD, not a bug witness: this holds on the pre-change module
    # too, and is here because the v2 rewrite of `load_state` is exactly the
    # kind of change that can quietly break it. The direction is the safe one —
    # an answer we cannot read must never become permission to close something —
    # and it must never raise, because a Bridge is constructed before any work
    # is done and a raise there leaves the operator with no bridge rather than a
    # quiet one.
    path = tmp_path / "broken.json"
    path.write_text(body)
    client = RecordingClient(spaces=[PROJECTS])

    bridge = m.Bridge(FakeWatcher([]), client, state_path=path)
    lines = bridge.cleanup()
    assert "workspace_close" not in _names(client)
    assert any("nothing to close" in ln for ln in lines), lines


@pytest.mark.parametrize("spaces,expected", [
    ("nope", []),
    ({}, []),
    ({"s1": {"workspace_id": 7}}, []),          # no usable workspace id
    ({"": {"workspace_id": "wZ"}}, []),        # no session key
    ({"s1": "w1"}, []),                         # not a record at all
    ({"s1": {"workspace_id": "w1"}}, [("w1",)]),        # the one good one
    ({"s1": {"workspace_id": "w1", "tab_id": 9}}, [("w1",)]),  # non-string dropped
    # The mixed case, and the one that matters: a bad record must not cost a
    # good one. v1 rejected the whole file on a version mismatch, so it could
    # never demonstrate this — it simply closed nothing, which is the same
    # outcome for the bad entries and the wrong one for the good one.
    ({"s1": {"workspace_id": 7}, "s2": {"workspace_id": "w2"}}, [("w2",)]),
    ({"s1": "junk", "s2": {"workspace_id": "w2"}}, [("w2",)]),
])
def test_a_v2_state_file_of_wrong_shaped_entries_reads_as_empty(seams, tmp_path,
                                                                spaces, expected):
    # The v2 reader's own job, and new with the version: which malformed entries
    # survive. The direction is the same as above — only a record carrying a
    # usable `workspace_id` is one a close can be aimed at — but the shape it is
    # checking did not exist before v2. A non-string `tab_id` is dropped while
    # the record around it is kept: one bad field must not cost a workspace id
    # that was fine.
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps({"version": m.STATE_VERSION,
                                "spaces": spaces, "pending": "nope"}))
    client = RecordingClient(spaces=[PROJECTS])

    bridge = m.Bridge(FakeWatcher([]), client, state_path=path)
    bridge.cleanup()

    assert [c[1] for c in client.calls if c[0] == "workspace_close"] == expected


def test_a_missing_state_file_reads_as_empty_without_raising(seams, tmp_path):
    bridge = m.Bridge(FakeWatcher([]), RecordingClient(),
                      state_path=tmp_path / "absent.json")
    assert bridge._spaces == {} and bridge._pending == {}


# --- a dry run adopts from the file WITHOUT calling herdr ---------------------

def test_a_dry_run_adopts_and_claims_without_making_a_single_call(seams):
    # Zero client calls is the whole contract of a dry run, and it is what makes
    # it safe to run against a live herdr at all. So its adopt/claim decision
    # rests on the state file alone and is explicitly unverified — which is what
    # the line says.
    client = RecordingClient(spaces=[space_info("wA", "already"),
                                     space_info("wB", "og")])
    write_state(os.environ["OMNIGENT_HOME"],
                spaces={"s1": {"workspace_id": "wA", "tab_id": "wA:t1",
                               "pane_id": "wA:p1", "owner": "bridge"}},
                pending={"/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1",
                                     "pane_id": "wB:p1", "owner": "launcher"}})
    bridge = m.Bridge(FakeWatcher([]), client, dry_run=True)

    lines = bridge.reconcile([
        Ev("added", "s1", root_session("A", workspace="/repo/a")),
        Ev("added", "s2", root_session("B", workspace="/repo/og")),
    ])

    assert client.calls == [], "the dry run reached the live client"
    assert any("dry-run: adopt 'A' → space wA" in ln for ln in lines), lines
    assert any("dry-run: claim 'B' → space wB" in ln for ln in lines), lines
    # Idempotent, like every other dry run: the second pass finds the record.
    assert bridge.reconcile([
        Ev("added", "s1", root_session("A", workspace="/repo/a")),
        Ev("added", "s2", root_session("B", workspace="/repo/og")),
    ]) == []


def test_a_dry_run_never_rewrites_the_state_file(seams):
    # A dry run that claimed or pruned would destroy the very records the NEXT
    # real run needs — the exact defect that makes a dry-run --cleanup
    # dangerous, arriving through the adopt path instead of the cleanup one.
    #
    # The control is in this test: the SAME operation on a REAL run does rewrite
    # the file (it has to — that is what claiming is). Without it, "the file is
    # unchanged" would also be satisfied by a module that never writes it at all,
    # which is the wrong reason to be green.
    client = RecordingClient(spaces=[space_info("wB", "og")])
    path = write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1",
                     "pane_id": "wB:p1", "owner": "launcher"}})
    before = path.read_text()

    m.Bridge(FakeWatcher([]), client, dry_run=True).reconcile(
        [Ev("added", "s1", root_session("T", workspace="/repo/og"))])
    assert path.read_text() == before

    # Control: the real run claims it, and the file says so.
    live = RecordingClient(spaces=[space_info("wB", "og")])
    m.Bridge(FakeWatcher([]), live).reconcile(
        [Ev("added", "s1", root_session("T", workspace="/repo/og"))])
    saved = json.loads(path.read_text())
    assert saved["pending"] == {}
    assert saved["spaces"]["s1"]["workspace_id"] == "wB"


def test_a_dry_run_claim_moves_nothing_in_memory_either(seams):
    # The file is only HALF of what a dry run must leave alone, and the half
    # that mattered here was the other one. `pending` and `spaces` are the
    # in-memory copies `_synced` is measured against, so a move between them
    # that never reaches the file is not neutral: `_memory_decides` reads a key
    # memory no longer holds, carrying the very record memory consumed, as a
    # COMPLETED claim (its third case) and refuses to let the file put it back.
    # A dry run that moved the record and skipped the write therefore retired a
    # real launcher record for good — and the next session in that directory
    # opens a second space, which is the entire class of bug this state file
    # exists to end, reached through `--dry-run`.
    client = RecordingClient(spaces=[space_info("wB", "og")])
    path = write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1",
                     "pane_id": "wB:p1", "owner": "launcher"}})
    bridge = m.Bridge(FakeWatcher([]), client, dry_run=True)
    before = dict(bridge._pending)

    lines = bridge.reconcile([
        Ev("added", "s1", root_session("T", workspace="/repo/og"))])

    # It still SAYS what it would have claimed: the claim is decidable from the
    # file alone, and a dry run that reported nothing would be useless. What
    # changed is that saying it costs nothing.
    assert any("dry-run: claim 'T' → space wB" in ln for ln in lines), lines
    assert bridge._pending == before, \
        "a dry-run claim consumed the pending record in memory"
    assert bridge._spaces == {}, \
        "a dry-run claim created a space record in memory"

    # And the fold at the next pass boundary has to be a no-op against an
    # unchanged file — otherwise the un-saved move above would be what the fold
    # reads as finished, which is the mechanism the two assertions above deny.
    bridge._reload_state()
    assert bridge._pending == before, \
        "the next fold read the dry run as a completed claim"

    # Control: the claim this dry run declined still works, against the same
    # file. Without it "nothing was consumed" would also be satisfied by a claim
    # that cannot happen at all — the wrong reason to be green.
    live = RecordingClient(spaces=[space_info("wB", "og")])
    m.Bridge(FakeWatcher([]), live).reconcile(
        [Ev("added", "s1", root_session("T", workspace="/repo/og"))])
    saved = json.loads(path.read_text())
    assert saved["pending"] == {}
    assert saved["spaces"]["s1"]["workspace_id"] == "wB"


# --- a worker never claims a space, before its root or otherwise --------------

def test_a_worker_never_claims_the_launchers_space(seams):
    # Roots only. A sub-agent never owns a space, and a worker that reached the
    # launcher's directory BEFORE its root would claim the launcher's whole
    # space for itself — leaving the root to open a second one, which is the
    # exact duplicate this ordering exists to prevent, reintroduced from the
    # other end. `parent_session_id` is the guard.
    client = RecordingClient(spaces=[space_info("wB", "og")])
    write_state(os.environ["OMNIGENT_HOME"], spaces={}, pending={
        "/repo/og": {"workspace_id": "wB", "tab_id": "wB:t1", "pane_id": "wB:p1",
                     "owner": "launcher", "cwd": "/repo/og"}})
    bridge = m.Bridge(FakeWatcher([]), client)

    bridge.reconcile([Ev("added", "w1", sub_session(
        "root", "coder_zen:fix", workspace="/repo/og"))])

    assert set(bridge._pending) == {"/repo/og"}
    # It got a space of its own (its parent has none yet), as a worker with no
    # space to sit in always does.
    assert _names(client).count("workspace_create") == 1


def test_an_adopted_record_without_a_pane_id_becomes_a_tab_in_that_space(seams):
    # An upgraded v1 record carries no pane id, so there is nothing to adopt ONTO
    # — but the space is still this session's, and putting the session in a NEW
    # tab inside it beats opening a SECOND space beside it. The operator sees one
    # space for one conversation either way; this is the version with one extra
    # tab rather than one extra space.
    client = RecordingClient(spaces=[space_info("w9", "nine", pane_count=1)])
    write_state(os.environ["OMNIGENT_HOME"], spaces={
        "s1": {"workspace_id": "w9", "owner": "bridge", "cwd": "/repo/og"}})
    bridge = m.Bridge(FakeWatcher([]), client)

    bridge.reconcile([Ev("added", "s1", root_session(
        "T", workspace="/repo/og", state="idle"))])

    assert _names(client).count("workspace_create") == 0
    assert _call(client, "tab_create")[1] == ("w9",)
    # ...and the record is brought up to date, so the next restart can adopt the
    # pane this call created instead of falling back again.
    assert bridge._spaces["s1"]["pane_id"] == "pane1"
    assert bridge._spaces["s1"]["tab_id"] == "tab1"


# ---------------------------------------------------------------------------
# a recorded workspace is a CACHE, and herdr's listing is the only truth
#
# THE BUG, from the live bridge log during a real fan-out of three workers:
#
#     error ab222b88…: invalid_metadata_request: missing metadata field to set or clear
#     error ab222b88…: pane_not_found: pane wV:p1 not found      (x9, the ROOT)
#     error f193205d…: workspace_not_found: workspace wV not found  (worker 1)
#     error 5b011e8e…: workspace_not_found: workspace wV not found  (worker 2)
#     error 3f3a1c0a…: workspace_not_found: workspace wV not found  (worker 3)
#
# and `workspace_list` answering with exactly one space, `wT`. wV had been
# recorded once, at `workspace.create`, and never checked again: a ROOT's record
# is validated when it adopts a space (`_existing_space`) but a sub-agent's is a
# lookup in `_recs`, so every worker was handed an id herdr had never heard of —
# and the root's own pane reported against the same dead space on every poll,
# forever, with nothing repairing the record and nothing said to the operator.
#
# `og_herdr_launch` had this rule already and states it once, in `space_status`;
# the tests below are the bridge's half of it.
# ---------------------------------------------------------------------------

def test_a_worker_whose_roots_space_is_gone_lands_in_a_fresh_space(seams):
    # THE FIX, on the failing case. The root's space was closed by the operator
    # (or, just as easily, by `og start <mux>` case (C) closing the pane that
    # happened to be the last one in it — closing a workspace's last pane removes
    # the workspace, measured). The next worker must NOT be created in `wV`.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.operator_closed("ws1")
    client.calls.clear()

    lines = bridge.reconcile([Ev("added", "w1", sub_session(
        "root", "coder_zen:fix"))])

    assert _names(client).count("workspace_create") == 1, (
        "the dead root's space was trusted instead of re-validated")
    assert _names(client).count("tab_create") == 1
    assert _call(client, "tab_create")[1] == ("ws2",), (
        "the worker did not land in the re-projected space")
    assert ("ws1",) not in [c[1] for c in client.calls if c[0] == "tab_create"]
    assert any("ws1" in ln and "no workspace ws1" in ln for ln in lines), lines
    # The whole crew is together again: the worker is a tab of the root's CURRENT
    # space, which is the layout this bridge exists to produce.
    assert bridge._recs["w1"]["workspace_id"] == \
        bridge._recs["root"]["workspace_id"] == "ws2"
    # The dead id is nowhere in memory or in the file, so the next restart does
    # not make the same decision against it.
    assert "ws1" not in json.dumps(bridge._spaces)
    assert "ws1" not in Path(bridge.state_path).read_text()


def test_a_space_created_in_a_pass_is_visible_to_the_rest_of_that_pass(seams):
    # The re-validation reads a per-PASS snapshot, and a space created during
    # the pass is not in it — the next worker's `_space_of` would read the
    # fresh space as gone and re-project the root AGAIN. That is the
    # duplicate-space bug with one more step to reach it, and it is reachable in
    # ordinary use: any pass that opens a space and then files a worker in it.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")

    bridge.reconcile([
        Ev("added", "root", root_session("New Alignment")),
        Ev("added", "w1", sub_session("root", "coder_zen:fix")),
        Ev("added", "w2", sub_session("root", "coder_cmdcode:stale-comments")),
    ])

    assert _names(client).count("workspace_create") == 1
    assert [c[1] for c in client.calls if c[0] == "tab_create"] == \
        [("ws1",), ("ws1",)]


def test_a_worker_in_a_live_roots_space_opens_no_second_space(seams):
    # The control for the test above, and the direction that must not regress:
    # when the listing DOES carry the recorded id, that id is used and nothing is
    # created. `workspace.list` really is asked every time — that is the price
    # of the guard, and a bridge that never asked would satisfy this half for
    # the wrong reason.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.calls.clear()

    bridge.reconcile([Ev("added", "w1", sub_session(
        "root", "coder_zen:fix"))])

    assert "workspace_list" in _names(client)
    assert _names(client).count("workspace_create") == 0
    assert _call(client, "tab_create")[1] == ("ws1",)


def test_an_unreadable_workspace_list_does_not_split_a_conversation_in_two(
        seams):
    # "Cannot tell" is not "absent", and here it has a price: a worker whose
    # parent's space cannot be confirmed would be given a space OF ITS OWN, so
    # one hiccup in the listing would scatter the operator's conversation across
    # two spaces — for a space that is sitting there perfectly well.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.calls.clear()
    client.fail = {"workspace_list": FakeHerdrError("boom", "cannot list")}

    lines = bridge.reconcile([Ev("added", "w1", sub_session(
        "root", "coder_zen:fix"))])

    # An ERROR, not an empty answer: it is in the pass's lines…
    assert any("cannot list workspaces" in ln for ln in lines), lines
    # …the recorded id is used unchanged, and nothing is adopted over it.
    assert _names(client).count("workspace_create") == 0
    assert _call(client, "tab_create")[1] == ("ws1",)
    assert bridge._recs["root"]["workspace_id"] == "ws1"
    # The record is also untouched on disk, so the NEXT pass tries again.
    assert json.loads(Path(bridge.state_path).read_text())["spaces"]["root"] \
        ["workspace_id"] == "ws1"


def test_a_workspace_not_found_repairs_the_record_instead_of_repeating(seams):
    # The guard consults the listing, but a record can still go stale BETWEEN two
    # events — herdr re-mints ids on restart, the operator closes one tab out of
    # a space that stays. Then the error arrives from herdr rather than from the
    # listing, and before this fix it was logged and nothing else: the record
    # survived, so the next poll repeated it forever.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.calls.clear()
    client.fail = {"tab_create": FakeHerdrError(
        "workspace_not_found", "workspace ws1 not found")}

    first = bridge.reconcile([Ev("added", "w1", sub_session(
        "root", "coder_zen:fix"))])

    assert sum("workspace_not_found" in ln for ln in first) == 1, first
    # Repaired ONCE: the retry went through the ordinary create path, and the
    # worker is now tracked in a space herdr has.
    assert any("could not be projected" in ln and "w1" in ln for ln in first), \
        first
    assert "w1" in bridge._recs
    client.calls.clear()

    # And the session is projected rather than re-failing: a second poll for the
    # same worker finds a ready record and does nothing, and no workspace id is
    # ever used that herdr has not confirmed this pass.
    second = bridge.reconcile([Ev("added", "w1", sub_session(
        "root", "coder_zen:fix"))])
    assert second == []
    assert client.calls == []


def test_a_pane_not_found_on_an_update_reprojects_once_rather_than_forever(
        seams):
    # The other half of the log: NINE identical `pane_not_found` lines for one
    # root, one per poll, with no repair and nothing said to the operator. The
    # root's space is still listed here — only its tab is gone — so the repair is
    # a new TAB in the same space, never a second space for one conversation.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.calls.clear()
    client.fail = {"report_agent": FakeHerdrError(
        "pane_not_found", "pane ws1:p1 not found")}

    first = bridge.reconcile([Ev("changed", "root", root_session(
        "New Alignment", state="working"), previous=root_session(
            "New Alignment", state="idle"))])

    assert sum("pane_not_found" in ln for ln in first) == 1, first
    assert _names(client).count("workspace_create") == 0, (
        "one conversation must never get two spaces, however stale the pane is")
    assert _names(client).count("tab_create") == 1
    assert bridge._recs["root"]["pane_id"] != "pane1"
    # Still owns the space: it is the root's own space and its removal must
    # close it, not just the tab that was re-made.
    assert bridge._recs["root"]["owns_space"] is True

    # The next poll reports against the new pane and says nothing about the old.
    client.calls.clear()
    client.fail = {}
    again = bridge.reconcile([Ev("changed", "root", root_session(
        "New Alignment", state="idle"), previous=root_session(
            "New Alignment", state="working"))])
    assert again == ["update root → state=idle"]
    assert _call(client, "report_agent")[1][0] == bridge._recs["root"]["pane_id"]


def test_a_repair_that_fails_again_is_said_once_and_stops(seams):
    # Bounded on purpose: the re-projection goes through the ordinary create
    # path, so a herdr that is still broken fails it too. That must be ONE more
    # line and a dropped record — never a retry loop inside one event, and never
    # a record left behind still naming a pane herdr does not have.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([Ev("added", "root", root_session("New Alignment"))])
    client.operator_closed("ws1")
    client.calls.clear()
    # The pane is gone AND the space is gone, so the repair has to create one.
    client.fail_always = {
        "report_agent": FakeHerdrError("pane_not_found", "pane ws1:p1 not found"),
        "workspace_create": FakeHerdrError("connect", "herdr is unreachable"),
    }

    lines = bridge.reconcile([Ev("changed", "root", root_session(
        "New Alignment", state="working"), previous=root_session(
            "New Alignment", state="idle"))])

    assert sum("could not re-project" in ln for ln in lines) == 1, lines
    # Exactly one attempt: the failing call is made once inside the repair and
    # once in the update it belongs to — never in a loop.
    assert _names(client).count("workspace_create") == 1, lines
    assert "root" not in bridge._recs
    # ...and with no record there is nothing left to report against: the session
    # is ignored from here rather than failing on every poll.
    client.calls.clear()
    client.fail_always = {}
    assert bridge.reconcile([Ev("changed", "root", root_session(
        "New Alignment", state="idle"))]) == []
    assert client.calls == []


# ---------------------------------------------------------------------------
# pane metadata: a report with nothing to set is refused by herdr
#
#     invalid_metadata_request: missing metadata field to set or clear
#
# `pane_id` and `source` are the only params herdr REQUIRES, so the bare frame
# og sent for a session with no title yet is schema-valid and still refused.
# ---------------------------------------------------------------------------

def test_no_metadata_frame_is_sent_for_a_session_with_no_title(seams):
    # A fresh `og chat` conversation has `title: None` until its first message,
    # and `_drop_none` then strips the only field that was going to be set.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "s1", root_session(
        None, state="idle"))])

    assert "report_metadata" not in _names(client), (
        "a metadata report with no title to set is refused by herdr")
    # Setup still COMPLETES, so a redelivered `added` does not send it later
    # either, and the pane is not left half-configured.
    assert bridge._recs["s1"]["ready"] is True and bridge._recs["s1"]["step"] == 3
    assert any("add s1" in ln for ln in lines), lines


def test_a_title_arriving_later_is_still_reported_as_metadata(seams):
    # The reason the call is skipped rather than sent empty: the title is not
    # lost, it is reported by the next event that carries one.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session(None, state="idle"))])

    lines = bridge.reconcile([Ev("changed", "s1", root_session(
        "First question", state="working"), previous=root_session(
            None, state="idle"))])

    assert _call(client, "report_metadata")[2]["title"] == "First question"
    assert lines == ["update s1 → state=working (title)"]


def test_a_title_going_away_does_not_send_an_empty_report(seams):
    # The other direction, which the live log also shows: `title_changed` is
    # true for None -> "x" and for "x" -> None alike, and only the second one
    # has nothing to say.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", root_session("T", state="idle"))])
    client.calls.clear()

    bridge.reconcile([Ev("changed", "s1", root_session(
        None, state="working"), previous=root_session("T", state="idle"))])

    assert "report_metadata" not in _names(client)


def test_a_failed_repair_leaves_no_stale_record_behind(seams):
    # The ORDER inside a repair, which is the contract and not a detail: forget
    # the stale record BEFORE anything that can fail. Forgetting it afterwards
    # leaves the dead record in place whenever the re-projection itself fails —
    # and then the next poll reports against the same pane herdr has never
    # heard of, and says so again, which is the whole failure being repaired.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/og")
    bridge.reconcile([
        Ev("added", "root", root_session("New Alignment")),
        Ev("added", "w1", sub_session("root", "coder_zen:fix"))])
    client.operator_closed("ws1")
    client.calls.clear()
    # A worker whose pane is gone, whose ROOT's space is gone, and a herdr that
    # keeps refusing the reports — so both re-projections die part-way.
    client.fail_always = {"report_agent": FakeHerdrError(
        "pane_not_found", "pane pane2 not found")}

    lines = bridge.reconcile([Ev("changed", "w1", sub_session(
        "root", "coder_zen:fix"), previous={})])

    assert sum("could not re-project" in ln for ln in lines) == 1, lines
    assert "w1" not in bridge._recs, \
        "the stale worker record survived a repair that failed"
    # The root did move — its own re-projection got as far as creating the space,
    # and its record says so rather than naming the closed one.
    assert bridge._recs["root"]["workspace_id"] != "ws1"
    client.fail_always = {}
    client.calls.clear()
    assert bridge.reconcile([Ev("changed", "w1", sub_session(
        "root", "coder_zen:fix"), previous={})]) == []
    assert client.calls == [], "the same failure was repeated on the next poll"
