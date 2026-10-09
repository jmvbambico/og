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
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}
        self._seq = 0

    def _call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        # One-shot: a queued failure fires once, so a later event's call to the
        # same method succeeds — which is exactly what "does not abort the loop"
        # is meant to demonstrate.
        exc = self.fail.pop(name, None)
        if exc is not None:
            raise exc

    def tab_create(self, workspace, cwd, label, focus=False):
        self._call("tab_create", workspace, cwd=cwd, label=label)
        self._seq += 1
        return {"tab": {"id": f"tab{self._seq}"},
                "root_pane": {"id": f"pane{self._seq}"}}

    def tab_close(self, tab_id):
        self._call("tab_close", tab_id)

    def pane_run(self, pane_id, command):
        self._call("pane_run", pane_id, command)

    def pane_close(self, pane_id):
        self._call("pane_close", pane_id)

    def workspace_list(self):
        self._call("workspace_list")
        return []

    def report_agent(self, pane_id, source, agent, state, message=None,
                     seq=None, agent_session_id=None):
        self._call("report_agent", pane_id, source, agent=agent, state=state)

    def release_agent(self, pane_id, source, agent, seq=None):
        self._call("release_agent", pane_id, source, agent=agent)

    def report_metadata(self, pane_id, source, title=None, display_agent=None,
                        ttl_ms=None):
        self._call("report_metadata", pane_id, source, title=title)


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


def _names(client):
    return [c[0] for c in client.calls]


def _call(client, name):
    return next(c for c in client.calls if c[0] == name)


# ---------------------------------------------------------------------------
# action sequences
# ---------------------------------------------------------------------------

def test_added_sequence(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws",
                      source="og-bridge")
    session = {"title": "Fix bug", "workspace": "/tmp/ws", "state": "working"}
    lines = bridge.reconcile([Ev("added", "s1", session)])

    assert _names(client) == ["tab_create", "pane_run", "report_agent",
                              "report_metadata"]
    tc = _call(client, "tab_create")
    assert tc[1] == ("ws",)
    assert tc[2]["cwd"] == "/tmp/ws"
    assert tc[2]["label"] == "og:Fix bug"
    assert _call(client, "pane_run")[1] == ("pane1", "omnigent attach s1")
    ra = _call(client, "report_agent")
    assert ra[1] == ("pane1", "og-bridge")
    assert ra[2] == {"agent": "omnigent", "state": "working"}
    assert _call(client, "report_metadata")[2]["title"] == "Fix bug"
    assert len(lines) == 1 and "add s1" in lines[0]


def test_added_defaults_cwd_to_home_and_label_to_session_id(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "abcdef123456", {})])
    assert _call(client, "tab_create")[2]["cwd"] == str(Path.home())
    assert _call(client, "tab_create")[2]["label"] == "og:abcdef12"
    assert len(lines) == 1


def test_changed_sequence_reports_state_and_title(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", {"title": "Old", "state": "idle"})])
    client.calls.clear()

    lines = bridge.reconcile([
        Ev("changed", "s1", {"title": "New", "state": "working"},
           previous={"title": "Old", "state": "idle"})])

    assert _names(client) == ["report_agent", "report_metadata"]
    assert _call(client, "report_agent")[2]["state"] == "working"
    assert _call(client, "report_metadata")[2]["title"] == "New"
    assert "update s1" in lines[0]


def test_changed_without_title_change_skips_metadata(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", {"title": "Same", "state": "idle"})])
    client.calls.clear()

    bridge.reconcile([Ev("changed", "s1", {"title": "Same", "state": "working"},
                         previous={"title": "Same", "state": "idle"})])
    assert _names(client) == ["report_agent"]


def test_removed_sequence(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    client.calls.clear()

    lines = bridge.reconcile([Ev("removed", "s1", {})])
    assert _names(client) == ["release_agent", "tab_close"]
    ra = _call(client, "release_agent")
    assert ra[1] == ("pane1", "og-bridge")
    assert ra[2] == {"agent": "omnigent"}
    assert _call(client, "tab_close")[1] == ("tab1",)
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
    session = {"title": "T", "state": "idle"}
    first = bridge.reconcile([Ev("added", "s1", session)])
    second = bridge.reconcile([Ev("added", "s1", session)])
    assert len(first) == 1
    assert second == []
    assert _names(client).count("tab_create") == 1


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
# error handling
# ---------------------------------------------------------------------------

def test_herdr_error_is_recorded_and_not_fatal(seams):
    client = RecordingClient(fail={"tab_create": FakeHerdrError("no-sock",
                                                                "cannot reach herdr")})
    bridge = m.Bridge(FakeWatcher([]), client,
                      # a second, healthy session must still be processed
                      )
    lines = bridge.reconcile([
        Ev("added", "bad", {"title": "bad", "state": "idle"}),
        Ev("added", "good", {"title": "good", "state": "idle"}),
    ])
    assert any("error bad" in ln and "no-sock" in ln for ln in lines)
    # the good session still got its pane despite the earlier failure
    assert any("add good" in ln for ln in lines)
    assert _names(client).count("tab_create") == 2


def test_herdr_error_on_update_does_not_abort_remaining_events(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "a", {"state": "idle"})])
    client.calls.clear()
    # Arm the update's report_agent to fail once; the following add must still go
    # through, proving one bad call does not abort the rest of the batch.
    client.fail = {"report_agent": FakeHerdrError("x", "y")}
    lines = bridge.reconcile([
        Ev("changed", "a", {"state": "working"}),
        Ev("added", "c", {"state": "idle"}),
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
        Ev("added", "s1", {"title": "T", "state": "idle"}),
        Ev("changed", "s1", {"title": "U", "state": "working"},
           previous={"title": "T", "state": "idle"}),
        Ev("removed", "s1", {}),
    ])
    assert client.calls == []
    assert len(lines) == 3
    assert all("dry-run:" in ln for ln in lines)


def test_dry_run_is_still_idempotent(seams):
    bridge = m.Bridge(FakeWatcher([]), None, dry_run=True)
    first = bridge.reconcile([Ev("added", "s1", {"title": "T"})])
    second = bridge.reconcile([Ev("added", "s1", {"title": "T"})])
    assert len(first) == 1
    assert second == []


# ---------------------------------------------------------------------------
# no elicitation resolution — by contract the bridge only reports `blocked`
# ---------------------------------------------------------------------------

def test_blocked_is_reported_not_resolved(seams, monkeypatch):
    monkeypatch.setattr(m, "_state", lambda s: "blocked")
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    bridge.reconcile([Ev("added", "s1", {"title": "T"})])
    ra = _call(client, "report_agent")
    assert ra[2]["state"] == "blocked"
    # only reporting calls happened; nothing that could resolve an elicitation
    assert _names(client) == ["tab_create", "pane_run", "report_agent",
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
    watcher = FakeWatcher([[Ev("added", "s1", {"title": "T"})]])
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
    assert args.server == "http://127.0.0.1:6767"
    assert args.socket is None
    assert args.workspace is None
    assert args.source == "og-bridge"


def test_build_parser_overrides():
    args = m.build_parser().parse_args([
        "--once", "--dry-run", "--server", "http://example:1",
        "--socket", "/tmp/s.sock", "--workspace", "w1", "--source", "me",
    ])
    assert args.once and args.dry_run
    assert args.server == "http://example:1"
    assert args.socket == "/tmp/s.sock"
    assert args.workspace == "w1"
    assert args.source == "me"


def test_module_imports_without_sibling_modules():
    """The siblings are absent in this worktree; that this import succeeded is
    the proof the bridge does not import them at module load."""
    assert not (Path(m.__file__).parent / "og_herdr_client.py").exists()
    assert not (Path(m.__file__).parent / "og_herdr_watch.py").exists()
