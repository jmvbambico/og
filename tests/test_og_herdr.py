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
        # Queued create returns, so a test can hand back a malformed reply
        # (missing ids) that the default well-formed shape never produces.
        self.create_results = list(create_results) if create_results else []
        # What workspace_list reports, for the operator's own workspaces.
        self.spaces = list(spaces) if spaces else []
        self._seq = 0

    def _call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        # One-shot: a queued failure fires once, so a later event's call to the
        # same method succeeds — which is exactly what "does not abort the loop"
        # is meant to demonstrate.
        exc = self.fail.pop(name, None)
        if exc is not None:
            raise exc

    def _next(self):
        self._seq += 1
        return self._seq

    def workspace_create(self, label, cwd, focus=False):
        """The MEASURED workspace.create reply: workspace, tab AND root_pane, all
        three in one call — the schema's own result listing is lossy."""
        self._call("workspace_create", label, cwd=cwd, focus=focus)
        if self.create_results:
            return self.create_results.pop(0)
        n = self._next()
        return {"type": "workspace_created",
                "workspace": {"workspace_id": f"ws{n}", "label": label},
                "tab": {"tab_id": f"tab{n}", "workspace_id": f"ws{n}"},
                "root_pane": {"pane_id": f"pane{n}", "tab_id": f"tab{n}"}}

    def workspace_close(self, workspace_id):
        self._call("workspace_close", workspace_id)

    def tab_create(self, workspace_id, cwd, label, focus=False):
        self._call("tab_create", workspace_id, cwd=cwd, label=label)
        if self.create_results:
            return self.create_results.pop(0)
        n = self._next()
        # The REAL herdr 0.9.3 shape: `tab_id` / `pane_id`, never a generic `id`.
        return {"tab": {"tab_id": f"tab{n}"},
                "root_pane": {"pane_id": f"pane{n}"}}

    def tab_close(self, tab_id):
        self._call("tab_close", tab_id)

    def pane_run(self, pane_id, command):
        self._call("pane_run", pane_id, command)

    def pane_close(self, pane_id):
        self._call("pane_close", pane_id)

    def workspace_list(self):
        self._call("workspace_list")
        return self.spaces

    def report_agent(self, pane_id, source, agent, state, message=None,
                     seq=None, agent_session_id=None):
        self._call("report_agent", pane_id, source, agent=agent, state=state)

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
    assert _names(client) == ["workspace_create", "pane_run", "report_agent",
                              "report_metadata"]
    wc = _call(client, "workspace_create")
    assert wc[1] == ("Fix bug",)
    assert wc[2]["cwd"] == "/tmp/ws"
    assert wc[2]["focus"] is False
    assert _call(client, "pane_run")[1] == (
        "pane1", "omnigent attach --server http://127.0.0.1:6767 s1")
    ra = _call(client, "report_agent")
    assert ra[1] == ("pane1", "og-bridge")
    assert ra[2] == {"agent": "Claude Code", "state": "working"}
    assert _call(client, "report_metadata")[2]["title"] == "Fix bug"
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

    assert _names(client) == ["report_agent", "report_metadata"]
    assert _call(client, "report_agent")[2]["state"] == "working"
    # The STORED label, not one re-derived from this event: a `changed` carries
    # the listing row, which has no `harness` to derive from, and reporting a
    # different agent string would make herdr show two markers for one pane.
    assert _call(client, "report_agent")[2]["agent"] == "Claude Code"
    assert _call(client, "report_metadata")[2]["title"] == "New"
    assert "update s1" in lines[0]


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
    assert _names(client) == ["workspace_create", "pane_run", "report_agent",
                              "report_metadata"]


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
