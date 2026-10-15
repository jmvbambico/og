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

An ACP session's pane runs `omnigent attach --server <url> <session_id>` — a thin
co-drive client that streams that session's I/O, so the user can work from herdr
instead of the browser. A session's OWN tab is a per-agent tab: a harness hosted
as a real terminal TUI (`kind: native`) has a vendor CLI of its own and that tab
ATTACHES to it interactively, so a blocking prompt the CLI is holding can be
answered from here (see `_pane_command`). The one exception is a ROOT's tab,
which is the conversation's MAIN PANE and keeps the Omnigent view; a root's
vendor CLI gets a tab of its own, opened on demand when the operator selects that
row in `og agents` (see `og_herdr_launch.focus_agent_terminal`). The agent column
reads as a TOOL NAME ("Claude Code", from installer/registry.json) and the title
beside it says which worker and which task, which is what makes the list above
readable at a glance.

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
import contextlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only; see `state_lock`
    fcntl = None

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
#                                   "pane_id": …, "owner": …, "cwd": …,
#                                   "cli_pane_id": … (optional)}},
#      "pending": {"<cwd>":         {"workspace_id": …, "tab_id": …,
#                                   "pane_id": …, "owner": "launcher", …}}}
#
# `cli_pane_id` is the one OPTIONAL extra: the id of the read-only pane that
# mirrors the native harness TUI (see og_herdr_launch). Absent on every record
# written before that feature, which is exactly what "no such pane is open" means.
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

# The OPTIONAL space-record key naming an agent's read-only CLI-mirror pane.
# PRESENT means the pane is open and holds its id; ABSENT means none is. Named
# HERE rather than in og_herdr_launch because `_read_spaces` below is the reader
# that must CARRY it through a load: a field that whitelist does not name is
# silently dropped, and then the launcher's own toggle would read back "closed"
# after opening, and open a SECOND pane on the next keypress — a real defect a
# draft of this feature shipped with. Additive by design: a record written before
# the feature has no key and reads as closed, so no version bump, no migration.
CLI_PANE_FIELD = "cli_pane_id"

# Who opened the space. Recorded, not enforced: the sides differ in where a
# session's pane comes from (the bridge types `omnigent attach` into a pane it
# created for a LIVE session; the launcher types `og chat` into one it created;
# the bridge types the resume line, unsubmitted, into one it created for a
# HISTORY session) but agree that a space any of them opened is cleaned up the
# same way.
#
# `history` is the one value that is READ rather than merely recorded: it marks
# a space opened for a past conversation (see `_add_history_space`), and those
# are the spaces the configurable cap counts (`_history_space_count`). It lives
# in this field rather than a new one because `owner` already round-trips
# through the state file (`_read_spaces`), and a new key would have had to be
# added to the on-disk format for no other reason.
OWNER_BRIDGE = "bridge"
OWNER_LAUNCHER = "launcher"
OWNER_HISTORY = "history"

# The owners that mean "the BRIDGE created this workspace", so a removal closes
# the whole space rather than one tab of it. The launcher's spaces are excluded
# on purpose: closing those is the launcher's business, and for a launcher space
# the bridge closes only the tab it adopted (see `_adopt`).
BRIDGE_OWNED = frozenset({OWNER_BRIDGE, OWNER_HISTORY})

# How many past-conversation ("history") spaces the bridge keeps on screen.
# ZERO MEANS NO LIMIT, and zero is the default: the point of the feature is to
# see EVERY non-archived root the way the web sidebar does, and the operator
# prunes by hand rather than being handed an arbitrary cut. A POSITIVE value
# caps how many such spaces are open at once, and the cap trims the OLDEST —
# the listing is newest-first, so the recent conversations are the ones kept.
# Only history spaces are subject to it: a LIVE session always gets its space,
# whatever this is set to. Set through the install's `herdr_session_space_cap`
# key; see `main` and og_herdr_launch.session_space_cap.
DEFAULT_SPACE_CAP = 0


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


def resume_command(session_id: str) -> str:
    """The line a HISTORY space's pane is given — typed but NOT submitted.

    A live session's pane is handed `attach_command` and submitted, because a
    runner is up and there is a conversation to join. A past conversation has no
    runner, and starting one is the one thing this must not do: a space for a
    dead session must not resurrect it. So its pane gets this line left in the
    shell UNSUBMITTED — the operator presses Enter to resume, and until they do
    nothing runs (see `pane_send_text`).

    `og chat --resume <id>` and not a bare `omnigent run`, because `og chat` is
    what brings the local server and the agent bundle with it, exactly as it does
    for a fresh session in a launcher pane. `og chat` forwards its arguments to
    `omnigent run` verbatim (`bin/og`: `exec omnigent run "$AGENT_DIR" --server
    ... "$@"`), and `omnigent run -r/--resume <conversation id>` is documented
    to attach to a prior conversation — so this is a supported invocation and
    not a hopeful one.

    It names no `--server`, unlike `attach_command`, and that is deliberate:
    `og chat` has no such flag — it always drives the LOCAL og server on
    `$PORT`. That is right for a bridge on the same machine and is the honest
    limit of the line; a bridge pointed at a remote `--server` cannot resume
    through `og chat`, and the pane says so rather than attaching elsewhere.

    Quoted per part for the same reason `attach_command` is: the string is
    TYPED INTO A SHELL, so a session id is operator input and must not be able
    to break the line or run anything.
    """
    return " ".join(shlex.quote(part) for part in
                    ("og", "chat", "--resume", session_id))


# ---------------------------------------------------------------------------
# the pane's environment: how a pane og opens keeps the browser shut
# ---------------------------------------------------------------------------
#
# ONE statement of the rule, shared by the two things that open panes. The
# launcher had it first and got it right; the bridge never learned it, and a
# `og start herdr` left panes on the operator's screen that would open a browser
# the moment something in them ran. So the rule lives HERE, next to the pane
# commands it is about, and `og_herdr_launch` imports it — one direction still,
# as for `space_status`/`workspace_index`. A second copy is how this came back.

def pane_env_for(environ: dict) -> dict:
    """The environment every pane og opens is started with.

    A mux launch asked for a TERMINAL interface, so the panes must not steal
    focus with a browser. A pane runs an omnigent command that can open the live
    conversation — `og chat` (-> `omnigent run`) opens it by default
    (cli.py: `auto_open_conversation` defaults to `prompt is None`), and
    `og chat --resume <id>` in a history space does the same the moment the
    operator presses Enter. `omnigent run` has no --no-open and no env var for
    it, but `webbrowser` — what it calls — honours `$BROWSER`, and pointing that
    at `true(1)` makes `webbrowser.open()` run `/usr/bin/true <url>`: it succeeds
    and opens nothing. Measured, not assumed: with `BROWSER=true`,
    `webbrowser._tryorder` comes back `['true', 'MacOSX', 'chrome', 'firefox',
    'safari']` and `open()` returns True, so `true` is tried first and the
    platform openers never run. So the suppression is this one variable.

    IT HAS TO TRAVEL TO THE PANE, not to the process that asks for the pane: a
    pane runs its command in a fresh shell, so an export in the launcher or the
    bridge reaches neither. herdr takes an env map on the CREATING calls and
    installs it in that pane's shell — `workspace.create`, `tab.create`,
    `pane.split`, and nothing else (see herdr's own request schema:
    `PaneSendTextParams` is `pane_id` + `text` and has no `env`). That is why
    every create in both modules threads this map and nothing is done about
    `pane_run`/`pane_send_text`: a line typed into an existing pane runs in the
    shell herdr started when the pane was created, so it inherits what that
    create installed. Creation is the only place the value can go.

    An operator who already set `$BROWSER` has expressed a preference — a real
    browser — so it is passed through untouched; only an unset `$BROWSER` is
    defaulted. Same rule the accounts auto-open guard in bin/og follows.

    The bridge is a DAEMON, so `environ` here is whatever environment it was
    started in — not the operator's shell as it is now. Measured, not assumed:
    bin/og starts it `nohup env PYTHONUNBUFFERED=1 python3 og_herdr.py ...`, and
    `env` without `-i` passes the caller's environment through with one variable
    added, so an exported `$BROWSER` does reach the bridge — the same answer the
    launcher gets, because both are children of the same `og start`. The one
    honest limit is that a bridge already running keeps the value it started
    with: `og start herdr` is idempotent about the daemon (a live pidfile
    means it is not restarted), so a `$BROWSER` the operator changes afterwards
    reaches a fresh launcher's panes and not the running bridge's. That is a
    staleness, not a divergence — same rule, read once, per process.

    KNOWN LIMIT, measured in omnigent 0.17.0 (conversation_browser.py:
    `open_conversation_url`): on macOS that helper does NOT go through
    `webbrowser` — it runs `subprocess.run(["open", url])` directly, so a
    `BROWSER=true` pane does not shut THAT open. So this map does what it can do
    from here (it silences the `webbrowser` openers, and on every non-darwin
    platform it is the whole answer), but on macOS the conversation open needs
    omnigent to route through `webbrowser`, or `auto_open_conversation: false`.
    Fixing that from og means the global config write this code deliberately
    replaced (commit ef2bdd9) — so it is written down here rather than done.
    """
    return {"BROWSER": environ["BROWSER"] if "BROWSER" in environ else "true"}


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


def _says_gone(exc) -> bool:
    """Whether an error from herdr means the thing we named is NOT there.

    `not_found` covers `release_agent`'s "no such marker", which the removal path
    already absorbs on its own; the namespaced forms are what a projection
    actually comes back with — `workspace_not_found` for a tab created in a space
    that is gone, `pane_not_found` for a report aimed at a closed pane. Both are
    a statement about the RECORD, which is why `reconcile` repairs it on them.

    Matched on the SUFFIX rather than against a fixed list, so a herdr that
    gains a namespaced variant of the same fact is handled without a second edit
    here — and a transport failure (`connect`, `timeout`, `disconnected`) is not
    matched at all, because a socket that could not be reached has proved
    nothing about any workspace.
    """
    code = getattr(exc, "code", "") or ""
    return code == "not_found" or code.endswith("_not_found")


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


# ---------------------------------------------------------------------------
# the rule about recorded workspace ids
#
# ONE statement of it, shared with og_herdr_launch (which asks the same question
# of the same listing before it believes the same file). The launcher had this
# first, and got it right; the bridge never learned it, and a recorded workspace
# it never re-validated is what a fan-out of sub-agents projects into:
#
#     error f193205d…: workspace_not_found: workspace wV not found   (worker 1)
#     error 5b011e8e…: workspace_not_found: workspace wV not found   (worker 2)
#     error 3f3a1c0a…: workspace_not_found: workspace wV not found   (worker 3)
#
# wV had been recorded once and gone (the operator closed the space, or `og start
# herdr` case (C) closed the pane that was the last one in it — closing a
# workspace's last pane removes the workspace, measured). Every worker after that
# was handed the dead id, forever.
#
# THE THREE ANSWERS, and collapsing any two of them is a bug:
#   recorded id IS in the listing   — the space is there; use it
#   recorded id is NOT in it        — herdr says the space is GONE; re-adopt
#   the listing could not be read   — we CANNOT TELL; believe the record
#
# The third is the one that is easy to get wrong and expensive when it is: an
# unreadable listing read as an empty one says "your space is gone", and the
# bridge would open a SECOND space for a conversation that already has one.
# The record is a cache, not a source of truth (docs/MUX-LAUNCHER §3) — only
# herdr's own listing can say an id is gone.
# ---------------------------------------------------------------------------

SPACE_LIVE = "live"
SPACE_GONE = "gone"
SPACE_UNCONFIRMED = "unconfirmed"


def workspace_index(spaces) -> dict:
    """`{workspace_id: row}` from a `workspace.list` REPLY; `{}` for an empty one.

    Built from a REPLY and never from a failure: an empty `{}` here means herdr
    answered and said it has no workspaces, which is a different fact from "the
    answer never arrived". Deciding between those is `space_status`'s job, not
    this one's — keeping the parse free of the judgement is what stops the two
    from being confused again.
    """
    index = {}
    for ws in spaces if isinstance(spaces, list) else []:
        if not isinstance(ws, dict):
            continue
        ws_id = ws.get("workspace_id")
        if isinstance(ws_id, str) and ws_id:
            index[ws_id] = ws
    return index


def space_status(recorded: str | None, index: dict | None) -> str:
    """`SPACE_LIVE` / `SPACE_GONE` / `SPACE_UNCONFIRMED` for one recorded id.

    `index` is `None` for "we could not ask" and a dict for "herdr answered";
    `{}` is an answer, not a shrug.

    Nothing recorded is `SPACE_UNCONFIRMED` rather than `SPACE_GONE`: with no id
    there is nothing for herdr to have or not have, and calling that GONE would
    let a caller that skips the "nothing recorded" branch invent a death.
    """
    if not recorded or index is None:
        return SPACE_UNCONFIRMED
    return SPACE_LIVE if recorded in index else SPACE_GONE


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


# The sibling of `_REGISTRY_LABELS`, read from the same catalog for the same
# reason: `vendor` is a registry fact (it drives the cross-vendor review check),
# so the view that draws a per-vendor mark reads it from here rather than
# keeping a second harness->vendor table that could drift from the catalog.
_REGISTRY_VENDORS: dict[str, str] | None = None


def harness_vendors() -> dict[str, str]:
    """`{harness id: vendor}` from installer/registry.json, cached.

    The same degradation contract as `harness_labels`, and for the same reason:
    never raises and never returns a half-read table. A missing, unreadable or
    malformed catalog yields `{}`, and a harness that is absent falls back to no
    vendor — the caller (a mark, a review check) then renders nothing rather
    than guessing a vendor the registry does not name. A broken catalog must
    cost a cosmetic detail, never a pane.
    """
    global _REGISTRY_VENDORS
    if _REGISTRY_VENDORS is not None:
        return _REGISTRY_VENDORS
    vendors: dict[str, str] = {}
    try:
        catalog = json.loads(REGISTRY_PATH.read_text())
    except (OSError, ValueError):
        catalog = None
    rows = catalog.get("agents") if isinstance(catalog, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        harness, vendor = row.get("harness"), row.get("vendor")
        if isinstance(harness, str) and harness and \
                isinstance(vendor, str) and vendor:
            vendors[harness] = vendor
    _REGISTRY_VENDORS = vendors
    return vendors


# The THIRD sibling of `_REGISTRY_LABELS` / `_REGISTRY_VENDORS`, read from the
# same catalog for the same reason: whether a harness has a TERMINAL to mirror is
# a registry fact (its `kind`), not something to re-derive from the harness id's
# spelling. `og agents` asks this to tell a native harness — hosted as a real TUI
# in a private tmux server — from an ACP one, which is a plain stdio child with
# no pty and so nothing to show, without a second kind table that could drift
# from the catalog.
_REGISTRY_KINDS: dict[str, str] | None = None


def harness_kinds() -> dict[str, str]:
    """`{harness id: kind}` from installer/registry.json, cached.

    The same degradation contract as its two siblings, and for the same reason:
    never raises and never returns a half-read table. A missing, unreadable or
    malformed catalog yields `{}`, and a harness absent from it reads as no kind
    — the caller then treats it as neither native nor ACP, which is the honest
    "unknown" and never a guess that would open a pane with nothing behind it. A
    broken catalog must cost a cosmetic detail, never a pane.
    """
    global _REGISTRY_KINDS
    if _REGISTRY_KINDS is not None:
        return _REGISTRY_KINDS
    kinds: dict[str, str] = {}
    try:
        catalog = json.loads(REGISTRY_PATH.read_text())
    except (OSError, ValueError):
        catalog = None
    rows = catalog.get("agents") if isinstance(catalog, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        harness, kind = row.get("harness"), row.get("kind")
        if isinstance(harness, str) and harness and \
                isinstance(kind, str) and kind:
            kinds[harness] = kind
    _REGISTRY_KINDS = kinds
    return kinds


# The registry kinds Omnigent hosts WITHOUT a pty: `acp-builtin` and `acp-user`
# are plain stdio JSON-RPC children (see docs/HERDR.md), so there is no terminal
# to mirror, by construction. Named as a set so the membership is stated once.
ACP_KINDS = frozenset({"acp-builtin", "acp-user"})


def harness_has_terminal(harness: object) -> "bool | None":
    """Whether `harness` is one Omnigent hosts as a real terminal TUI.

    True for a `native` registry kind, False for an ACP one, and None when the
    harness is unknown (no id, or not in the catalog). None is a THIRD answer on
    purpose: "I do not know this harness" must not be read as either "it has a
    terminal" (which would try to attach to nothing) or "it is ACP" (which would
    print the wrong reason to the operator). The caller reports the honest
    "no terminal found" for None.
    """
    if not isinstance(harness, str) or not harness.strip():
        return None
    kind = harness_kinds().get(harness.strip())
    if kind in ACP_KINDS:
        return False
    if kind == "native":
        return True
    return None


# The harness an installed sub-agent ACTUALLY runs on, read from the spec
# Omnigent installed for it — the one honest answer a WORKER row can give.
#
# A worker's LISTING row carries no `harness` (only a root's detail fetch does),
# so a mark resolved from the row alone drew a blank for every worker. The fix is
# NOT to guess a vendor from the agent's NAME: a name that merely resembles a
# registry id says nothing about which harness ran, and a plausible wrong mark is
# worse than an honest atom because nothing on screen flags the guess. The name
# is still the KEY we look up by — but the VALUE comes from the spec, whose
# `executor.harness` is the harness the session was dispatched on. Layout:
#
#     <home>/agents/<Bundle>/agents/<name>/config.yaml
#
# There can be more than one bundle directory; see `_scan_agent_harnesses` for
# how a collision is resolved deterministically.
_REGISTRY_AGENT_HARNESSES: dict[str, str] | None = None

# The prefix the orchestrator puts on a worker name in a session title
# (`coder_zen:task`). The installed spec is named/`dir`'d `coder_zen` while the
# row's title, through `split_title`, hands over `zen` — so both spellings are
# indexed and either resolves.
_WORKER_PREFIX = "coder_"

# The two directory levels under the home, and the spec's filename:
#   <home>/agents/<Bundle>/agents/<name>/config.yaml
AGENTS_DIRNAME = "agents"
SPEC_FILENAME = "config.yaml"

try:
    import yaml as _yaml
except Exception:  # pragma: no cover - the "no PyYAML" test drives this path
    # PyYAML is NOT a dependency of `og agents` — the module docstring insists it
    # must run in any terminal — so its absence has to degrade to "no worker
    # harness known" (every worker falls to the atom) rather than stop the pane
    # from starting. The spec scan below is the only reader of YAML here, and it
    # is the only thing this guard protects.
    _yaml = None


def default_home() -> Path:
    """The Omnigent home directory, read from the environment NOW.

    `$OMNIGENT_HOME`, else `~/.omnigent`. Deliberately a function and not a
    module-level constant, for the same reason `default_state_path` is: the value
    depends on the environment, and a test (or an operator running against a
    sandbox home) must be able to redirect it without editing a constant frozen
    at import time.
    """
    return Path(os.environ.get("OMNIGENT_HOME") or Path.home() / ".omnigent")


def agent_harnesses() -> dict[str, str]:
    """`{agent name: harness id}` from the installed agent specs, cached.

    The WORKER counterpart of `harness_labels` / `harness_vendors`, and the one
    input `og agents` needs to mark a row by the harness it ran on rather than by
    what its name resembles.

    Cached on ONE scan of the specs, not a filesystem walk per call: the pane
    polls every few seconds and re-scans on every row, so after the first call
    this must cost nothing. A test redirecting the home (or making PyYAML
    disappear) resets `_REGISTRY_AGENT_HARNESSES` to force a fresh scan.

    The same degradation contract as its two siblings, and for the same reason:
    never raises and never returns a half-read table. A missing home, a missing
    agents directory, an unreadable or malformed spec or the absence of PyYAML
    all yield `{}` (or omit that one agent) — the caller then draws the atom,
    which is a cosmetic loss, never a pane that fails to render.
    """
    global _REGISTRY_AGENT_HARNESSES
    if _REGISTRY_AGENT_HARNESSES is not None:
        return _REGISTRY_AGENT_HARNESSES
    _REGISTRY_AGENT_HARNESSES = _scan_agent_harnesses(default_home())
    return _REGISTRY_AGENT_HARNESSES


def _scan_agent_harnesses(home: Path) -> dict[str, str]:
    """One walk of `<home>/agents/*/agents/*/config.yaml` -> `{alias: harness}`.

    DETERMINISTIC across bundles: the bundle directories are sorted by name and
    the FIRST spec that names a given alias wins (`setdefault`). A mark that
    changed between runs because a directory listing happened to come back in a
    different order would be a bug, so the order is fixed here rather than left
    to the filesystem. (There is normally one bundle — the one `og install.sh`
    wrote — so this only matters if a machine carries several.)

    Both the spec's own `name:` field and its DIRECTORY name are indexed, and each
    is indexed with the `coder_` prefix stripped as well: the row's agent name is
    the title's `<role>_<agent>` prefix with `coder_` removed (see `split_title`),
    while the installed spec is spelled `coder_zen`.

    Never raises: a missing/unreadable `agents` directory, or an unreadable
    bundle, contributes nothing. See `_read_spec` for the per-file contract.
    """
    out: dict[str, str] = {}
    try:
        bundles = sorted(p for p in (home / AGENTS_DIRNAME).iterdir()
                         if p.is_dir())
    except OSError:
        return out
    for bundle in bundles:
        try:
            specs = sorted(p for p in (bundle / AGENTS_DIRNAME).iterdir()
                           if p.is_dir())
        except OSError:
            continue
        for spec_dir in specs:
            name, harness = _read_spec(spec_dir / SPEC_FILENAME)
            if not harness:
                continue
            for alias in (name, spec_dir.name):
                if not alias:
                    continue
                if alias.startswith(_WORKER_PREFIX):
                    out.setdefault(alias[len(_WORKER_PREFIX):], harness)
                out.setdefault(alias, harness)
    return out


def _read_spec(path: Path) -> tuple:
    """`(name, harness)` from one spec's `config.yaml`, or `(None, None)`.

    Never raises. A missing file, unreadable bytes, malformed YAML, a spec that
    is not a mapping, an `executor` that is not one, or an executor with no
    harness all read as "no harness known": the caller draws the atom, which is a
    cosmetic loss and never a pane that fails to render.

    The harness is read from `executor.harness`, and from `executor.config.harness`
    where the installer actually writes it (see templates/coder.yaml.tmpl) — the
    two are checked so a spec in either shape resolves.
    """
    if _yaml is None:
        return None, None
    try:
        data = _yaml.safe_load(path.read_text())
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    name = data.get("name")
    name = name if isinstance(name, str) and name else None
    executor = data.get("executor")
    if not isinstance(executor, dict):
        return name, None
    harness = executor.get("harness")
    if not isinstance(harness, str) or not harness:
        config = executor.get("config")
        harness = config.get("harness") if isinstance(config, dict) else None
    if not isinstance(harness, str) or not harness:
        harness = None
    return name, harness


def agent_harness(agent: object) -> str | None:
    """The harness id the agent NAME's installed spec declares, or None.

    The lookup is EXACT (case-sensitive): the names come from `split_title`,
    which strips `coder_` from the title's prefix, and the scan indexes both the
    `coder_`-prefixed and bare spellings — so `zen` and `coder_zen` both resolve
    and no case-folding is needed for the names this view actually sees (verified
    against a live listing: every worker name is lowercase).

    Never raises: a non-string or empty name resolves to None, exactly as a name
    with no installed spec does, and the caller then draws the atom.
    """
    if not isinstance(agent, str) or not agent.strip():
        return None
    return agent_harnesses().get(agent.strip())


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


def _copy_record(rec):
    """One record, copied one level deep — see `_snapshot` for why."""
    return dict(rec) if isinstance(rec, dict) else rec


def _snapshot(records: dict) -> dict:
    """`{key: an independent copy of the record}`, for `Bridge._synced`.

    One level deep on purpose, and a copy rather than the mapping itself: a
    record is handed around by reference (`_claim_pending` MOVES one from
    `pending` into `spaces`, `_fold_state` takes one from the file, and
    `_existing_space` edits `record["cwd"]` in place), and `_synced`'s whole job
    is to answer "is what I hold still what the file last held" — which it cannot
    do if the thing it is comparing against moves whenever the thing it is
    comparing does, nor if the record it just took aliases the map it compares
    against (that comparison would be a tautology, and every key would read as
    unchanged no matter what this process did to it). Records are flat maps of
    JSON scalars (`_read_spaces` builds them that way), so one level is the whole
    of the structure.
    """
    return {key: _copy_record(rec) for key, rec in records.items()}


def _memory_decides(key: str, record: dict, mine: dict, onfile: dict) -> bool:
    """Whether memory's answer for `key` is this process's FINAL one.

    The merge rule per key, and the whole of it is `_synced`: what the file held
    the last time this process KNEW it held it. Two shapes of answer, and they
    must not be confused — which is the bug this predicate exists to fix, because
    the shape the rule cannot tell apart is one the bridge MANUFACTURES ITSELF.

      * memory HOLDS the key, and holds something other than `_synced` — an edit
        whose save has not landed. Memory wins: rolling it back would put this
        bridge on a workspace it has already typed an attach command into.
      * memory DOES NOT hold the key. Then memory does not disagree with the
        file about what a record is; it has finished with one. It wins only if
        the file's copy is the very record it finished with — do NOT resurrect a
        record this process has already claimed (that would put the space back
        in play for the next session in the directory: a second client on one
        session) or deliberately dropped (pruned as dead, or forgotten because
        its workspace was closed).

    A DIFFERENT record under that key is somebody else's news and the file wins:
    the launcher keys `pending` by directory and replaces what is there, so a
    record that is not the one memory consumed was written after this process
    last knew what the file held, and ignoring it opens a second space for a
    directory that already has one. Reading "the key is absent" as "a local edit"
    — which is what treating every difference the same way did — makes this
    process's own completed claim indistinguishable from an unflushed edit, and
    that is a distinction the file cannot be asked to make for us.
    """
    if key in mine:
        return mine[key] != onfile.get(key)
    held = onfile.get(key)
    return held is not None and held == record


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
                 cwd=None, server=DEFAULT_SERVER, state_path=None, filer=None,
                 space_cap=DEFAULT_SPACE_CAP, pane_env=None, agents_pane=True):
        self.watcher = watcher
        self.client = client
        self.dry_run = dry_run
        self.source = source
        # Files each new ROOT session under the install's project folder
        # (og_projects.ProjectFiler), or None to leave filing off. It is a seam
        # and not a call made here, because the bridge is AST-guarded against
        # reaching for the network of its own accord (see the tests) — the
        # watcher owns every HTTP call it makes, and this one arrives injected.
        self.filer = filer
        # Whether a filing failure has already been reported. Filing is a
        # convenience, not the job, so a broken server warns ONCE per bridge and
        # never once per session — a daemon that logs a line per new root for the
        # life of the process is the noise this flag exists to prevent.
        self._file_warned = False
        # How many history spaces (past conversations) this bridge may keep on
        # screen; see DEFAULT_SPACE_CAP. A live session is never subject to it.
        self.space_cap = space_cap
        # Whether a history/resume space gets the `og agents` split its sibling
        # `og start` opens for a fresh conversation. TRUE by default, matching
        # `og_herdr_launch.agents_pane_enabled`'s default for an install that
        # predates the key — a history space must read the install's
        # `herdr_agents_pane` exactly as a fresh start does, and a Bridge built
        # by anything at all gets the configured layout unless told otherwise.
        # `main` reads the key and passes it here; see `_split_agents`.
        self.agents_pane = agents_pane
        # The pane-shell environment installed into every pane this bridge asks
        # herdr to create, so none of them can open a browser (see
        # `pane_env_for`). DERIVED from this process when not given, rather than
        # defaulting to "send no env": the bug this fixes was a path that forgot
        # the map, so the default has to be the safe one — a Bridge built by
        # anything at all suppresses unless it is explicitly told otherwise.
        self.pane_env = pane_env_for(dict(os.environ)) if pane_env is None \
            else pane_env
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
        # Where this bridge notes which herdr tab shows which agent — derived
        # from the state path so a redirected install moves both files together.
        # See `remember_agent_tab` for why herdr itself cannot answer this.
        self.agent_tabs_path = agent_tabs_path(self.state_path)
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
        # What the file held the last time this process KNEW it held it: at this
        # load, and at every save that landed since. The recency signal
        # `_fold_state` needs to tell a record another process just wrote from a
        # record this one is holding — see there for why neither "always the
        # file" nor "always memory" is right.
        self._synced: dict[str, dict] = {"spaces": _snapshot(self._spaces),
                                         "pending": _snapshot(self._pending)}
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

    def reconcile(self, events, live_ids=None) -> list:
        """Apply a batch of session events, returning human-readable action
        lines. One bad pane is recorded and skipped, never fatal: the bridge
        must survive a single failing herdr call and keep projecting the rest.

        `live_ids` is the watcher's live session set for the pass that just
        happened, or None when it could not be obtained. It is what makes the
        orphan sweep (`_close_orphan_spaces`) safe: None is a shrug, not an
        answer, and sweeps nothing — a listing that cannot be read must never
        read as "every session is gone".
        """
        # The workspace listing is a per-PASS snapshot, not a per-call one:
        # adoption, claiming and pending-pruning all ask the same question of
        # the same list, and a batch of ten new sessions must see one consistent
        # answer rather than ten racing listings.
        self._ws_index = None
        self._ws_ok = False
        self._ws_error = None
        self._pass_notes = []
        # Ahead of `_prune_pending`, which saves and would otherwise write this
        # process's construction-time copy of the file straight over a record
        # the launcher wrote since. See `_reload_state` for the whole argument.
        self._reload_state()
        lines: list[str] = list(self._prune_pending())
        lines.extend(self._close_orphan_spaces(live_ids))
        for event in self._roots_first(events):
            try:
                lines.extend(self._apply(event))
            except _error_cls() as exc:
                lines.append(_error_line(event.session_id, exc))
                if event.kind != "removed" and _says_gone(exc):
                    # The record is stale NOW, not just unlucky: herdr said the
                    # pane or space we named is not there. Repair it once (see
                    # `_reproject`) instead of logging the same line on every
                    # poll for the rest of the process — which is what nine
                    # `pane_not_found` lines for one root turned out to be. A
                    # `removed` is excluded: that session is on its way out, and
                    # re-opening a pane for it would be the opposite of the
                    # cleanup the event asked for.
                    try:
                        self._reproject(event.session_id, event.session)
                    except _error_cls() as repair_exc:
                        lines.append(
                            "error {0}: could not re-project it: {1}: {2}".format(
                                event.session_id,
                                getattr(repair_exc, "code", "herdr-error"),
                                getattr(repair_exc, "message", repair_exc)))
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

    def _live_session_ids(self):
        """The watcher's live session set for the pass just observed, or None.

        `poll_once`/`watch` populate the watcher's `_seen` as their final step,
        so this is only ever read on the path that has just polled. None makes
        the orphan sweep a no-op — and None is a shrug, never "everything is
        gone": a listing that cannot be read must never close every space the
        operator has.

        Which paths those are: every event pass, and the idle pass `run_forever`
        runs when a poll produced no events at all. The idle pass is not a hole
        in that rule, because it is handed to `watch` as the `on_idle` callback
        and fires from inside the poll loop — so it cannot arrive before a poll
        has written `_seen`, which is the one case where `_seen` is empty,
        `_last_truncated` still reads False from `__init__`, and a confident
        answer would be "no session is live" on a daemon that has not looked yet.

        A TRUNCATED listing is the subtle half of that, not the absent one. On
        a cold start `_seen` began empty, so the truncated merge yields only
        what was partially fetched — a LIVE session that simply was not fetched
        reads as absent. The sweep needs a COMPLETE listing before it may
        conclude a session is gone; a partial one defers to the next pass,
        where the orphan is still waiting but a closed operator space is not.
        A watcher that cannot say whether it was truncated (no
        `_last_truncated` attribute — the FakeWatcher in tests, or a watcher
        that has not polled) is treated as truncated, never as complete.

        The signal is `_last_truncated`, the watcher's record of whether the
        listing was ACTUALLY truncated — and NOT `_listing_truncated`, which
        looks interchangeable and is not. That flag answers "has the operator
        been TOLD?", and it is set only on a write that landed, so that a
        failed notice is retried rather than swallowed (see the watcher's
        `_announce_listing`). The two agree only while every write succeeds.
        The moment stderr refuses one — a daemon started with no terminal to
        write to, an operator who redirected it away — a cold start whose first
        listing was truncated left `_listing_truncated` False while `_seen` held
        a partial set, and the sweep read that partial set as a complete answer
        and closed the spaces of sessions that were still running. Unrecoverable:
        a space is where the operator's open workspace lives. So the fact is
        recorded unconditionally in `poll_once`, and this reads the fact.
        """
        seen = getattr(self.watcher, "_seen", None)
        if not isinstance(seen, dict):
            return None
        if getattr(self.watcher, "_last_truncated", True):
            return None
        return frozenset(seen)

    def run_once(self) -> list:
        """Poll the watcher once and reconcile whatever it returned."""
        events = self.watcher.poll_once()
        return self.reconcile(events, live_ids=self._live_session_ids())

    def run_forever(self) -> None:
        """Consume watcher.watch() indefinitely, reconciling each event — and
        reconciling the polls that produced NONE, which is the only thing that
        reaches the orphan sweep in the case it exists for.

        `run_once` always reconciles, and every test that exercises the sweep
        goes through it or through `reconcile` directly, so nothing here was
        ever driven the way an operator drives the bridge: as a daemon, for days,
        against a listing that has stopped changing. In that daemon this method
        used to reconcile strictly per event, so the sweep ran only when the
        server said something — and the one deletion that never reaches the
        server's history (a session deleted in the web UI while the bridge was
        down, then a restart) produces no event ever: `_seen` began empty, so
        the deleted session is simply not in the first listing, `watch()` yields
        nothing, and the recorded space is never closed. Not "closed late" —
        never, for the life of the process. That defeats both causes the sweep
        exists for, so a sweep that is correct and never reached is not a
        feature.

        Hence the idle callback, which is why this is a scheduling fix at the
        one place where the schedule lives rather than a change to what the sweep
        decides. `on_idle` fires only after a poll that returned (see
        `SessionWatcher.watch`), so the pass below always reads a `_seen` that a
        real poll wrote and the truncation guard in `_live_session_ids` still has
        the last word on it. It also covers the second half of the gap, which a
        startup-only sweep would leave open: a first listing that is TRUNCATED
        defers the sweep, and the listing that completes it afterwards may well
        produce no events — so the orphan is still waiting on the very next quiet
        poll, which is exactly what this runs.

        The watcher must therefore accept `on_idle` — `SessionWatcher` does, and
        `FakeWatcher` in the tests predates it, which is why no test here drives
        this loop through that fake.
        """
        for event in self.watcher.watch(
                on_idle=lambda: self._reconcile_pass([])):
            self._reconcile_pass([event])

    def _reconcile_pass(self, events) -> None:
        """One reconcile pass, reported on stdout. The daemon's only output.

        Split out of `run_forever` so the event path and the idle path are the
        same call — an empty batch and a one-event batch differ in nothing else,
        and two hand-written copies of this loop is how the two paths would come
        to disagree about what a pass is.
        """
        for line in self.reconcile(events, live_ids=self._live_session_ids()):
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
                  cwd=None, owner: str = OWNER_BRIDGE) -> None:
        """Record a space as this session's, so `--cleanup` can close it later.

        `owner` is `bridge` unless a caller says otherwise — a history space
        passes `OWNER_HISTORY` so the cap can count it (see
        `_history_space_count`). A space the launcher opened keeps
        `owner: "launcher"` all the way through a claim (see `_claim_pending`),
        because the owner records who opened it, not who is looking at it now.
        """
        self._spaces[sid] = {"workspace_id": ws_id, "owner": owner,
                             **({"tab_id": tab_id} if tab_id else {}),
                             **({"pane_id": pane_id} if pane_id else {}),
                             **({"cwd": cwd} if cwd else {})}
        self._save_state()

    def _mark_synced(self) -> None:
        """Record what the file holds, now that we know it does.

        `_synced` is the recency signal `_fold_state` decides against, so it is
        only ever set from a write that COMPLETED: `save_state` swallows a write
        it could not finish (a read-only home, a full disk), and a snapshot taken
        from a failed write would claim the file holds something it never got —
        which would let that older copy win the next merge, the one direction
        that is not recoverable by looking at herdr again.
        """
        self._synced = {"spaces": _snapshot(self._spaces),
                        "pending": _snapshot(self._pending)}

    def _save_state(self) -> bool:
        """Write both maps, and — only when the write LANDED — remember the copy.

        Through `state_update`, so this is the launcher's discipline too: fold in
        whatever the file gained while this process was working, and do it while
        the launcher provably is not writing. Writing this process's maps
        straight over the file was the last unguarded whole-file write in the
        module, and it could erase a launcher's record that landed since the
        bridge last read — the same loss `state_update` was added to stop, one
        step downstream of where it first showed up. The fold inside the
        transaction is what makes the write safe to make at all: it is why this
        cannot drop what it never saw.
        """
        def publish(fresh: dict) -> dict:
            self._fold_state(fresh)
            return {"spaces": self._spaces, "pending": self._pending}

        return state_update(self.state_path, publish, on_saved=self._mark_synced)

    def _reload_state(self) -> None:
        """Fold the state file as it is NOW into this process's two maps.

        THE STALE SNAPSHOT. `og start herdr` starts this bridge BEFORE it execs
        the launcher (see `bin/og`), and the launcher then writes its own
        `pending[cwd]` record and exits. Both processes write the same file,
        whole, last-writer-wins (see `save_state`), so maps loaded once at
        construction cannot see anything written after that instant — and the
        miss is silent AND visible at the same time: the dedupe in
        `_existing_space` / `_claim_pending` finds no record, concludes the
        session has no space, and opens a SECOND one for a directory that
        already has one. One operator's state file held three spaces for one
        directory, two of them the bridge's own, and the extra pane was
        something they had to notice and close by hand. The dedupe only ever
        worked across a bridge RESTART, which is why the duplicate looked like
        it came and went with `og start`.

        WHERE, and why this point. Twice per pass, and the two are not the same
        job. Once at the pass boundary, ahead of `_prune_pending`, because
        pruning SAVES: it writes this process's whole copy of the file, so a
        prune run against the construction-time snapshot would not merely miss
        the launcher's record, it would DELETE it — and a record no process holds
        any more can never be found by a later pass either. Once more inside
        `_claim_pending`, immediately before the decision it feeds, because the
        pass-boundary fold is not new enough by the time it is acted on: between
        the two sit `_prune_pending`'s `workspace.list` round trip and every
        event handled before this one, which is easily tens of milliseconds and
        is exactly how long a launcher's `_record` takes to land. The claim is
        the only decision in a pass that reads `pending` and it is the one whose
        miss opens a second space, so it is the one that re-reads. It is also
        once per claim, not once per event: `_claim_pending` is reached only for
        a session with no `spaces` record, and the claim puts one there.

        There, and only there, the read is part of a LOCKED transaction — see
        `_claim_pending` and `state_lock`. This method's read stands alone, and
        deliberately so: it is not deciding anything that will be acted on
        without another read, so there is nothing for a lock to protect there.

        MERGE, decided per key against the last-known copy — never a wholesale
        replace, and never a blind `setdefault`. See `_fold_state`, which states
        the rule; the short form is that a key this process has not changed
        since the file last held it belongs to whoever wrote the file last,
        including a writer who REPLACED it.

        ADDITIVE ONLY, which is what makes an unreadable file a no-op. Missing,
        empty and unparseable all read back as two empty maps (`load_state`),
        and merging two empty maps in changes nothing — so a file the bridge
        cannot read leaves it behaving exactly as it behaves today. That is
        the shape doing the work, not a fallback bolted onto it: a reload that
        CLEARED on failure could convince a live bridge that the space a
        session already has is recorded nowhere, and the next thing it does
        with that belief is open a second space. An unreadable file must never
        be able to cause the very duplicate this exists to prevent.

        No note is passed to `load_state`, deliberately: `_state_note` is
        printed once at startup about the file as it was at startup, and a
        per-pass reload would either repeat that line forever or drown the
        pass's own output under it.
        """
        try:
            fresh = load_state(self.state_path)
        except Exception:
            # `load_state` is already total — it swallows read and parse errors
            # and reports through the note instead — so reaching here means it
            # grew a failure mode, and this bridge is a daemon whose whole job
            # is projecting sessions. Its pre-reload view is exactly today's
            # behaviour, which is stale but never fatal; a raise here would
            # stop the pass instead.
            return
        self._fold_state(fresh)

    def _fold_state(self, fresh: dict) -> None:
        """Merge a freshly-read file in, key by key, `_synced` deciding.

        FOUR CASES, and the fourth is why this is not `setdefault`:

          * the file has a key this process holds unchanged since the file last
            held it — the FILE WINS. There is nothing local to protect, and the
            writer who put that record there is the more recent writer;
          * the file has a key this process changed WITHOUT a save landing
            (`_save_state` only re-snapshots on a write that completed) —
            MEMORY WINS, because it is ahead on purpose and rolling it back
            would put this bridge on a workspace it has already typed an attach
            command into;
          * the file has a key memory no longer has, carrying the record memory
            consumed or dropped — MEMORY WINS, which is what keeps a just-claimed
            record from un-claiming itself on the next pass and a pruned one from
            coming back;
          * the file has a key memory no longer has, carrying a DIFFERENT record
            — the FILE WINS. This process finished with the old one, and someone
            wrote a new one since; refusing it opens a second space for a
            directory that already has one.

The rule itself is `_memory_decides`, which is where the distinction
        between the second case and the last two is drawn. The second and the
        third used to be ONE case — "memory differs from `_synced`" — and that is
        the defect this shape fixes, because a CLAIM is a normal, completed
        operation that lands in the middle of it: the record has moved out of
        `pending` while the write that would say so has not reached the file, so
        memory differs from `_synced` for that key exactly as it does for an
        unflushed edit, and a rule that reads the difference as "protect my local
        edit" cannot tell them apart. It could only ever resolve the tie towards
        memory, which is what kept the bridge from honouring the launcher's
        replacement under a directory it had already claimed the first record of.

        `onfile` is updated with a COPY of each record it takes, not the object
        memory takes: an alias would make the next comparison a comparison of a
        record with itself, and every key would then read as unchanged whatever
        this process did to it.

        A key the file NO LONGER has is never removed from memory. That is the
        additive half, and it is what makes an unreadable file a no-op: missing,
        empty and unparseable all read back as two empty maps (`load_state`),
        and folding two empty maps in changes nothing at all — so a file the
        bridge cannot read leaves it behaving exactly as it behaves today. The
        shape does the work rather than a fallback bolted onto it: a reload that
        CLEARED on failure could convince a live bridge that the space a session
        already has is recorded nowhere, and the next thing it does with that
        belief is open a second space. An unreadable file must never be able to
        cause the very duplicate this exists to prevent. (A record the launcher
        dropped on purpose — `_unrecord`, after a failed `og chat` — survives in
        memory for one pass and names a space that was closed with it, so the
        very next `_prune_pending` drops it again.)
        """
        for bucket in ("spaces", "pending"):
            mine = getattr(self, "_" + bucket)
            onfile = self._synced[bucket]
            for key, record in (fresh.get(bucket) or {}).items():
                if _memory_decides(key, record, mine, onfile):
                    continue
                mine[key] = record
                onfile[key] = _copy_record(record)

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

    def _file_under_project(self, sid: str) -> list:
        """File a newly-seen ROOT session under this install's project folder.

        The write lives HERE, in the bridge, and not in the watcher: the watcher
        is read-only by contract — `og_agents` depends on that and says so — and
        the bridge is the component that already acts on session events. The HTTP
        itself belongs to the injected `filer`, so the bridge stays free of any
        network call of its own (the tests pin that with an AST scan).

        The name is the orchestrator bundle from the install plan, read here
        rather than hard-coded, so a user's `hivemind` folder is the one the
        session lands in. It defaults the same way the launcher's does, via
        `og_herdr_launch`, so there is one spelling of the key and its fallback.

        Best effort throughout. A dry run makes no calls at all; an already-filed
        session is left exactly as it was (by the filer); and a failure is warned
        ONCE and never raised, so it can neither take the bridge down nor keep the
        session's pane from opening. There is no retry: a session is filed once.
        """
        if self.filer is None or self.dry_run:
            return []
        from og_herdr_launch import agent_name, install_config

        name = agent_name(install_config())
        status, detail = self.filer.file(sid, name)
        if status == "filed":
            return [f"filed {sid} under the '{name}' project"]
        if status == "failed" and not self._file_warned:
            self._file_warned = True
            return [f"warn: could not file session {sid} under the '{name}' "
                    f"project ({detail}); it stays unfiled"]
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

        # File this ROOT under the install's project folder, once, on first
        # sighting. A sub-agent is skipped: it nests under its root in the
        # sidebar already and must stay unlabelled, or every worker would sprout
        # a folder of its own. The lines are folded into whichever return below
        # fires, so a filing failure is reported without ever touching the pane.
        filed = self._file_under_project(sid) if parent is None else []

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
                    return filed + [
                        f"dry-run: {taken['action']} '{title}' → space "
                        f"{taken['workspace_id']} ({cwd}) — unverified, "
                        f"read from the state file without asking herdr"]
                # A record naming a pane we can confirm is finished with: the
                # space, its tab and its pane all exist and something is already
                # running in it. Re-running setup would type the attach command
                # a second time into a live pane — for a launcher's space that
                # is a second co-drive client on one session, which is the race
                # `_state` exists to avoid. So: adopt it and touch nothing.
                if taken["pane_ok"]:
                    return filed + self._adopt(sid, taken["record"],
                                               taken["action"], title, agent,
                                               state, command)
                # No pane to resume onto (an upgraded v1 record carries no pane
                # id). The space is still this session's, so it becomes a TAB in
                # that space rather than a second space beside it.
                space = taken["workspace_id"]
        if space is None:
            space = self._space_of(parent)

        # A ROOT with no live runner is a PAST conversation, and it gets a space
        # too — a history one, a plain shell holding the resume line unsubmitted
        # — rather than a live pane. Checked AFTER `_existing_space`, so a space
        # already recorded for this session (written by this process or an
        # earlier one) is adopted rather than duplicated. See `_history_root`.
        #
        # `filed` is folded in here like every other return below, not dropped:
        # a dead root is filed the same as a live one, and a filing failure (or
        # its once-per-bridge warning) must still reach the operator for it.
        if space is None and self._is_history_root(session):
            return filed + self._history_root(sid, title, cwd, agent, state)

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
            return filed + [f"dry-run: add '{title}' → {where} ({cwd}) → "
                            f"{command} [{state}]"]

        if space is None:
            return filed + self._add_space(sid, title, cwd, agent, state,
                                           command, parent,
                                           harness=session.get("harness"))
        return filed + self._add_tab(sid, title, space, cwd, agent, state,
                                     command, parent,
                                     harness=session.get("harness"))

    # -- past conversations: a space for every non-archived ROOT -------------
    #
    # The web sidebar lists every conversation the operator has had; this is the
    # herdr half of that. A root whose runner is gone gets a space labelled with
    # its title and a pane holding the resume line, unsubmitted — visible and
    # resumable, with nothing running. A sub-agent is deliberately NOT extended
    # this treatment (see `_is_history_root` and og_herdr_watch._projectable):
    # a dead worker is noise.

    @staticmethod
    def _is_history_root(session: dict) -> bool:
        """Whether this event is a ROOT with no live runner — a past conversation.

        `runner_online is False` is the one signal that tells a dead root from a
        live one, and the watcher forwards it on the event for exactly that
        reason (og_herdr_watch._enrich). It is only ever an explicit boolean
        false: a missing key — an unreadable detail row, or a session dict from
        any other source — reads as LIVE and takes today's path, word for word.

        The parent check is what keeps this to roots. A sub-agent's behaviour is
        unchanged, and giving a dead worker a space of its own would be the one
        thing the layout exists to prevent.
        """
        if not isinstance(session, dict):
            return False
        if session.get("runner_online") is not False:
            return False
        return _parent_of(session) is None

    def _history_space_count(self) -> int:
        """How many spaces this bridge has open for past conversations.

        Counted from the RECORDS rather than kept as a running tally, so the cap
        holds across a restart. The 20 newest roots are given spaces and written
        to the state file; the next process adopts those (no new open, so no new
        record) and refuses the older ones on the same count. A plain counter
        would reset with the process and open 20 more on every restart until the
        whole history was on screen — the mess the cap exists to prevent,
        arriving 20 at a time.

        `owner == OWNER_HISTORY` is what marks a record as one of these; see the
        constant for why the marker lives in that field.

        A LOWERED cap governs new spaces only: it does not retroactively close
        the ones already open. The count above decides what opens next, and
        nothing here reaches for a live herdr space to enforce the number — a
        space the operator may be reading a past conversation in is not ours to
        close behind their back, and lowering a cap in og-install.json is a
        statement about the future.
        """
        return sum(1 for rec in self._spaces.values()
                   if rec.get("owner") == OWNER_HISTORY)

    def _history_root(self, sid: str, title, cwd: str, agent: str,
                      state: str) -> list:
        """Project a past conversation, or say why it is skipped.

        Three gates, in order, and each one is a real machine's state rather
        than a hypothetical:

        * a DRY RUN records the placeholder a real run would mint (so a worker
          later in the same batch resolves its parent consistently) and prints
          the line, without touching herdr;
        * a workspace DIRECTORY that is gone gets no space. A space's pane is a
          shell whose cwd has to exist — herdr would open it and immediately
          have it die — and a past session whose checkout was a temporary
          worktree (the common case: a delegated worker's tree is deleted when it
          finishes) names a directory that is simply not there. Checked here
          rather than in `_add_space`, because a LIVE session must keep today's
          exact behaviour, broken cwd and all.
        * the CAP: `space_cap` 0 (the default) means NO cap — every non-archived
          root gets a space, as the operator asked. When a positive cap is set,
          the listing is newest-first, so the roots that arrive FIRST are the
          most recent and the ones past the cap are left alone. The cap governs
          what opens from here on; it never closes a space already open, however
          low it is set afterwards (see `_history_space_count`).
        """
        if self.dry_run:
            self._recs[sid] = {
                "workspace_id": f"{DRY_RUN_SPACE}{sid}",
                "tab_id": None, "pane_id": None, "title": title,
                "agent": agent, "parent": None, "owns_space": False,
                "step": 3, "ready": True, "history": True,
            }
            return [f"dry-run: add '{title}' → history space ({cwd}) → "
                    f"{resume_command(sid)} (unsubmitted, resume by hand) "
                    f"[{state}]"]
        if not os.path.isdir(cwd):
            return [f"skip {sid}: no space for '{title}' — its directory {cwd} "
                    f"is gone, so the pane would be a shell with nowhere to "
                    f"open"]
        if self.space_cap and self._history_space_count() >= self.space_cap:
            return [f"skip {sid}: no space for '{title}' — {self.space_cap} "
                    f"past-conversation spaces are already open, and the cap "
                    f"keeps the newest"]
        return self._add_history_space(sid, title, cwd, agent, state)

    def _add_history_space(self, sid: str, title, cwd: str, agent: str,
                           state: str, harness: str | None = None) -> list:
        """Open a space for a past conversation, holding the resume line typed
        but UNSENT.

        The space and its shell come from one `workspace.create`, exactly as a
        live root's do — the only difference is what is put INTO the pane. A
        live root gets `attach_command` SUBMITTED (`pane_run` appends the
        newline that submits it); a history root gets `resume_command` typed
        UNSENT (`pane_send_text` adds no newline), so the pane sits at a shell
        prompt with the line to resume sitting on it. Nothing runs, and the
        session is not resurrected, until the operator presses Enter — which is
        the whole point, and why `pane_run` must not be used here.

        The record carries `history: True` (for `_setup_pane`) and is remembered
        with `owner: OWNER_HISTORY` (for the cap, and so `--cleanup` still closes
        it). `owns_space` is True: this bridge created the space, so a removal
        closes the whole thing rather than one tab of it.

        Everything after the pane text — the agent marker and the pane's title
        metadata — is the SAME as a live root's (see `_setup_pane`), so the space
        reads like a conversation in the list rather than a nameless shell, and
        a title change, an adoption or a removal all take the ordinary path.

        `env` is `self.pane_env`, and this is the create that needs it most: the
        space is silent until the operator presses Enter — which is the whole
        design — so an unsuppressed pane here is indistinguishable from a
        suppressed one until the one keystroke that is the point of the feature
        runs `og chat --resume`, which opens a browser. Nothing about the day it
        goes wrong looks like anything on screen; see `pane_env_for`.

        A history space also gets the `og agents` split a fresh launch opens,
        gated on `agents_pane` — see `_split_agents`. It runs in its OWN pane,
        so it neither submits the resume line above nor starts a runner for the
        dead session.
        """
        label = title or sid[:8]
        result = self.client.workspace_create(
            label=label, cwd=cwd, focus=False, env=self.pane_env)
        ws_id = _id_of(result.get("workspace"), "workspace_id")
        tab_id = _id_of(result.get("tab"), "tab_id")
        pane_id = _id_of(result.get("root_pane"), "pane_id")
        if ws_id is None or tab_id is None or pane_id is None:
            return self._reject_partial_create(sid, ws_id, tab_id, pane_id)
        rec = {"workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
               "title": title, "agent": agent, "parent": None,
               "owns_space": True, "step": 0, "ready": False, "cwd": cwd,
               "history": True, "harness": harness}
        self._recs[sid] = rec
        self._remember(sid, ws_id, tab_id=tab_id, pane_id=pane_id, cwd=cwd,
                       owner=OWNER_HISTORY)
        # Same reason `_add_space` drops it: this pass's listing predates the
        # space, and a later lookup in the same batch must not read it as gone.
        self._ws_index = None
        labelled = self._rename_first_tab(sid, tab_id, label)
        self._setup_pane(sid, rec, state)
        # The agents pane comes AFTER the chat pane's line, exactly as in a
        # fresh launch (`_open_space` splits after typing `og chat`): the split
        # is the second thing done to a space, and it runs `og agents` in its
        # OWN pane, so the resume line just typed stays unsubmitted. See
        # `_split_agents`.
        split = self._split_agents(ws_id, tab_id, pane_id, cwd)
        return [f"add {sid} → history space {ws_id} tab {tab_id} pane {pane_id} "
                f"'{title}' '{resume_command(sid)}' (unsubmitted) [{state}]"] \
            + labelled + split

    def _split_agents(self, ws_id: str, tab_id: str, root_pane: str,
                      cwd: str) -> list:
        """Open `og agents` beside a history space's chat pane, when enabled.

        A history/resume space must honour `herdr_agents_pane` exactly as a
        fresh `og start` does — same command, same ratio, same degradation — or
        a past conversation resumed from the spaces list comes up without the
        pane its sibling launch opens. The split is the SAME implementation the
        launcher calls (`og_herdr_launch.split_agents_pane`), imported HERE
        rather than at module load because the launcher imports this module and
        this module must import without it (see `main`).

        It runs NOTHING on the chat pane: `og agents` goes into its own pane, so
        the resume line stays typed-but-unsubmitted and no runner starts for the
        dead session — the constraint that shapes a history space (see
        `resume_command`). And it is not conditional on the resume having been
        submitted: there is no signal for that, and the pane belongs beside a
        chat whether or not the operator has pressed Enter yet.

        Best effort, like the launcher's: a failure is a returned LINE for
        `reconcile` to report, never an exception — a cosmetic pane must not take
        the space, and the visible conversation in it, down with it.
        """
        if not self.agents_pane:
            return []
        from og_herdr_launch import split_agents_pane

        return split_agents_pane(self.client, ws_id, tab_id, root_pane, cwd,
                                 self.pane_env)

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
        # Three states, and collapsing any two of them is a bug — `space_status`
        # is where that is stated, once, for this module and the launcher:
        #   live        — the space is there
        #   gone        — herdr says it is NOT, so the record is stale
        #   unconfirmed — we could not find out, so we claim nothing
        # A real run that cannot find out adopts nothing: a wrong answer here
        # means a dead pane id, which is the one outcome worse than a duplicate
        # space.
        index = self._workspace_index()
        status = SPACE_UNCONFIRMED if self.dry_run else \
            space_status(ws_id, index)
        info = index.get(ws_id) if index is not None else None
        if status == SPACE_GONE:
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
            # The space's first tab follows it onto the title too — but ONLY when
            # the space itself was relabelled (the same `label != title` test
            # `_rename_space` skips on), so an adoption of an already-correct
            # space does not fire a redundant `tab.rename` every time. This is
            # the adoption path for a space the launcher made: it labels the tab
            # from the directory, and an already-titled session may never produce
            # the `title_changed` that `_rename_own_space` needs, so the tab would
            # otherwise keep the directory name for the life of the session.
            if title and info.get("label") != title:
                self._pass_notes.extend(
                    self._rename_first_tab(sid, record.get("tab_id"), title))
        # NOT in a dry run. In a REAL run the claim has already written for
        # itself (it has to — that is what makes it atomic), so this save is
        # for the `cwd` refresh above and for the plain adopt, neither of which
        # has written yet; and in a dry run there is nothing written at all to
        # build on, `_claim_pending` having left both maps exactly as it found
        # them. It stays guarded rather than moved into `_save_state` for the
        # reason it always gave: a dry run that wrote would consume the pending
        # record and leave the NEXT REAL RUN with nothing to adopt — the same
        # defect that makes a dry-run `--cleanup` dangerous, arriving through
        # the adopt path instead of the cleanup one.
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
            "owns_space": record.get("owner") in BRIDGE_OWNED,
            "step": 3, "ready": True,
            "cwd": record.get("cwd") or self.cwd}
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

        THE SECOND FOLD, HERE, not only at the pass boundary — the one place in
        a pass that reads the file at the moment it is acted on. The fold that
        opened the pass was read before `_prune_pending`'s `workspace.list`
        round trip and before every event handled ahead of this one, and a
        launcher's `_record` landing anywhere inside that gap is invisible to it.
        It is invisible to THIS session's record too: the launcher writes before
        it types `og chat` (`og_herdr_launch._open_space`), so the session this
        claim is for does not exist yet at the instant the pass began, and the
        record that explains it cannot be the one being looked up. What CAN
        arrive in the gap is a record for a DIFFERENT session in this same
        directory — a launch the operator started while the first conversation
        was still appearing — and a miss there is the same duplicate as any
        other.

        THE CLAIM IS ONE TRANSACTION, and that is what this shape exists for.
        The obvious version — re-read the file, then look the record up, then pop
        it — has a window between the re-read and the pop, and a third read does
        not close it, only moves it. A second `og start herdr` in this directory
        could replace the record in that window; the bridge would then claim the
        OLD one, and the save that follows would write out a `pending` map with
        no record for that directory at all, so the space the operator had just
        been given a pane in would belong to nothing and the session it was
        started for would open a second space. So the re-read, the decision and
        the write all happen inside one `state_update` (see `state_lock`), and the
        launcher takes the same lock on its side (og_herdr_launch._record): the
        bridge claims the record it could see, and a launcher blocked behind the
        lock lands its own record on top of the committed claim instead of
        being erased by it.

        Which is also why the claim writes HERE rather than at the end of
        `_existing_space`. It is the last point where the record can be moved
        atomically; by the time that method saves, it has asked herdr for the
        workspace listing and maybe renamed a space, and everything in that gap
        is outside the lock. A dry run consumes NOTHING — not the write, and not
        the move either, which is the half that used to be missing here.
        """
        if not cwd:
            return None
        claimed: dict | None = None

        def consume(state: dict) -> dict | None:
            nonlocal claimed
            self._fold_state(state)
            record = self._pending.get(cwd)
            if record is None:
                return None
            if self.dry_run:
                # DECIDED BEFORE THE MOVE, not after it. A dry run that moved
                # the record and then declined to write would leave `_pending`
                # one key short and `_spaces` one key long with no save, and
                # `_synced` still holding the old shape — so the next
                # `_fold_state` reads that as a COMPLETED claim, memory wins
                # (`_memory_decides`, third case), and the pending record is
                # never resurrected. A run that changed nothing would have
                # durably removed a real launcher record from this bridge's
                # view, and the next real session in that directory would open a
                # second space. The same defect a dry-run `--cleanup` guards
                # against, arriving through the claim.
                #
                # A COPY, for the same reason one level deeper: the record stays
                # where it is in `_pending`, so handing out the live object
                # would let a caller's in-place edit (`_existing_space`
                # refreshing `record["cwd"]`) write through into memory the dry
                # run is supposed to have left alone. It is still the record it
                # would have claimed, which is all the dry-run line needs.
                claimed = _copy_record(record)
                return None
            self._pending.pop(cwd, None)
            self._spaces[sid] = record
            claimed = record
            return {"spaces": self._spaces, "pending": self._pending}

        state_update(self.state_path, consume, on_saved=self._mark_synced)
        return claimed

    def _rename_own_space(self, sid: str, rec: dict, title: str) -> list:
        """Rename the space a session is NAMED FOR when its title arrives.

        The gap `_rename_space`'s old docstring denied. A space this bridge
        CREATES is labelled `title or sid[:8]` (`_add_space`), and Omnigent
        titles a conversation only once it has a first message — so a space
        opened for a brand-new session is created with the first eight
        characters of the session id, and, before this, kept that string for the
        life of the session. The title arrives on the `changed` event that
        follows, and this brings the space's label up to it then. It is not a
        race and not an adoption-only concern: it is the ordinary path for every
        new session.

        Only a space this session is the SUBJECT of is renamed — never the space
        a worker's tab lives in. The test is the same one `_removed` uses for
        whose space a record names: `spaces[sid]` has to point at this record's
        workspace. A sub-agent's record names its PARENT's workspace (`_add_tab`)
        and has no `spaces` entry of its own, so without that test a worker's
        title arriving would rename the operator's whole conversation to
        `fix-space-rename`.

        The label to compare against comes from the per-PASS workspace listing
        (`_workspace_index`, cached for the whole pass), never a fresh listing
        per event. When that listing could not be read there is no label to
        compare against, and the rename is attempted anyway rather than skipped:
        the title does not arrive twice (`title_changed` is a one-shot on the
        event that carries it), so a skipped rename here is exactly the `sid[:8]`
        label surviving — the bug this exists to fix.

        Returns the lines to ADD to the event's own — empty unless the rename
        failed, and the failure is RECORDED here rather than raised, as
        `_reject_partial_create` does for its cleanup. The rename is an ADDITION
        to `_changed`'s work: `report_agent` and `report_metadata` ARE that work
        and are allowed to fail the event, but a cosmetic space label is not, so
        an addition failing must not discard the bookkeeping of the work that
        already succeeded. That bookkeeping is `rec["title"]` — persisted and
        read back across a restart, so leaving it stale makes the record
        disagree with the session it names, past the life of the process — and
        the event's own `update …` line, this bridge's audit trail.

        ONE attempt, never a retry, and that is by construction rather than
        oversight: `title_changed` is derived from the watcher's `previous`, not
        from `rec`, so once the watcher has seen the new title the event does
        not recur and a failed rename is not re-attempted — the space keeps its
        `sid[:8]` label for the life of the session. Repeating a failing call on
        every poll is the shape the dead-workspace repair already had to stop
        (see `_reproject`), and a label is not worth re-earning that lesson for.
        Absorbing it hides nothing about a dead space either: a workspace that
        is gone took its panes with it, so `report_agent` above — which runs
        first and DOES propagate — is what reports it and drives the repair.

        The space's first TAB is relabelled in the same breath and with the same
        absorb-and-record shape (see `_rename_first_tab`): `workspace.create`
        cannot label the tab it makes, so the root's tab carries the space's own
        label from `_add_space`/the launcher and must move onto the title here
        with the space. A tab rename that fails is a second line in the same
        return, never a second failure mode.
        """
        ws_id = rec.get("workspace_id")
        own = self._spaces.get(sid)
        if not own or own.get("workspace_id") != ws_id:
            return []
        index = self._workspace_index()
        info = index.get(ws_id) if index is not None else None
        lines: list[str] = []
        try:
            self._rename_space(ws_id, title, info or {})
        except _error_cls() as exc:
            lines.append(_error_line(sid, exc))
        # ...and the space's FIRST TAB follows it. `workspace.create` labels only
        # the space, so the root's tab was opened unlabelled (herdr shows its tab
        # number) and the launcher's own create labels it from the directory —
        # either way it must converge onto the title here, or the operator is left
        # with a correctly-named space holding a tab still called `1`.
        lines.extend(self._rename_first_tab(sid, rec.get("tab_id"), title))
        return lines

    def _rename_first_tab(self, sid: str, tab_id, label) -> list:
        """Give a space's first tab the space's own label, absorbing failure.

        herdr's `workspace.create` labels the WORKSPACE only — its `label` is the
        workspace's, and it has no field for the tab's — so the first tab a create
        makes implicitly is UNLABELLED and herdr falls back to the tab number:
        the bare `1` the operator sees. `tab.create` carries its own `label`,
        which is why a worker's tab already reads a real name. This is the call
        that closes the gap for the root tab.

        Renamed by the id that came back from the create (`_add_space`,
        `_add_history_space`, and the launcher's own record carried through
        `_adopt`) or the id the record already carries (`_rename_own_space`) —
        never by position. herdr tab ids are stable across a reorder, so the
        session's own tab is relabelled wherever the operator has dragged it and
        the WRONG tab is never touched. A tab the operator has since CLOSED
        answers `tab_not_found`, which is ABSORBED here rather than raised: the
        label is cosmetic, and `reconcile` treats a raised `*_not_found` as a
        stale record and repairs the session — a repair driven by a tab label
        would re-open a pane over a cosmetic failure, which is a far worse
        outcome than a tab that keeps its number. The same absorb-and-record
        shape `_rename_own_space` chose for the space label, kept consistent.

        Returns the lines to add — empty on success. A missing `tab_id` (an
        upgraded v1 record carries none) or a missing label is a no-op: there is
        nothing named to rename.
        """
        if not tab_id or not label:
            return []
        try:
            self.client.tab_rename(tab_id, label)
        except _error_cls() as exc:
            return [_error_line(sid, exc)]
        return []

    def _rename_space(self, ws_id: str, title, info: dict) -> None:
        """Rename a space to the session title, when the label differs.

        The launcher labels a space from the DIRECTORY because no session
        existed when it made one — there was no title to use — and the rename IS
        that handoff. A space this bridge CREATES is labelled `title or sid[:8]`
        (`_add_space`) for the same reason: a conversation is titled only once
        it has a message, so a space opened for a brand-new session starts as
        the id prefix and wants the identical rename the moment Omnigent names
        the conversation. So this is called for a taken-over space AND for one
        this process opened — see `_rename_own_space`, which is how.

        Skipped when the label already matches (so a re-delivered title is not a
        `workspace.rename` of pure noise) and when there is no title yet. A
        label that DIFFERS is replaced, including one the operator set by hand:
        the space is a projection of the conversation and the conversation's
        name is authoritative, which is already the rule on the adoption path —
        and across a restart the bridge cannot tell a hand-set label from its
        own stale one, so honouring it would make the space's name depend on the
        order events happened to arrive in.
        """
        if not title or info.get("label") == title:
            return
        # Through the client's NAMED wrapper, not the generic `call`: the schema
        # conformance guard in tests/test_og_herdr_client.py enumerates wrappers
        # by introspection, so a frame sent via `call` is invisible to it — which
        # is how `pane.run`, a method herdr never had, survived 223 tests.
        self.client.workspace_rename(ws_id, title)

    def _workspace_index(self) -> dict | None:
        """`{workspace_id: info}` from workspace.list, once per reconcile pass.

        Returns None when the listing could not be read — which is NOT the same
        as `{}`, which means herdr was asked and answered that it has no
        workspaces at all. That distinction is the difference between "this
        space is gone" and "we do not know", and only herdr can tell them
        apart; `space_status` names the three answers and both this module and
        the launcher read them through it. Callers treat None as UNCONFIRMED:
        nothing is adopted, nothing is claimed, nothing is pruned, and no
        recorded space is repaired away.

        That is the conservative direction, and deliberately so. Treating
        unknown as absent would turn one transient herdr hiccup into a
        duplicate space for every live session. A duplicate is visible and
        closable; a session that silently lost its space is a conversation the
        operator cannot see at all.

        A `--dry-run` never calls herdr, so it has no listing at all and gets
        the same UNCONFIRMED — which is what it actually knows. It used to get
        `{}`, which reads as "herdr says it has nothing", and that is exactly
        the third-answer-collapsed-into-the-second mistake.
        """
        if self._ws_index is not None:
            return self._ws_index if self._ws_ok else None
        if self.dry_run or self.client is None:
            self._ws_index, self._ws_ok = {}, False
            return None
        try:
            spaces = self.client.workspace_list()
        except _error_cls() as exc:
            self._ws_index, self._ws_ok = {}, False
            self._ws_error = "error: cannot list workspaces: {0}: {1}".format(
                getattr(exc, "code", "herdr-error"),
                getattr(exc, "message", exc))
            return None
        index = workspace_index(spaces)
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

    def _close_orphan_spaces(self, live_ids) -> list:
        """Close recorded spaces whose session is no longer live.

        Mirrors the existence rule of `_prune_pending`: a space in `spaces`
        whose session id is absent from `live_ids` is orphaned — its session
        died without a `removed` event this pass (a close that failed last
        time and left the record, or a session gone before the bridge learned
        otherwise). Each absent id is routed through `_removed`, so the
        ownership guard it already carries (root vs worker, launcher-owned or
        not) and the close/forget/drop-children cleanup are reused rather than
        re-implemented here.

        `live_ids` is None when the live listing could not be read — and that
        is the one way this goes catastrophically wrong: a shrug must never
        read as "every session is gone", or this closes every space the
        operator has. None (and `dry_run`) sweeps nothing, the same safety
        `_prune_pending` leans on `_workspace_index` for.

        `_removed` acts only on `_recs`, which is rebuilt by every process, so
        an orphan this process never projected had nothing for it to act on and
        was silently skipped — see `_hydrate_for_close`, which is what makes the
        case that matters here (a session deleted while the bridge was down, then
        a restart) reachable at all rather than merely correct in the abstract.
        """
        if self.dry_run or live_ids is None or not self._spaces:
            return []
        gone = [sid for sid in self._spaces if sid not in live_ids]
        if not gone:
            return []
        lines: list[str] = []
        for sid in list(gone):
            hydrated = self._hydrate_for_close(sid)
            try:
                lines.extend(self._removed(sid))
            except _error_cls() as exc:
                # A failed close must not take the sweep down: keep the record
                # and retry next pass, exactly as `_removed`'s own caller would.
                lines.append(_error_line(sid, exc))
            finally:
                # The handle is this call's alone and does not outlive it, even
                # when the close failed. `_removed` keeps its record on purpose,
                # so retrying means re-hydrating from `_spaces` next pass — and
                # a hydrated record left in `_recs` would be read by `_added` as
                # "already projected", silently swallowing a session that is
                # live and unprojected.
                self._discard_hydrated(sid, hydrated)
        return lines

    def _hydrate_for_close(self, sid: str) -> dict | None:
        """The smallest `_recs` entry that lets `_removed` close this space, or
        None when there is nothing to hydrate.

        WHY THIS EXISTS. The two halves of what a bridge knows do not survive
        equally well. `_spaces` is the durable record — it is written to the
        state file and read back by the next process. `_recs` holds the live
        HANDLES (the agent string, the parent, how far through setup a pane got)
        and is rebuilt from nothing on every start; it is filled only when a
        session is seen and adopted. So the sweep, which finds orphans by
        scanning `_spaces`, and `_removed`, which acts only on `_recs`, agree
        within a process and disagree at exactly one moment: the restart. An
        operator who deletes a session in the web UI while the bridge is down
        gets exactly that moment — the space is recorded, the session is gone
        from the very first listing, and the sweep hands `_removed` a session it
        has no handle for, which answers nothing and closes nothing. The sweep
        ran, found the orphan, and declined: the same outcome as never running,
        on the one path that needed it.

        Built from `_spaces` and nothing else, so a space og never recorded can
        never be closed by this — the sweep only ever asks about ids it is
        already holding a record for. `owner` becomes `owns_space` through the
        same `BRIDGE_OWNED` rule `_adopt` uses, so a record rebuilt for a close
        decides the same thing the record would have decided had this process
        projected the session itself. Deriving it a second, differently, is how
        a launcher space would start being closed whole.

        NOT rebuilt: the agent string, the parent, and the setup step. The state
        file carries none of them, and inventing a value for a process this one
        cannot see would be a guess dressed as a fact. Each is read by exactly
        one caller and none of them is `_removed`; the agent's absence costs the
        release below and nothing else.

        Returns the record it made so the caller can drop it again, and None
        when `_recs` already holds a real record (this process projected the
        session, so there is nothing to rebuild) or when the state-file entry
        names no workspace — a close aimed at nothing is not a close.
        """
        if sid in self._recs:
            return None
        record = self._spaces.get(sid)
        if not isinstance(record, dict):
            return None
        ws_id = record.get("workspace_id")
        if not isinstance(ws_id, str) or not ws_id:
            return None
        hydrated = {
            "workspace_id": ws_id,
            "tab_id": record.get("tab_id"),
            "pane_id": record.get("pane_id"),
            "title": None,
            # The state file does not carry the agent string, and `_removed`
            # releases the agent marker only when it can name one — see there.
            "agent": None,
            # Only a ROOT is recorded in `_spaces`: a sub-agent's own tab is
            # deliberately not (see `_add_tab`), so this is not a guess about
            # the parent, it is the only shape a record here can have.
            "parent": None,
            "owns_space": record.get("owner") in BRIDGE_OWNED,
        }
        self._recs[sid] = hydrated
        return hydrated

    def _discard_hydrated(self, sid: str, hydrated: dict | None) -> None:
        """Drop a record `_hydrate_for_close` made, and only that one.

        The identity check is the whole guard: a real record that appeared
        against this sid mid-pass (an `added` cannot interleave — the sweep runs
        before the batch — but a record written by a later pass certainly can)
        must not be dropped by a cleanup meant for a temporary handle. Cheap
        enough to be worth saying out loud.
        """
        if hydrated is not None and self._recs.get(sid) is hydrated:
            self._recs.pop(sid, None)

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

        THE PARENT'S SPACE IS RE-VALIDATED HERE, every time, before it is used —
        and this is the half of the guard the bridge did not have. A ROOT's
        record is validated when it adopts one (`_existing_space`), but a
        sub-agent's is a lookup in `_recs`, and that map was written once at
        `workspace.create` and never checked again. So the operator closed the
        conversation's space — or `og start <mux>` case (C) closed the pane that
        happened to be the last one in it, and closing a workspace's last pane
        removes the workspace (measured) — and every worker delegated after that
        was handed a dead id:

            error f193205d…: workspace_not_found: workspace wV not found
            error 5b011e8e…: workspace_not_found: workspace wV not found
            error 3f3a1c0a…: workspace_not_found: workspace wV not found

        one per worker, once, and the root's own pane reporting
        `pane_not_found` on every poll after it. `space_status` says what to do
        with the answer, and a GONE verdict repairs the parent (`_reproject`) so
        the worker lands in a space that exists. An UNCONFIRMED verdict changes
        nothing: the recorded id is used as it always was, because an unreadable
        listing is not evidence and inventing a second space over a live one is
        the worse mistake (see `space_status`).
        """
        if not parent:
            return None
        rec = self._recs.get(parent)
        if not rec:
            return None
        ws_id = rec.get("workspace_id")
        if space_status(ws_id, self._workspace_index()) == SPACE_GONE:
            return self._reproject(parent)
        return ws_id

    def _reproject(self, sid: str, session: dict | None = None) -> str | None:
        """Give a session a pane again after herdr said the old one is gone.

        THE ONGOING FAILURE, as distinct from the first one. The guard in
        `_space_of` and `_existing_space` stops a dead id being USED, but a
        record can also go stale between two events — herdr restarts and re-mints
        ids, the operator closes one tab out of a space that stays — and then the
        error arrives from herdr instead of from the listing. Before this, that
        error was logged and nothing else: the record survived it, so every
        later poll for the same session repeated it, forever, silently. Nine
        identical `pane_not_found` lines for one root is what it looked like.

        So a `*_not_found` from herdr means the record is stale NOW, and the
        repair is: forget the record, THEN project the session again from what
        is still known about it. The order is the contract, not a detail —
        dropping it afterwards would leave the dead record in place whenever the
        re-projection itself fails, which is precisely the failure this exists
        to stop: the next poll would report against the same pane herdr has
        never heard of, and say it again.

        Deliberately ONE attempt — the re-projection goes through the ordinary
        `_add_space` / `_add_tab`, so a herdr that is still broken raises out of
        here into `reconcile`'s handler, is said once, and the record stays
        dropped. A session with no record is ignored by `changed` and `removed`,
        so there is nothing left to repeat.

        A record is not required. `reconcile` calls this for any `*_not_found`,
        including one from the create call itself, which fails BEFORE anything is
        recorded (by design — see `_reject_partial_create`). There the event's
        own session is the only description of the session there is, and the
        re-projection is built from that.

        Where the session goes:
          a WORKER returns to its parent's space, which `_space_of` has just
            re-validated — and repairs in turn if that one is gone too, so a
            whole crew moves together rather than one worker at a time;
          a ROOT keeps its own space while herdr still lists it (the operator
            may have closed only the tab, and a second space for one
            conversation is the mistake this whole file is careful about) and
            gets a brand-new one when the space itself is gone.

        Returns the workspace the session now lives in, or None if it could not
        be projected at all — which the caller reads as "this session has no
        space", exactly as for any other reason there is none.
        """
        rec = self._recs.get(sid)
        session = session if isinstance(session, dict) else {}
        if rec is None and not session:
            return None
        if self.dry_run:
            # A dry run creates nothing, so there is nothing to repair and its
            # lines have to describe a run it is not performing.
            return (rec or {}).get("workspace_id")
        # FORGET IT FIRST, before anything that can fail — see the docstring.
        # `rec` is a local from here on, so the rest of the repair can still read
        # what the stale record said.
        self._recs.pop(sid, None)
        parent = (rec or {}).get("parent") or _parent_of(session)
        # The re-projection needs what `_added` needs. An `added` event carries
        # the DETAIL row and so the title and the directory; a `changed` event
        # carries the LISTING row, which has neither `harness` nor
        # `workspace` — so the record's own copy of both is the fallback. That
        # is why `_add_space`/`_add_tab` cache `cwd` on it: the event that
        # triggers a repair is often the one shape that cannot supply it.
        title = session.get("title") or (rec or {}).get("title")
        agent = (rec or {}).get("agent") or agent_label(session)
        cwd = session.get("workspace") or (rec or {}).get("cwd") or self.cwd
        state = _state(session)
        command = attach_command(self.server, sid)
        # A HISTORY space keeps its treatment through a repair: its pane must
        # still be handed the resume line UNSENT, never a submitted attach (which
        # would run on a session nobody is watching). Read from the RECORD, not
        # from `session`: the event that triggers a repair is usually a `changed`,
        # whose LISTING row carries no `runner_online` — the one signal
        # `_is_history_root` needs — so the record's own flag is the only reliable
        # source here.
        history = bool((rec or {}).get("history"))

        if parent is None:
            recorded_ws = rec.get("workspace_id") if rec else None
            if space_status(recorded_ws, self._workspace_index()) != SPACE_GONE:
                # The space is still there (or we cannot tell), so the pane is
                # the only thing that died: a new TAB in the same space.
                space = recorded_ws
                stale = "the pane {0} it named".format(
                    (rec or {}).get("pane_id"))
            else:
                self._forget_stale(sid, recorded_ws)
                space = None
                stale = "workspace {0}".format(recorded_ws)
        else:
            space = self._space_of(parent)
            stale = "its own record"

        # The old pane is gone either way, so setup restarts from the beginning
        # on the new one rather than resuming onto a pane herdr has closed.
        # The harness the pane routing reads: the RECORD's first, because the
        # event that triggers a repair is usually a `changed`, whose LISTING row
        # carries no `harness` (exactly why the title and label above do the
        # same).
        harness = (rec or {}).get("harness") or session.get("harness")
        if history and space is None:
            lines = self._add_history_space(sid, title, cwd, agent, state,
                                            harness=harness)
        elif space is None:
            lines = self._add_space(sid, title, cwd, agent, state, command,
                                    parent, harness=harness)
        else:
            lines = self._add_tab(sid, title, space, cwd, agent, state, command,
                                  parent, history=history, harness=harness)
            owned = self._spaces.get(sid) or {}
            if owned.get("workspace_id") == space and \
                    owned.get("owner") in BRIDGE_OWNED:
                # A root put back into its OWN space still owns it: `_add_tab`
                # cannot know that, and a root that forgets it would close its
                # tab instead of its space on removal and leave the space
                # stranded with nothing recording it.
                self._recs[sid]["owns_space"] = True
        # Pass notes rather than the event's lines: this is work the event did
        # not ask for — it belongs to a DIFFERENT session — and the same place
        # `_existing_space` reports a record it dropped.
        self._pass_notes.append(
            "state: {0} could not be projected — herdr has no {1} any more — "
            "so it was projected again rather than the error repeated on "
            "every poll".format(sid, stale)
            if rec else
            "state: {0} could not be projected at all; tried once more rather "
            "than repeating the error on every poll".format(sid))
        self._pass_notes.extend(lines)
        return self._recs.get(sid, {}).get("workspace_id")

    def _forget_stale(self, sid: str, ws_id: str | None) -> None:
        """Drop a record for a workspace herdr no longer lists, in both halves.

        Memory and the file, because a record that survives in only one of them
        is the shape this file already shipped once (the v1 map that was updated
        in one place and read in another): the next restart would make the same
        decision against the same dead id.
        """
        self._recs.pop(sid, None)
        if ws_id:
            self._drop_space_record(sid, ws_id)

    def _add_space(self, sid: str, title, cwd: str, agent: str, state: str,
                   command: str, parent: str | None,
                   harness: str | None = None) -> list:
        """Open a workspace for a session, and record it as ours.

        One `workspace.create` brings the space AND its first tab together —
        the live reply carries `workspace`, `tab` and `root_pane` — so the
        root's own pane costs no second call.

        `focus` is False and must stay False. Several sessions routinely appear
        in a single poll, and each of them asking for focus would rip the
        operator out of whatever they were typing, once per session, forever.
        The bridge reports; it does not take over the screen.

        `env` is `self.pane_env` for the same reason `focus` is False: the pane
        this creates is the operator's to work in, and a browser opening out of
        it takes the screen away from them (see `pane_env_for`).
        """
        label = title or sid[:8]
        result = self.client.workspace_create(
            label=label, cwd=cwd, focus=False, env=self.pane_env)
        ws_id = _id_of(result.get("workspace"), "workspace_id")
        tab_id = _id_of(result.get("tab"), "tab_id")
        pane_id = _id_of(result.get("root_pane"), "pane_id")
        if ws_id is None or tab_id is None or pane_id is None:
            return self._reject_partial_create(sid, ws_id, tab_id, pane_id)
        rec = {"workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
               "title": title, "agent": agent, "parent": parent,
               "owns_space": True, "step": 0, "ready": False, "cwd": cwd,
               "harness": harness}
        self._recs[sid] = rec
        self._remember(sid, ws_id, tab_id=tab_id, pane_id=pane_id, cwd=cwd)
        # The pass's listing is a SNAPSHOT, taken before this space existed, so
        # it must not be believed for the rest of the pass: the very next
        # `_space_of` in this batch asks it whether the ROOT's space is still
        # there, and a snapshot taken thirty milliseconds earlier would answer
        # no — re-projecting a space the bridge itself just opened, which is the
        # duplicate-space bug with one more step to reach it. Dropped rather than
        # amended, because the honest content of a row for a workspace created
        # here is unknown (`pane_count` decides whether an adopted record's pane
        # can be resumed on) and guessing it would be inventing a fact.
        self._ws_index = None
        labelled = self._rename_first_tab(sid, tab_id, label)
        self._setup_pane(sid, rec, state)
        return [f"add {sid} → space {ws_id} tab {tab_id} pane {pane_id} "
                f"'{title}' '{command}' [{state}]"] + labelled

    def _add_tab(self, sid: str, title, space: str, cwd: str, agent: str,
                 state: str, command: str, parent: str | None,
                 history: bool = False, harness: str | None = None) -> list:
        """Put a sub-agent's pane in a TAB inside its parent's space.

        This is the whole point of the layout: the operator clicks one space and
        sees that conversation's own agent plus every worker it delegated. A
        sub-agent with a space of its own would only be correct when its parent
        has none, and then it is `_add_space`'s job, not this one's.

        `history` is False for every ordinary sub-agent and True only for a
        HISTORY root being repaired back into its own surviving space (see
        `_reproject`): it is carried onto the record so `_setup_pane` hands the
        pane the resume line unsubmitted rather than a submitted attach.

        A sub-agent's worker pane is the operator's window onto that worker, and
        it is opened exactly like a root's — so it carries `self.pane_env` too,
        from the same `pane_env_for`. It used not to: the launcher threaded the
        map through all three of its own creates and this one threaded nothing,
        which is how a fan-out of workers put panes on the screen that would
        open a browser.
        """
        result = self.client.tab_create(space, cwd=cwd,
                                        label=title or sid[:8], focus=False,
                                        env=self.pane_env)
        tab_id = _id_of(result.get("tab"), "tab_id")
        pane_id = _id_of(result.get("root_pane"), "pane_id")
        if tab_id is None or pane_id is None:
            return self._reject_partial_create(sid, None, tab_id, pane_id)
        rec = {"workspace_id": space, "tab_id": tab_id, "pane_id": pane_id,
               "title": title, "agent": agent, "parent": parent,
               "owns_space": False, "step": 0, "ready": False, "cwd": cwd,
               "harness": harness,
               **({"history": True} if history else {})}
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

    def _pane_command(self, sid: str, rec: dict) -> tuple:
        """`(command, role)` for the tab this create just made.

        THE MAIN PANE IS NEVER REPURPOSED. A ROOT's own tab is the
        conversation's Omnigent view — the launcher's `og chat` for a space it
        opened, `omnigent attach` for one the bridge opened — and it stays that
        way. The operator's layout (the chat, the agents pane beside it) is not
        this feature's to rearrange. `TAB_ROLE_MAIN` records it as such, and
        the root's real vendor CLI gets its OWN tab, opened on demand by
        `og_herdr_launch.focus_agent_terminal` (see `TAB_ROLE_CLI`).

        A SUB-AGENT's tab is a per-agent tab and nothing else, so for a harness
        Omnigent hosts as a real terminal TUI (`kind: native` — claude, codex, …)
        it runs that agent's vendor CLI, attached INTERACTIVELY to Omnigent's own
        private tmux server. That is where a blocking question — a permission
        prompt — is actually answered, and `og chat` has no way to answer one the
        vendor CLI is holding.

        An ACP harness is a plain stdio JSON-RPC child with NO pty, so it has no
        terminal to attach to BY CONSTRUCTION; its tab keeps `omnigent attach`,
        which is the agent's own og chat view and the same one `og chat`'s
        down-arrow menu gives. `None` from `harness_has_terminal` — an unknown
        harness — is the THIRD answer and is deliberately not collapsed into
        either branch: the tab keeps the attach REPL, which is always valid, and
        no promise about a vendor CLI is made.

        Best effort by construction. Every failure here — no terminal
        resolvable, no tmux on PATH, the launcher module unavailable — falls back
        to the attach command rather than leaving a pane with nothing running in
        it, which is the one outcome `_open_space` refuses to leave behind.

        `rec["harness"]` is the session's own harness id, taken from the detail
        row the watcher enriches every `added` with (measured: a sub-agent's
        detail row carries `harness` just as a root's does). An absent one means
        a session dict from some other source, which resolves as "unknown".
        """
        attach = attach_command(self.server, sid)
        # A root's tab IS the main pane (see above); nothing else is decided here.
        if rec.get("parent") is None:
            return attach, TAB_ROLE_MAIN
        if harness_has_terminal(rec.get("harness")) is not True:
            return attach, TAB_ROLE_MAIN
        if shutil.which("tmux") is None:
            return attach, TAB_ROLE_MAIN
        try:
            # Imported HERE, not at module load: the launcher imports this
            # module, so this module must import without it (see `main`).
            from og_herdr_launch import (read_only_attach_command,
                                         terminal_behind)
        except Exception:
            return attach, TAB_ROLE_MAIN
        try:
            # Deliberately WITHOUT this bridge's watcher. `SessionWatcher.
            # _get_json` is the transport every listing read in this process
            # shares, so a terminal lookup riding it would count as a POLL — and
            # against a test's scripted opener it would consume a scripted
            # response and change what the NEXT poll sees. The lookup therefore
            # opens its own bounded connection (see og_herdr_launch.
            # RUNNER_TIMEOUT_SECONDS) and the poll count stays exactly what it
            # was.
            terminal = terminal_behind(sid, dict(os.environ))
        except Exception:
            return attach, TAB_ROLE_MAIN
        if terminal is None:
            return attach, TAB_ROLE_MAIN
        return read_only_attach_command(*terminal), TAB_ROLE_CLI

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

        NOTHING here carries a pane environment, and that is not an omission:
        herdr's `pane.send_text` has no `env` to carry (see `pane_env_for`), and
        the pane these type into was created with the map already installed, so
        the command runs in a shell that already has it.

        The one other variation is step 0 for a HISTORY space: its pane is handed
        the resume line UNSUBMITTED rather than a submitted attach, so a past
        conversation is not resurrected (see `_add_history_space`). Steps 1 and 2
        are the same, so a history space's record is an ordinary one and title
        changes, adoption and removal all take the ordinary path.
        """
        pane_id = rec["pane_id"]
        if rec["step"] <= 0:
            if rec.get("history"):
                # A past conversation's pane gets the resume line UNSENT: typed
                # into the shell, no trailing newline, so nothing runs and the
                # session is not resurrected until the operator presses Enter.
                # `pane_run` would append that newline and start it (see
                # `_add_history_space`).
                command, role = resume_command(sid), TAB_ROLE_MAIN
            else:
                command, role = self._pane_command(sid, rec)
            rec["tab_role"] = role
            rec["pane_command"] = command
            # The tab is noted BEFORE the line is typed into it: the tab id comes
            # out of the create reply and is the only place herdr ever tells us
            # which tab a session's pane landed in (`agent.get` cannot be asked,
            # from a session id, later — see `remember_agent_tab`), so the note
            # must not depend on the pane's command succeeding. The ROLE is what
            # lets Enter tell an agent's vendor-CLI tab from its Omnigent one.
            remember_agent_tab(self.agent_tabs_path, sid, rec.get("tab_id"),
                               rec.get("workspace_id"), role=role)
            if rec.get("history"):
                self.client.pane_send_text(pane_id, command)
            else:
                self.client.pane_run(pane_id, command)
            rec["step"] = 1
        if rec["step"] <= 1:
            # The agent label is the session's harness product name ("Claude
            # Code"), stored on the record rather than re-derived per step, so
            # the release on removal matches exactly what was reported. herdr
            # matches the two by that string; a release naming something else
            # would leave the marker up.
            #
            # `agent_session_id` is the Omnigent session id, and it is sent so
            # the record can be focused later: `agent.focus {target}` resolves a
            # target against a record's id, and the `og agents` pane focuses the
            # session this id names — a ROOT, since only a root gets a pane (and
            # so a record) of its own; Enter on a worker aims at that worker's
            # ROOT for exactly this reason. Without the id the record carries
            # nothing for a target to match, so that pane could never bring this
            # agent's space forward.
            self.client.report_agent(pane_id, self.source, agent=rec["agent"],
                                     state=state, agent_session_id=sid)
            rec["step"] = 2
        if rec["step"] <= 2:
            # `pane.report_metadata` with NOTHING to set is refused by herdr:
            #
            #     invalid_metadata_request: missing metadata field to set or clear
            #
            # measured in the live bridge log on a root whose session had no
            # title yet — a fresh `og chat` conversation has `title: None` until
            # its first message, and `_drop_none` then strips the only field
            # that was going to be set, leaving a frame with `pane_id` and
            # `source` and nothing else. Those are the only two params herdr
            # REQUIRES (PaneReportMetadataParams), which is why the frame is
            # schema-valid and still rejected: the schema cannot say "you must
            # mean something by this".
            #
            # So there is no field to set, and the call is skipped rather than
            # sent empty. The step still advances: a later `added` must not come
            # back and send the same rejected frame, and the title itself is
            # reported by `_changed` the moment there is one.
            if rec["title"]:
                self.client.report_metadata(pane_id, self.source,
                                            title=rec["title"])
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
        #
        # Re-sent every time so the record keeps the id that `agent.focus` matches
        # on — the Omnigent session id of the ROOT this pane belongs to, which is
        # what the `og agents` pane resolves a row to and sends as the target (a
        # worker's OWN id has no record; see `focus_target`).
        self.client.report_agent(rec["pane_id"], self.source,
                                 agent=rec["agent"], state=state,
                                 agent_session_id=sid)
        renamed: list[str] = []
        if title_changed:
            # A title that is None is a title that cannot be reported: see
            # `_setup_pane` for the measured refusal herdr answers with a frame
            # carrying nothing but `pane_id` and `source`. `rec["title"]` is
            # still updated below, so the real title is reported by the next
            # event that actually carries one.
            if title:
                self.client.report_metadata(rec["pane_id"], self.source,
                                           title=title)
                # ...and the SPACE's label follows the title too, not just the
                # pane's metadata: a space opened before the conversation had a
                # name is labelled with `sid[:8]` and was left that way for the
                # life of the session. The rename is absorbed rather than
                # raised (see `_rename_own_space`), so neither the metadata call
                # above nor the bookkeeping below depends on it succeeding.
                renamed = self._rename_own_space(sid, rec, title)
        rec["title"] = title
        return [f"update {sid} → state={state}" + (" (title)" if title_changed
                                                   else "")] + renamed

    def _forget_tab(self, sid: str) -> None:
        """Drop this session's entries from the agent-tab sidecar.

        EVERY role goes, because this rides a removal and both tabs it can name
        are about to be closed. Never raises and never fails the removal it rides
        on: an un-dropped note costs a stale tab id that a later `tab.focus`
        would aim at — a click that goes to the wrong tab, not a lost pane — so
        the cheap thing to protect is the sweep, not the file. See
        `forget_agent_tab`.
        """
        forget_agent_tab(self.agent_tabs_path, sid)

    def _close_cli_tab(self, sid: str) -> list:
        """Close the on-demand vendor-CLI tab this agent may have. Returns lines.

        A WORKER's vendor-CLI tab is a tab of its OWN beside the worker's own tab
        in its parent's space, so closing the worker's tab does not take it: it
        has to be closed here or it stays on screen with nothing recording it.
        (For a root the space close already took it, and `tab.close` then answers
        `not_found` — counted as done, the same rule `_removed` uses.)

        Best effort, and a failure is RETURNED rather than raised: a removal that
        dies here would leave the mapping in place and retry forever. It is only
        attempted when a CLI tab is actually noted, so an ordinary removal costs
        no call at all.
        """
        rec = agent_tab_record(self.agent_tabs_path, sid, TAB_ROLE_CLI)
        tab_id = rec.get("tab_id")
        if not tab_id:
            return []
        try:
            self.client.tab_close(tab_id)
        except _error_cls() as exc:
            if getattr(exc, "code", None) == "not_found":
                return []
            return [_error_line(sid, exc)]
        return []

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
                # Its note goes with its pane — the same reason its record does.
                # No `_close_cli_tab` here: the whole SPACE is being closed, so
                # every tab in it, the child's vendor-CLI tab included, is already
                # going with it. See `_forget_tab`.
                self._forget_tab(child_id)
                lines.append(f"remove {child_id} (pane {child.get('pane_id')}; "
                             f"tab closed with its parent's space)")

    def _removed(self, sid: str) -> list:
        rec = self._recs.get(sid)
        if rec is None:
            return []
        owns_space = rec["owns_space"]
        # WHY: a ROOT session owns the space og recorded for it, even when the
        # launcher opened it — that record reads `owner: launcher`, which is
        # what stamps `owns_space` False here. Herdr removes a space with its
        # last tab, so a SINGLE-tab launcher space already died on tab_close;
        # the leak was a launcher space HELD OPEN by worker tabs, because
        # `_drop_children` was skipped when owns_space stayed False and the
        # close never reached `workspace_close`. Forcing the space-wide close
        # on a root makes it take its workers down with it, launcher-owned or
        # not. A WORKER (parent set) is untouched and keeps its tab-only close.
        if rec.get("parent") is None and \
                self._spaces.get(sid, {}).get("workspace_id") == \
                rec["workspace_id"]:
            owns_space = True
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
        #
        # BEST EFFORT WHEN THE STRING IS NOT KNOWN, which is the hydrated case
        # (see `_hydrate_for_close`): the state file records which pane a space
        # belongs to and not which agent claimed it, so after a restart there is
        # nothing to name and `rec["agent"]` would raise a KeyError out of the
        # sweep and kill the daemon. The release is skipped and the close goes
        # ahead, because the close is the operator-visible half and the marker
        # goes with the pane it was drawn on — the very next statement closes the
        # tab or the space that holds that pane. Leaving the operator's workspace
        # open forever, to release a marker nobody will ever see again, is the
        # wrong trade; the reverse — skip the close — is not a trade at all.
        #
        # A record with no PANE id is skipped for the same reason and has always
        # been: a v1-upgraded record names neither tab nor pane, and there is
        # nothing to release either way.
        pane_id = rec["pane_id"]
        agent = rec.get("agent")
        if pane_id and agent:
            try:
                self.client.release_agent(pane_id, self.source, agent=agent)
            except _error_cls() as exc:
                if getattr(exc, "code", None) != "not_found":
                    # Any other error keeps the mapping: popping it here would
                    # leave the pane and tab untracked with no way to retry,
                    # leaking them for good. The error is recorded by
                    # `reconcile` and a later `removed` retries this cleanup.
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
                lines += self._close_cli_tab(sid)
                self._forget_tab(sid)
                return lines
            # As above: any other error keeps the mapping for a later retry.
            raise
        self._recs.pop(sid, None)
        # The panes are gone, so the tab ids this agent was noted under name tabs
        # herdr may already have re-used. Dropped here — after the close landed,
        # never before it — so a failed close (which keeps the mapping for a
        # retry) keeps the notes it will need for that retry. The vendor-CLI tab
        # is closed FIRST: it is a tab of its own, so the close above does not
        # necessarily have taken it with it.
        lines = []
        lines += self._close_cli_tab(sid)
        self._forget_tab(sid)
        lines += [f"remove {sid} (pane {pane_id}, {what} {target})"]
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
    constant that was frozen when the module happened to be imported. The home
    itself is resolved by `default_home`, so this and the agent-spec scan cannot
    disagree about which home they are reading.
    """
    return default_home() / STATE_FILENAME


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
        # `CLI_PANE_FIELD` is listed so the optional CLI-mirror pane SURVIVES a
        # load — it is written through `state_update`, and a field this whitelist
        # omits would be dropped on the very next read. See its definition.
        for field in ("tab_id", "pane_id", "owner", "cwd", CLI_PANE_FIELD):
            value = entry.get(field)
            if isinstance(value, str) and value:
                rec[field] = value
        kept[key] = rec
    return kept


# ---------------------------------------------------------------------------
# which herdr TAB shows which agent: $OMNIGENT_HOME/og-agent-tabs.json
# ---------------------------------------------------------------------------
# WHY A FILE OF ITS OWN. herdr's `agent.get`/`agent.list` DO carry a `tab_id`,
# but herdr exposes no way to travel from an OMNIGENT session id to that record,
# and that is measured rather than assumed (live server, three probes):
#   * `agent.get {target}` resolves a target that is a live agent NAME
#     (registered by `agent.start`) or the PANE id hosting the agent. A session
#     id, a session title, a tab id and a terminal id all answer
#     `agent_not_found`.
#   * the `agent_session_id` that `pane.report_agent` accepts IS stored, but it
#     is surfaced NOWHERE — not in `agent.get`, not in `agent.list`, not in
#     `pane.get` (all three checked).
# So a view cannot look an agent's tab up after the fact; og has to write it
# down at the one moment both ids are in its hands, which is when it creates the
# tab. This file is that note.
#
# A SIDECAR and not a field on the space record, and the reason is destructive:
# `--cleanup` closes the `workspace_id` of every record in the state file, while
# a WORKER's tab belongs to its PARENT's space — recording a worker there would
# put the parent's space in the cleanup set under a second key and make the
# file's answer to "which space is this session's?" ambiguous for every
# adoption. Nothing but this feature reads this file, so adding to it cannot
# change what any existing path does.
AGENT_TABS_FILENAME = "og-agent-tabs.json"

# An agent can have MORE THAN ONE record, and which one Enter should reach
# depends on what the operator asked for — so the note is keyed by ROLE, not
# just by session.
#
#   `main` — the tab holding the agent's OMNIGENT view. For a root that is the
#            conversation's own pane (the launcher's `og chat`, or the
#            `omnigent attach` the bridge opens when it owns the space), which
#            the operator must keep. For a worker it is the tab the bridge
#            creates for it, and on an ACP harness that tab IS the agent's og
#            chat view — the same view `og chat`'s down-arrow menu gives.
#   `cli`  — the tab holding the agent's REAL vendor CLI, attached
#            interactively. Created on demand in the agent's space by
#            `og_herdr_launch.focus_agent_terminal`, never part of the main
#            pane, so a native ROOT can have both.
#
# Without the role there is no way to tell those two apart for one session, and
# Enter on a native root would land on the chat pane — which is exactly the
# wrong tab, and the failure this keying exists to make impossible.
TAB_ROLE_MAIN = "main"
TAB_ROLE_CLI = "cli"
TAB_ROLES = (TAB_ROLE_MAIN, TAB_ROLE_CLI)


def agent_tabs_path(state_path: Path) -> Path:
    """The agent-tab sidecar that belongs beside `state_path`.

    Derived from the state path rather than from `$OMNIGENT_HOME`, so one
    redirect (a sandbox, a test's tmp home, the launcher's own `--state`) moves
    both files together — the reason `launch_log_path` derives from the launch's
    own log directory instead of from $HOME.
    """
    return state_path.parent / AGENT_TABS_FILENAME


def _read_tab_record(entry) -> dict:
    """One `tab_id`/`workspace_id` record out of the sidecar, or `{}`.

    Only a non-empty string `tab_id` is kept: that is what `tab.focus` needs,
    and the workspace id is optional because it is only there to make the note
    readable (`og_herdr_launch` uses it as the space to open a vendor-CLI tab
    in, and knows how to fall back to the state file's record when it is absent).
    """
    if not isinstance(entry, dict):
        return {}
    tab_id = entry.get("tab_id")
    if not isinstance(tab_id, str) or not tab_id:
        return {}
    rec = {"tab_id": tab_id}
    ws_id = entry.get("workspace_id")
    if isinstance(ws_id, str) and ws_id:
        rec["workspace_id"] = ws_id
    return rec


def load_agent_tabs(path: Path) -> dict:
    """`{session_id: {role: {"tab_id":…, "workspace_id":…}}}`, or `{}`.

    The same degradation contract as `load_state`, for the same reason: a
    missing, unreadable or malformed file is "we recorded nothing", which costs
    the operator a click on the tab they wanted — never a pane that fails. Only
    known roles are kept, and a role with no usable tab id is dropped rather
    than carried as a half-record.

    A FLAT entry — `{"tab_id": …}` with no role level — is read as a `main`
    note. Nothing in this tree ever wrote that shape (the role level shipped with
    the feature), but a file that says which tab an agent has should be believed
    for that much: read as "nothing", it would make the next Enter on a native
    agent open a SECOND vendor-CLI tab beside the recorded one.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    tabs: dict = {}
    for sid, entry in data.items():
        if not isinstance(sid, str) or not sid or not isinstance(entry, dict):
            continue
        if "tab_id" in entry:
            flat = _read_tab_record(entry)
            if flat:
                tabs[sid] = {TAB_ROLE_MAIN: flat}
            continue
        roles = {}
        for role in TAB_ROLES:
            rec = _read_tab_record(entry.get(role))
            if rec:
                roles[role] = rec
        if roles:
            tabs[sid] = roles
    return tabs


def agent_tab_record(path: Path, session_id: object,
                     role: str = TAB_ROLE_MAIN) -> dict:
    """The tab record for one agent and one role, or `{}`. Never raises.

    `role` is `TAB_ROLE_MAIN` (the agent's Omnigent view) or `TAB_ROLE_CLI` (the
    vendor-CLI tab created on demand). An unknown role names no tab.
    """
    if not isinstance(session_id, str) or not session_id:
        return {}
    if role not in TAB_ROLES:
        return {}
    return (load_agent_tabs(path).get(session_id) or {}).get(role) or {}


def remember_agent_tab(path: Path, session_id: object, tab_id: object,
                       workspace_id: object = None,
                       role: str = TAB_ROLE_MAIN) -> bool:
    """Record which tab shows this session's pane, under `role`. Never raises.

    One locked read-modify-write, through the SAME `state_lock` the state file
    uses, on a different path — `state_lock` takes any path and locks a sidecar
    beside it, so two bridges on one machine serialise here exactly as they do
    there. `state_update` itself is not reused because it parses the state file's
    `{version, spaces, pending}` shape, and this is a small nested map.

    THE RETURN IS LOAD-BEARING, and this is the PR #38/#39 lesson: a caller that
    creates a tab must know whether the note LANDED. `focus_agent_terminal`
    closes the tab it just opened when this says False, because an on-screen tab
    with nothing recording it is exactly how the next Enter opens a second one.

    Writing one role never disturbs the other: the entry is merged, so a `cli`
    note added later leaves the `main` note (and its workspace id) in place.
    """
    if not isinstance(session_id, str) or not session_id:
        return False
    if not isinstance(tab_id, str) or not tab_id:
        return False
    if role not in TAB_ROLES:
        return False

    def put(tabs: dict) -> dict:
        rec = {"tab_id": tab_id}
        if isinstance(workspace_id, str) and workspace_id:
            rec["workspace_id"] = workspace_id
        entry = tabs.get(session_id)
        if not isinstance(entry, dict):
            entry = {}
        entry[role] = rec
        tabs[session_id] = entry
        return tabs

    return _update_agent_tabs(path, put)


def forget_agent_tab(path: Path, session_id: object,
                     role: object = None) -> bool:
    """Drop a session's tab record — one role, or all of them. Never raises.

    Called when the panes go, so a later Enter on a reaped agent cannot target a
    tab herdr has already reused. `role=None` (the default) drops EVERY role for
    the session, which is what a removal wants: every tab it noted is about to be
    closed. Naming a role drops only that one, leaving the others alone.

    `decide` returns the state to write, or None to write NOTHING: removing the
    last entry leaves `{}`, which is a real state to save (the file must stop
    naming the dead tab) and must not be confused with "nothing to do" — that is
    the one distinction the two return values carry.
    """
    if not isinstance(session_id, str) or not session_id:
        return False
    wanted = TAB_ROLES if role is None else (role,)
    if any(entry not in TAB_ROLES for entry in wanted):
        return False

    def drop(tabs: dict):
        entry = tabs.get(session_id)
        if not isinstance(entry, dict):
            return None
        changed = False
        for name in wanted:
            if entry.pop(name, None) is not None:
                changed = True
        if not entry:
            tabs.pop(session_id, None)
        return tabs if changed else None

    return _update_agent_tabs(path, drop)


def _update_agent_tabs(path: Path, decide) -> bool:
    """One locked read-modify-write of the sidecar. Never raises."""
    try:
        with state_lock(path):
            decided = decide(load_agent_tabs(path))
            if decided is None:
                return False
            return _write_agent_tabs(path, decided)
    except Exception:
        return False


def _write_agent_tabs(path: Path, tabs: dict) -> bool:
    """Write the sidecar atomically. Never raises; False on any failure.

    Atomic via `os.replace` for the reason `_write_state` is: a crash mid-write
    must not leave a file the next read discards as corrupt, which here would
    silently un-remember every agent's tab at once.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".og-agent-tabs")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(tabs, handle, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError):
        return False
    return True


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


# How deep THIS THREAD already is inside `state_lock`, so a write issued from
# inside a transaction does not deadlock against the transaction that issued it.
# The only nesting that exists is `load_state`'s in-place v1 upgrade writing from
# within one, and re-entering `flock` on a second fd in the same process
# DEADLOCKS rather than succeeding (measured, macOS).
#
# Per THREAD, and that is not a refinement: a process-wide counter reads "locked"
# to a second thread in the same process and lets it straight past, which is the
# same loss this lock exists to prevent wearing a disguise. The bridge itself is
# single-threaded (`run_forever` feeds one event at a time) and the launcher is a
# short-lived script, so this costs nothing and keeps the lock honest for the one
# case that can still reach it from inside this process — a test firing a writer
# from a second thread, which is also how the claim's race is proved.
_state_lock_depth = threading.local()


@contextlib.contextmanager
def state_lock(path: Path):
    """Hold `path` against the other writer, for the length of one block.

    THE LOCK THIS FILE DID NOT HAVE, and the reason it is advisory rather than
    absent is a real interleaving rather than a hypothetical one. Both writers
    write the file WHOLE, so every interesting operation is a read-modify-write;
    re-reading immediately before writing (og_herdr_launch._record, and the
    bridge's own merge) narrows that window to microseconds but does not close
    it, and it cannot: a second read only moves the race to the other side of
    itself. The bridge's claim is where that stopped being theoretical — it reads
    the file, decides on the record it found, moves it into `spaces`, and writes
    — so a launcher that replaced `pending[cwd]` inside that span had its record
    erased by the claim's own save, and the space it had just opened for the
    operator ended up claimable by nothing: the next session in that directory
    opened a second one.

    A SIDECAR file, never the state file itself. `save_state` writes through
    `os.replace`, which swaps the INODE, and a lock rides on the inode: taking it
    on `og-herdr.json` would mean the holder and the next arrival lock two
    different inodes and exclude nobody. So the lock lives beside the file, is
    never unlinked (removing a locked file breaks exclusion the same way), and
    outlives every process — an empty `og-herdr.json.lock` in `$OMNIGENT_HOME`
    is the cost of this and nothing else.

    Advisory, and it only binds processes that take it, so a THIRD writer that
    ignores it is still last-writer-wins: that is the file's nature and this does
    not pretend otherwise. Where `fcntl` does not exist, or the sidecar cannot be
    created (an unwritable home — which `save_state` reports as a failed write
    anyway), this yields without locking and the behaviour is the whole-file
    last-writer-wins the docstrings have always described.
    """
    depth = getattr(_state_lock_depth, "depth", 0)
    if fcntl is None or depth:
        yield
        return
    lock_path = path.parent / f"{path.name}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
    except OSError:
        yield
        return
    _state_lock_depth.depth = depth + 1
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
        _state_lock_depth.depth = depth


def _write_state(path: Path, state: dict) -> bool:
    """The write itself, unlocked: `save_state` and `state_update` hold the lock.

    Separate from `save_state` so a transaction can write the state it just
    decided on without re-entering `state_lock` — see `_state_lock_depth` for why
    that would otherwise deadlock rather than merely be redundant.
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
        return False
    return True


def save_state(path: Path, state: dict) -> bool:
    """Write the ownership state, atomically, under the shared lock, and never
    raise at the caller.

    Returns whether the file now holds this state. A failed write is swallowed:
    the worst case is a workspace that `--cleanup` does not know about, which
    the operator can close by hand, and the alternative — refusing to project a
    session because a local file could not be written — trades a cosmetic
    bookkeeping failure for a missing pane.

    The answer matters to exactly one caller: `Bridge._save_state`, which uses
    it to decide whether its in-memory maps are now in step with the file (see
    `Bridge._fold_state`). It is a return rather than an exception because the
    swallowing is deliberate and load-bearing everywhere else.

    A caller that means it — that has decided something about the file's CONTENT
    and wants it kept — wants `state_update`, not this: writing a map read at
    some earlier instant over a file someone else has written since is exactly
    the loss `state_lock` exists to make impossible, and taking the lock around
    only the write does not do it.
    """
    with state_lock(path):
        return _write_state(path, state)


def state_update(path: Path, decide, on_saved=None) -> bool:
    """Read the file, decide about it, and write the answer — as ONE transaction.

    THE READ-MODIFY-WRITE, which is every operation on this file that means
    anything, done while the other writer cannot write. `decide(state)` is handed
    the freshly loaded file and returns the state to write, or a falsy value to
    write nothing at all (a claim that found no record, a dry run that must
    consume nothing). `on_saved` is called only when a write actually LANDED,
    which is what `Bridge._save_state` needs to refresh `_synced`.

    `decide` returns the maps rather than mutating them in place because the
    bridge's answer is not the file's: it folds what it read into its own memory
    and writes that back (see `Bridge._fold_state`). The alternative — mutating
    the loaded dict — would have the bridge writing the launcher's copy of the
    file it had just read, which is the one thing the merge exists to avoid.
    """
    with state_lock(path):
        state = load_state(path)
        decided = decide(state)
        if not decided:
            return False
        landed = _write_state(path, decided)
        if landed and on_saved is not None:
            on_saved()
        return landed


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

    # The two install keys this daemon honours — how many past-conversation
    # spaces to keep open (`herdr_session_space_cap`) and whether a history space
    # opens the `og agents` split (`herdr_agents_pane`) — both from the same
    # og-install.json the launcher reads. Imported HERE, not at module load: this
    # module must import without the launcher — the launcher imports THIS one —
    # and the readers live beside the other config readers in og_herdr_launch.
    # ONE `install_config()` read feeds both, so the two cannot see different
    # files.
    from og_herdr_launch import (agents_pane_enabled, install_config,
                                 session_space_cap)

    config = install_config()
    space_cap = session_space_cap(config)
    agents_pane = agents_pane_enabled(config)

    # Read once and shared: the filer below files against the same server and
    # token the watcher polls, so `_load_token` must not be called twice.
    token = _load_token(args.server)
    watcher = SessionWatcher(base_url=args.server, token=token)
    client = None
    filer = None
    if not args.dry_run:
        # Constructed only for a real run: --dry-run must make zero calls, and
        # building either is the step that could reach a socket or the network.
        from og_herdr_client import HerdrClient
        from og_projects import ProjectFiler

        client = HerdrClient(socket_path=args.socket)
        # Files og's own new root sessions into the install's project folder,
        # against the same server and token the watcher uses.
        filer = ProjectFiler(args.server, token=token)

    bridge = Bridge(watcher, client,
                    dry_run=args.dry_run, source=args.source, cwd=args.cwd,
                    # The same URL the watcher polls, and the one the pane's
                    # `omnigent attach --server` has to name: the pane cannot
                    # discover it (see attach_command).
                    server=args.server, filer=filer, space_cap=space_cap,
                    agents_pane=agents_pane)

    if args.once:
        for line in bridge.run_once():
            print(line)
        return 0
    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
