"""`og start <mux>` — the launcher: which space opens, and what runs in it.

Every test here injects a fake herdr client and a stub session API, so nothing
in this file can reach the operator's live herdr server, live spaces, or live
sessions. That is not a stylistic preference: the launcher closes panes and
removes workspaces, and a test that could do that to a real session would be a
worse hazard than the bug it is looking for.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "installer"))

from og_herdr import load_state  # noqa: E402  (the bridge's own reader)
from og_herdr_launch import (  # noqa: E402
    AGENTS_PANE_RATIO,
    DEFAULT_AGENT_NAME,
    Launch,
    LaunchError,
    agents_pane_enabled,
    agent_name,
    install_config,
    is_inside,
    launch,
    live_session,
)
from og_herdr_watch import SessionEvent  # noqa: E402

SERVER = "http://127.0.0.1:6767"
CWD = "/Users/someone/projects/og"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

def create_reply(ws="w1", tab="w1:t1", pane="w1:p1", **over):
    """A `workspace.create` result in the shape herdr actually sends.

    `tab` and `root_pane` come from the measurement, not the schema's own
    listing of the result — see og_herdr_client.workspace_create.
    """
    reply = {
        "type": "workspace_created",
        "workspace": {"workspace_id": ws, "label": "og"},
        "tab": {"tab_id": tab, "workspace_id": ws},
        "root_pane": {"pane_id": pane, "tab_id": tab, "workspace_id": ws},
    }
    reply.update(over)
    return reply


class FakeClient:
    """A herdr client that records every call and answers from a script.

    `pings_before_up` is how many pings fail before the socket answers, which is
    what drives the launcher's wait for `herdr server`.
    """

    def __init__(self, workspaces=(), create=None, split=None, panes=None,
                 pings_before_up=0):
        self.calls = []
        self.socket_path = "/tmp/fake-herdr/herdr.sock"
        self.closed = False
        self._workspaces = list(workspaces)
        self._create = create if create is not None else create_reply()
        self._split = split if split is not None else {"type": "ok"}
        self._panes = panes if panes is not None else [
            {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1"},
            {"pane_id": "w1:p2", "tab_id": "w1:t1", "workspace_id": "w1"},
        ]
        self._pings = pings_before_up
        self.split_params = None

    # -- the recording seam -------------------------------------------------

    def call(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if method == "workspace.list":
            return {"type": "workspace_list", "workspaces": self._workspaces}
        if method == "workspace.create":
            return self._create
        if method == "pane.split":
            self.split_params = params
            return self._split
        if method == "pane.list":
            return {"type": "pane_list", "panes": self._panes}
        if method == "ping":
            if self._pings > 0:
                self._pings -= 1
                raise RuntimeError("connect: no such socket")
            return {"type": "pong"}
        return {"type": "ok"}

    # -- the wrappers the launcher uses ------------------------------------

    def ping(self):
        return self.call("ping")

    def workspace_list(self):
        return self.call("workspace.list")["workspaces"]

    def workspace_create(self, label, cwd, focus=False):
        return self.call("workspace.create",
                         {"label": label, "cwd": cwd, "focus": focus})

    def workspace_close(self, workspace_id):
        self.call("workspace.close", {"workspace_id": workspace_id})

    def pane_run(self, pane_id, command):
        # The real wrapper appends the newline that submits the line; mirrored
        # here so an assertion on the text sees what herdr receives.
        self.call("pane.send_text", {"pane_id": pane_id,
                                     "text": command + "\n"})

    def pane_close(self, pane_id):
        self.call("pane.close", {"pane_id": pane_id})

    def close(self):
        self.closed = True

    # -- assertions ---------------------------------------------------------

    @property
    def methods(self):
        return [method for method, _ in self.calls]

    def params_of(self, method):
        return [params for name, params in self.calls if name == method]

    def text_typed(self):
        return [params["text"].strip()
                for name, params in self.calls if name == "pane.send_text"]


class FakeWatcher:
    """A SessionWatcher stand-in: `poll_once` returns what the test scripted."""

    base_url = SERVER

    def __init__(self, sessions=()):
        self.base_url = SERVER
        self.events = [
            SessionEvent("added", session.get("id", "s?"), session)
            for session in sessions]

    def poll_once(self):
        return list(self.events)


class UnreadableWatcher(FakeWatcher):
    def poll_once(self):
        raise OSError("connection refused")


def root_session(sid="s1", cwd=CWD, status="idle", **extra):
    session = {"id": sid, "parent_session_id": None, "archived": False,
               "status": status, "workspace": cwd,
               "title": "the conversation"}
    session.update(extra)
    return session


def make_ctx(tmp_path, client, watcher, *, inside=False, env=None,
             agents_pane=True, spawn=None, agent="hivemind"):
    environment = {"HOME": str(tmp_path)} if env is None else dict(env)
    if inside:
        environment.setdefault("HERDR_ENV", "1")
        environment.setdefault("HERDR_PANE_ID", "w0:p9")
    return Launch(client=client, watcher=watcher, cwd=CWD, server=SERVER,
                  agent=agent, agents_pane=agents_pane, env=environment,
                  state_path=tmp_path / "og-herdr.json",
                  spawn=spawn if spawn is not None else (lambda: None),
                  log_path=tmp_path / "logs" / "herdr-server.log")


# ---------------------------------------------------------------------------
# which session this is (the "is there one in $PWD" question)
# ---------------------------------------------------------------------------

def test_live_session_picks_the_root_whose_directory_matches(tmp_path):
    watcher = FakeWatcher([
        root_session("s-worker", cwd=CWD, parent_session_id="s2"),
        root_session("s-other", cwd="/elsewhere"),
        root_session("s-mine"),
    ])
    found = live_session(watcher, CWD)
    assert found["id"] == "s-mine"


def test_an_idle_root_counts_as_live_because_it_is_waiting_for_the_human(tmp_path):
    assert live_session(FakeWatcher([root_session(status="idle")]), CWD)["id"] == "s1"


def test_a_failed_root_does_not_block_a_fresh_start(tmp_path):
    assert live_session(FakeWatcher([root_session(status="failed")]), CWD) is None


def test_an_archived_root_does_not_block_a_fresh_start(tmp_path):
    assert live_session(FakeWatcher([root_session(archived=True)]), CWD) is None


def test_a_worker_session_is_not_the_conversation(tmp_path):
    watcher = FakeWatcher([root_session(parent_session_id="s-root")])
    assert live_session(watcher, CWD) is None


def test_a_session_in_another_directory_is_not_ours(tmp_path):
    assert live_session(FakeWatcher([root_session(cwd="/somewhere/else")]),
                        CWD) is None


def test_an_unreadable_session_list_is_an_error_not_an_empty_answer(tmp_path):
    """Guessing "no session" would open a SECOND conversation for a session that
    was merely unreachable — the one mistake nothing undoes."""
    with pytest.raises(LaunchError):
        live_session(UnreadableWatcher(), CWD)


def test_is_inside_reads_herdr_env():
    assert is_inside({"HERDR_ENV": "1"}) is True
    assert is_inside({"HERDR_ENV": "0"}) is False
    assert is_inside({}) is False


# ---------------------------------------------------------------------------
# (A) outside herdr, nothing in this directory
# ---------------------------------------------------------------------------

def test_case_a_creates_a_space_runs_chat_and_asks_to_attach(tmp_path):
    client = FakeClient()
    ctx = make_ctx(tmp_path, client, FakeWatcher())

    assert launch(ctx) == "attach"

    assert client.methods == [
        "ping",                      # is the herdr server up?
        "workspace.create",
        "tab.rename",
        "pane.send_text",            # og chat
        "pane.split",
        "pane.list",                 # the split's new pane is not named
        "pane.send_text",            # og agents
    ]
    assert client.params_of("workspace.create")[0] == {
        "label": "og", "cwd": CWD, "focus": True}
    assert client.params_of("tab.rename")[0] == {"tab_id": "w1:t1",
                                                 "label": "hivemind"}
    assert client.text_typed() == ["og chat", "og agents"]
    assert client.params_of("pane.send_text")[0]["pane_id"] == "w1:p1"
    assert client.params_of("pane.send_text")[1]["pane_id"] == "w1:p2"
    assert client.split_params["direction"] == "right"
    assert client.split_params["target_pane_id"] == "w1:p1"


def test_case_a_starts_the_herdr_server_when_it_is_down(tmp_path):
    """One start, then a wait for the socket — never a blind sleep."""
    started = []
    client = FakeClient(pings_before_up=2)
    ctx = make_ctx(tmp_path, client, FakeWatcher(),
                   spawn=lambda: started.append("herdr server"))

    assert launch(ctx) == "attach"
    assert started == ["herdr server"]
    assert client.methods.count("ping") == 3  # one probe, two while waiting


def test_case_a_reports_rather_than_hangs_when_the_server_never_opens(
        tmp_path, monkeypatch):
    monkeypatch.setattr("og_herdr_launch.SERVER_WAIT_SECONDS", 0.2)
    monkeypatch.setattr("og_herdr_launch.SERVER_POLL_SECONDS", 0.01)
    client = FakeClient(pings_before_up=10_000)
    ctx = make_ctx(tmp_path, client, FakeWatcher())

    with pytest.raises(LaunchError) as excinfo:
        launch(ctx)
    assert "did not answer" in str(excinfo.value)
    assert str(ctx.log_path) in str(excinfo.value)


def test_case_a_skips_the_server_check_when_it_is_already_up(tmp_path):
    started = []
    ctx = make_ctx(tmp_path, FakeClient(), FakeWatcher(),
                   spawn=lambda: started.append("x"))
    launch(ctx)
    assert started == []


def test_case_a_writes_a_pending_record_the_bridge_can_read(tmp_path):
    """The contract with og_herdr: it claims this by directory. Asserted with the
    BRIDGE'S OWN reader, not a re-implementation of it."""
    client = FakeClient()
    launch(make_ctx(tmp_path, client, FakeWatcher()))

    state = load_state(ctx_path(tmp_path))
    record = state["pending"][CWD]
    assert record == {"workspace_id": "w1", "tab_id": "w1:t1",
                      "pane_id": "w1:p1", "owner": "launcher", "cwd": CWD}
    assert "spaces" not in state or state["spaces"] == {}


def ctx_path(tmp_path):
    return tmp_path / "og-herdr.json"


# ---------------------------------------------------------------------------
# (B) outside herdr, the session is already here
# ---------------------------------------------------------------------------

def test_case_b_focuses_the_existing_space_and_attaches(tmp_path):
    (tmp_path / "og-herdr.json").write_text(
        '{"version": 2, "spaces": {"s1": {"workspace_id": "w7", '
        '"tab_id": "w7:t1", "pane_id": "w7:p1", "owner": "launcher"}}, '
        '"pending": {}}')
    client = FakeClient(workspaces=[{"workspace_id": "w7", "pane_count": 1}])
    ctx = make_ctx(tmp_path, client, FakeWatcher([root_session()]))

    assert launch(ctx) == "attach"
    assert client.methods == ["workspace.list", "workspace.focus"]
    assert client.params_of("workspace.focus")[0] == {"workspace_id": "w7"}


def test_case_b_adopts_when_the_session_has_no_space(tmp_path):
    """`omnigent attach`, NOT `og chat` — a second chat is a second conversation."""
    client = FakeClient()
    ctx = make_ctx(tmp_path, client, FakeWatcher([root_session()]))

    assert launch(ctx) == "attach"
    assert client.methods == [
        "workspace.list",            # nothing recorded: is that the whole story?
        "workspace.create",
        "tab.rename",
        "pane.send_text",
    ]
    assert client.text_typed() == [
        "omnigent attach --server {0} s1".format(SERVER)]
    assert client.params_of("workspace.create")[0]["label"] == "the conversation"

    state = load_state(ctx_path(tmp_path))
    assert state["spaces"]["s1"]["workspace_id"] == "w1"
    assert state["spaces"]["s1"]["owner"] == "launcher"
    assert state["pending"] == {}


def test_case_b_readopts_when_the_recorded_space_is_gone(tmp_path):
    """The file is a cache: a workspace herdr no longer lists must not be
    focused, and adopting over it must not leave the record lying."""
    (tmp_path / "og-herdr.json").write_text(
        '{"version": 2, "spaces": {"s1": {"workspace_id": "w-gone"}}, '
        '"pending": {}}')
    client = FakeClient(workspaces=[{"workspace_id": "w-other"}])
    launch(make_ctx(tmp_path, client, FakeWatcher([root_session()])))

    assert client.params_of("workspace.create")
    assert client.params_of("workspace.focus") == []  # never focused, never closed
    state = load_state(ctx_path(tmp_path))
    assert state["spaces"]["s1"]["workspace_id"] == "w1"


def test_case_b_refuses_to_guess_when_the_workspace_list_is_unreadable(tmp_path):
    class Blind(FakeClient):
        def workspace_list(self):
            raise RuntimeError("socket gone")

    with pytest.raises(LaunchError):
        launch(make_ctx(tmp_path, Blind(), FakeWatcher([root_session()])))


# ---------------------------------------------------------------------------
# (C) inside herdr, the session is already here
# ---------------------------------------------------------------------------

def test_case_c_focuses_the_session_space_then_closes_this_pane(tmp_path):
    (tmp_path / "og-herdr.json").write_text(
        '{"version": 2, "spaces": {"s1": {"workspace_id": "w7"}}, "pending": {}}')
    client = FakeClient(workspaces=[{"workspace_id": "w7"}])
    ctx = make_ctx(tmp_path, client, FakeWatcher([root_session()]), inside=True)

    assert launch(ctx) == "done"
    # The order IS the trick: closing a workspace's last pane removes the
    # workspace, so this pane must only close AFTER the operator is moved.
    assert client.methods == ["workspace.list", "workspace.focus", "pane.close"]
    assert client.params_of("pane.close") == [{"pane_id": "w0:p9"}]


def test_case_c_adopts_first_then_focuses_and_closes(tmp_path):
    client = FakeClient()
    ctx = make_ctx(tmp_path, client, FakeWatcher([root_session()]), inside=True)

    assert launch(ctx) == "done"
    assert client.methods == [
        "workspace.list",
        "workspace.create",
        "tab.rename",
        "pane.send_text",     # the attach, into the adopted space
        "workspace.focus",
        "pane.close",
    ]
    assert client.text_typed() == ["omnigent attach --server {0} s1".format(SERVER)]


def test_case_c_without_a_pane_id_refuses_rather_than_leaving_an_orphan(tmp_path):
    """Without $HERDR_PANE_ID the space herdr opened for the operator cannot be
    taken away with it, so say so instead of silently leaving one."""
    client = FakeClient()
    ctx = make_ctx(tmp_path, client, FakeWatcher([root_session()]),
                   env={"HERDR_ENV": "1"})

    with pytest.raises(LaunchError) as excinfo:
        launch(ctx)
    assert "HERDR_PANE_ID" in str(excinfo.value)
    assert "pane.close" not in client.methods


# ---------------------------------------------------------------------------
# (D) inside herdr, nothing in this directory
# ---------------------------------------------------------------------------

def test_case_d_creates_the_space_without_attaching_again(tmp_path):
    client = FakeClient()
    ctx = make_ctx(tmp_path, client, FakeWatcher(), inside=True)

    assert launch(ctx) == "done"
    # No ping: we are already inside the multiplexer, so its server is up by
    # definition and starting another one would be a second server.
    assert client.methods == [
        "workspace.create", "tab.rename", "pane.send_text",
        "pane.split", "pane.list", "pane.send_text",
    ]
    assert load_state(ctx_path(tmp_path))["pending"][CWD]["workspace_id"] == "w1"


# ---------------------------------------------------------------------------
# the agents pane
# ---------------------------------------------------------------------------

def test_agents_pane_off_means_no_split(tmp_path):
    client = FakeClient()
    ctx = make_ctx(tmp_path, client, FakeWatcher(), agents_pane=False)

    launch(ctx)
    assert client.methods == ["ping", "workspace.create", "tab.rename",
                              "pane.send_text"]
    assert client.text_typed() == ["og chat"]


def test_the_split_gives_the_chat_80_percent(tmp_path):
    """`pane.split`'s `ratio` is the share RETAINED by the pane being split
    (measured: 0.8 left the original at 172 columns, the new pane at 43), so 0.8
    is chat-left 80 / agents-right 20. Asserted as the EXACT params dict, so a
    dropped or renamed key — which silently restores herdr's 50/50 — fails here.
    """
    client = FakeClient()
    launch(make_ctx(tmp_path, client, FakeWatcher()))

    assert client.split_params == {
        "target_pane_id": "w1:p1", "direction": "right",
        "focus": False, "cwd": CWD, "ratio": 0.8,
    }
    assert AGENTS_PANE_RATIO == 0.8


def test_agents_pane_defaults_on_when_the_install_never_answered():
    """An install predating the key must still get the split it was configured
    for by default; defaulting to False would take it away silently."""
    assert agents_pane_enabled({}) is True
    assert agents_pane_enabled({"herdr_agents_pane": None}) is True
    assert agents_pane_enabled({"herdr_agents_pane": True}) is True
    assert agents_pane_enabled({"herdr_agents_pane": False}) is False


def test_a_split_reply_that_names_the_pane_is_believed(tmp_path):
    """The schema's pane.split result names nothing, so the launcher falls back
    to a listing — but a server that DOES name it should not be second-guessed."""
    client = FakeClient(split={"type": "ok", "pane": {"pane_id": "w1:pX"}})
    launch(make_ctx(tmp_path, client, FakeWatcher()))
    assert client.methods[-1] == "pane.send_text"
    assert client.params_of("pane.send_text")[1]["pane_id"] == "w1:pX"


def test_a_split_that_cannot_be_found_costs_the_agents_pane_only(tmp_path):
    client = FakeClient(panes=[{"pane_id": "w1:p1", "tab_id": "w1:t1"}])
    launch(make_ctx(tmp_path, client, FakeWatcher()))
    assert client.text_typed() == ["og chat"]


# ---------------------------------------------------------------------------
# a create reply that cannot be used
# ---------------------------------------------------------------------------

def test_a_create_reply_missing_the_pane_closes_the_space_it_just_opened(tmp_path):
    """Otherwise the operator finds an empty space nobody recorded — `og herdr
    --cleanup` does not know about it either."""
    client = FakeClient(create={"type": "workspace_created",
                                "workspace": {"workspace_id": "w1"}})
    ctx = make_ctx(tmp_path, client, FakeWatcher())

    with pytest.raises(LaunchError):
        launch(ctx)
    assert client.methods == ["ping", "workspace.create", "workspace.close"]
    assert "workspace.send_text" not in client.methods
    assert load_state(ctx_path(tmp_path))["pending"] == {}


def test_a_create_reply_with_no_workspace_id_cannot_close_anything(tmp_path):
    client = FakeClient(create={"type": "workspace_created"})
    with pytest.raises(LaunchError):
        launch(make_ctx(tmp_path, client, FakeWatcher()))
    assert "workspace.close" not in client.methods


def test_a_split_failure_never_fails_the_launch(tmp_path):
    class HalfDead(FakeClient):
        def call(self, method, params=None):
            if method == "pane.split":
                self.calls.append((method, params or {}))
                raise RuntimeError("no room")
            return super().call(method, params)

    client = HalfDead()
    launch(make_ctx(tmp_path, client, FakeWatcher()))
    assert client.text_typed() == ["og chat"]


# ---------------------------------------------------------------------------
# the install's own settings
# ---------------------------------------------------------------------------

def test_agent_name_defaults_when_the_plan_predates_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    assert agent_name(install_config()) == DEFAULT_AGENT_NAME
    (tmp_path / "og-install.json").write_text('{"agent_name": "hivemind"}')
    assert agent_name(install_config()) == "hivemind"


def test_install_config_survives_a_missing_or_broken_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    assert install_config() == {}
    (tmp_path / "og-install.json").write_text("{not json")
    assert install_config() == {}


# ---------------------------------------------------------------------------
# a multiplexer og has no backend for
# ---------------------------------------------------------------------------

def test_an_unimplemented_multiplexer_warns_and_starts_without_it(capsys):
    from og_herdr_launch import main

    assert main(["--mux", "tmux", "--once"]) == 0
    out = capsys.readouterr().out
    assert "no backend" in out
    assert "herdr.dev" not in out  # no false claim that tmux was installed
