#!/usr/bin/env python3
"""og start <mux> — run the orchestrator inside a terminal multiplexer.

bin/og has already started the server by the time this runs (the pane runs
`og chat`, which needs a live server, so the order is not negotiable). What is
left is the part that is specific to a multiplexer: open a space for this
directory, put a chat in it, and either attach the TUI or stay in the one we
are already inside.

Four cases, keyed on two facts — are we already inside herdr, and does this
directory already have a live session:

    ┌───────────────┬────────────────┬──────────────────┐
    │               │ session in cwd │ no session       │
    ├───────────────┼────────────────┼──────────────────┤
    │ not in herdr  │ (B) focus +    │ (A) create, then │
    │               │     attach     │     attach       │
    ├───────────────┼────────────────┼──────────────────┤
    │ in herdr      │ (C) focus, then│ (D) create only  │
    │               │   close $PANE  │                  │
    └───────────────┴────────────────┴──────────────────┘

(A) and (D) type `og chat`, which MINTS the session — so at that point there is
no session id to record the space under, and the handshake is a `pending`
record keyed by directory that og_herdr's bridge claims when a session appears
there. (B) and (C) already have one, and adopt.

Everything here is a view. `og start` works without a multiplexer at all, so a
failure to open a space is reported and the og server is left running.

Read by tests through injected fakes: the herdr client, the session watcher and
the spawn of the mux's own server. Nothing in this module connects to a herdr
socket at import time, and no test has to.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from og_herdr import (
    OWNER_LAUNCHER,
    _id_of,
    attach_command,
    default_state_path,
    load_state,
    save_state,
)

# The only multiplexer with a backend. A registry row buys detection and
# documentation; the code behind it is written per multiplexer (see
# docs/MUX-LAUNCHER.md §8), so an id this module does not implement degrades to
# "no multiplexer" rather than to a half-implemented space.
MUX_HERDR = "herdr"

DEFAULT_SERVER = "http://127.0.0.1:6767"

# What the pane runs. Both are og's own entry points, so a pane is not tied to
# herdr — `og agents` in a plain terminal is the same program.
CHAT_COMMAND = "og chat"
AGENTS_COMMAND = "og agents"

# Statuses that mean "this session is the one to come back to". `idle`
# deliberately counts: a root reads idle for the whole time it waits for the
# human to type, which is precisely the session they want returned to. A failed
# or runner-offline session must not block a fresh start.
LIVE_STATUSES = frozenset({"running", "idle"})

# og-install.json keys. Both have a default, so an install that predates them
# still launches.
AGENT_NAME_KEY = "agent_name"
AGENTS_PANE_KEY = "herdr_agents_pane"
DEFAULT_AGENT_NAME = "dev-lead"

# How long to wait for `herdr server` to answer a ping before giving up. Bounded
# on purpose: a launcher that blocks here is an `og start` that never finishes
# and never says why.
SERVER_WAIT_SECONDS = 15.0
SERVER_POLL_SECONDS = 0.25


class LaunchError(Exception):
    """A launch that cannot go on. Reported and non-zero, never a traceback."""


def warn(line: str) -> None:
    print("! " + line)


def info(line: str) -> None:
    print("-> " + line)


# ---------------------------------------------------------------------------
# what the install asked for
# ---------------------------------------------------------------------------

def install_config(home: Optional[Any] = None) -> dict:
    """`~/.omnigent/og-install.json`, or {} when it cannot be read.

    Read from the environment NOW rather than frozen at import, for the same
    reason og_herdr.default_state_path is: a test (or an operator in a sandbox)
    redirects $OMNIGENT_HOME, and a constant cannot follow it.
    """
    base = Path(home or os.environ.get("OMNIGENT_HOME")
                or Path.home() / ".omnigent")
    try:
        data = json.loads((base / "og-install.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def agent_name(config: dict) -> str:
    """The orchestrator bundle's name — the orchestrator TAB's label.

    From the install plan, and defaulting to the same "dev-lead" bin/og uses for
    an unconfigured install, so an install whose plan predates the key still gets
    a labelled tab rather than herdr's default ("1", which the UI hides).
    """
    name = config.get(AGENT_NAME_KEY)
    return name if isinstance(name, str) and name else DEFAULT_AGENT_NAME


def agents_pane_enabled(config: dict) -> bool:
    """Whether to open `og agents` beside the chat, defaulting to TRUE.

    The key is newer than the install that reads it, and an install that predates
    it must still get the layout it was configured for by default — defaulting
    to False would quietly take the split away from everyone who never chose.
    """
    value = config.get(AGENTS_PANE_KEY, True)
    return True if value is None else bool(value)


# ---------------------------------------------------------------------------
# which session, if any, this directory already has
# ---------------------------------------------------------------------------

def live_session(watcher, cwd: str) -> Optional[dict]:
    """The live ROOT session whose detail-row `workspace` is `cwd`, or None.

    Read through `og_herdr_watch.SessionWatcher` rather than by fetching the API
    directly, so pagination, `kind=any`, per-server token matching and the
    runner-offline rule all stay in one place — re-implementing any of them here
    is how a launcher and a bridge start disagreeing about what is live.

    `poll_once` is used rather than `list_sessions` because the directory is on
    the DETAIL row only; poll_once enriches exactly the sessions it has not seen
    before, which for a fresh process is all of them, so it costs one detail
    fetch per live session and then stops.

    A listing that cannot be read is an ERROR, not "no session". Guessing "none"
    here is the one outcome that cannot be undone: it opens a second space and
    types a second `og chat` into a conversation the operator already had.

    The first match wins, which is the newest: the listing is newest-first, and
    a directory with two live roots is a situation to add to, not to arbitrate.
    """
    try:
        events = watcher.poll_once()
    except Exception as exc:  # the watcher is strict by design
        raise LaunchError(
            "could not read the session list from {0}: {1}".format(
                getattr(watcher, "base_url", "the server"), exc))
    for event in events:
        session = getattr(event, "session", None) or {}
        if session.get("parent_session_id"):
            continue  # a delegated worker, not the conversation itself
        if session.get("archived"):
            continue
        if session.get("status") not in LIVE_STATUSES:
            continue
        # The directory is on the DETAIL row, which is why poll_once was used.
        if session.get("workspace") != cwd:
            continue
        return session
    return None


# ---------------------------------------------------------------------------
# the four cases
# ---------------------------------------------------------------------------

@dataclass
class Launch:
    """Everything a launch needs, injected so no test touches a real system."""

    client: Any                      # herdr socket client
    watcher: Any                     # og_herdr_watch.SessionWatcher
    cwd: str
    server: str
    agent: str
    agents_pane: bool
    env: dict
    state_path: Path
    spawn: Callable[[], Any] = None  # start the mux's own server, if it is down
    log_path: Optional[Path] = None

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


def launch(ctx: Launch) -> str:
    """Do the work. Returns "attach" (caller execs the TUI) or "done"."""
    inside = is_inside(ctx.env)
    session = live_session(ctx.watcher, ctx.cwd)
    if inside:
        if session:
            return _close_here(ctx, session)
        return _open_space(ctx, attach=False)
    if session:
        return _attach_there(ctx, session)
    ensure_server(ctx)
    _open_space(ctx, attach=True)
    return "attach"


def is_inside(env: dict) -> bool:
    """Whether this process is already running inside the multiplexer.

    `HERDR_ENV` is the flag herdr sets for every process it spawns;
    `HERDR_WORKSPACE_ID` / `HERDR_PANE_ID` ride along with it and are read
    separately where a pane id is needed. Checking the workspace id as well
    would be redundant for this question and would make "inside" depend on a
    value that a caller may legitimately not have.
    """
    return env.get("HERDR_ENV") == "1"


def _attach_there(ctx: Launch, session: dict) -> str:
    """(B) A session is already live in this directory, and we are outside.

    Put the operator on it instead of starting a second conversation.
    """
    record, adopted = space_for(ctx, session)
    if adopted:
        # `_adopt` created the space with focus=True, so it is already in front;
        # a second workspace.focus would be a redundant round trip. Only an
        # EXISTING space needs to be brought forward.
        return "attach"
    info("session {0} already has a space ({1}); focusing it".format(
        session.get("id"), record["workspace_id"]))
    focus(ctx, record["workspace_id"])
    return "attach"


def _close_here(ctx: Launch, session: dict) -> str:
    """(C) Inside herdr, and this directory already has a session.

    The operator pressed "new space", got a shell, and typed `og start herdr` in
    a directory that already had a conversation. Focus the session's space, then
    close THIS pane — and because closing a workspace's last pane removes the
    workspace (measured), the space herdr just made for them goes with it. No
    orphan space, and no error path.

    Unlike (B), the focus is NOT skipped when the space was just adopted: the
    pane about to be closed lives in a DIFFERENT space, so the target has to be
    brought forward explicitly before the close, not left to create's own focus.
    """
    record, _adopted = space_for(ctx, session)
    focus(ctx, record["workspace_id"])
    pane = ctx.env.get("HERDR_PANE_ID")
    if not pane:
        raise LaunchError(
            "HERDR_PANE_ID is not set, so this pane cannot be closed and the "
            "space herdr opened would be left behind; run `og start herdr` from "
            "a herdr pane")
    info("focusing space {0} and closing this pane ({1})".format(
        record["workspace_id"], pane))
    ctx.client.pane_close(pane)
    return "done"


def _open_space(ctx: Launch, attach: bool) -> str:
    """(A)/(D) A new space for a new conversation.

    The label is the DIRECTORY's basename, because at this point no session
    exists: `og chat` below is what mints one, and og_herdr's bridge renames the
    space to the conversation title once it appears. That rename is the whole
    reason the pending record is written at all.
    """
    label = os.path.basename(ctx.cwd.rstrip(os.sep)) or ctx.cwd
    created = ctx.client.workspace_create(label=label, cwd=ctx.cwd, focus=True)
    if not isinstance(created, dict):
        created = {}
    ws_id = _id_of(created.get("workspace"), "workspace_id")
    tab_id = _id_of(created.get("tab"), "tab_id")
    pane_id = _id_of(created.get("root_pane"), "pane_id")
    # `tab` and `root_pane` come back in the same reply (measured; the schema's
    # own listing of the result omits both) — see og_herdr_client.workspace_create.
    # A reply missing them cannot be half-used: the space would sit there with a
    # shell nobody is typing into, and it is NOT in the state file yet, so
    # `og herdr --cleanup` would not close it either. Closing it here is the only
    # thing that keeps the operator from finding stray empty spaces all week.
    if ws_id and not (tab_id and pane_id):
        try:
            ctx.client.workspace_close(ws_id)
        except Exception as exc:
            warn("could not close the space {0} I just opened ({1}); close it "
                 "by hand".format(ws_id, exc))
        raise LaunchError(
            "workspace.create returned {0}; expected workspace, tab and "
            "root_pane".format(sorted(created) or "an empty result"))
    if not ws_id:
        raise LaunchError(
            "workspace.create returned no workspace id: {0!r}".format(created))

    ctx.client.call("tab.rename", {"tab_id": tab_id, "label": ctx.agent})
    ctx.client.pane_run(pane_id, CHAT_COMMAND)
    if ctx.agents_pane:
        _split_agents(ctx, ws_id, tab_id, pane_id)
    # A pending record, not a space record: the session this space is for does
    # not exist yet, so there is no id to key it by. og_herdr claims it by
    # directory when the session appears.
    _record(ctx, "pending", ctx.cwd, {
        "workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
        "owner": OWNER_LAUNCHER, "cwd": ctx.cwd,
    })
    info("opened space {0} for {1}".format(ws_id, ctx.cwd))
    return "attach" if attach else "done"


def _split_agents(ctx: Launch, ws_id: str, tab_id: str, root_pane: str) -> None:
    """Put `og agents` in a split beside the chat.

    Best effort by design: the chat pane is the session, and losing the whole
    launch because a cosmetic second pane could not be found is the wrong trade.
    """
    try:
        reply = ctx.client.call("pane.split", {
            "target_pane_id": root_pane, "direction": "right",
            "focus": False, "cwd": ctx.cwd,
        })
    except Exception as exc:
        warn("could not open the og agents pane ({0}); the chat is up "
             "without it".format(exc))
        return
    pane_id = _split_pane_id(ctx, ws_id, tab_id, root_pane, reply)
    if not pane_id:
        warn("herdr split the chat pane but reported no new pane id; `og "
             "agents` was not started there")
        return
    try:
        ctx.client.pane_run(pane_id, AGENTS_COMMAND)
    except Exception as exc:
        warn("could not start og agents in the split pane ({0})".format(exc))


def _split_pane_id(ctx: Launch, ws_id: str, tab_id: str, root_pane: str,
                   reply: Any) -> Optional[str]:
    """The pane `pane.split` just made.

    The reply does not name it: the schema's `pane.split` result is `{"type":
    "ok"}` — no pane, no id. So the new pane is found by DIFFERENCE against the
    tab, which is exact while there is exactly one split (there is: the launch
    makes one) and is pinned here so a future second split cannot quietly return
    the wrong pane. A reply that does carry the pane is preferred over the
    listing, since a server that starts naming it should not have its answer
    second-guessed.
    """
    if isinstance(reply, dict):
        found = _id_of(reply, "pane_id") or _id_of(reply.get("pane"), "pane_id")
        if found:
            return found
    try:
        panes = ctx.client.call("pane.list", {"workspace_id": ws_id})
    except Exception:
        return None
    others = sorted(
        str(pane.get("pane_id")) for pane in (panes or {}).get("panes", [])
        if isinstance(pane, dict) and pane.get("tab_id") == tab_id
        and pane.get("pane_id") and pane.get("pane_id") != root_pane)
    return others[0] if others else None


# ---------------------------------------------------------------------------
# adopting a session that has no space yet
# ---------------------------------------------------------------------------

def focus(ctx: Launch, ws_id: str) -> None:
    ctx.client.call("workspace.focus", {"workspace_id": ws_id})


def space_for(ctx: Launch, session: dict) -> tuple:
    """The space this session is on, adopting (creating) one if it has none.

    Returns `(record, adopted)`, where `adopted` is True when a NEW space was
    created because the recorded one — if any — was not in herdr's listing. The
    callers differ on what that means: (B) attaches without a second focus
    (create already focused), (C) focuses either way because it is about to
    close the pane it is in, which lives in a different space.

    `workspace.list` is asked EVERY time, before the state file is believed.
    "nothing recorded" and "herdr has no such space" are different answers, and
    so is "the listing could not be read": the record is a cache, not a source
    of truth (docs/MUX-LAUNCHER §3), so only herdr's own listing can say an id
    is gone. Skipping the listing whenever the file was silent is the same
    three-outcome collapse the bridge already learned to avoid.

    A listing that cannot be read is an error rather than an empty answer: an
    empty one would read as "your space is gone" and adopt a second space over
    the first (see live_workspaces).
    """
    sid = str(session.get("id") or "")
    if not sid:
        raise LaunchError("the session in {0} has no id".format(ctx.cwd))
    record = _load(ctx)["spaces"].get(sid)
    ws_id = (record or {}).get("workspace_id")
    live = live_workspaces(ctx)
    if ws_id and ws_id in live:
        return record, False
    if ws_id:
        info("the recorded space {0} is gone; adopting again".format(ws_id))
    return _adopt(ctx, session), True


def live_workspaces(ctx: Launch) -> set:
    """Every workspace herdr currently lists, by id.

    A listing that cannot be read is an error rather than an empty answer: an
    empty one would read as "your space is gone" and adopt a second space over
    the first.
    """
    try:
        listing = ctx.client.workspace_list()
    except Exception as exc:
        raise LaunchError("could not list herdr workspaces: {0}".format(exc))
    return {str(entry.get("workspace_id")) for entry in (listing or [])
            if isinstance(entry, dict) and entry.get("workspace_id")}


def _adopt(ctx: Launch, session: dict) -> dict:
    """Give an existing session a space, and ATTACH it rather than chat.

    `og chat` would mint a NEW conversation — the one thing adoption exists to
    avoid — so the pane gets `omnigent attach --server <url> <session id>`,
    built by og_herdr so there is one spelling of it.
    """
    sid = str(session.get("id") or "")
    label = session.get("title") or os.path.basename(ctx.cwd.rstrip(os.sep))
    cwd = session.get("workspace") or ctx.cwd
    created = ctx.client.workspace_create(label=str(label), cwd=str(cwd),
                                         focus=True)
    if not isinstance(created, dict):
        created = {}
    ws_id = _id_of(created.get("workspace"), "workspace_id")
    tab_id = _id_of(created.get("tab"), "tab_id")
    pane_id = _id_of(created.get("root_pane"), "pane_id")
    if not (ws_id and tab_id and pane_id):
        if ws_id:
            try:
                ctx.client.workspace_close(ws_id)
            except Exception:
                pass
        raise LaunchError(
            "workspace.create returned {0}; expected workspace, tab and "
            "root_pane".format(sorted(created) or "an empty result"))
    ctx.client.call("tab.rename", {"tab_id": tab_id, "label": ctx.agent})
    ctx.client.pane_run(pane_id, attach_command(ctx.server, sid))
    record = {"workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
              "owner": OWNER_LAUNCHER, "cwd": str(cwd)}
    _record(ctx, "spaces", sid, record)
    info("adopted session {0} into space {1}".format(sid, ws_id))
    return record


# ---------------------------------------------------------------------------
# the state file — og_herdr owns the format, this only reads and writes it
# ---------------------------------------------------------------------------

def _load(ctx: Launch) -> dict:
    state = load_state(ctx.state_path)
    return {"spaces": state.get("spaces") or {},
            "pending": state.get("pending") or {}}


def _record(ctx: Launch, bucket: str, key: str, record: dict) -> None:
    """Put one record into the shared state file, through og_herdr's writer.

    Re-read immediately before writing: the file is not a lock and both this
    process and the bridge write it, whole and last-writer-wins (see
    og_herdr.save_state). A load at the top of the launch and a save here would
    silently drop whatever the bridge wrote in between.
    """
    state = load_state(ctx.state_path)
    state.setdefault(bucket, {})[key] = record
    save_state(ctx.state_path, state)


# ---------------------------------------------------------------------------
# the multiplexer's own server
# ---------------------------------------------------------------------------

def ensure_server(ctx: Launch) -> None:
    """Make sure the multiplexer's server is answering, starting it if not.

    Only reached when we are OUTSIDE it, where there is nothing to attach to.
    Bounded: a `herdr server` that never opens its socket is reported, never
    waited on, because an `og start` that hangs forever here would look like a
    hang in og rather than in herdr.
    """
    if _ping(ctx.client):
        return
    info("the herdr server is not running; starting it")
    try:
        ctx.spawn()
    except Exception as exc:
        raise LaunchError("could not start the herdr server: {0}".format(exc))
    deadline = time.monotonic() + SERVER_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _ping(ctx.client):
            return
        time.sleep(SERVER_POLL_SECONDS)
    raise LaunchError(
        "the herdr server did not answer on {0} within {1:.0f}s{2}".format(
            getattr(ctx.client, "socket_path", "its socket"),
            SERVER_WAIT_SECONDS, _log_hint(ctx)))


def _ping(client) -> bool:
    """Whether the socket answers. Any failure reads as "not yet"."""
    try:
        client.ping()
        return True
    except Exception:
        return False


def _log_hint(ctx: Launch) -> str:
    if not ctx.log_path:
        return ""
    return "; its output is in {0}".format(ctx.log_path)


def spawn_herdr_server(binary: str, log_path: Path) -> Callable[[], Any]:
    """A callable that starts `herdr server` detached, logging to `log_path`.

    Detached with its own session so it outlives the launcher: the launcher ends
    in `exec herdr`, and a server started as a child of a process that replaces
    itself would be reaped with it.
    """
    def _spawn() -> Any:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "ab")
        return subprocess.Popen(
            [binary, "server"], stdout=handle, stderr=handle,
            stdin=subprocess.DEVNULL, start_new_session=True)
    return _spawn


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="og start {0}".format(MUX_HERDR),
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mux", default=MUX_HERDR,
                    help="multiplexer to open the session in (default: {0})"
                         .format(MUX_HERDR))
    ap.add_argument("--server", default=DEFAULT_SERVER,
                    help="Omnigent server the pane's attach command names "
                         "(default: {0})".format(DEFAULT_SERVER))
    ap.add_argument("--socket", default=None,
                    help="herdr socket path (default: the client's own)")
    ap.add_argument("--bin", default=None,
                    help="the multiplexer binary (default: the one on PATH)")
    ap.add_argument("--once", action="store_true",
                    help="do the work, print what was done, and exit instead of "
                         "attaching the multiplexer")
    return ap


def build_launch(args: argparse.Namespace) -> Launch:
    """The real dependencies: the socket client, the watcher, the spawn."""
    from og_herdr_client import HerdrClient
    from og_herdr_watch import SessionWatcher

    config = install_config()
    client = HerdrClient(socket_path=args.socket)
    watcher = SessionWatcher(base_url=args.server,
                             token=SessionWatcher.discover_token(args.server))
    binary = args.bin or shutil.which(args.mux) or args.mux
    log_path = (Path(os.environ.get("OMNIGENT_HOME")
                     or Path.home() / ".omnigent") / "logs"
                / "herdr-server.log")
    return Launch(client=client, watcher=watcher, cwd=os.getcwd(),
                  server=args.server, agent=agent_name(config),
                  agents_pane=agents_pane_enabled(config), env=dict(os.environ),
                  state_path=default_state_path(),
                  spawn=spawn_herdr_server(binary, log_path),
                  log_path=log_path)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.mux != MUX_HERDR:
        # A registry row buys detection and the README table; the backend code
        # is written per multiplexer. Say so and start without one, because the
        # server bin/og already started is perfectly usable either way.
        warn("{0} is a supported multiplexer id but og has no backend for it "
             "yet (docs/MUX-LAUNCHER.md) — starting without it.".format(args.mux))
        return 0

    ctx = build_launch(args)
    try:
        action = launch(ctx)
    except LaunchError as exc:
        ctx.close()
        print("error: {0}".format(exc), file=sys.stderr)
        return 1
    if action != "attach":
        ctx.close()
        return 0
    if args.once:
        ctx.close()
        info("--once: not attaching; the TUI would take over this terminal")
        return 0
    binary = args.bin or shutil.which(args.mux) or args.mux
    try:
        os.execvp(binary, [binary])
    except OSError as exc:  # pragma: no cover - exec only fails if it vanished
        ctx.close()
        print("error: could not run {0}: {1}".format(binary, exc),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
