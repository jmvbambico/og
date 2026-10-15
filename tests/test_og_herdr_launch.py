"""`og start <mux>` — the launcher: which space opens, and what runs in it.

Every test here injects a fake herdr client and a stub session API, so nothing
in this file can reach the operator's live herdr server, live spaces, or live
sessions. That is not a stylistic preference: the launcher closes panes and
removes workspaces, and a test that could do that to a real session would be a
worse hazard than the bug it is looking for.

The last section drives `og chat` itself, in a throwaway HOME against a fake
`omnigent`. That is the same discipline one file over: the handshake has a
BASH half, and bash is where "the flag was absent" and "the flag was given an
empty value" are told apart.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "installer"))

import og_herdr  # noqa: E402  (the shared state-file discipline, tested from here)
import og_herdr_launch  # noqa: E402
from og_herdr import DEFAULT_SPACE_CAP, load_state, save_state  # noqa: E402  (the bridge's own readers)
from og_herdr_launch import (  # noqa: E402
    AGENTS_PANE_RATIO,
    DEFAULT_AGENT_NAME,
    DEFAULT_PROJECTS_DIR,
    Launch,
    LaunchError,
    _hold_for_problems,
    agents_pane_enabled,
    agent_name,
    chat_pane,
    install_config,
    is_inside,
    launch,
    launch_log_path,
    live_session,
    local_host_id,
    main,
    pane_env_for,
    projects_dir,
    record_chat_pane,
    resolve_chat_dir,
    session_space_cap,
)
from og_herdr_watch import SessionEvent, SessionWatcher  # noqa: E402

SERVER = "http://127.0.0.1:6767"
CWD = "/Users/someone/projects/og"

# The two machines in every host test below: this one, which the server calls
# online, and the work-mac whose sessions share the SAME project path.
LOCAL_HOST = "host_this_mac"
WORK_HOST = "host_work_mac"

# A `/v1/hosts` payload in the measured shape. The offline host is listed FIRST
# on purpose: the read picks the ONLINE entry by status, not by position.
HOSTS = {"hosts": [{"host_id": WORK_HOST, "status": "offline"},
                   {"host_id": LOCAL_HOST, "status": "online"}]}


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _redirect_the_omnigent_home(tmp_path, monkeypatch):
    """Point `$OMNIGENT_HOME` at an empty directory for every test in this file.

    `local_host_id` reads this machine's OWN daemon record out of
    `<OMNIGENT_HOME>/daemons/*.json` — which is the point: it is the one source
    that says "this machine" rather than inferring it from a list of every host
    the account has. The cost is that on a developer who has actually installed
    og, the suite would read the REAL one: the launcher's host id would come out
    of the operator's live `~/.omnigent`, and the tests below would both depend
    on this machine's state and fail outright against it (CI has no
    `~/.omnigent` at all, so they would go green there and red here — the exact
    asymmetry the PYTHONPATH fixture in conftest.py exists to prevent).

    Redirecting the variable is the documented way to move that home (see
    og_herdr.default_home), so this changes where the answer is read from and
    nothing else. A test that WANTS a record writes one under `tmp_path`.
    """
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path / "omnigent-home"))

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
                 pings_before_up=0, state_path=None):
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
        # When set, every `pane.send_text` snapshots the pending records at the
        # moment the line is sent. This is how the ORDERING defect is caught: a
        # record written AFTER the send is absent from the snapshot.
        self.state_path = state_path
        self.pending_at_send = []

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

    def workspace_create(self, label, cwd, focus=False, env=None):
        # Mirrors the real wrapper: the `env` key is present only when the
        # caller asked for one, so an assertion can prove a launch that passes
        # no pane environment sends no `env` at all.
        params = {"label": label, "cwd": cwd, "focus": focus}
        if env:
            params["env"] = env
        return self.call("workspace.create", params)

    def workspace_close(self, workspace_id):
        self.call("workspace.close", {"workspace_id": workspace_id})

    def pane_run(self, pane_id, command):
        # Snapshot the pending records BEFORE recording this send, so an
        # assertion can prove the record was already on disk when the line
        # went out. The real wrapper appends the newline that submits the line;
        # mirrored here so an assertion on the text sees what herdr receives.
        if self.state_path is not None:
            self.pending_at_send.append(
                dict(load_state(self.state_path).get("pending") or {}))
        self.call("pane.send_text", {"pane_id": pane_id,
                                     "text": command + "\n"})

    def pane_close(self, pane_id):
        self.call("pane.close", {"pane_id": pane_id})

    def tab_rename(self, tab_id, label):
        # The real wrapper, mirrored so an assertion on the recorded frame sees
        # the same method name and params herdr receives.
        self.call("tab.rename", {"tab_id": tab_id, "label": label})

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
    """A SessionWatcher stand-in: `poll_once` returns what the test scripted.

    `_get_json` answers the OTHER request the launcher makes of a watcher — the
    `/v1/hosts` lookup behind `local_host_id` — and records the URLs it was asked
    for, so a test can prove that lookup rides the watcher's own path (which owns
    the server and its token) and happens once per launch. `hosts` overrides the
    payload; `hosts_error` makes the endpoint fail the way a server that is down
    would.
    """

    base_url = SERVER

    def __init__(self, sessions=(), hosts=None, hosts_error=None):
        self.base_url = SERVER
        self.events = [
            SessionEvent("added", session.get("id", "s?"), session)
            for session in sessions]
        self.hosts = HOSTS if hosts is None else hosts
        self.hosts_error = hosts_error
        self.urls = []

    def poll_once(self):
        return list(self.events)

    def _get_json(self, url):
        self.urls.append(url)
        if self.hosts_error is not None:
            raise self.hosts_error
        return self.hosts


class UnreadableWatcher(FakeWatcher):
    def poll_once(self):
        raise OSError("connection refused")


class _Body(io.BytesIO):
    """What a stub opener's `urlopen` hands back: a context manager with read()."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class RoutingOpener:
    """A stub HTTP layer for a REAL `SessionWatcher`, routed by path.

    `FakeWatcher` can hand `live_session` a session the real watcher would never
    emit — which is precisely how the host rule shipped green while being unable
    to fire: the launcher tests put `host_id` on the session dict, and nothing
    ever proved the real `_enrich` forwards it from the detail row. This answers
    by URL instead of by call order: the listing, one `/v1/sessions/{id}` detail
    fetch per session, and the `/v1/hosts` lookup behind `local_host_id` — which
    also pins that the hosts request travels the watcher's own authenticated
    path rather than a second opener of its own.
    """

    def __init__(self, rows, details, hosts=None):
        self.rows = list(rows)
        self.details = dict(details)
        self.hosts = HOSTS if hosts is None else hosts
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        path = urllib.parse.urlsplit(request.full_url).path
        if path == "/v1/hosts":
            body = self.hosts
        elif path == "/v1/sessions":
            body = {"object": "list", "data": self.rows, "has_more": False}
        else:
            body = self.details[urllib.parse.unquote(path.rsplit("/", 1)[-1])]
        return _Body(json.dumps(body).encode())


def real_watcher(rows, details, hosts=None):
    """The REAL watcher over a stubbed HTTP layer: no network, and no fake able
    to report a shape the enrichment path does not actually produce."""
    return SessionWatcher(base_url=SERVER,
                          opener=RoutingOpener(rows, details, hosts))


class NoSplitClient(FakeClient):
    """A multiplexer whose `pane.split` fails — the agents pane's own failure."""

    def call(self, method, params=None):
        if method == "pane.split":
            self.calls.append((method, params or {}))
            raise RuntimeError("no room")
        return super().call(method, params)


def root_session(sid="s1", cwd=CWD, status="idle", **extra):
    session = {"id": sid, "parent_session_id": None, "archived": False,
               "status": status, "workspace": cwd,
               "title": "the conversation"}
    session.update(extra)
    return session


def listed(sid="s1", status="idle", **extra):
    """A LISTING row in the MEASURED shape — the keys `GET /v1/sessions?kind=any`
    actually returns, which is to say no `host_id`, no `workspace` and no
    `runner_online`. Anything the launcher reads beyond these three has to have
    been merged in from the detail fetch, which is the whole point of building
    the fixture from the API's shape rather than from what the code wants."""
    row = {"id": sid, "agent_id": "a1", "agent_name": "hivemind",
           "archived": False, "comments_count": 0, "created_at": 1791566251,
           "external_session_id": "ses_" + sid, "labels": {}, "owner": "cryogenix",
           "parent_session_id": None, "pending_elicitations_count": 0,
           "permission_level": 4, "runner_id": "runner_token_b6d2",
           "status": status, "title": "the conversation",
           "updated_at": 1791566384, "viewer_unread": False}
    row.update(extra)
    return row


def detail_of(sid="s1", workspace=CWD, host_id=None, **extra):
    """A `GET /v1/sessions/{id}` row: the directory, the machine, and the rest
    the listing does not carry."""
    row = {"id": sid, "workspace": workspace, "parent_session_id": None,
           "kind": "default", "harness": "opencode-native",
           "sub_agent_name": None, "host_id": host_id, "git_branch": None,
           "pending_elicitations": [], "runner_online": True, "status": "idle",
           "title": "the conversation"}
    row.update(extra)
    return row


def make_ctx(tmp_path, client, watcher, *, inside=False, env=None,
             agents_pane=True, spawn=None, pane_env=None):
    environment = {"HOME": str(tmp_path)} if env is None else dict(env)
    if inside:
        environment.setdefault("HERDR_ENV", "1")
        environment.setdefault("HERDR_PANE_ID", "w0:p9")
    return Launch(client=client, watcher=watcher, cwd=CWD, server=SERVER,
                  agents_pane=agents_pane, env=environment,
                  state_path=tmp_path / "og-herdr.json",
                  pane_env={} if pane_env is None else pane_env,
                  spawn=spawn if spawn is not None else (lambda: None),
                  log_path=tmp_path / "logs" / "herdr-server.log")


def daemon_home(tmp_path, records):
    """An `$OMNIGENT_HOME` whose `daemons/` holds `records` — a signature ->
    record mapping, written as the host daemon writes it.

    Under the same path the autouse fixture redirects the variable to, so a test
    that builds one here also exercises the `launch` path that reads the
    environment rather than the `home` argument.
    """
    home = tmp_path / "omnigent-home"
    (home / "daemons").mkdir(parents=True, exist_ok=True)
    for signature, record in records.items():
        (home / "daemons" / "{0}.json".format(signature)).write_text(
            json.dumps(record))
    return home


def daemon_record(signature=None, host_id=LOCAL_HOST, server_url=SERVER, **over):
    """One `daemons/*.json` row, shaped like the real one (`omnigent host status`
    prints the same `host_id`). `resolved_server_url` is null on a server-mode
    record, which is why every URL key is exercised separately below."""
    row = {"config_sig": signature or "18fa469718d9c868", "host_id": host_id,
           "log_path": None, "mode": "server", "pid": 59562,
           "resolved_server_url": None, "server_url": server_url,
           "started_at": 1791941709, "target": server_url}
    row.update(over)
    return row


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


# --- the runner rule, re-applied here because poll_once withholds it for ROOTS ---
# THE REGRESSION THIS PINS: poll_once projects a dead ROOT on purpose — it becomes
# a history space in the bridge — so an event is not the same thing as a session
# to attach to. A dead root reads status "idle" and passes every listing filter, so
# without this check it wins the "is there a live session here?" question and
# `launch` types an attach that dies with "no online runner — its host is offline".

def test_a_dead_root_in_this_directory_is_not_the_live_session(tmp_path):
    assert live_session(FakeWatcher([root_session(runner_online=False)]), CWD) is None


def test_a_live_root_in_this_directory_is_still_the_live_session(tmp_path):
    """The other side of the same pair: the check must not refuse a root whose
    runner IS online. `runner_online: True` is forwarded onto the event, so this is
    the exact object live_session sees in production."""
    found = live_session(FakeWatcher([root_session(runner_online=True)]), CWD)
    assert found["id"] == "s1"


def test_an_unreadable_runner_answer_still_counts_as_live(tmp_path):
    """THE CHOSEN DIRECTION for the three-state field. Only an explicit
    `runner_online: False` is dead; an absent key — a detail row never read, or one
    that carried no boolean — is UNKNOWN and counts as LIVE. Refusing it would be
    the one outcome that cannot be undone: a second space and a second `og chat`
    into a conversation the operator already had. The ambiguous case therefore errs
    toward attaching, which is at worst a visible, re-runnable error."""
    assert live_session(FakeWatcher([root_session()]), CWD)["id"] == "s1"
    # A None spelling is the same "unknown" — the fetcher could not fill it — and
    # must likewise stay live, not read as "offline".
    assert live_session(
        FakeWatcher([root_session(runner_online=None)]), CWD)["id"] == "s1"


def test_a_dead_root_does_not_mask_a_live_one_behind_it(tmp_path):
    """The skip CONTINUES the scan rather than stopping it: a directory with a dead
    past conversation AND a live one still finds the live one."""
    watcher = FakeWatcher([root_session("s-dead", runner_online=False),
                           root_session("s-live", runner_online=True)])
    assert live_session(watcher, CWD)["id"] == "s-live"


def test_a_dead_root_in_another_directory_does_not_interfere(tmp_path):
    """Not ours is not ours, dead or alive — a dead root elsewhere leaves THIS
    directory with no session, so its launch still opens a fresh space."""
    watcher = FakeWatcher([root_session("s-dead", cwd="/elsewhere",
                                        runner_online=False)])
    assert live_session(watcher, CWD) is None


# --- the host rule: a session on ANOTHER machine is not this directory's ---
# THE REGRESSION THIS PINS: a session running on a work-mac, in the SAME project
# path as this one (`~/projects/og` is not an unusual directory to have checked
# out twice), turned up in the local `kind=any` listing, matched
# `workspace == cwd`, passed the status and runner checks, and was handed back as
# "the live session" — so `launch` took case (B), `_adopt` typed the attach, and
# the pane said:
#
#     Error: Session … has no online runner on … - its host is offline.
#
# `runner_is_offline` cannot catch that, by design: it is True only for an
# explicit `runner_online: False`, and a session on a machine that is simply not
# here reports nothing at all. The answer exists though — `/v1/hosts` already
# says which host this machine is — so this is a real comparison, not a heuristic.

def test_a_session_on_another_machine_is_not_the_live_session():
    watcher = FakeWatcher([root_session("s-work", host_id=WORK_HOST)])
    assert live_session(watcher, CWD, host_id=LOCAL_HOST) is None


def test_a_session_on_this_machine_is_still_the_live_session():
    """The other side of the pair: a filter that refused everything would pass
    the test above and lose case (B) entirely, which looks exactly like a launch
    that worked."""
    watcher = FakeWatcher([root_session(host_id=LOCAL_HOST)])
    assert live_session(watcher, CWD, host_id=LOCAL_HOST)["id"] == "s1"


def test_a_session_naming_no_host_is_not_assumed_to_be_another_machines():
    """Unknown is not "not mine". The rule needs BOTH ids known and different;
    one side unreadable leaves the session alone, for the reason the runner rule
    reads an unreadable answer as live: a skip we could not justify opens a second
    space and types a second `og chat` into a conversation the operator already
    had, and the opposite error is a line on screen they can act on."""
    for extra in ({}, {"host_id": None}, {"host_id": ""}, {"host_id": 7}):
        watcher = FakeWatcher([root_session(**extra)])
        assert live_session(watcher, CWD, host_id=LOCAL_HOST)["id"] == "s1", extra


def test_a_session_on_another_machine_does_not_mask_this_one_behind_it():
    """The skip CONTINUES the scan, like every other filter here: a foreign row
    listed first (the listing is newest-first) must not hide the real one."""
    watcher = FakeWatcher([root_session("s-work", host_id=WORK_HOST),
                           root_session("s-mine", host_id=LOCAL_HOST)])
    assert live_session(watcher, CWD, host_id=LOCAL_HOST)["id"] == "s-mine"


def test_the_skip_says_which_session_and_whose_host_it_was(capsys):
    """A silently skipped session is indistinguishable from a launch that simply
    found nothing, and the operator has no way to tell that a conversation on
    another machine is the reason they got a new one."""
    live_session(FakeWatcher([root_session("s-work", host_id=WORK_HOST)]),
                 CWD, host_id=LOCAL_HOST)
    out = capsys.readouterr().out
    assert "s-work" in out and WORK_HOST in out and "not this machine" in out


def test_a_foreign_session_opens_a_new_space_instead_of_a_doomed_attach(tmp_path):
    """THE REGRESSION, end to end: the case the bug produced. A session from the
    work-mac in this directory must fall through to case (A) — open a space, run
    `og chat`, split the agents pane — rather than adopt and type an attach that
    dies with "its host is offline"."""
    client = FakeClient()
    watcher = FakeWatcher([root_session("s-work", host_id=WORK_HOST)])

    assert launch(make_ctx(tmp_path, client, watcher)) == "attach"

    assert client.text_typed() == ["og chat", "og agents"]
    assert ("omnigent attach --server {0} s-work".format(SERVER)
            not in client.text_typed())


def test_a_hosts_endpoint_that_cannot_be_read_still_attaches(tmp_path):
    """THE CHOSEN DIRECTION for OUR side of the comparison. The very session the
    test above refuses is attached when the lookup that would have refused it
    could not be answered: a hosts endpoint we could not read is not evidence that
    anything is foreign. Refusing on that evidence is the outcome that cannot be
    undone — a second space, and a second `og chat` typed into a conversation the
    operator already had."""
    client = FakeClient()
    watcher = FakeWatcher([root_session("s-work", host_id=WORK_HOST)],
                          hosts_error=OSError("connection refused"))

    launch(make_ctx(tmp_path, client, watcher))

    assert client.text_typed() == [
        "omnigent attach --server {0} s-work".format(SERVER)]


def test_no_online_host_means_this_machine_is_unknown_not_foreign(tmp_path):
    """The same session again, with a payload that answers the request and still
    cannot say who we are: every host is offline (this one included — it is what
    a machine that has not brought its host up looks like). "No online host" is
    an absence, not a verdict, so nothing is filtered."""
    client = FakeClient()
    watcher = FakeWatcher([root_session("s-work", host_id=WORK_HOST)],
                          hosts={"hosts": [{"host_id": LOCAL_HOST,
                                            "status": "offline"}]})

    launch(make_ctx(tmp_path, client, watcher))

    assert client.text_typed() == [
        "omnigent attach --server {0} s-work".format(SERVER)]


def test_the_host_lookup_rides_the_watchers_own_request_and_happens_once(
        tmp_path):
    """ONE request per launch, asked of the watcher — and only when this
    machine's daemon record has nothing to say (the autouse fixture points
    `$OMNIGENT_HOME` at an empty directory, which is the single-machine-install
    case). The watcher is the only holder of the base URL and the per-server
    token, and `--server` can name a server we were not handed one for — a second
    urllib call would be a second opinion about which server we are talking to,
    and would present a credential to a host it was not issued for. Counted once
    even with several candidates to judge."""
    watcher = FakeWatcher([root_session("s1"),
                           root_session("s-elsewhere", cwd="/elsewhere"),
                           root_session("s-dead", runner_online=False),
                           root_session("s-work", host_id=WORK_HOST)])

    launch(make_ctx(tmp_path, FakeClient(), watcher))

    assert watcher.urls == [SERVER + "/v1/hosts"]


def test_the_local_host_is_the_one_the_server_calls_online():
    """`/v1/hosts` answers a LIST and the machine we are is the entry whose status
    is online — the same read `ensure_project` in bin/og makes before it pins a
    project to a host, deliberately in the same shape, so the pin and this filter
    cannot disagree about which machine og is on."""
    assert local_host_id(FakeWatcher()) == LOCAL_HOST
    # Order is the server's, not ours: the first entry listed is not the answer.
    watcher = FakeWatcher(hosts={"hosts": [
        {"host_id": "host_asleep", "status": "offline"},
        {"host_id": LOCAL_HOST, "status": "online"}]})
    assert local_host_id(watcher) == LOCAL_HOST


def test_an_answer_that_does_not_name_this_machine_leaves_it_unknown():
    """Everything /v1/hosts can fail to say, in one list. None means UNKNOWN and
    unknown filters nothing; the launch must never die here over a lookup whose
    only job is to make one filter sharper."""
    for payload in ({},                                   # no hosts key
                    {"hosts": []},                       # nothing connected
                    {"hosts": [{"host_id": LOCAL_HOST, "status": "offline"}]},
                    {"hosts": [{"status": "online"}]},    # online, but no id
                    {"hosts": [{"host_id": 7, "status": "online"}]},
                    {"hosts": [{"host_id": "", "status": "online"}]},
                    {"hosts": "nonsense"},                # not a list
                    [],                                  # not an object
                    "nonsense"):
        assert local_host_id(FakeWatcher(hosts=payload)) is None, payload
    assert local_host_id(FakeWatcher(hosts_error=OSError("refused"))) is None


def test_the_host_rule_runs_against_the_real_enrichment_path(tmp_path):
    """THE TEST THAT WAS MISSING, end to end, and the reason the first version
    of this fix could not work: the launcher tests drove a FAKE watcher, which
    could put `host_id` on a session the real `_enrich` never emits — because
    the listing row does not carry it and nothing forwarded the detail's. The
    rule passed its tests and could never fire in production.

    Everything here is real except the socket: a LISTING row in the measured
    shape (no `host_id` anywhere in it) plus a DETAIL fetch per session, through
    the real `SessionWatcher`, into the real `live_session`. The work-mac session
    must be skipped and a fresh space opened."""
    rows = [listed("s-work")]
    details = {"s-work": detail_of("s-work", host_id=WORK_HOST)}
    client = FakeClient()

    assert launch(make_ctx(tmp_path, client, real_watcher(rows, details))) == "attach"

    assert client.text_typed() == ["og chat", "og agents"]
    assert ("omnigent attach --server {0} s-work".format(SERVER)
            not in client.text_typed())


def test_a_session_on_this_machine_still_attaches_through_the_real_watcher(
        tmp_path):
    """The other direction through the same real path, so the test above cannot
    be satisfied by a filter that refuses everything. This session came off the
    same detail row, from the same fetch, carrying THIS machine's host."""
    rows = [listed("s-mine")]
    details = {"s-mine": detail_of("s-mine", host_id=LOCAL_HOST)}
    client = FakeClient()

    assert launch(make_ctx(tmp_path, client, real_watcher(rows, details))) == "attach"

    assert client.text_typed() == [
        "omnigent attach --server {0} s-mine".format(SERVER)]


def test_a_real_detail_row_with_no_host_still_attaches(tmp_path):
    """The unknown direction, through the real path: a detail row whose
    `host_id` is null (a sub-agent's shape) reaches the launcher with NO key at
    all — not a null — and the rule must leave it alone rather than read the
    absence as "somewhere else"."""
    rows = [listed("s-nohost")]
    details = {"s-nohost": detail_of("s-nohost", host_id=None)}
    client = FakeClient()

    assert launch(make_ctx(tmp_path, client, real_watcher(rows, details))) == "attach"

    assert client.text_typed() == [
        "omnigent attach --server {0} s-nohost".format(SERVER)]


def test_the_hosts_lookup_rides_the_same_authenticated_path(tmp_path):
    """With a real watcher in hand, the fallback request is provably the
    watcher's own: same opener, same base URL, and the token that was handed to
    it — `--server` can name a server we hold no token for, and a second opener
    would be a second opinion about which server we are on. (On a machine with a
    daemon record there is no request at all; see
    `test_a_daemon_record_skips_the_hosts_request_entirely`.)"""
    opener = RoutingOpener([listed("s-mine")],
                           {"s-mine": detail_of("s-mine", host_id=LOCAL_HOST)})
    watcher = SessionWatcher(base_url=SERVER, token="secret", opener=opener)

    launch(make_ctx(tmp_path, FakeClient(), watcher))

    hosts = [r for r in opener.requests
             if urllib.parse.urlsplit(r.full_url).path == "/v1/hosts"]
    assert [r.full_url for r in hosts] == [SERVER + "/v1/hosts"]
    assert all(r.get_header("Authorization") == "Bearer secret" for r in hosts)


# --- which machine am I? the daemon record first, the API only if unambiguous ---
# THE REVIEW FINDING, which is the second bug in this story and the more
# expensive one: `/v1/hosts` describes every host the ACCOUNT has, across every
# machine, so "the first online entry" is a position in a list this process has
# no stake in. On a user who genuinely runs two machines — the user this whole
# bug came from — the other one sorts first, and every `og start herdr` then
# judged THIS machine's own session foreign: skipped, and a brand new space
# opened. That is the unrecoverable direction (a second space, and a second `og
# chat` into a conversation the operator already had) arriving through the very
# filter added to prevent it.
#
# So: this machine's own daemon record decides, and an ambiguous answer is
# unknown rather than a guess.

def test_a_daemon_record_beats_another_machine_listed_first_and_online(tmp_path):
    """THE REGRESSION GUARD for that finding, and it fails against the version
    that took the first online host: `/v1/hosts` answers with exactly one online
    entry, it is the WRONG machine, and the record this machine's own daemon
    wrote says so."""
    home = daemon_home(tmp_path, {"21c1ba0105d3dd34": daemon_record()})
    hosts = {"hosts": [{"host_id": WORK_HOST, "status": "online"}]}

    assert local_host_id(FakeWatcher(hosts=hosts), home=home) == LOCAL_HOST
    # …and what the API alone says, which is precisely what the record exists to
    # correct. Pinned so the fallback can never quietly grow back into "first
    # online wins".
    assert local_host_id(FakeWatcher(hosts=hosts),
                         home=tmp_path / "no-daemon-home") == WORK_HOST


def test_two_online_hosts_and_no_daemon_record_is_unknown_not_the_first(tmp_path):
    """The finding's direct answer: the list cannot say who we are, so we do not
    say. Guessing would skip the operator's own sessions on a machine that has
    both online."""
    hosts = {"hosts": [{"host_id": WORK_HOST, "status": "online"},
                       {"host_id": LOCAL_HOST, "status": "online"}]}
    assert local_host_id(FakeWatcher(hosts=hosts),
                         home=tmp_path / "no-daemon-home") is None


def test_a_local_session_still_attaches_when_the_hosts_list_cannot_say(tmp_path):
    """…and the consequence of that unknown, end to end. The session below IS on
    this machine, so a filter that guessed would skip it and open a second space
    in a conversation the operator already had. Unknown filters nothing."""
    client = FakeClient()
    watcher = FakeWatcher([root_session("s-mine", host_id=LOCAL_HOST)],
                          hosts={"hosts": [{"host_id": WORK_HOST, "status": "online"},
                                           {"host_id": LOCAL_HOST,
                                            "status": "online"}]})

    launch(make_ctx(tmp_path, client, watcher))

    assert client.text_typed() == [
        "omnigent attach --server {0} s-mine".format(SERVER)]


def test_one_online_host_and_no_daemon_record_is_used(tmp_path):
    """The fallback's precondition, and the case it is still good for: ONE
    online host leaves nothing to confuse it with. This is the shape a
    single-machine install is in, which is why the fallback exists at all."""
    assert local_host_id(FakeWatcher(), home=tmp_path / "no-daemon-home") == LOCAL_HOST


@pytest.mark.parametrize("url_key", ["server_url", "target",
                                     "resolved_server_url"])
def test_a_record_counts_as_ours_under_any_of_its_three_url_keys(tmp_path, url_key):
    """Which key is populated depends on mode and age — `resolved_server_url` is
    null on a server-mode record — so reading only one would make the record
    invisible for half the daemons this codebase starts."""
    record = daemon_record(**{url_key: SERVER})
    home = daemon_home(tmp_path, {"sig": record})

    assert local_host_id(FakeWatcher(), home=home) == LOCAL_HOST


def test_a_record_for_another_server_is_not_ours(tmp_path):
    """`--server` can name a server this launch is not talking to, and a daemon
    record for THAT one says nothing about us."""
    home = daemon_home(tmp_path, {"sig": daemon_record(
        server_url="http://10.0.0.9:6767")})

    assert local_host_id(FakeWatcher(), home=tmp_path / "no-daemon-home") == LOCAL_HOST
    assert local_host_id(FakeWatcher(hosts={"hosts": []}),
                         home=home) is None


@pytest.mark.parametrize("shape", ["no_daemon_dir", "unreadable", "not_json",
                                   "not_an_object", "no_host_id", "null_host_id",
                                   "empty_host_id", "numeric_host_id",
                                   "another_server_only"])
def test_a_daemon_record_that_cannot_answer_falls_through_to_the_api(tmp_path,
                                                                     shape):
    """Every way a record can fail to say anything, and NONE of them may raise:
    this lookup only ever sharpens a filter, so it must not be able to stop a
    launch. Each one falls through to the unambiguous single-online answer, which
    is what the pre-record behaviour was."""
    home = tmp_path / "omnigent-home"
    if shape == "no_daemon_dir":
        home = tmp_path / "never-created"
    elif shape == "unreadable":
        # A DIRECTORY named like a record: read_text raises IsADirectoryError,
        # which is the portable stand-in for "the file could not be read" (a
        # chmod-000 file is not, under a runner that may be root).
        (home / "daemons").mkdir(parents=True)
        (home / "daemons" / "sig.json").mkdir()
    elif shape == "not_json":
        home = daemon_home(tmp_path, {})
        (home / "daemons" / "sig.json").write_text("{not json")
    elif shape == "not_an_object":
        home = daemon_home(tmp_path, {})
        (home / "daemons" / "sig.json").write_text('["a", "list"]')
    elif shape == "no_host_id":
        record = daemon_record()
        del record["host_id"]
        home = daemon_home(tmp_path, {"sig": record})
    elif shape == "null_host_id":
        home = daemon_home(tmp_path, {"sig": daemon_record(host_id=None)})
    elif shape == "empty_host_id":
        home = daemon_home(tmp_path, {"sig": daemon_record(host_id="")})
    elif shape == "numeric_host_id":
        home = daemon_home(tmp_path, {"sig": daemon_record(host_id=7)})
    else:
        home = daemon_home(tmp_path, {"sig": daemon_record(
            server_url="http://10.0.0.9:6767")})

    # Falls through to the API's single online host rather than raising or
    # returning a half-answer.
    assert local_host_id(FakeWatcher(), home=home) == LOCAL_HOST


@pytest.mark.parametrize("spelling", ["http://127.0.0.1:6767/",
                                      "HTTP://127.0.0.1:6767"])
def test_a_record_counts_as_ours_when_the_urls_differ_only_in_spelling(tmp_path,
                                                                      spelling):
    """The match goes through `og_herdr_watch._server_key`, the same canonical
    form that decides which token belongs to which server, so a trailing slash or
    a shouted scheme is one server rather than two — and a userinfo in the URL
    cannot reach the comparison, the same reason that function strips it."""
    home = daemon_home(tmp_path, {"sig": daemon_record(server_url=spelling)})

    assert local_host_id(FakeWatcher(), home=home) == LOCAL_HOST


def test_a_record_naming_another_port_is_not_ours(tmp_path):
    """Canonicalisation must not be so aggressive that it merges servers: the
    port is part of the identity, exactly as it is for the token."""
    home = daemon_home(tmp_path, {"sig": daemon_record(
        server_url="http://127.0.0.1:6768")})

    assert local_host_id(FakeWatcher(hosts={"hosts": []}), home=home) is None


def test_two_records_for_this_server_that_agree_are_one_answer(tmp_path):
    """The filename is a config signature, so a restarted daemon writes a second
    one. Agreement is not ambiguity — it is the same machine, twice."""
    home = daemon_home(tmp_path, {
        "21c1ba0105d3dd34": daemon_record(),
        "9f0c1122aabbccdd": daemon_record(signature="deadbeefdeadbeef"),
    })

    assert local_host_id(FakeWatcher(), home=home) == LOCAL_HOST


def test_two_records_for_this_server_that_disagree_are_unknown(tmp_path):
    """And disagreement IS ambiguity, and it is NOT a fall-through to the API:
    two daemons on this machine claiming this server with two different machines
    is our own evidence contradicting itself, and that is precisely when letting
    the account-wide list answer instead would be worst. Unknown even though
    `/v1/hosts` would have offered a tidy single online host."""
    home = daemon_home(tmp_path, {
        "21c1ba0105d3dd34": daemon_record(),
        "9f0c1122aabbccdd": daemon_record(host_id=WORK_HOST),
    })

    assert local_host_id(FakeWatcher(hosts=HOSTS), home=home) is None


def test_contested_records_still_attach_rather_than_skipping(tmp_path):
    """…and unknown still means unknown: the session below is on this machine,
    so the contested lookup must cost the operator nothing."""
    daemon_home(tmp_path, {"21c1ba0105d3dd34": daemon_record(),
                           "9f0c1122aabbccdd": daemon_record(host_id=WORK_HOST)})
    client = FakeClient()
    watcher = FakeWatcher([root_session("s-mine", host_id=LOCAL_HOST)])

    launch(make_ctx(tmp_path, client, watcher))

    assert client.text_typed() == [
        "omnigent attach --server {0} s-mine".format(SERVER)]


def test_a_daemon_record_still_lets_a_foreign_session_be_skipped(tmp_path):
    """(f) The behaviour the rule exists for, with the id now coming from the
    record rather than the API. A work-mac session in this directory must not be
    adopted just because the identity is now certain."""
    daemon_home(tmp_path, {"21c1ba0105d3dd34": daemon_record()})
    client = FakeClient()
    watcher = FakeWatcher([root_session("s-work", host_id=WORK_HOST)])

    launch(make_ctx(tmp_path, client, watcher))

    assert client.text_typed() == ["og chat", "og agents"]
    assert ("omnigent attach --server {0} s-work".format(SERVER)
            not in client.text_typed())


def test_a_daemon_record_still_attaches_this_machines_own_session(tmp_path):
    """The other side of that pair, and the regression guard for the finding in
    its worst shape: a session on THIS host must never be skipped just because
    another machine exists."""
    daemon_home(tmp_path, {"21c1ba0105d3dd34": daemon_record()})
    client = FakeClient()
    watcher = FakeWatcher([root_session("s-mine", host_id=LOCAL_HOST)],
                          hosts={"hosts": [{"host_id": WORK_HOST, "status": "online"},
                                           {"host_id": LOCAL_HOST,
                                            "status": "online"}]})

    launch(make_ctx(tmp_path, client, watcher))

    assert client.text_typed() == [
        "omnigent attach --server {0} s-mine".format(SERVER)]


def test_a_daemon_record_skips_the_hosts_request_entirely(tmp_path):
    """With an identity in hand there is nothing left for `/v1/hosts` to answer,
    so the launch makes no request at all."""
    daemon_home(tmp_path, {"21c1ba0105d3dd34": daemon_record()})
    watcher = FakeWatcher([root_session("s-mine", host_id=LOCAL_HOST)])

    launch(make_ctx(tmp_path, FakeClient(), watcher))

    assert watcher.urls == []


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
    # The tab is labelled with the SPACE's own label (the directory basename),
    # not the agent bundle's name: `workspace.create` cannot label the tab it
    # makes, so without this it would show herdr's tab number.
    assert client.params_of("tab.rename")[0] == {"tab_id": "w1:t1",
                                                 "label": "og"}
    assert client.text_typed() == ["og chat", "og agents"]
    assert client.params_of("pane.send_text")[0]["pane_id"] == "w1:p1"
    assert client.params_of("pane.send_text")[1]["pane_id"] == "w1:p2"
    assert client.split_params["direction"] == "right"
    assert client.split_params["target_pane_id"] == "w1:p1"


def test_a_dead_root_in_the_cwd_opens_a_new_space_and_splits_the_agents_pane(tmp_path):
    """THE REGRESSION, end to end. A project whose only session is a dead past
    conversation must fall through to case (A): open a space, run `og chat`, and
    split the agents pane — NOT take case (B) and type a doomed attach.

    The split is asserted because it is the SECOND symptom: case (B) never splits,
    so a directory that wrongly takes (B) silently loses its agents pane too, which
    is the unexplained missing-pane report this shares a root cause with."""
    client = FakeClient()
    ctx = make_ctx(tmp_path, client, FakeWatcher([root_session(runner_online=False)]))

    assert launch(ctx) == "attach"

    assert client.methods == [
        "ping",                      # is the herdr server up?
        "workspace.create",
        "tab.rename",
        "pane.send_text",            # og chat — a FRESH conversation
        "pane.split",                # the agents pane IS split
        "pane.list",
        "pane.send_text",            # og agents
    ]
    assert client.text_typed() == ["og chat", "og agents"]
    # The doomed attach is exactly what must NOT have been typed.
    assert "omnigent attach --server {0} s1".format(SERVER) not in client.text_typed()


def test_a_live_root_in_the_cwd_still_attaches_and_never_splits(tmp_path):
    """"Unchanged" pinned on purpose. A root whose runner is online still takes
    case (B) and adopts with an attach; a runner check that refused EVERYTHING
    would satisfy the regression test above — and an attach that never happens
    looks exactly like a launch that worked."""
    client = FakeClient()
    ctx = make_ctx(tmp_path, client, FakeWatcher([root_session(runner_online=True)]))

    assert launch(ctx) == "attach"
    assert client.text_typed() == ["omnigent attach --server {0} s1".format(SERVER)]
    assert "pane.split" not in client.methods


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


def test_case_a_writes_the_pending_record_before_it_types_chat(tmp_path):
    """THE RACE THIS GUARDS: the bridge is already polling when the launcher
    runs, and `og chat` mints the session the instant it is typed — so the
    pending record must be on disk BEFORE that line goes out, or the poll can
    see a root session with no record and open a second space.

    This asserts the ORDER, not just that both happened: the snapshot is taken
    at the instant the chat line is sent. A test that only checked the record
    existed afterwards would pass on the broken tree (where it was written
    last)."""
    client = FakeClient(state_path=ctx_path(tmp_path))
    launch(make_ctx(tmp_path, client, FakeWatcher()))

    assert client.text_typed() == ["og chat", "og agents"]
    assert len(client.pending_at_send) == 2
    # When `og chat` went out, the record was already there.
    assert CWD in client.pending_at_send[0]


def test_case_d_writes_the_pending_record_before_it_types_chat(tmp_path):
    """(D) shares `_open_space`, so it shares the ordering fix."""
    client = FakeClient(state_path=ctx_path(tmp_path))
    launch(make_ctx(tmp_path, client, FakeWatcher(), inside=True))

    assert client.text_typed() == ["og chat", "og agents"]
    assert CWD in client.pending_at_send[0]


def test_a_record_waits_for_the_lock_the_bridge_holds_and_lands_on_top(tmp_path):
    """BOTH WRITERS TAKE THE SAME LOCK, which is the whole of the discipline.

    `_record` re-reads before it writes, and that used to be enough to argue it
    could not erase what the bridge wrote. It is not enough, because the bridge
    now reads, decides and writes inside one transaction: a launcher whose write
    lands between the bridge's read and its claim is erased by the claim's own
    save, taking the operator's freshly opened space out of the file. So both
    sides hold `og_herdr.state_lock`, and this asserts the launcher side really
    does — it cannot get in while the bridge holds the file, and when it gets in
    it reads the bridge's committed state and writes on top of it.

    The competitor is a thread, which is the same thing to `flock` as a second
    process: the lock rides the open file description, and two opens exclude
    each other within one process exactly as they do across two.
    """
    path = ctx_path(tmp_path)
    path.write_text(json.dumps(
        {"version": 2,
         "spaces": {"s1": {"workspace_id": "w7", "tab_id": "w7:t1",
                           "pane_id": "w7:p1", "owner": "bridge"}},
         "pending": {}}))
    ctx = make_ctx(tmp_path, FakeClient(), FakeWatcher())
    record = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1",
              "owner": "launcher", "cwd": CWD}
    done = threading.Event()

    def write_it():
        og_herdr_launch._record(ctx.state_path, "pending", CWD, record)
        done.set()

    with og_herdr.state_lock(path):
        writer = threading.Thread(target=write_it, daemon=True)
        writer.start()
        # Short on purpose, and it can only fail this way if the lock is not
        # being taken: `_record` has no other reason to take a tenth of a second
        # to write a 200-byte file.
        assert not done.wait(0.1), "_record wrote while the bridge held the file"

    writer.join(timeout=10)
    assert done.is_set(), "_record never completed once the lock was released"
    state = load_state(path)
    # The bridge's record is still there: the re-read happened INSIDE the lock,
    # not before it was taken.
    assert state["spaces"]["s1"]["workspace_id"] == "w7"
    assert state["pending"][CWD] == record


def test_a_chat_that_fails_to_start_leaves_no_pending_record(tmp_path):
    """A failure AFTER the record is written must not leave a pending record
    pointing at a space: the space is closed, no session will ever claim the
    record, and the bridge only prunes records whose workspace is GONE (this one
    is live), so the launcher has to drop it itself."""
    class NoPane(FakeClient):
        def pane_run(self, pane_id, command):
            raise RuntimeError("pane is gone")

    client = NoPane(state_path=ctx_path(tmp_path))
    with pytest.raises(RuntimeError):
        launch(make_ctx(tmp_path, client, FakeWatcher()))

    assert load_state(ctx_path(tmp_path))["pending"] == {}
    assert client.params_of("workspace.close") == [{"workspace_id": "w1"}]


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


def test_case_b_never_opens_an_agents_pane_and_says_nothing_when_it_cannot(
        tmp_path, capsys):
    """THE KNOWN GAP, pinned on purpose and deliberately NOT fixed here.

    Attaching to an existing session's space never splits, so a space whose
    agents pane was closed by hand — or that predates the feature — gives the
    operator no pane and no explanation. At the keyboard that is
    indistinguishable from case (A)'s split failing, for the same reason: the
    launch says nothing either way.

    The split needs a tab id and a root pane, and an existing space has neither
    trustworthy (the layout may have changed since the record was written), so
    "add the pane if it is missing" is a design decision, not a line of code —
    see the note in `_attach_there`. Whoever closes the gap changes THIS test,
    which is what makes it a to-do rather than an accident.
    """
    (tmp_path / "og-herdr.json").write_text(
        '{"version": 2, "spaces": {"s1": {"workspace_id": "w7", '
        '"tab_id": "w7:t1", "pane_id": "w7:p1", "owner": "launcher"}}, '
        '"pending": {}}')
    client = FakeClient(workspaces=[{"workspace_id": "w7", "pane_count": 1}])
    ctx = make_ctx(tmp_path, client, FakeWatcher([root_session()]))

    assert launch(ctx) == "attach"
    assert "pane.split" not in client.methods
    assert ctx.problems == [], "this case reports nothing today, by design"
    assert capsys.readouterr().out.count("!") == 0


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
    # The adopted space's first tab is labelled with the SAME string as the
    # space — the session's title — not the agent bundle's name.
    assert client.params_of("tab.rename")[0] == {"tab_id": "w1:t1",
                                                 "label": "the conversation"}

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


def test_the_split_leaves_the_agents_pane_room_for_its_footer(tmp_path):
    """`pane.split`'s `ratio` is the share RETAINED by the pane being split, so
    0.7 leaves the agents pane ~30% — enough for its ~52-column footer and the
    27-column QR; at 0.8 the operator's 215-column terminal clipped the footer.
    Asserted as the EXACT params dict, so a dropped or renamed key — which
    silently restores herdr's 50/50 — fails here.
    """
    client = FakeClient()
    launch(make_ctx(tmp_path, client, FakeWatcher()))

    assert AGENTS_PANE_RATIO == 0.7
    assert client.split_params == {
        "target_pane_id": "w1:p1", "direction": "right",
        "focus": False, "cwd": CWD, "ratio": AGENTS_PANE_RATIO,
    }


# ---------------------------------------------------------------------------
# the pane environment — how a mux launch keeps the browser shut
# ---------------------------------------------------------------------------
# The chat pane runs `og chat` in a FRESH shell, so the only way to reach it is
# herdr's per-create env map. `pane_env_for` builds the map and LIVES IN
# og_herdr now — this module imports it, which is why importing it from
# `og_herdr_launch` below still works and is itself part of what these pin: the
# lift must not change what a launch sends. These assert that it is threaded
# onto every pane a launch creates, that an operator's own $BROWSER is respected,
# and that a launch with no map sends no `env` key at all.

def test_a_mux_launch_passes_browser_true_into_every_pane_it_creates(tmp_path):
    """Asserted as the EXACT env map, so a dropped or renamed `env` key fails
    here instead of as a stray browser tab the operator sees — and the map is
    the whole point, since an export from the launcher never reaches the pane."""
    client = FakeClient()
    launch(make_ctx(tmp_path, client, FakeWatcher(),
                    pane_env={"BROWSER": "true"}))

    assert client.params_of("workspace.create")[0]["env"] == {"BROWSER": "true"}
    assert client.split_params["env"] == {"BROWSER": "true"}


def test_the_adopted_space_also_carries_the_pane_env(tmp_path):
    """(B)/(C) create their space through `_adopt`, a different call site from
    `_open_space` — it must pass the same map, or launching into an existing
    session would still let its pane open a browser."""
    client = FakeClient()
    launch(make_ctx(tmp_path, client, FakeWatcher([root_session()]),
                    pane_env={"BROWSER": "true"}))

    assert client.params_of("workspace.create")[0]["env"] == {"BROWSER": "true"}


def test_an_operator_browser_setting_is_passed_through_not_overridden():
    """Someone whose $BROWSER names a real browser has expressed a preference,
    and og uses it rather than replacing it with `true` — the same rule the
    accounts auto-open guard follows."""
    assert pane_env_for({"BROWSER": "/usr/bin/firefox"}) == {
        "BROWSER": "/usr/bin/firefox"}
    assert pane_env_for({}) == {"BROWSER": "true"}


def test_a_launch_with_no_pane_env_sends_no_browser(tmp_path):
    """The behaviour change is SCOPED: a launch built with no pane environment
    sends no `env` key at all, so nothing that is not a mux suppression is
    touched."""
    client = FakeClient()
    launch(make_ctx(tmp_path, client, FakeWatcher()))  # pane_env defaults {}

    assert "env" not in client.params_of("workspace.create")[0]
    assert "env" not in client.split_params


def test_agents_pane_defaults_on_when_the_install_never_answered():
    """An install predating the key must still get the split it was configured
    for by default; defaulting to False would take it away silently."""
    assert agents_pane_enabled({}) is True
    assert agents_pane_enabled({"herdr_agents_pane": None}) is True
    assert agents_pane_enabled({"herdr_agents_pane": True}) is True
    assert agents_pane_enabled({"herdr_agents_pane": False}) is False


def test_session_space_cap_defaults_to_no_limit_and_reads_the_install_key():
    """0 means no limit, and 0 is the default: every non-archived root is listed.

    Same shape as `agents_pane_enabled`: one key in og-install.json, read through
    one function with a default, so an install that predates the key lists
    EVERYTHING rather than being handed a cut nobody asked for.
    """
    assert DEFAULT_SPACE_CAP == 0
    assert session_space_cap({}) == 0
    assert session_space_cap({"herdr_session_space_cap": 0}) == 0
    assert session_space_cap({"herdr_session_space_cap": 50}) == 50
    # Anything that is not a real, non-negative int falls back to no limit rather
    # than raising: this feeds a background daemon, and a hand-edited number must
    # not kill it. `True` is an int in Python, so it is refused explicitly — a cap
    # of 1 is not what `true` means.
    for bad in (None, "5", 3.5, -1, True, False, [5], {"n": 5}):
        assert session_space_cap({"herdr_session_space_cap": bad}) == 0, bad


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


def test_the_shared_split_returns_a_line_rather_than_reporting_it():
    """`split_agents_pane` is the ONE implementation both the launcher's
    `_split_agents` and the bridge's history/resume space call, so it must be
    free of either caller's reporting channel: it RETURNS the failure lines and
    the caller routes them (the launcher through `report`; the bridge through its
    reconcile lines). A split that reported for itself would be unusable from the
    bridge — and a second copy of it would drift."""
    from og_herdr_launch import split_agents_pane

    lines = split_agents_pane(NoSplitClient(), "w1", "w1:t1", "w1:p1", CWD, {})

    assert len(lines) == 1 and "could not open the og agents pane" in lines[0]
    assert "no room" in lines[0]


def test_the_shared_split_starts_og_agents_when_it_can():
    from og_herdr_launch import split_agents_pane

    client = FakeClient()
    lines = split_agents_pane(client, "w1", "w1:t1", "w1:p1", CWD, {})

    assert lines == []
    assert client.text_typed()[-1] == "og agents"
    # The split pane is found by difference against the tab, since the reply
    # names no pane — the same lookup the launcher has always done.
    assert client.params_of("pane.send_text")[-1]["pane_id"] == "w1:p2"


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
# a degradation the operator can actually find out about
# ---------------------------------------------------------------------------
# THE DEFECT THIS PINS: `og start herdr` ends in `exec herdr`, and herdr's TUI
# takes the terminal into the alternate screen within milliseconds. A warning
# that reached only the terminal was therefore wiped before a human could read
# it, so a missing agents pane produced no cause at all — reported from a second
# machine, where nobody could see what og had said a moment earlier.
#
# Two channels, because they answer different questions:
#   * the log answers "what happened?" afterwards, on any machine, forever;
#   * the hold answers "did something go wrong?" in the moment, before the
#     operator is inside the TUI and has forgotten they saw anything.
# Neither alone closes the gap: a log nobody knows to open is the same void with
# a file in it, and a countdown nobody sees is the same warning as before.


def _launch_log(tmp_path):
    return tmp_path / "logs" / "herdr-launch.log"


def test_a_split_failure_is_written_to_the_launcher_log(tmp_path):
    """The durable channel. Timestamp-prefixed so a log spanning two launches is
    still readable, which is the only time anybody ever reads this file."""
    client = NoSplitClient()
    launch(make_ctx(tmp_path, client, FakeWatcher()))

    lines = _launch_log(tmp_path).read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(time.strftime("%Y-%m-%d")), lines[0]
    assert "could not open the og agents pane" in lines[0]
    assert "no room" in lines[0]


def test_the_launcher_log_is_not_the_file_herdr_writes_its_own_output_to(tmp_path):
    """`log_path` is herdr's stdout and stderr (spawn_herdr_server appends to
    it). An og warning written there reads as the multiplexer's, and an operator
    tailing herdr's log to find out why herdr misbehaved would find og's
    sentence and no answer."""
    ctx = make_ctx(tmp_path, NoSplitClient(), FakeWatcher())
    launch(ctx)

    assert ctx.log_path.name == "herdr-server.log"
    assert launch_log_path(ctx) != ctx.log_path
    assert not ctx.log_path.exists(), "og wrote into herdr's own log"


def test_a_launch_that_worked_writes_no_launch_log(tmp_path):
    """The log means "something went wrong". A file written on every launch is a
    file nobody opens."""
    launch(make_ctx(tmp_path, FakeClient(), FakeWatcher()))
    assert not _launch_log(tmp_path).exists()


def test_a_launch_with_no_log_path_still_reports_without_raising(tmp_path):
    """A caller that passes no `log_path` has no durable channel, which is the
    old behaviour — not a reason for the launch to fail."""
    ctx = make_ctx(tmp_path, NoSplitClient(), FakeWatcher())
    ctx.log_path = None
    assert launch(ctx) == "attach"
    assert ctx.problems and "og agents" in ctx.problems[0]


def test_a_degraded_launch_holds_the_terminal_before_the_tui_takes_it(
        tmp_path, monkeypatch, capsys):
    """Re-surfaced, not just printed once: `_open_space` prints `info("opened
    space ...")` AFTER the split, so by exec time the first copy has scrolled off
    the top. The hold waits — in whole seconds, counted by a bounded loop rather
    than a wall-clock deadline, so it cannot overrun its own budget."""
    monkeypatch.setattr("og_herdr_launch.operator_is_watching", lambda: True)
    monkeypatch.setattr("og_herdr_launch.DEGRADE_HOLD_SECONDS", 3)
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)

    ctx = make_ctx(tmp_path, NoSplitClient(), FakeWatcher())
    launch(ctx)
    capsys.readouterr()          # discard the first copy, as a terminal would scroll it
    _hold_for_problems(ctx)

    out = capsys.readouterr().out
    assert "1 thing about this launch did not work" in out
    assert "could not open the og agents pane" in out
    assert "the chat pane itself is unaffected" in out
    assert slept == [1.0, 1.0, 1.0]


def test_the_hold_is_skipped_when_nobody_is_watching(tmp_path, monkeypatch):
    """`og start herdr > run.log` is a real invocation, and its stdout is a file.
    Sitting there counting down for nobody would be a six-second start-up cost
    added to every degraded launch in a script."""
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("held the terminal"))
    ctx = make_ctx(tmp_path, NoSplitClient(), FakeWatcher())
    launch(ctx)
    _hold_for_problems(ctx)     # operator_is_watching is False under pytest


def test_the_hold_is_skipped_when_nothing_degraded(tmp_path, monkeypatch):
    """The ordinary launch costs exactly what it cost before."""
    monkeypatch.setattr("og_herdr_launch.operator_is_watching", lambda: True)
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("held the terminal"))
    ctx = make_ctx(tmp_path, FakeClient(), FakeWatcher())
    launch(ctx)
    assert ctx.problems == []
    _hold_for_problems(ctx)


def test_the_reason_outlives_the_exec(tmp_path, monkeypatch):
    """THE WHOLE FIX, end to end: the agents pane fails, the chat still comes up,
    the multiplexer is STILL attached, the reason is on the terminal, and it is
    in the log afterwards. The exec is stubbed — which is the point, because the
    real one is what was destroying the message."""
    client = NoSplitClient()
    ctx = make_ctx(tmp_path, client, FakeWatcher())
    monkeypatch.setattr("og_herdr_launch.build_launch", lambda args: ctx)
    monkeypatch.setattr("og_herdr_launch.operator_is_watching", lambda: True)
    monkeypatch.setattr("og_herdr_launch.DEGRADE_HOLD_SECONDS", 2)
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    execed = []
    monkeypatch.setattr("os.execvp", lambda binary, argv: execed.append(binary))

    assert main(["--mux", "herdr"]) == 0

    # The launch did not fail: the chat is up and the TUI is attached anyway.
    assert client.text_typed() == ["og chat"]
    assert len(execed) == 1
    # …and the operator was held long enough to read why, before it was attached.
    assert slept == [1.0, 1.0]
    # …and the same reason is readable afterwards.
    assert "could not open the og agents pane" in _launch_log(tmp_path).read_text()


def test_the_hold_is_skipped_by_once_which_keeps_the_terminal(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    """`--once` returns before the exec, so nothing takes the terminal and the
    text is already on screen — waiting there would be pure delay. This is the
    diagnostic the operator is told to run, so it must stay instant."""
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("held the terminal"))
    ctx = make_ctx(tmp_path, NoSplitClient(), FakeWatcher())
    monkeypatch.setattr("og_herdr_launch.build_launch", lambda args: ctx)
    monkeypatch.setattr("os.execvp", lambda *a: pytest.fail("attached"))
    assert main(["--mux", "herdr", "--once"]) == 0
    assert "could not open the og agents pane" in capsys.readouterr().out


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
# `og chat --dir` — where the session is rooted
# ---------------------------------------------------------------------------
# `--dir` has to answer two questions the operator cannot: is this path a real
# directory (a typo must not become an empty session root), and where does a
# RELATIVE path start. It is a flag rather than a positional because everything
# else in `og chat`'s arguments is forwarded to `omnigent run` verbatim —
# `og chat --resume <id>` is how a history space resumes — so a bare directory
# would collide with the forwarding instead of joining it.

def test_projects_dir_defaults_to_projects_and_expands_tilde():
    """`~` is expanded where it is READ, not where it is written, so a plan
    written by hand and one written by `og setup` mean the same thing."""
    assert DEFAULT_PROJECTS_DIR == "~/projects"
    assert projects_dir({}) == Path.home() / "projects"
    assert projects_dir({"projects_dir": "~/code"}) == Path.home() / "code"


def test_projects_dir_falls_back_rather_than_raising_on_a_hand_edited_value():
    """The `session_space_cap` rule: this is read on the way to exec'ing a REPL,
    so an unusable value must not be the reason `og chat` will not start."""
    for bad in (None, "", "   ", 7, ["~/projects"], {"dir": "~/projects"}):
        assert projects_dir({"projects_dir": bad}) == Path.home() / "projects", bad


def test_an_absolute_dir_is_used_exactly_as_given(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    assert resolve_chat_dir(str(target), {}) == target.resolve()


def test_a_relative_dir_resolves_against_the_configured_projects_root(tmp_path):
    """The default, and the case the flag exists for: `og chat --dir og` from
    $HOME means the og checkout, wherever the operator keeps their work."""
    (tmp_path / "og").mkdir()
    config = {"projects_dir": str(tmp_path)}
    assert resolve_chat_dir("og", config) == (tmp_path / "og").resolve()
    # A configured root, and the default when there is none, are the same rule
    # read twice — asserted through the reader so both spellings stay honest.
    assert projects_dir(config) == tmp_path
    assert projects_dir({}) == Path.home() / "projects"


def test_a_dir_that_does_not_exist_is_refused_not_created(tmp_path):
    """`omnigent run` roots the session at its own cwd, so a directory nobody
    made has to be made by something — and a silently mkdir'd typo is a session
    rooted in an empty directory with no error anywhere."""
    with pytest.raises(LaunchError) as excinfo:
        resolve_chat_dir("nope", {"projects_dir": str(tmp_path)})
    assert "--dir nope" in str(excinfo.value)
    assert not (tmp_path / "nope").exists()

    with pytest.raises(LaunchError):
        resolve_chat_dir(str(tmp_path / "absent"), {})


# ---------------------------------------------------------------------------
# `og chat` inside a herdr pane — the other end of the launcher's handshake
# ---------------------------------------------------------------------------
# THE FLOW THIS PINS: the operator presses "new space", gets a plain shell,
# `cd`s into a repo and types `og chat`. No launcher ran, so nothing wrote the
# pending record the bridge adopts — and a bridge that sees a root session in a
# directory it has no space for opens a SECOND space for the conversation
# already on screen. So `og chat` writes the record itself, for the pane it is
# in, and splits the agents pane the way `_open_space` does.
#
# The marker is `HERDR_ENV=1` — herdr's own flag for a managed pane, measured
# from herdr's bundled agent skill (`herdr --skill`: `test "${HERDR_ENV:-}" = 1`)
# rather than invented here. Outside a herdr pane every one of these must be a
# NO-OP: a plain `og chat` in an ordinary terminal may not touch the state file.

class SplitClient(FakeClient):
    """A herdr that makes the pane `pane.split` promises to make.

    `FakeClient`'s listing is scripted and STATIC, which is the launcher's
    world: a space it created itself, where the one other pane already in the
    listing IS the split. A tab the operator made needs the listing to grow, so
    `before` is what herdr lists until a split happens and `after` is what it
    lists afterwards.
    """

    def __init__(self, before=("w1:p1",), after=("w1:p1", "w1:p2")):
        super().__init__(panes=[{"pane_id": p, "tab_id": "w1:t1",
                                 "workspace_id": "w1"} for p in before])
        self._after = [{"pane_id": p, "tab_id": "w1:t1", "workspace_id": "w1"}
                       for p in after]

    def call(self, method, params=None):
        reply = super().call(method, params)
        if method == "pane.split":
            self._panes = self._after
        return reply


class UnreadableOnceClient(FakeClient):
    """`pane.list` fails once — the pre-split baseline — then answers.

    THE DEFECT THIS REPRODUCES. The baseline read is what stops `og agents`
    from being typed into a pane the operator split themselves. When that read
    FAILS, the "baseline" is not an empty tab, it is an unknown one: keep the
    difference-against-the-tab rule and every pane in the tab looks brand new,
    so the lowest-numbered other pane wins — and on an operator's tab that is
    their pane, taking over their shell. The post-split listing here works
    perfectly, which is exactly how the bug hid: only the FIRST read fails.
    """

    def __init__(self, panes, fail_next_list=True):
        super().__init__(panes=panes)
        self.fail_next_list = fail_next_list

    def call(self, method, params=None):
        if method == "pane.list" and self.fail_next_list:
            self.fail_next_list = False
            self.calls.append((method, params or {}))
            raise RuntimeError("the socket went away")
        return super().call(method, params)


def _pane_env(**extra):
    env = {"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "w1:t1",
           "HERDR_PANE_ID": "w1:p1"}
    env.update(extra)
    return env


def test_record_chat_pane_writes_the_record_the_bridge_claims(tmp_path):
    """The same record `_open_space` writes, through the same writer, keyed by
    the same thing. Asserted with the BRIDGE'S OWN reader and against the shape
    case (A) produces — a record that only this path understands would be a
    record nothing adopts."""
    launch(make_ctx(tmp_path, FakeClient(), FakeWatcher()))
    opened = load_state(ctx_path(tmp_path))["pending"][CWD]

    state = tmp_path / "chat.json"
    written = record_chat_pane(state, CWD, "w1", "w1:t1", "w1:p1")

    assert written == opened
    assert load_state(state)["pending"][CWD] == opened


def test_record_chat_pane_keeps_what_the_bridge_already_recorded(tmp_path):
    """`save_state` is whole-file and last-writer-wins and is NOT a lock, so the
    write re-reads first: a load at the top and a save at the end drops whatever
    the bridge wrote in between — here, every space it has adopted."""
    state = tmp_path / "og-herdr.json"
    save_state(state, {"spaces": {"s1": {"workspace_id": "w0", "cwd": "/elsewhere"}},
                       "pending": {}})

    record_chat_pane(state, CWD, "w1", "w1:t1", "w1:p1")

    kept = load_state(state)
    assert kept["spaces"]["s1"]["workspace_id"] == "w0"
    assert kept["pending"][CWD]["pane_id"] == "w1:p1"


def test_chat_pane_inside_herdr_records_the_pane_and_splits_it(tmp_path,
                                                               monkeypatch):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = SplitClient()

    assert chat_pane(client=client, env=_pane_env()) == 0

    assert load_state(tmp_path / "og-herdr.json")["pending"][os.getcwd()] == {
        "workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1",
        "owner": "launcher", "cwd": os.getcwd()}
    # The split is `split_agents_pane`, the same implementation `_open_space`
    # uses — asserted on its effect, not by re-reading the source.
    assert client.text_typed() == ["og agents"]
    assert client.params_of("pane.send_text")[-1]["pane_id"] == "w1:p2"
    assert client.split_params["target_pane_id"] == "w1:p1"
    assert client.split_params["ratio"] == AGENTS_PANE_RATIO


def test_chat_pane_outside_herdr_touches_nothing(tmp_path, monkeypatch, capsys):
    """A plain `og chat` in an ordinary terminal must behave exactly as it did
    before this existed: no state file, no socket, nothing on stdout."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = FakeClient()

    assert chat_pane(client=client, env={"HOME": str(tmp_path)}) == 0

    assert client.calls == []
    assert not (tmp_path / "og-herdr.json").exists()
    assert capsys.readouterr() == ("", "")


def test_chat_pane_does_not_split_when_the_install_turned_it_off(tmp_path,
                                                                 monkeypatch):
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "og-install.json").write_text('{"herdr_agents_pane": false}')
    client = FakeClient()

    assert chat_pane(client=client, env=_pane_env()) == 0

    # The RECORD is still written: the agents pane is cosmetic, the pending
    # record is what stops a second space appearing.
    assert os.getcwd() in load_state(tmp_path / "og-herdr.json")["pending"]
    assert client.calls == []


def test_chat_pane_never_types_og_agents_into_a_pane_the_operator_already_had(
        tmp_path, monkeypatch, capsys):
    """THE DIFFERENCE FROM `og start`, and it is a real one. A launch makes its
    own space, so the tab holds exactly one pane and "any pane but the chat
    pane" is the split it just made. This tab is the operator's: they may have
    split it before typing `og chat`, and then the lowest-numbered other pane is
    THEIRS — typing `og agents` into it would take over their shell.

    The pane is found by difference against the tab AS IT WAS before the split,
    and when that cannot be told the answer is no agents pane at all rather than
    a guess."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = SplitClient(before=("w1:p1", "w1:p2"),
                         after=("w1:p1", "w1:p2", "w1:p3"))

    assert chat_pane(client=client, env=_pane_env()) == 0

    assert client.params_of("pane.send_text")[-1]["pane_id"] == "w1:p3"
    # The chat is recorded and the space is split either way — only the cosmetic
    # second command is at stake.
    assert os.getcwd() in load_state(tmp_path / "og-herdr.json")["pending"]
    assert "pane.split" in client.methods
    assert capsys.readouterr().err == ""


def test_chat_pane_rather_than_guess_when_the_new_pane_cannot_be_told(tmp_path,
                                                                     monkeypatch,
                                                                     capsys):
    """The other half of the same rule. If herdr split the pane but the listing
    never grows — or grows in a way that cannot be read — there is no pane id to
    be sure of, and the safe answer is no `og agents` anywhere rather than one
    in a pane the operator may be using."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = SplitClient(before=("w1:p1",), after=("w1:p1",))  # never grows

    assert chat_pane(client=client, env=_pane_env()) == 0

    assert client.text_typed() == []
    assert "no new pane id" in capsys.readouterr().err


def test_a_baseline_listing_that_fails_starts_og_agents_nowhere(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    """THE REVIEW DEFECT. `tab_pane_ids` could not read the tab, and it answered
    with an empty set — which `known_panes is not None` then took as herdr's
    verified "this tab had no panes". Every pane in the tab became "new", the
    lowest-numbered other pane won, and on a tab the operator made themselves
    `og agents` was typed into THEIR work.

    So "could not read it" and "it was empty" are different answers now, and
    the first one ends the split: a missing agents pane is cosmetic, a command
    in somebody's pane is not."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = UnreadableOnceClient(
        [{"pane_id": "w1:p1", "tab_id": "w1:t1"},      # the chat
         {"pane_id": "w1:p2", "tab_id": "w1:t1"}])     # the operator's

    assert chat_pane(client=client, env=_pane_env()) == 0

    assert client.text_typed() == []
    assert "pane.split" not in client.methods, (
        "nothing was split, so there is no pane nothing runs in")
    assert "could not read this tab's panes" in capsys.readouterr().err


def test_an_operator_pane_is_never_the_one_og_agents_starts_in(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """The same defect, asserted the only way that matters: whatever the herdr
    answers AFTER the failed baseline read, no pane that already existed can be
    the one a command is typed into."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    theirs = {"pane_id": "w1:p2", "tab_id": "w1:t1"}
    client = UnreadableOnceClient(
        [{"pane_id": "w1:p1", "tab_id": "w1:t1"}, theirs,
         {"pane_id": "w1:p3", "tab_id": "w1:t1"}])

    assert chat_pane(client=client, env=_pane_env()) == 0

    typed_into = {params["pane_id"]
                  for params in client.params_of("pane.send_text")}
    assert typed_into == set() or theirs["pane_id"] not in typed_into
    # And the operator's pane is still just a pane: nothing was sent into it.
    assert "pane.send_text" not in client.methods


def test_an_empty_baseline_is_an_answer_and_still_finds_the_new_pane(tmp_path,
                                                                    monkeypatch):
    """THE BEHAVIOUR THE FIX MUST NOT LOSE. A baseline that genuinely holds no
    panes is herdr answering, and it is the strongest case for the difference
    rule: every pane in the tab after the split is one herdr just made, so the
    agents pane is found and started."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    client = SplitClient(before=(), after=("w1:p1", "w1:p2"))

    assert chat_pane(client=client, env=_pane_env()) == 0

    assert client.text_typed() == ["og agents"]
    assert client.params_of("pane.send_text")[-1]["pane_id"] == "w1:p2"


def test_chat_pane_without_the_pane_ids_says_so_and_records_nothing(tmp_path,
                                                                    monkeypatch,
                                                                    capsys):
    """herdr inside, but no workspace/tab/pane id in the environment. A record
    naming a pane that does not exist would be adopted by the bridge and then
    fail every setup call against — worse than recording nothing, which costs
    the operator one space they can close."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert chat_pane(client=FakeClient(), env={"HERDR_ENV": "1"}) == 0

    assert "was not recorded" in capsys.readouterr().err
    assert not (tmp_path / "og-herdr.json").exists()


def test_a_split_failure_costs_the_agents_pane_only_and_is_logged(tmp_path,
                                                                  monkeypatch):
    """Same trade the launcher makes: the chat IS the session. Logged as well as
    printed, because `og chat` ends in `exec omnigent run` and the REPL takes
    the terminal in the same way herdr's TUI does — a line that reached only the
    terminal is gone before anyone reads it."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert chat_pane(client=NoSplitClient(), env=_pane_env()) == 0

    line = _launch_log(tmp_path).read_text().strip()
    assert "could not open the og agents pane" in line
    assert os.getcwd() in load_state(tmp_path / "og-herdr.json")["pending"]


def test_chat_pane_prints_the_resolved_dir_as_data_and_prose_to_stderr(tmp_path,
                                                                       monkeypatch,
                                                                       capsys):
    """bin/og captures stdout and cds to it, so anything but the path there
    would become a `cd` to a sentence."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "og-install.json").write_text(
        '{"projects_dir": "%s"}' % tmp_path)

    assert chat_pane(client=FakeClient(), env={}, argv=["--dir", "repo"]) == 0

    out = capsys.readouterr().out
    assert out.strip() == str(repo.resolve())


def test_chat_pane_refuses_a_dir_that_does_not_exist_with_nothing_on_stdout(
        tmp_path, monkeypatch, capsys):
    """A refusal, not a degradation: running the session somewhere the operator
    did not ask for is not a degraded version of what they asked for. The exit
    is 2 and the stdout is EMPTY, so bin/og cannot cd to a half-answer."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    (tmp_path / "og-install.json").write_text(
        '{"projects_dir": "%s"}' % tmp_path)

    assert chat_pane(client=FakeClient(), env={},
                     argv=["--dir", "nope"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--dir nope" in captured.err


def test_chat_pane_records_the_dir_it_was_given_not_the_one_it_shell_outed_in(
        tmp_path, monkeypatch):
    """`--dir` and the pane are the same directory by the time the REPL starts
    (bin/og cds first), and the record must be keyed by that: the bridge claims
    it by matching the session's own `workspace` EXACTLY."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    assert chat_pane(client=FakeClient(), env=_pane_env(),
                     argv=["--dir", str(repo)]) == 0

    pending = load_state(tmp_path / "og-herdr.json")["pending"]
    assert list(pending) == [str(repo.resolve())]
    assert os.getcwd() not in pending


def test_main_dispatches_the_chat_subcommand(monkeypatch, tmp_path):
    """`og chat` reaches this as a SUBCOMMAND, so the `og start` parser keeps
    exactly the flags it had — `og start herdr --once` must not grow a
    vocabulary it does not want."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert main(["chat-pane"]) == 0
    assert not (tmp_path / "og-herdr.json").exists()


# ---------------------------------------------------------------------------
# a multiplexer og has no backend for
# ---------------------------------------------------------------------------

def test_an_unimplemented_multiplexer_warns_and_starts_without_it(capsys):
    from og_herdr_launch import main

    assert main(["--mux", "tmux", "--once"]) == 0
    out = capsys.readouterr().out
    assert "no backend" in out
    assert "herdr.dev" not in out  # no false claim that tmux was installed


# ---------------------------------------------------------------------------
# `--dir` at the bash seam: absent is not the same as empty
# ---------------------------------------------------------------------------
# `og chat --dir ""` is a value somebody TYPED. Handing the chat the current
# directory instead is the one thing this flag must never do — a conversation
# rooted where the operator did not ask, with no error anywhere — and that is
# exactly what "treat the empty string as no flag" produced, on both the bash
# side (`[[ -n "$dir" ]]`) and the Python side (`if args.dir:`). Both now test
# for the flag, and the refusal is the same one line a nonexistent path gets.
#
# Driven end to end because the half that decides is bash: bin/og has to hand
# `--dir ""` to the helper rather than deciding on its own that nothing was
# asked for. The REPL stand-in prints the directory it was started in, which is
# what `omnigent run` roots the session at — so "the chat ran in the wrong
# place" is a visible line, not an inference.

OG_BIN = Path(os.environ.get("OG_BIN") or
              Path(__file__).resolve().parent.parent / "bin" / "og")
FAKE_OMNIGENT = """#!/bin/sh
if [ "$1" = server ] && [ "$2" = status ]; then
  echo "running at http://127.0.0.1:6767"; exit 0
fi
echo "RUN-CWD=$(pwd)"
"""


def _og_chat(tmp_path, args=(), cwd=None):
    """Run `og chat <args>` from `cwd` against a throwaway HOME and a fake REPL."""
    home = tmp_path / "home"
    (home / ".omnigent" / "agents" / "dev-lead").mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    (bindir / "omnigent").write_text(FAKE_OMNIGENT)
    (bindir / "omnigent").chmod(0o755)
    work = cwd or (home / "here")
    work.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(OG_BIN), "chat", *args], cwd=str(work),
        capture_output=True, text=True, timeout=30,
        env={"HOME": str(home),
             # The fake first so the real `omnigent` on this machine cannot be
             # reached; python3 has to stay findable, because --dir goes through
             # the helper.
             "PATH": os.pathsep.join([str(bindir), os.environ.get("PATH", "")]),
             "OG_REPO": str(Path(__file__).resolve().parent.parent),
             "TERM": "xterm-256color"})


def test_an_empty_dir_is_refused_rather_than_read_as_no_dir(tmp_path):
    """`--dir ""`: exit non-zero, one line, and NOTHING ELSE HAPPENS — no
    directory invented, no REPL started, no state written."""
    result = _og_chat(tmp_path, ["--dir", ""])

    assert result.returncode == 2
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1, result.stderr
    assert "--dir" in lines[0] and "empty" in lines[0], lines[0]
    assert "RUN-CWD" not in result.stdout, "the chat ran anyway"
    assert list((tmp_path / "home" / "here").iterdir()) == []
    assert not (tmp_path / "home" / ".omnigent" / "og-herdr.json").exists()


def test_the_equals_form_of_an_empty_dir_is_refused_too(tmp_path):
    """`--dir=` is the same value typed the other way, and used to be the same
    silent no-op."""
    result = _og_chat(tmp_path, ["--dir="])

    assert result.returncode == 2
    assert "--dir" in result.stderr and "empty" in result.stderr
    assert "RUN-CWD" not in result.stdout
    assert list((tmp_path / "home" / "here").iterdir()) == []


def test_no_dir_flag_at_all_still_runs_the_chat_where_it_stands(tmp_path):
    """The behaviour the refusal must not have cost. No flag is not an empty
    flag: the chat starts where the operator is, with no cd, no python, no
    state — exactly as before any of this existed."""
    here = tmp_path / "home" / "here"
    result = _og_chat(tmp_path)

    assert result.returncode == 0, result.stderr
    assert f"RUN-CWD={here.resolve()}" in result.stdout
    assert not (tmp_path / "home" / ".omnigent" / "og-herdr.json").exists()


def test_an_empty_dir_is_refused_by_the_helper_on_its_own(tmp_path, monkeypatch,
                                                          capsys):
    """The same refusal from the Python side, for a direct
    `og_herdr_launch.py chat-pane --dir ''`. Two callers, one rule — and here it
    is the only gate, so `Path('')` resolving to the current directory would be
    a silent misreading with nothing behind it."""
    monkeypatch.setenv("OMNIGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert chat_pane(argv=["--dir", ""], client=FakeClient(), env=_pane_env()) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--dir" in captured.err and "empty" in captured.err
    assert not (tmp_path / "og-herdr.json").exists()

    with pytest.raises(LaunchError):
        resolve_chat_dir("", {})
