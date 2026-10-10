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
import os
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
    def __init__(self, fail=None, create_results=None):
        self.calls = []
        self.fail = fail or {}
        # Queued tab_create returns, so a test can hand back a malformed reply
        # (missing ids) that the default well-formed shape never produces.
        self.create_results = list(create_results) if create_results else []
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
        if self.create_results:
            return self.create_results.pop(0)
        self._seq += 1
        # The REAL herdr 0.9.3 shape: `tab_id` / `pane_id`, never a generic `id`.
        return {"tab": {"tab_id": f"tab{self._seq}"},
                "root_pane": {"pane_id": f"pane{self._seq}"}}

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
                      source="og-bridge", cwd="/tmp/ws")
    session = {"title": "Fix bug", "state": "working"}
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


def test_cwd_flag_is_honoured_for_new_panes(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/srv/repo")
    bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    assert _call(client, "tab_create")[2]["cwd"] == "/srv/repo"


def test_added_defaults_cwd_to_process_cwd_and_label_to_session_id(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client)
    lines = bridge.reconcile([Ev("added", "abcdef123456", {})])
    # No --cwd: the pane opens in the bridge's own cwd, not a hardcoded path.
    assert _call(client, "tab_create")[2]["cwd"] == os.getcwd()
    assert _call(client, "tab_create")[2]["label"] == "og:abcdef12"
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
    assert _call(client, "tab_create")[2]["cwd"] == "/repo/under/test"


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
    bridge.reconcile([Ev("added", "s1", {
        "title": "Fix bug", "state": "working",
        "workspace": "/Users/cryogenix/projects/og"})])

    assert _call(client, "tab_create")[2]["cwd"] == "/Users/cryogenix/projects/og"


def test_a_workers_workspace_wins_over_the_bridge_cwd(seams):
    # What the watcher sends for a worker is its PARENT's directory (the
    # worker's own detail row is None). An approximation, and the best the API
    # offers — but a far better one than the daemon's cwd.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/daemon/cwd")
    bridge.reconcile([Ev("added", "w1", {
        "title": "coder_zen:fix", "state": "working", "id": "w1",
        "parent_session_id": "root",
        "workspace": "/Users/cryogenix/projects/og"})])

    assert _call(client, "tab_create")[2]["cwd"] == "/Users/cryogenix/projects/og"


@pytest.mark.parametrize("session_workspace", [None, ""],
                         ids=["absent_key", "empty_string"])
def test_a_missing_or_empty_workspace_falls_back_to_the_bridge_cwd(
        seams, session_workspace):
    # The watcher omits the key when it knows nothing, so an empty value should
    # not arrive — but `.get()` must treat both the same anyway, because an
    # empty string as a cwd is a pane that opens nowhere.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/under/test")
    session = {"title": "T", "state": "idle"}
    if session_workspace is not None:
        session["workspace"] = session_workspace
    bridge.reconcile([Ev("added", "s1", session)])

    assert _call(client, "tab_create")[2]["cwd"] == "/repo/under/test"


def test_a_session_workspace_is_used_even_alongside_the_fallback(seams):
    # The other side of the same pair: with a real directory present, --cwd is
    # ignored. The two tests together pin "prefer the session, else --cwd",
    # which a single one-sided assertion would not.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, cwd="/repo/under/test")
    bridge.reconcile([Ev("added", "s1", {
        "title": "T", "state": "idle", "workspace": "/repo/from/session"})])

    assert _call(client, "tab_create")[2]["cwd"] == "/repo/from/session"


def test_the_dry_run_line_reports_the_session_directory(seams):
    # The dry-run output is how this was noticed at all ("every line showed the
    # same cwd"), so it has to name the directory the pane would really open in.
    bridge = m.Bridge(FakeWatcher([]), RecordingClient(), dry_run=True,
                      cwd="/daemon/cwd")
    lines = bridge.reconcile([Ev("added", "s1", {
        "title": "Fix bug", "state": "working",
        "workspace": "/Users/cryogenix/projects/og"})])

    assert lines == ["dry-run: add og:Fix bug "
                     "(/Users/cryogenix/projects/og) → omnigent attach s1 "
                     "[working]"]


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
# recovery from a herdr call that fails part-way through one event
# ---------------------------------------------------------------------------

def test_partial_setup_failure_retries_on_the_same_pane_without_a_second_tab(
        seams):
    # pane_run fails once. The tab and pane already exist, so the redelivered
    # `added` must resume setup on them rather than create a second tab.
    client = RecordingClient(fail={"pane_run": FakeHerdrError("boom", "pane gone")})
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    session = {"title": "T", "state": "idle"}

    first = bridge.reconcile([Ev("added", "s1", session)])
    assert any("error s1" in ln for ln in first)
    # The mapping was recorded after tab_create, so the pane stays tracked.
    assert bridge._tabs["s1"]["tab_id"] == "tab1"
    assert bridge._tabs["s1"]["pane_id"] == "pane1"
    assert _names(client).count("tab_create") == 1
    client.calls.clear()

    second = bridge.reconcile([Ev("added", "s1", session)])
    assert _names(client).count("tab_create") == 0
    assert _names(client) == ["pane_run", "report_agent", "report_metadata"]
    assert _call(client, "pane_run")[1] == ("pane1", "omnigent attach s1")
    assert "resume add s1" in second[0]
    assert bridge._tabs["s1"]["ready"] is True


def test_added_with_no_usable_ids_records_error_and_never_reports_on_none(
        seams):
    client = RecordingClient(create_results=[{"tab": {}, "root_pane": {}}])
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    lines = bridge.reconcile([
        Ev("added", "bad", {"title": "bad", "state": "idle"}),
        Ev("added", "good", {"title": "good", "state": "idle"}),
    ])
    assert any("error bad" in ln for ln in lines)
    assert any("add good" in ln for ln in lines)
    # Nothing usable was stored for the malformed create, and none of the
    # follow-up calls were made with a None pane.
    assert "bad" not in bridge._tabs
    assert _names(client).count("pane_run") == 1
    assert _call(client, "report_agent")[1][0] == "pane1"

    # A later `changed` for the rejected session must not report against a None
    # pane: there is no mapping, so it is ignored.
    client.calls.clear()
    assert bridge.reconcile([Ev("changed", "bad", {"state": "working"})]) == []
    assert client.calls == []


def test_added_with_tab_id_but_no_pane_id_closes_the_orphan_tab(seams):
    client = RecordingClient(create_results=[{"tab": {"tab_id": "tabZ"},
                                              "root_pane": {}}])
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    lines = bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    assert any("error s1" in ln for ln in lines)
    assert _names(client) == ["tab_create", "tab_close"]
    assert _call(client, "tab_close")[1] == ("tabZ",)
    assert "s1" not in bridge._tabs


def test_failed_orphan_tab_close_is_recorded_not_raised(seams):
    client = RecordingClient(
        create_results=[{"tab": {"tab_id": "tabZ"}, "root_pane": {}}],
        fail={"tab_close": FakeHerdrError("boom", "cannot close")})
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    lines = bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    # Both the malformed-reply line and the failed cleanup line are recorded.
    assert sum("error s1" in ln for ln in lines) == 2
    assert _names(client) == ["tab_create", "tab_close"]
    assert "s1" not in bridge._tabs


def test_added_with_pane_id_but_no_tab_id_closes_the_orphan_pane(seams):
    # No tab id means there is no tab handle to close, but the pane was created;
    # close it directly so it is not left behind.
    client = RecordingClient(create_results=[
        {"tab": {}, "root_pane": {"pane_id": "paneZ"}}])
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    lines = bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    assert any("error s1" in ln for ln in lines)
    assert _names(client) == ["tab_create", "pane_close"]
    assert _call(client, "pane_close")[1] == ("paneZ",)
    assert "s1" not in bridge._tabs


def test_failed_orphan_pane_close_is_recorded_not_raised(seams):
    client = RecordingClient(
        create_results=[{"tab": {}, "root_pane": {"pane_id": "paneZ"}}],
        fail={"pane_close": FakeHerdrError("boom", "cannot close")})
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    lines = bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    assert sum("error s1" in ln for ln in lines) == 2
    assert _names(client) == ["tab_create", "pane_close"]
    assert "s1" not in bridge._tabs


def test_failed_removal_keeps_the_mapping_for_a_retry(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    client.calls.clear()

    client.fail = {"release_agent": FakeHerdrError("boom", "cannot release")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])
    assert any("error s1" in ln for ln in lines)
    # Cleanup failed, so the mapping survives and a later `removed` can retry.
    assert "s1" in bridge._tabs
    client.calls.clear()

    again = bridge.reconcile([Ev("removed", "s1", {})])
    assert _names(client) == ["release_agent", "tab_close"]
    assert "remove s1" in again[0]
    assert "s1" not in bridge._tabs


def test_removal_where_herdr_says_not_found_is_treated_as_done(seams):
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    client.calls.clear()

    client.fail = {"release_agent": FakeHerdrError("not_found", "pane not found")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])
    # Already-gone is success for us: drop the mapping and do not retry.
    assert "s1" not in bridge._tabs
    assert any("remove s1" in ln for ln in lines)
    client.calls.clear()

    assert bridge.reconcile([Ev("removed", "s1", {})]) == []
    assert client.calls == []


def test_release_agent_not_found_still_attempts_tab_close(seams):
    # A `not_found` from release_agent means only "there was no marker to
    # release" — it does NOT mean the tab is gone (setup may have failed before
    # reporting the marker, leaving the tab open). So tab_close must still run.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    client.calls.clear()

    client.fail = {"release_agent": FakeHerdrError("not_found", "no marker")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])

    assert _names(client) == ["release_agent", "tab_close"]
    assert _call(client, "tab_close")[1] == ("tab1",)
    # tab_close succeeded, so only now is the mapping dropped.
    assert "s1" not in bridge._tabs
    assert any("remove s1" in ln for ln in lines)


def test_release_agent_not_found_then_tab_close_failure_keeps_the_mapping(seams):
    # release_agent says "no marker" (swallowed), but tab_close then fails with a
    # real error: the tab's fate is unknown, so the mapping must survive and a
    # later `removed` must retry the whole cleanup.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    client.calls.clear()

    client.fail = {"release_agent": FakeHerdrError("not_found", "no marker"),
                   "tab_close": FakeHerdrError("boom", "cannot close")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])
    assert _names(client) == ["release_agent", "tab_close"]
    assert any("error s1" in ln for ln in lines)
    assert "s1" in bridge._tabs
    client.calls.clear()

    again = bridge.reconcile([Ev("removed", "s1", {})])
    assert _names(client) == ["release_agent", "tab_close"]
    assert "remove s1" in again[0]
    assert "s1" not in bridge._tabs


def test_tab_close_not_found_is_treated_as_already_gone(seams):
    # Only a `not_found` from tab_close proves the tab is gone (closing a tab's
    # only pane removes the tab, so a later tab.close answers tab_not_found):
    # drop the mapping and never retry.
    client = RecordingClient()
    bridge = m.Bridge(FakeWatcher([]), client, workspace="ws")
    bridge.reconcile([Ev("added", "s1", {"title": "T", "state": "idle"})])
    client.calls.clear()

    client.fail = {"tab_close": FakeHerdrError("not_found", "tab_not_found")}
    lines = bridge.reconcile([Ev("removed", "s1", {})])
    assert _names(client) == ["release_agent", "tab_close"]
    assert "already gone" in lines[0]
    assert "s1" not in bridge._tabs
    client.calls.clear()

    assert bridge.reconcile([Ev("removed", "s1", {})]) == []
    assert client.calls == []


def test_underlying_id_lookup_is_key_explicit_not_order_dependent():
    # The live `root_pane` object carries BOTH keys, so an order-sensitive lookup
    # would silently return the wrong one; the caller must name the key.
    root_pane = {"pane_id": "w1:pQ", "terminal_id": "term",
                 "workspace_id": "w1", "tab_id": "w1:tG"}
    tab = {"tab_id": "w1:tG"}
    assert m._id_of(root_pane, "pane_id") == "w1:pQ"
    assert m._id_of(root_pane, "tab_id") == "w1:tG"
    assert m._id_of(tab, "tab_id") == "w1:tG"
    assert m._id_of(tab, "pane_id") is None


def test_fake_client_returns_the_real_api_key_names():
    client = RecordingClient()
    result = client.tab_create("ws", cwd="/tmp", label="x")
    assert set(result) == {"tab", "root_pane"}
    assert "tab_id" in result["tab"] and "id" not in result["tab"]
    assert "pane_id" in result["root_pane"] and "id" not in result["root_pane"]


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
