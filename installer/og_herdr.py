#!/usr/bin/env python3
"""og herdr — project live Omnigent sessions into herdr spaces.

Omnigent runs coding agents and is normally driven from a browser; herdr is a
terminal multiplexer for coding agents. This bridge watches the running Omnigent
server for sessions coming and going and gives each ROOT session its own herdr
workspace — herdr's UI calls these "spaces" — with that conversation's delegated
sub-agents as TABS inside it. So one space shows one conversation's whole crew:

    space  "Omnigent Herdr Integration Feasibility"
      tab  Claude Code                              <- the root's own agent
      tab  coder_zen:space-per-root                 <- a sub-agent
      tab  coder_cmdcode:fix-stale-comments         <- another

Every pane runs `omnigent attach --server <url> <session_id>` — a thin co-drive
client that streams that session's I/O, so the user can work from herdr instead
of the browser. The agent column reads as a TOOL NAME ("Claude Code", from
installer/registry.json) and the title beside it says which worker and which
task, which is what makes the list above readable at a glance.

A sub-agent never gets a space of its own while its parent has one; it goes in
beside its parent. The exception is a sub-agent whose parent has no space at all
— the parent's runner is offline, so the watcher never projected it — and that
worker gets its own space rather than being dropped.

The low-level work lives in two sibling modules, `og_herdr_client` (a herdr
socket client) and `og_herdr_watch` (a session watcher). They are imported
LAZILY, inside the functions that need them, so importing this module never
requires them to exist — which is what lets the tests run with fakes.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

DEFAULT_SERVER = "http://127.0.0.1:6767"
DEFAULT_SOURCE = "og-bridge"

# The agent label reported for a session that names no harness at all — a
# listing row whose detail fetch failed, or a session dict from any other
# source. It is a fallback and not the normal path: the real labels come from
# the registry (see agent_label), and this only has to be non-empty, because
# herdr's `agent` is a required field.
DEFAULT_AGENT_LABEL = "omnigent"

# The prefix of the workspace id a `--dry-run` records for a root. A dry run
# creates nothing, so there is no real id to record — but a sub-agent later in
# the same batch has to be able to say "tab in that space" without mistaking the
# absence of a workspace for "this worker gets a space of its own", which is the
# one thing the dry run must not misreport.
DRY_RUN_SPACE = "dry-run-space:"

# Where the bridge records the spaces it knows about, so that `--cleanup` can
# close exactly those and nothing else, AND so that a space already on screen
# can be adopted rather than duplicated.
#
#     {"version": 2,
#      "spaces":  {"<session_id>": {"workspace_id": …, "tab_id": …,
#                                   "pane_id": …, "owner": …, "cwd": …}},
#      "pending": {"<cwd>":         {"workspace_id": …, "tab_id": …,
#                                   "pane_id": …, "owner": "launcher", …}}}
#
# v2 is keyed by SESSION ID where v1 was keyed by workspace id, because the two
# callers want opposite lookups. `--cleanup` asks "is this space ours?" and is
# happy with either key. Adoption asks "does this session already have a space?",
# and only the session-keyed half answers it — v1 could not tell a session the
# launcher had already put on screen from one it had never seen, which is
# precisely the question that decides between adopting a space and opening a
# second one for the same conversation.
#
# Every id here is the ID herdr minted, never the label: the labels are the
# operators' session titles verbatim (that display is the point), so a prefix or
# a substring match would either wreck the label or miss the match — and
# `--cleanup` closing a workspace it did not open is unrecoverable in a
# terminal multiplexer.
#
# Beside og-quota.json in the same $OMNIGENT_HOME, and named for the same
# reason: state that has to outlive the process but is not worth a directory.
STATE_FILENAME = "og-herdr.json"
STATE_VERSION = 2

# The version this module used to write, `{"version": 1,
# "workspaces": {workspace_id: session_id}}`. It is UPGRADED on read, never
# discarded: a v1 file can describe spaces that are live on the operator's
# screen right now, and throwing it away orphans them from `--cleanup` — which
# leaves the operator closing spaces by hand, one per conversation.
STATE_VERSION_V1 = 1

# Who opened the space. Recorded, not enforced: the two sides differ in where a
# session's pane comes from (the bridge types `omnigent attach` into a pane it
# created; the launcher types `og chat` into one it created) but agree that a
# space either of them opened is cleaned up the same way, so nothing branches on
# this yet. It is here so the file can answer the question later without a
# format change.
OWNER_BRIDGE = "bridge"
OWNER_LAUNCHER = "launcher"


def attach_command(server: str, session_id: str) -> str:
    """The exact command a session's pane is given, built from parts.

    `--server` is not optional decoration, it is the whole command. Measured in
    a real pane, `omnigent attach <id>` answers

        Error: No server to attach to. `attach` joins a LIVE session on a
        running server — start one with `omnigent run`…

    because `omnigent attach --help` says the server "defaults to the configured
    server, or a local server already running in the background", and neither of
    those defaults resolves from a plain shell in a herdr pane: there is no
    configured server there and the discovery is a foreground operation. Naming
    the server explicitly changes the answer to a *different* error — "has no
    online runner on <url>" — which proves the server was found and leaves only
    the runner question, which the watcher now answers before projecting
    (og_herdr_watch.runner_is_offline). So every pane that was opened without it
    was a pane that could only ever show "No server to attach to".

    Each argument is quoted separately and the parts joined with spaces, rather
    than one `shlex.quote` over the finished string: the quoting is what keeps a
    server URL containing a space, a semicolon or a `$(…)` from breaking the
    line — this string is TYPED INTO A SHELL, so a URL is operator input, not a
    trusted constant — while the common case (a plain URL and a hex session id)
    still comes out unquoted and readable in the pane and in the dry-run line.
    """
    return " ".join(shlex.quote(part) for part in
                    ("omnigent", "attach", "--server", server, session_id))


# ---------------------------------------------------------------------------
# lazy seams to the sibling modules
#
# Nothing here imports og_herdr_client / og_herdr_watch at module load. Tests
# replace these two functions with fakes and never need the real modules.
# ---------------------------------------------------------------------------

def _state(session: dict) -> str:
    """herdr's state (idle|working|blocked|unknown) for an Omnigent session.

    WHY the bridge stops at "blocked" and never answers: Omnigent broadcasts an
    elicitation to every connected client but parks a SINGLE Future for the
    answer, so the first resolver wins and every other client gets `not_found`.
    Auto-answering an elicitation here would race the user's own browser or
    phone for that one Future — so the bridge only REPORTS the blocked state and
    leaves the decision to a real client.
    """
    from og_herdr_watch import herdr_state

    return herdr_state(session)


def _error_cls():
    """The sibling client's HerdrError, resolved lazily so this module imports
    without it. Used as the `except` type in reconcile."""
    from og_herdr_client import HerdrError

    return HerdrError


def _error_line(sid: str, exc) -> str:
    """One `reconcile`-style error line for a failed herdr call."""
    code = getattr(exc, "code", "herdr-error")
    message = getattr(exc, "message", str(exc))
    return f"error {sid}: {code}: {message}"


def _id_of(entry, key: str) -> str | None:
    """The id `key` out of a create result's `workspace` / `tab` / `root_pane`
    sub-object.

    herdr 0.9.3 names these `workspace_id`, `tab_id` and `pane_id`; there is no
    generic `id` key, so checking one would only ever match a shape the server
    never sends.

    The caller STATES which id it wants rather than this function trying keys in
    a fixed order, because the live `root_pane` object carries BOTH keys — so an
    order-sensitive lookup is only accidentally correct, and reversing it would
    hand the TAB id to `pane_run` and type the attach command at a tab handle.
    The real shape, quoted from a live server:

        "root_pane":{"pane_id":"w1:pQ","terminal_id":"...",
                     "workspace_id":"w1","tab_id":"w1:tG",...}
    """
    if isinstance(entry, dict):
        return entry.get(key) or None
    return None


def _parent_of(source) -> str | None:
    """The parent session id carried by a session object, or None for a root.

    `parent_session_id` decides whether a session is a delegated worker, and it
    is the one signal that is present on EVERY event shape: it is on the LISTING
    row, so it rides `added`, `changed`, and the `previous` object of a
    `removed` (whose `session` is `{}` — the session is gone). The `kind` field
    the detail row also carries would settle the same question, but it only
    exists on the `added` path by construction, so it cannot classify a removal.
    """
    if isinstance(source, dict):
        parent = source.get("parent_session_id")
        if isinstance(parent, str) and parent:
            return parent
    return None


# ---------------------------------------------------------------------------
# agent labels: herdr's agent column should read like a tool name
# ---------------------------------------------------------------------------
#
# The operator's list of a conversation's agents is "Claude Code",
# "zen: space-per-session", "cmdcode: fix-stale-comments" — a TOOL name and a
# task. herdr's agent column is where the tool name goes, and the session title
# goes to report_metadata beside it, so the two together give that list. The
# tool names are not free-form text: they live in installer/registry.json,
# already the one catalog of "harness id -> product name" in this project, so a
# second spelling of "Claude Code" here would be a second thing to keep in step.

# Path(__file__).parent, NOT Path(__file__).resolve().parent: `__file__` has been
# absolute since 3.9 and this module needs 3.10, so resolution buys nothing, and
# the `resolve` in it collides with the AST guard that forbids this module from
# CALLING anything whose name looks like an elicitation resolver (see
# test_source_never_calls_an_elicitation_resolver). Both spellings put the file
# next to this one; only one of them passes that guard, and the guard is worth
# keeping blunt.
REGISTRY_PATH = Path(__file__).parent / "registry.json"

# Filled on first use, then kept: the catalog is a file in the repo and does not
# change while the bridge runs.
_REGISTRY_LABELS: dict[str, str] | None = None


def harness_labels() -> dict[str, str]:
    """`{harness id: product label}` from installer/registry.json, cached.

    Never raises and never returns a half-read table. A missing, unreadable or
    malformed catalog yields `{}`, which makes every harness fall back to its
    own id — a column of `claude-native` instead of `Claude Code`. That is a
    cosmetic loss; failing a pane over it would be not cosmetic at all, and the
    bridge cannot tell a broken catalog from an unfamiliar harness, so it must
    not try to distinguish them.
    """
    global _REGISTRY_LABELS
    if _REGISTRY_LABELS is not None:
        return _REGISTRY_LABELS
    labels: dict[str, str] = {}
    try:
        catalog = json.loads(REGISTRY_PATH.read_text())
    except (OSError, ValueError):
        catalog = None
    rows = catalog.get("agents") if isinstance(catalog, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        harness, label = row.get("harness"), row.get("label")
        if isinstance(harness, str) and harness and \
                isinstance(label, str) and label:
            labels[harness] = label
    _REGISTRY_LABELS = labels
    return labels


def agent_label(session: dict) -> str:
    """The name herdr shows in a pane's agent column for this session.

    The session's `harness` — which arrives on the event from the DETAIL row,
    see og_herdr_watch.DETAIL_FIELDS — mapped through the registry to its
    product name. A harness the registry does not list falls back to the raw id,
    which is ugly but honest: it names exactly what ran. A session with no
    harness at all falls back further, to DEFAULT_AGENT_LABEL.

    Note this reads `harness`, NOT the listing row's `agent_name`. Those are
    two sources and they disagree — a sub-agent's listing row reads the ROOT's
    agent name, so every worker in a conversation would be labelled with the
    orchestrator, which is wrong for all of them at once.
    """
    if not isinstance(session, dict):
        return DEFAULT_AGENT_LABEL
    harness = session.get("harness")
    if not isinstance(harness, str) or not harness:
        return DEFAULT_AGENT_LABEL
    return harness_labels().get(harness, harness)


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------

class Bridge:
    """Reconciles Omnigent session events into herdr workspaces and tabs.

    State is the in-memory map session_id -> {workspace_id, tab_id, pane_id,
    title, agent, parent, owns_space, step, ready}. It lives for the process
    lifetime: the bridge projects the sessions that are live while it runs.

    The map is the whole of the layout logic. A sub-agent's space is simply its
    parent's `workspace_id`, looked up by `parent_session_id` — so a root needs
    no second index to be findable, and a grandchild resolves through its own
    parent exactly the same way.
    """

    def __init__(self, watcher, client, dry_run=False, source=DEFAULT_SOURCE,
                 cwd=None, server=DEFAULT_SERVER, state_path=None):
        self.watcher = watcher
        self.client = client
        self.dry_run = dry_run
        self.source = source
        # The directory new panes open in when the session does not name one of
        # its own (see `_added`). It comes from THIS process, so with no
        # explicit --cwd it is the bridge's own cwd.
        self.cwd = os.getcwd() if cwd is None else cwd
        # The Omnigent server those panes attach to. Held here rather than read
        # from the watcher's base_url because the pane runs the CLI in a
        # separate shell with no idea the watcher exists, and because the user
        # said which server with `--server` — the same string the watcher is
        # pointed at, and the one worth naming in the command.
        self.server = server
        # Which of herdr's workspaces THIS bridge opened, or the launcher did,
        # so `--cleanup` can close exactly those and nothing else. Read at
        # construction rather than at import so a test can point $OMNIGENT_HOME
        # somewhere disposable.
        self.state_path = state_path or default_state_path()
        # Whatever `load_state` wants to say about the file it read. A list, not
        # a string, because there can be no note at all (a clean file) or exactly
        # one (missing, stale, unparseable) and the caller prints whichever.
        self._state_note: list[str] = []
        state = load_state(self.state_path, self._state_note)
        # `{session_id: record}` — a space this session already has, so it is
        # adopted rather than duplicated — and `{cwd: record}` — a space the
        # launcher opened before any session existed, waiting for the session
        # that appears in that directory to claim it.
        self._spaces: dict[str, dict] = state["spaces"]
        self._pending: dict[str, dict] = state["pending"]
        # `{workspace_id: info}` from workspace.list, for one reconcile pass.
        # None on `_ws_index` means "not asked yet"; `_ws_ok` says whether the
        # fetch SUCCEEDED, because an empty listing and an unreadable one are
        # different answers — see `_workspace_index`.
        self._ws_index: dict | None = None
        self._ws_ok: bool = False
        # Whatever the last workspace.list fetch failed with, reported once per
        # pass so a listing that cannot be read is visible rather than silent.
        self._ws_error: str | None = None
        # Lines the adopt/claim path produced that are not about any one event —
        # a pruned pending record, a stale record dropped — so they are
        # reported even though no event asked for them. Reset per pass, and
        # prepended to that pass's lines so they read first.
        self._pass_notes: list[str] = []
        self._recs: dict[str, dict] = {}

    # -- public surface -----------------------------------------------------

    @property
    def _owned(self) -> dict[str, str]:
        """`{workspace_id: session_id}` — the v1 view of what is ours, derived.

        `--cleanup` wants the workspace-keyed half, and `_forget` wants to ask
        "is this workspace still recorded?". Deriving it from `spaces` rather
        than keeping a second copy is what stops the two from disagreeing: the
        v1 bug shape was a map that was updated in one place and read in
        another.
        """
        return {rec["workspace_id"]: sid
                for sid, rec in self._spaces.items() if rec.get("workspace_id")}

    def reconcile(self, events) -> list:
        """Apply a batch of session events, returning human-readable action
        lines. One bad pane is recorded and skipped, never fatal: the bridge
        must survive a single failing herdr call and keep projecting the rest.
        """
        # The workspace listing is a per-PASS snapshot, not a per-call one:
        # adoption, claiming and pending-pruning all ask the same question of
        # the same list, and a batch of ten new sessions must see one consistent
        # answer rather than ten racing listings.
        self._ws_index = None
        self._ws_ok = False
        self._ws_error = None
        self._pass_notes = []
        lines: list[str] = list(self._prune_pending())
        for event in self._roots_first(events):
            try:
                lines.extend(self._apply(event))
            except _error_cls() as exc:
                lines.append(_error_line(event.session_id, exc))
        if self._ws_error:
            lines.append(self._ws_error)
            self._ws_error = None
        # Notes raised mid-batch go FIRST, ahead of the actions they explain.
        return self._pass_notes + lines

    @staticmethod
    def _roots_first(events) -> list:
        """The batch reordered roots-before-sub-agents, everything else stable.

        THE ORDERING TRAP. The session listing is NEWEST-FIRST, and a sub-agent
        is newer than the root that spawned it — so the very first poll, and any
        poll where a new worker appears, emits the child BEFORE its parent. A
        batch handled in arrival order therefore reaches the child first, finds
        no space for its parent (the root has not been created yet), and either
        drops it or files it under a space of its own — leaving the operator's
        conversation scattered across two spaces, which is the exact layout this
        bridge exists to remove.

        Roots first makes the parent's space exist before any child looks for
        it. `sorted` is stable, so within each group the listing's own order
        survives — which keeps the batch's log lines in the order the operator
        would expect, and keeps every existing ordering guarantee that does not
        depend on parent/child.

        It fixes the trap WITHIN a batch, which is where it lives. Across two
        batches a child that arrives before its parent still falls back to its
        own space (see `_added`); nothing can conjure a space that has not been
        requested yet, and the alternative — holding the worker back until its
        parent appears — would strand it indefinitely, since a parent whose
        runner is offline is never projected at all.
        """
        return sorted(events, key=lambda ev: 1 if _parent_of(
            getattr(ev, "session", None) or getattr(ev, "previous", None)) else 0)

    def run_once(self) -> list:
        """Poll the watcher once and reconcile whatever it returned."""
        return self.reconcile(self.watcher.poll_once())

    def run_forever(self) -> None:
        """Consume watcher.watch() indefinitely, reconciling each event."""
        for event in self.watcher.watch():
            for line in self.reconcile([event]):
                print(line, flush=True)

    def cleanup(self) -> list:
        """Close every workspace this bridge recorded, report, and return.

        There is no single scratch workspace to close any more — each root got
        its own — so without this a test run leaves the operator closing spaces
        by hand, one per conversation, in a multiplexer where a mis-click loses
        their work.

        Both halves of the state file are closed, and that is the point of the
        v2 shape rather than an extension of it. A `pending` record is a space
        the LAUNCHER opened — for a directory, before any session existed — and
        it is still a space on the operator's screen that something opened on
        their behalf. Leaving it behind means `og start herdr` has no way back:
        the space stays, the next run opens another, and the operator closes
        them one at a time by hand. Whether the launcher or the bridge opened it
        is recorded (`owner`) and deliberately does not change the decision —
        today both are cleaned up alike, which is the safe direction while the
        `owner` field exists to make a narrower mode possible later.

        Only ids in the state file are touched. The operator's own workspaces are
        not in it and cannot be: herdr mints the id, this file records what the
        id was in answer to a create, and nothing else is ever written there.
        A workspace closed here is unrecoverable, so the rule is not "looks like
        ours" but "we recorded it".

        `not_found` counts as done — somebody closed it by hand, which is the
        outcome this was after. Any OTHER error keeps the record, so a later
        `--cleanup` retries it; dropping it would turn one transient failure into
        a space nobody ever closes.
        """
        lines: list[str] = list(self._state_note)
        targets = self._recorded_workspaces()
        if not targets:
            lines.append(f"no recorded workspaces in {self.state_path}; "
                         "nothing to close")
        before = {} if self.dry_run else self._space_labels(lines)

        closed: set[str] = set()
        for ws_id, sid in sorted(targets.items()):
            name = before.get(ws_id)
            shown = f" '{name}'" if name else ""
            who = f"session {sid}" if sid else "no session yet"
            if self.dry_run:
                lines.append(f"dry-run cleanup: close workspace {ws_id}{shown} "
                             f"({who})")
                closed.add(ws_id)
                continue
            try:
                self.client.workspace_close(ws_id)
            except _error_cls() as exc:
                if getattr(exc, "code", None) == "not_found":
                    lines.append(f"cleanup: workspace {ws_id}{shown} ({who}) "
                                 f"was already gone")
                    closed.add(ws_id)
                    continue
                lines.append(_error_line(sid or ws_id, exc))
                continue
            closed.add(ws_id)
            lines.append(f"cleanup: closed workspace {ws_id}{shown} ({who})")

        if self.dry_run:
            # Returned BEFORE the ownership record is touched: a dry run that
            # pruned the file would make the NEXT real --cleanup close nothing,
            # and the operator would be left with spaces nobody tracks.
            lines.append(f"dry-run cleanup: {len(closed)} workspace(s) would be "
                         "closed; nothing was")
            return lines

        if closed:
            for ws_id in closed:
                self._forget_workspace(ws_id)

        # Report what is left, so "cleanup finished" is checkable: the operator
        # can see their own workspace survived rather than take it on faith.
        after = self._space_labels(lines)
        for ws_id, label in sorted(after.items()):
            if ws_id in targets:
                continue
            lines.append(f"cleanup: left workspace '{ws_id}' "
                         f"({label!r}) — not this bridge's")
        return lines

    def _recorded_workspaces(self) -> dict[str, str | None]:
        """`{workspace_id: session_id or None}` — every closeable space, once.

        `spaces` and `pending` merged, deduplicated on the workspace id and
        sorted for a stable report. The dedupe is belt-and-braces: a claim MOVES
        a record, so the same workspace is never legitimately named twice — but
        two closes of one workspace is a `not_found` and a confusing line, and
        the merge is the one place that could produce it.

        The value is None for a pending record, which has no session yet — that
        is the whole difference between the two halves, and it is said in the
        line rather than guessed at.
        """
        targets: dict[str, str | None] = {}
        for sid, rec in self._spaces.items():
            ws_id = rec.get("workspace_id")
            if ws_id:
                targets.setdefault(ws_id, sid)
        for rec in self._pending.values():
            ws_id = rec.get("workspace_id")
            if ws_id:
                targets.setdefault(ws_id, None)
        return dict(sorted(targets.items()))

    def _space_labels(self, lines: list[str]) -> dict:
        """`{workspace_id: label}` from workspace.list, or {} if unreadable.

        A failure here is reported and swallowed: it degrades the report from
        "closed X, left the projects workspace" to "closed X" — the closes
        themselves are still correct, and a listing that cannot be read is not a
        reason to skip the cleanup.
        """
        try:
            spaces = self.client.workspace_list()
        except _error_cls() as exc:
            lines.append("error: cannot list workspaces: {0}: {1}".format(
                getattr(exc, "code", "herdr-error"), getattr(exc, "message", exc)))
            return {}
        labels = {}
        for ws in spaces if isinstance(spaces, list) else []:
            if not isinstance(ws, dict):
                continue
            ws_id = ws.get("workspace_id")
            if isinstance(ws_id, str) and ws_id:
                labels[ws_id] = ws.get("label") or ""
        return labels

    # -- the state file: which workspaces are ours ---------------------------

    def _remember(self, sid: str, ws_id: str, tab_id=None, pane_id=None,
                  cwd=None) -> None:
        """Record a space as this session's, so `--cleanup` can close it later.

        `owner` is `bridge` unconditionally: this is only reached from a create
        the bridge itself just made. A space the launcher opened keeps
        `owner: "launcher"` all the way through a claim (see `_claim_pending`),
        because the owner records who opened it, not who is looking at it now.
        """
        self._spaces[sid] = {"workspace_id": ws_id, "owner": OWNER_BRIDGE,
                             **({"tab_id": tab_id} if tab_id else {}),
                             **({"pane_id": pane_id} if pane_id else {}),
                             **({"cwd": cwd} if cwd else {})}
        self._save_state()

    def _save_state(self) -> None:
        save_state(self.state_path, {"spaces": self._spaces,
                                    "pending": self._pending})

    # -- event handlers -----------------------------------------------------

    def _apply(self, event) -> list:
        kind = event.kind
        sid = event.session_id
        if kind == "added":
            return self._added(sid, event.session)
        if kind == "changed":
            return self._changed(sid, event.session, event.previous or {})
        if kind == "removed":
            return self._removed(sid)
        return []

    def _added(self, sid: str, session: dict) -> list:
        command = attach_command(self.server, sid)
        rec = self._recs.get(sid)
        if rec is not None:
            # One pane per session: a redelivered `added` either finds setup
            # already complete (no-op) or a previous attempt that died part-way
            # — in which case the pane already exists and the remaining steps
            # resume on it instead of a second create opening a duplicate tab (or
            # a duplicate workspace) for the one session.
            if rec["ready"]:
                return []
            state = _state(session)
            self._setup_pane(sid, rec, state)
            return [f"resume add {sid} → pane {rec['pane_id']} "
                    f"'{command}' [{state}]"]

        title = session.get("title")
        # The pane's directory is the SESSION's when the watcher supplied one,
        # and this process's otherwise.
        #
        # The watcher fetches `GET /v1/sessions/{id}` once per newly seen
        # session and merges the result in: the LISTING row has no `workspace`
        # (measured against a live server — its keys are agent_id, agent_name,
        # archived, comments_count, created_at, external_session_id, id,
        # labels, owner, parent_session_id, pending_elicitations_count,
        # permission_level, runner_id, status, title, updated_at, viewer_unread),
        # while the detail row does. A sub-agent's own detail is None, so the
        # watcher resolves it to its PARENT's directory, which is an
        # approximation and the best the API offers — a delegated worker really
        # runs in its own git worktree, which is reported nowhere.
        #
        # `--cwd` stays the fallback for every case where no directory is known:
        # a detail fetch that failed, a parent whose own directory is unknown,
        # or a session dict from any other source. `.get()` rather than `[""]`
        # so an absent key falls through to the same place a `None` would.
        cwd = session.get("workspace") or self.cwd
        agent = agent_label(session)
        state = _state(session)
        parent = _parent_of(session)

        # (2)/(3) A space may already be on screen for this session without the
        # bridge having opened it in THIS process — because the launcher made it
        # and ran `og chat` in it, or because the bridge restarted and its
        # in-memory map went with the process. Both come out of the state file,
        # which is why the file is keyed by session id as well as by workspace.
        #
        # Roots only. A sub-agent never owns a space (`_space_of` below), and a
        # worker that appeared in the launcher's directory before its root would
        # otherwise claim the launcher's whole space for itself and leave the
        # root to open a second one — reintroducing through the back door the
        # very duplicate this ordering exists to prevent.
        space = None
        if parent is None:
            taken = self._existing_space(sid, session, title, cwd)
            if taken is not None:
                if self.dry_run:
                    self._recs[sid] = {
                        "workspace_id": taken["workspace_id"],
                        "tab_id": taken["record"].get("tab_id"),
                        "pane_id": taken["record"].get("pane_id"),
                        "title": title, "agent": agent, "parent": parent,
                        "owns_space": False, "step": 3, "ready": True,
                    }
                    return [f"dry-run: {taken['action']} '{title}' → space "
                            f"{taken['workspace_id']} ({cwd}) — unverified, "
                            f"read from the state file without asking herdr"]
                # A record naming a pane we can confirm is finished with: the
                # space, its tab and its pane all exist and something is already
                # running in it. Re-running setup would type the attach command
                # a second time into a live pane — for a launcher's space that
                # is a second co-drive client on one session, which is the race
                # `_state` exists to avoid. So: adopt it and touch nothing.
                if taken["pane_ok"]:
                    return self._adopt(sid, taken["record"], taken["action"],
                                       title, agent, state, command)
                # No pane to resume onto (an upgraded v1 record carries no pane
                # id). The space is still this session's, so it becomes a TAB in
                # that space rather than a second space beside it.
                space = taken["workspace_id"]
        if space is None:
            space = self._space_of(parent)

        if self.dry_run:
            # A dry run creates nothing, so a root's space id does not exist
            # either. A placeholder is still recorded, and it has to be
            # CONSISTENT with what a real run would mint — that is what lets a
            # sub-agent later in the same batch print "tab in the root's space"
            # instead of describing a layout the real run would not produce.
            self._recs[sid] = {
                "workspace_id": space or f"{DRY_RUN_SPACE}{sid}",
                "tab_id": None, "pane_id": None, "title": title,
                "agent": agent, "parent": parent, "owns_space": False,
                "step": 3, "ready": True,
            }
            where = f"tab in space {space}" if space else "space"
            return [f"dry-run: add '{title}' → {where} ({cwd}) → "
                    f"{command} [{state}]"]

        if space is None:
            return self._add_space(sid, title, cwd, agent, state, command,
                                   parent)
        return self._add_tab(sid, title, space, cwd, agent, state, command,
                             parent)

    # -- adopting a space that is already on screen --------------------------
    #
    # The order below is the whole contract, and it runs before anything is
    # created:
    #
    #   1. in `self._recs`                — this process is already on it
    #   2. in `spaces[session_id]`        — a space is already this session's
    #   3. a `pending` record for this cwd — the launcher's handshake
    #   4. nothing                        — create
    #
    # 2 before 3 because a claimed record is ALSO in `spaces` afterwards, and
    # 1 before both because a half-finished setup in this process outranks
    # anything on disk: the in-memory record knows which pane to resume on and
    # the file does not.

    def _existing_space(self, sid: str, session: dict, title,
                        cwd: str) -> dict | None:
        """Take over a space this session already has, or None to create one.

        Returns `{workspace_id, action, pane_ok, record}` — an envelope, and an
        envelope rather than extra keys stamped onto the record itself because
        the record is what `save_state` persists. Internal bookkeeping written
        into it would end up in the operator's file, where the next reader would
        have to know which keys are real.

        `action` is "adopt" or "claim", for the log line. `pane_ok` says whether
        the pane the record names can be confirmed live, which decides between
        "already done" and "there is nothing to adopt onto".

        EVERY branch requires the workspace to appear in `workspace.list`. A
        record naming a workspace that is not there is DROPPED rather than
        taken, because adopting it would hand the bridge a dead pane id and
        every later `report_agent` would fail against it — the one failure mode
        worse than a duplicate space, because a duplicate space the operator
        can close and a dead pane they never find out about.
        """
        record = self._spaces.get(sid)
        action = "adopt"
        if record is None:
            record = self._claim_pending(sid, cwd)
            action = "claim"
            if record is None:
                return None
        ws_id = record["workspace_id"]
        # Three states, and collapsing any two of them is a bug:
        #   index is a dict and ws_id is in it    — the space is there
        #   index is a dict and ws_id is not in it — herdr says it is GONE
        #   index is None                        — we could not find out
        # A dry run cannot find out either (zero client calls is its whole
        # contract), so its index is `{}` and the `dry_run` guard is what lets it
        # adopt on the file's word and SAY the line is unverified. A real run
        # that cannot find out adopts nothing: a wrong answer here means a dead
        # pane id, which is the one outcome worse than a duplicate space.
        index = self._workspace_index()
        info = index.get(ws_id) if index is not None else None
        if index is not None and info is None and not self.dry_run:
            self._drop_space_record(sid, ws_id)
            self._pass_notes.append(
                f"state: dropped the {action} record for {sid} — workspace "
                f"{ws_id} is no longer listed, so the record is stale and a "
                f"fresh space is created instead")
            return None

        # `cwd` is INFORMATIONAL in the file: the session's own `workspace` from
        # the API is authoritative, so this refreshes the cached copy and not
        # the other way round. Nothing above rests on the cached value — the
        # match in `_claim_pending` was made against the live session, not the
        # file's copy of it.
        if cwd:
            record["cwd"] = cwd
        # A workspace reporting no panes cannot hold the pane this record names,
        # so `pane_ok` is False and the caller takes the `_add_tab` route
        # instead. A proxy, not proof — only herdr knows — and it errs towards
        # doing something rather than declaring the record unusable, because an
        # upgraded v1 record never carries a pane id at all and because `info`
        # is None whenever the listing could not be read, or the run is a dry
        # run, which is the same "cannot confirm" the caller already absorbed.
        pane_ok = bool(record.get("pane_id")) and \
            bool((info or {}).get("pane_count"))
        if info is not None:
            self._rename_space(ws_id, title, info)
        # NOT in a dry run, and that is the whole reason this save is guarded
        # rather than left to `_save_state`: a dry run that claimed and wrote
        # would consume the pending record and leave the NEXT REAL RUN with
        # nothing to adopt — the same defect that makes a dry-run `--cleanup`
        # dangerous, arriving through the adopt path instead of the cleanup one.
        if not self.dry_run:
            self._save_state()
        return {"workspace_id": ws_id, "action": action, "pane_ok": pane_ok,
                "record": record}

    def _adopt(self, sid: str, record: dict, action: str, title, agent: str,
               state: str, command: str) -> list:
        """Rebuild the in-memory record from the state file and stop there.

        Reached only when the record's pane has been CONFIRMED live, which is
        what `_existing_space` gated on — so the record is marked ready without
        a single setup call. That is not a shortcut: re-running setup would type
        the attach command a second time into a pane that already has one, and
        for a launcher's space that is a second co-drive client on one session,
        which is the race `_state` exists to avoid.

        `owns_space` follows the recorded OWNER, not the fact that this process
        is taking over. A launcher-made space is the launcher's: when its
        session is removed the bridge closes that TAB, not the whole workspace,
        which is the narrowest correct blast radius (closing a space takes every
        tab in it, and there may be an `og agents` split beside the chat).
        """
        ws_id = record["workspace_id"]
        tab_id = record.get("tab_id")
        pane_id = record.get("pane_id")
        self._recs[sid] = {
            "workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
            "title": title, "agent": agent, "parent": None,
            "owns_space": record.get("owner") == OWNER_BRIDGE,
            "step": 3, "ready": True}
        return [f"{action} {sid} → space {ws_id} tab {tab_id} pane "
                f"{pane_id} '{title}' {command} [{state}] "
                f"(pane already live; setup not re-run)"]

    def _claim_pending(self, sid: str, cwd: str) -> dict | None:
        """Move a `pending[cwd]` record into `spaces[sid]`, or return None.

        This is the launcher handshake, and it is the only option that survives
        `exec herdr`: the launcher opens a space and types `og chat` into it
        BEFORE any session exists, so it has nothing to key a record by. It
        writes the pending record and exits. The session that later appears in
        that directory is — the launcher having run `og chat` there, and that
        being the only thing in that directory — the session this space belongs
        to.

        Matched on the directory EXACTLY. A prefix or substring match would
        hand one session a sibling checkout's space, and that is the one error
        here no close or rename undoes.

        `owner` stays whatever it was. The launcher opened this space, so it is
        still the launcher's; claiming it transfers nothing, and a record that
        claimed otherwise would misreport who to blame for a stray space.
        """
        record = self._pending.get(cwd) if cwd else None
        if record is None:
            return None
        self._pending.pop(cwd, None)
        self._spaces[sid] = record
        return record

    def _rename_space(self, ws_id: str, title, info: dict) -> None:
        """Rename a taken-over space to the session title, when it differs.

        The launcher's whole reason for labelling a space from the DIRECTORY is
        that no session existed when it made one — there was no title to use.
        Once the conversation has a name, the space is renamed to it, and that
        rename IS the handoff. Skipped when the label already matches (the
        common case for a bridge-created space, so a `workspace.rename` on
        every adoption would be noise) and when there is no title yet.

        Only ever called for a space being taken over, never for one this
        process created — a bridge-created space is labelled with the title at
        creation and never needs it back.
        """
        if not title or info.get("label") == title:
            return
        # `call` and not a named wrapper: `workspace.rename` has no wrapper on
        # the client module, and the frame is pinned against herdr's vendored
        # schema by a test in this file (see
        # test_the_workspace_rename_frame_matches_herdrs_schema), so using the
        # generic call does not quietly opt out of conformance.
        self.client.call("workspace.rename", {"workspace_id": ws_id,
                                              "label": title})

    def _workspace_index(self) -> dict | None:
        """`{workspace_id: info}` from workspace.list, once per reconcile pass.

        Returns None when the listing could not be read — which is NOT the same
        as `{}`, which means herdr was asked and answered that it has no
        workspaces at all. That distinction is the difference between "this
        space is gone" and "we do not know", and only herdr can tell them
        apart. Callers treat None as UNCONFIRMED: nothing is adopted, nothing is
        claimed, and nothing is pruned.

        That is the conservative direction, and deliberately so. Treating
        unknown as absent would turn one transient herdr hiccup into a
        duplicate space for every live session. A duplicate is visible and
        closable; a session that silently lost its space is a conversation the
        operator cannot see at all.

        A `--dry-run` never calls herdr, so it gets `{}` — it can neither
        confirm nor deny anything, and the lines it prints say so.
        """
        if self._ws_index is not None:
            return self._ws_index if self._ws_ok else None
        if self.dry_run or self.client is None:
            self._ws_index, self._ws_ok = {}, True
            return self._ws_index
        try:
            spaces = self.client.workspace_list()
        except _error_cls() as exc:
            self._ws_index, self._ws_ok = {}, False
            self._ws_error = "error: cannot list workspaces: {0}: {1}".format(
                getattr(exc, "code", "herdr-error"),
                getattr(exc, "message", exc))
            return None
        index = {}
        for ws in spaces if isinstance(spaces, list) else []:
            if not isinstance(ws, dict):
                continue
            ws_id = ws.get("workspace_id")
            if isinstance(ws_id, str) and ws_id:
                index[ws_id] = ws
        self._ws_index, self._ws_ok = index, True
        return index

    def _drop_space_record(self, sid: str, ws_id: str) -> None:
        """Forget a `spaces` record naming a workspace herdr no longer lists.

        Guarded on the workspace id matching: `_spaces` is keyed by session, and
        popping on the session alone would drop a good record because a
        DIFFERENT stale one had the same key. It cannot have, in practice, but
        the check costs one comparison and names the invariant.
        """
        record = self._spaces.get(sid)
        if record is not None and record.get("workspace_id") == ws_id:
            self._spaces.pop(sid, None)
            self._save_state()

    def _prune_pending(self) -> list:
        """Drop `pending` entries whose workspace is not in workspace.list.

        Existence is the WHOLE rule — no TTL, no clock, no age. A pending record
        names a space, and if the space is gone the record means nothing: the
        operator closed it, or the launch failed after the space was made. A
        time-based rule would need a clock and would still be wrong about the
        only case that matters — a space closed by hand must stop being
        claimable IMMEDIATELY, because claiming it would hand a pane to a
        workspace that no longer exists.

        So the rule is deterministic (same file plus same listing, same answer),
        which is what makes it testable at all, and it needs no field in the
        file that only a clock could advance.

        Pruned at the TOP of every pass, before the batch runs, so a stale
        record cannot claim a session during the same pass that discovers it is
        stale. An unreadable listing prunes nothing (see `_workspace_index`),
        which is the only reason this is safe to run unconditionally.
        """
        if not self._pending or self.dry_run:
            return []
        index = self._workspace_index()
        if index is None:
            # We could not ask, so we cannot claim to know of a single dead
            # space. Pruning on a failed listing would delete every pending
            # record in the file — which is the same as losing every launcher
            # space the operator has open.
            return []
        stale = [cwd for cwd, rec in self._pending.items()
                 if rec.get("workspace_id") not in index]
        if not stale:
            return []
        for cwd in stale:
            self._pending.pop(cwd, None)
        self._save_state()
        return [f"state: pruned {len(stale)} pending launcher record(s) whose "
                f"workspace no longer exists: {', '.join(sorted(stale))}"]

    def _space_of(self, parent: str | None) -> str | None:
        """The herdr workspace a sub-agent's tab belongs in, or None.

        None means the parent has no space in this bridge's records. That is
        reachable three ways — the parent was never seen, its create failed, or
        it was refused for an offline runner and so was never projected at all
        (`runner_is_offline`) — and NONE of them is a reason to drop the worker.
        A delegated agent with no pane is an agent the operator cannot watch
        work, and "its root is missing" is exactly the situation where they most
        need to see it. So the caller gives the child its OWN space instead, and
        says so; when the root later shows up the child keeps the space it got,
        because re-parenting it would mean closing a pane someone may be reading.
        """
        if not parent:
            return None
        rec = self._recs.get(parent)
        return rec.get("workspace_id") if rec else None

    def _add_space(self, sid: str, title, cwd: str, agent: str, state: str,
                   command: str, parent: str | None) -> list:
        """Open a workspace for a session, and record it as ours.

        One `workspace.create` brings the space AND its first tab together —
        the live reply carries `workspace`, `tab` and `root_pane` — so the
        root's own pane costs no second call.

        `focus` is False and must stay False. Several sessions routinely appear
        in a single poll, and each of them asking for focus would rip the
        operator out of whatever they were typing, once per session, forever.
        The bridge reports; it does not take over the screen.
        """
        result = self.client.workspace_create(
            label=title or sid[:8], cwd=cwd, focus=False)
        ws_id = _id_of(result.get("workspace"), "workspace_id")
        tab_id = _id_of(result.get("tab"), "tab_id")
        pane_id = _id_of(result.get("root_pane"), "pane_id")
        if ws_id is None or tab_id is None or pane_id is None:
            return self._reject_partial_create(sid, ws_id, tab_id, pane_id)
        rec = {"workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
               "title": title, "agent": agent, "parent": parent,
               "owns_space": True, "step": 0, "ready": False}
        self._recs[sid] = rec
        self._remember(sid, ws_id, tab_id=tab_id, pane_id=pane_id, cwd=cwd)
        self._setup_pane(sid, rec, state)
        return [f"add {sid} → space {ws_id} tab {tab_id} pane {pane_id} "
                f"'{title}' '{command}' [{state}]"]

    def _add_tab(self, sid: str, title, space: str, cwd: str, agent: str,
                 state: str, command: str, parent: str | None) -> list:
        """Put a sub-agent's pane in a TAB inside its parent's space.

        This is the whole point of the layout: the operator clicks one space and
        sees that conversation's own agent plus every worker it delegated. A
        sub-agent with a space of its own would only be correct when its parent
        has none, and then it is `_add_space`'s job, not this one's.
        """
        result = self.client.tab_create(space, cwd=cwd,
                                        label=title or sid[:8], focus=False)
        tab_id = _id_of(result.get("tab"), "tab_id")
        pane_id = _id_of(result.get("root_pane"), "pane_id")
        if tab_id is None or pane_id is None:
            return self._reject_partial_create(sid, None, tab_id, pane_id)
        rec = {"workspace_id": space, "tab_id": tab_id, "pane_id": pane_id,
               "title": title, "agent": agent, "parent": parent,
               "owns_space": False, "step": 0, "ready": False}
        self._recs[sid] = rec
        # A tab created INSIDE an adopted space: the space was already recorded
        # against this session, and it now names a pane of its own, so the
        # record is brought up to date rather than left pointing at nothing.
        # Skipped for a plain sub-agent, whose parent owns the space and whose
        # own tab is deliberately NOT recorded — a tab is not a workspace, and
        # `--cleanup` closes workspaces.
        known = self._spaces.get(sid)
        if known is not None and known.get("workspace_id") == space:
            known["tab_id"] = tab_id
            known["pane_id"] = pane_id
            self._save_state()
        self._setup_pane(sid, rec, state)
        return [f"add {sid} → space {space} tab {tab_id} pane {pane_id} "
                f"'{title}' '{command}' [{state}]"]

    def _setup_pane(self, sid: str, rec: dict, state: str) -> None:
        """Run the post-create setup steps for a session's pane.

        `rec["step"]` counts the steps that have already succeeded, so a failure
        mid-setup leaves the record pointing at the failed step. A redelivered
        `added` then resumes from there on the SAME pane rather than creating a
        second tab or workspace, and a step that already succeeded is not
        repeated — re-running pane_run would submit the attach command into the
        pane again.

        Only the CREATION call differs between a root and a sub-agent; these
        three steps are identical for both, and that is deliberate — they are
        what makes the agent column read "Claude Code" and the title beside it
        read which worker and which task, whichever kind of session this is.
        """
        pane_id = rec["pane_id"]
        if rec["step"] <= 0:
            self.client.pane_run(pane_id, attach_command(self.server, sid))
            rec["step"] = 1
        if rec["step"] <= 1:
            # The agent label is the session's harness product name ("Claude
            # Code"), stored on the record rather than re-derived per step, so
            # the release on removal matches exactly what was reported. herdr
            # matches the two by that string; a release naming something else
            # would leave the marker up.
            self.client.report_agent(pane_id, self.source, agent=rec["agent"],
                                     state=state)
            rec["step"] = 2
        if rec["step"] <= 2:
            self.client.report_metadata(pane_id, self.source, title=rec["title"])
            rec["step"] = 3
            rec["ready"] = True

    def _reject_partial_create(self, sid: str, ws_id, tab_id, pane_id) -> list:
        """Reject a create reply that carried no usable ids.

        Stores nothing: a `pane_id` of None in the mapping would make every
        later `changed` report against a pane that does not exist. A partial
        create may still have left something in the user's herdr, so the halves
        are cleaned up best-effort, from the OUTERMOST thing we can still name
        inward — closing the workspace takes its tab and pane with it, so there
        is no point also closing those:
          - a workspace id: close the orphan workspace, and with it its tab and
            pane (this is the `workspace.create` case);
          - a tab id but no workspace id: close the orphan tab;
          - a pane id but no tab id: there is no tab handle to close, so close
            the orphan pane directly.
        A failure of any cleanup is recorded, not raised, so the rest of the
        batch still runs.
        """
        if ws_id is not None:
            detail = "no tab or pane id"
        elif tab_id is None and pane_id is None:
            detail = "no workspace, tab or pane id"
        elif tab_id is None:
            detail = "no tab id"
        else:
            detail = "no pane id"
        lines = [f"error {sid}: bad_response: create reply had {detail}"]
        closer = None
        if ws_id is not None:
            closer = ("workspace_close", (ws_id,))
        elif tab_id is not None:
            closer = ("tab_close", (tab_id,))
        elif pane_id is not None:
            closer = ("pane_close", (pane_id,))
        if closer is not None:
            name, args = closer
            try:
                getattr(self.client, name)(*args)
            except _error_cls() as exc:
                lines.append(_error_line(sid, exc))
        return lines

    def _changed(self, sid: str, session: dict, previous: dict) -> list:
        rec = self._recs.get(sid)
        if rec is None:
            # A session we never opened: nothing to update, ignore it safely.
            return []
        state = _state(session)
        title = session.get("title")
        title_changed = previous.get("title") != title
        if self.dry_run:
            rec["title"] = title
            extra = " title" if title_changed else ""
            return [f"dry-run: update {sid} → state={state}{extra}"]
        # The stored label, not one re-derived from this event: a `changed`
        # carries the LISTING row, which has no `harness` to derive from, and
        # reporting a different agent string than the one we claimed would make
        # herdr show two markers for one pane.
        self.client.report_agent(rec["pane_id"], self.source,
                                 agent=rec["agent"], state=state)
        if title_changed:
            self.client.report_metadata(rec["pane_id"], self.source, title=title)
        rec["title"] = title
        return [f"update {sid} → state={state}" + (" (title)" if title_changed
                                                   else "")]

    def _drop_children(self, sid: str, lines: list[str]) -> None:
        """Forget the records of sessions whose pane just went with the space.

        A root's `workspace.close` takes every tab in that space down with it,
        including its workers'. Their records have to go in the same breath, or
        their later `removed` would answer `not_found` against a tab that was
        closed some time ago — turning one action into a burst of errors that
        each looks like a fresh failure.
        """
        for child_id, child in list(self._recs.items()):
            if child.get("parent") == sid:
                self._recs.pop(child_id, None)
                lines.append(f"remove {child_id} (pane {child.get('pane_id')}; "
                             f"tab closed with its parent's space)")

    def _removed(self, sid: str) -> list:
        rec = self._recs.get(sid)
        if rec is None:
            return []
        owns_space = rec["owns_space"]
        # A record adopted from the state file may name a pane without naming
        # the tab holding it — a v1 record carries neither, and a record written
        # by hand can name one and not the other. Closing the SPACE is then the
        # only thing left that can be closed by id, and it is the wider blast
        # radius, so it is a fallback rather than the default.
        if not owns_space and not rec["tab_id"]:
            owns_space = True
        what = "space" if owns_space else "tab"
        target = rec["workspace_id"] if owns_space else rec["tab_id"]
        if self.dry_run:
            self._recs.pop(sid, None)
            return [f"dry-run: remove {sid} (release pane, close {what})"]
        pane_id = rec["pane_id"]
        # release_agent and the close are handled SEPARATELY because a
        # `not_found` from the two means different things. From release_agent it
        # means only that no agent marker was registered for the pane — which
        # happens when setup failed part-way (see `step`), with the pane still
        # very much open. It does NOT prove the pane is gone, so it is
        # "nothing to release" and we PROCEED to close. Only a `not_found` from
        # the close proves the thing itself is gone: closing a tab's only pane
        # removes the tab, so a later tab.close answers tab_not_found, and a
        # closed workspace answers the same way. Folding the two into one try
        # also let a release_agent `not_found` skip the close entirely and then
        # drop the mapping —
        # orphaning an open tab that nothing tracked any more.
        #
        # The same agent string that was reported has to be released: herdr
        # matches a claim by (source, agent), so a release naming anything else
        # would leave our marker sitting on a pane we have already closed.
        try:
            self.client.release_agent(pane_id, self.source, agent=rec["agent"])
        except _error_cls() as exc:
            if getattr(exc, "code", None) != "not_found":
                # Any other error keeps the mapping: popping it here would leave
                # the pane and tab untracked with no way to retry, leaking them
                # for good. The error is recorded by `reconcile` and a later
                # `removed` retries this cleanup.
                raise
        closer = "workspace_close" if owns_space else "tab_close"
        try:
            getattr(self.client, closer)(target)
        except _error_cls() as exc:
            if getattr(exc, "code", None) == "not_found":
                # The thing is already gone: the outcome we wanted has happened,
                # so drop the mapping instead of retrying a removal that can
                # never succeed.
                self._recs.pop(sid, None)
                lines = [f"remove {sid} (pane {pane_id}, {what} {target}; "
                         f"already gone)"]
                self._forget(owns_space, target)
                return lines
            # As above: any other error keeps the mapping for a later retry.
            raise
        self._recs.pop(sid, None)
        lines = [f"remove {sid} (pane {pane_id}, {what} {target})"]
        # A closed workspace took its tabs with it, so its workers' panes are
        # already gone and their records have to go with them; a closed tab
        # affects nothing else.
        if owns_space:
            self._forget(True, target)
            self._drop_children(sid, lines)
        elif rec["workspace_id"]:
            # A claimed launcher's space dies with its last tab (measured:
            # closing a workspace's last pane removes the workspace). The
            # record has to go with it or the next session in that directory
            # would claim a space that is not there — which the stale-adoption
            # guard would catch anyway, but a poll later and with a line of
            # noise attached.
            #
            # Scoped to the record held against THIS session, and that is not
            # decoration: a sub-agent's record names its PARENT's workspace, and
            # a worker's removal must not forget the root's space. Only a
            # session whose own `spaces` entry names that workspace — a root,
            # including one that claimed the launcher's — is the owner of it.
            if self._spaces.get(sid, {}).get("workspace_id") == \
                    rec["workspace_id"]:
                self._forget_workspace(rec["workspace_id"])
        return lines

    def _forget(self, owns_space: bool, ws_id: str) -> None:
        """Stop recording a workspace as ours, once it is certainly closed.

        Pruning the state file here is what keeps a later `--cleanup` from
        answering `not_found` for a workspace this bridge closed itself, which
        would be true but would read as a fault.

        Takes the workspace id rather than deriving it, because the caller has
        already decided what `target` was and re-deciding it here would be a
        second opinion about the same close.
        """
        if owns_space:
            self._forget_workspace(ws_id)

    def _forget_workspace(self, ws_id: str) -> None:
        """Drop every record naming `ws_id`, from `spaces` and from `pending`.

        Both halves, because a workspace is only ever named by one of them at a
        time — claiming moves the record rather than copying it — and the two
        are cleared together so a cleanup cannot leave half of a claim behind.
        """
        dropped = False
        for sid, rec in list(self._spaces.items()):
            if rec.get("workspace_id") == ws_id:
                self._spaces.pop(sid, None)
                dropped = True
        for cwd, rec in list(self._pending.items()):
            if rec.get("workspace_id") == ws_id:
                self._pending.pop(cwd, None)
                dropped = True
        if dropped:
            self._save_state()


# ---------------------------------------------------------------------------
# which workspaces are ours: $OMNIGENT_HOME/og-herdr.json
# ---------------------------------------------------------------------------

def default_state_path() -> Path:
    """Where the ownership state lives, read from the environment NOW.

    Deliberately not a module-level constant: the value depends on
    $OMNIGENT_HOME, and a test (or an operator running against a sandbox home)
    must be able to redirect it by setting the variable, not by editing a
    constant that was frozen when the module happened to be imported. Same
    resolution as every other module here: $OMNIGENT_HOME, else ~/.omnigent.
    """
    return Path(os.environ.get("OMNIGENT_HOME") or Path.home() / ".omnigent") \
        / STATE_FILENAME


def load_state(path: Path, note: list[str] | None = None) -> dict:
    """`{"spaces": {...}, "pending": {...}}` at the CURRENT version; empty
    dicts when there is nothing usable.

    A missing file is the NORMAL case — nothing has been created yet, or
    everything already was — so it is not an error. Neither is a file from a
    version this module does not know how to convert, or one that cannot be
    parsed: all three read as "we recorded nothing", which makes `--cleanup` a
    no-op and says so.

    That direction is the safe one on purpose. The alternative — treating an
    unreadable file as "everything we ever had" — cannot even be written, since
    there is nothing to read; and treating it as "everything herdr has" is
    exactly the mistake that would close the operator's own workspaces. Nothing
    outside this file is ever closed, so an empty answer can only under-report.

    A v1 file is the exception, and it is UPGRADED IN PLACE. Its
    `{workspace_id: session_id}` map is exactly what v2's `spaces` half wants,
    just keyed the other way round, so the conversion is a transpose rather
    than a guess. The rewrite happens HERE, at read time, rather than at the
    first write, for the reason it matters: a run that only ever reads the file
    (`--cleanup`) is still the run that migrates it, so the next process to look
    finds one shape and one version.

    A v1 record carries no tab or pane id, and none is invented: the session
    still has its space, which is what `--cleanup` needs, but there is no pane
    to resume setup onto and none is guessed at. The upgrade is best-effort in
    exactly the way `save_state` is — a file that cannot be rewritten is left
    alone, since the read already succeeded and the caller's work does not
    depend on the migration landing.
    """
    def say(line: str) -> None:
        if note is not None:
            note.append(line)

    try:
        raw = path.read_text()
    except FileNotFoundError:
        say(f"no state file at {path} (nothing this bridge created is "
            f"recorded, so nothing is ours to close)")
        return {"spaces": {}, "pending": {}}
    except OSError as exc:
        say(f"cannot read {path}: {exc} — treating it as no recorded workspaces")
        return {"spaces": {}, "pending": {}}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        say(f"{path} is not valid JSON ({exc}) — treating it as no recorded "
            f"workspaces; close any leftovers by hand")
        return {"spaces": {}, "pending": {}}
    if not isinstance(data, dict):
        say(f"{path} is not a v{STATE_VERSION} state file — treating it as no "
            f"recorded workspaces")
        return {"spaces": {}, "pending": {}}

    version = data.get("version")
    if version == STATE_VERSION:
        return {"spaces": _read_spaces(data.get("spaces")),
                "pending": _read_spaces(data.get("pending"))}
    if version == STATE_VERSION_V1:
        upgraded = _upgrade_v1(data.get("workspaces"), path, say)
        save_state(path, upgraded)
        return upgraded
    say(f"{path} is not a v{STATE_VERSION} state file — treating it as no "
        f"recorded workspaces")
    return {"spaces": {}, "pending": {}}


def _read_spaces(raw) -> dict:
    """The well-formed records out of one `spaces` / `pending` map.

    Only records carrying a non-empty string `workspace_id` survive. Every other
    field is carried through only when it is a string, so a half-written entry
    cannot smuggle a non-string where a caller expects an id — and a missing
    `tab_id` / `pane_id` stays MISSING rather than becoming a guess, because
    they are the difference between "this pane" and "some pane".
    """
    if not isinstance(raw, dict):
        return {}
    kept = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not key or not isinstance(entry, dict):
            continue
        ws_id = entry.get("workspace_id")
        if not isinstance(ws_id, str) or not ws_id:
            continue
        rec = {"workspace_id": ws_id}
        for field in ("tab_id", "pane_id", "owner", "cwd"):
            value = entry.get(field)
            if isinstance(value, str) and value:
                rec[field] = value
        kept[key] = rec
    return kept


def _upgrade_v1(raw, path: Path, say) -> dict:
    """`{workspace_id: session_id}` → `{"spaces": {session_id: record}}`.

    Said out loud because it rewrites a file the operator may be looking at: the
    `workspaces` key is GONE, so a stale v1 reader would find nothing and close
    nothing. That is the safe direction to fail, but it is still a change to a
    file on disk and a silent one would be the wrong kind of clever.
    """
    pairs = raw if isinstance(raw, dict) else {}
    spaces = {}
    for ws_id, sid in pairs.items():
        if not isinstance(ws_id, str) or not ws_id or \
                not isinstance(sid, str) or not sid:
            continue
        # No tab_id, no pane_id, no cwd: v1 recorded only the pair, and the
        # upgrade records only the pair. Inventing the rest would be a guess
        # dressed as a fact, and a guessed pane id is what a stale adoption
        # hands to every later report_agent.
        spaces[sid] = {"workspace_id": ws_id, "owner": OWNER_BRIDGE}
    count = len(spaces)
    say(f"{path} is a v{STATE_VERSION_V1} state file — upgraded "
        f"{count} record(s) to v{STATE_VERSION} in place (the `workspaces` key "
        f"is now `spaces`, keyed by session id); no tab or pane id was recorded "
        f"and none was invented")
    return {"spaces": spaces, "pending": {}}


def save_state(path: Path, state: dict) -> None:
    """Write the ownership state, atomically, and never raise at the caller.

    A failed write is swallowed: the worst case is a workspace that `--cleanup`
    does not know about, which the operator can close by hand, and the
    alternative — refusing to project a session because a local file could not
    be written — trades a cosmetic bookkeeping failure for a missing pane.

    The file is NOT a lock and does not pretend to be one: the launcher and the
    bridge both write it, whole and last-writer-wins. That is enough for this
    content, where every writer re-reads before it writes and the only thing
    lost in a race is a record whose workspace is closed on the next poll
    anyway (see the pending-pruning rule in `Bridge._prune_pending`).
    """
    payload = {"version": STATE_VERSION,
               "spaces": state.get("spaces") or {},
               "pending": state.get("pending") or {}}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic, for the same reason og_quota's is: a crash mid-write must not
        # leave a file the next run misreads as corrupt and silently discards —
        # which here would mean silently orphaning every space it owned.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".og-herdr")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="og herdr", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true",
                    help="poll the server once, reconcile, and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the actions without calling herdr at all")
    ap.add_argument("--cleanup", action="store_true",
                    help="close every workspace this bridge created, report "
                         "what it closed, and exit (only workspaces recorded "
                         "in the state file are touched)")
    ap.add_argument("--server", default=DEFAULT_SERVER, metavar="URL",
                    help=f"Omnigent server URL (default: {DEFAULT_SERVER})")
    ap.add_argument("--socket", default=None, metavar="PATH",
                    help="herdr socket path (default: the client's own)")
    ap.add_argument("--cwd", default=os.getcwd(), metavar="PATH",
                    help="directory new panes open in (default: the bridge's "
                         "current working directory)")
    ap.add_argument("--source", default=DEFAULT_SOURCE, metavar="NAME",
                    help=f"reporting source name (default: {DEFAULT_SOURCE})")
    return ap


def _load_token(server_url: str) -> str | None:
    """Best-effort bearer token for the server, read from Omnigent's own token
    store exactly as bin/og does. Missing file or entry reads as no token, which
    the watcher handles like any other unauthenticated request."""
    path = Path(os.environ.get("OMNIGENT_HOME", Path.home() / ".omnigent")) \
        / "auth_tokens.json"
    try:
        store = json.loads(path.read_text())
    except Exception:
        return None
    rec = store.get(server_url) if isinstance(store, dict) else None
    if isinstance(rec, dict):
        token = rec.get("token")
        return token if isinstance(token, str) else None
    return None


def _run_cleanup(args) -> int:
    """`og herdr --cleanup`: close what we opened, say what that was, exit.

    No watcher, and no poll: cleanup is about herdr, and constructing a
    SessionWatcher here would mean an HTTP round trip whose only possible effect
    is to start projecting sessions we are in the middle of closing.
    """
    client = None
    if not args.dry_run:
        from og_herdr_client import HerdrClient

        client = HerdrClient(socket_path=args.socket)
    bridge = Bridge(None, client, dry_run=args.dry_run, source=args.source,
                    cwd=args.cwd, server=args.server)
    for line in bridge.cleanup():
        print(line)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cleanup:
        return _run_cleanup(args)

    from og_herdr_watch import SessionWatcher

    watcher = SessionWatcher(base_url=args.server, token=_load_token(args.server))
    client = None
    if not args.dry_run:
        # Constructed only for a real run: --dry-run must make zero herdr calls,
        # and building the client is the one step that could touch the socket.
        from og_herdr_client import HerdrClient

        client = HerdrClient(socket_path=args.socket)

    bridge = Bridge(watcher, client,
                    dry_run=args.dry_run, source=args.source, cwd=args.cwd,
                    # The same URL the watcher polls, and the one the pane's
                    # `omnigent attach --server` has to name: the pane cannot
                    # discover it (see attach_command).
                    server=args.server)

    if args.once:
        for line in bridge.run_once():
            print(line)
        return 0
    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        return 0
    return 0

    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
