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

That handshake now has a SECOND end, and it is the same record: the operator
can open a space by hand ("new space"), `cd` into a directory and type `og
chat` — so `og chat` itself has to write the pending record for the pane it is
running in, and open the `og agents` split beside it. See `chat_pane`, reached
as `og_herdr_launch.py chat-pane` from bin/og. It is the SAME writer for the
same reason this module is: one spelling of the state format, in one file.

The pending record is written BEFORE `og chat` is typed. The bridge is already
running (bin/og starts it first) and polls every few seconds, so a record
written AFTER the send can lose that race — the poll sees a root session with
no record for its directory and opens a second space. See `_open_space`.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from og_herdr import (
    DEFAULT_SPACE_CAP,
    OWNER_LAUNCHER,
    SPACE_LIVE,
    _id_of,
    attach_command,
    default_state_path,
    load_state,
    # The pane environment is stated ONCE, in og_herdr, next to the pane
    # commands it is about — the bridge opens panes too and had no copy of the
    # rule at all, which is how `og start herdr` left panes that opened a
    # browser. Imported here, not redefined: see og_herdr.pane_env_for.
    pane_env_for,
    save_state,
    space_status,
    workspace_index,
)

# The runner rule, imported rather than re-implemented: `poll_once` applies it
# for a SUB-AGENT but deliberately withholds it from a ROOT (a dead root is a
# history space, the spaces feature), so `live_session` is the one caller that
# has to ask it again — see the note there. The watcher is stdlib-only and
# imports nothing of ours at module load, so this is a cheap, acyclic import.
from og_herdr_watch import runner_is_offline

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

# The agents pane's share of the split. `pane.split`'s `ratio` is the fraction
# RETAINED by the pane being SPLIT (the chat), so the agents pane gets
# 1 - ratio; it is the only geometry the API takes — there is no column count
# to ask for, so a fraction has to be chosen rather than a floor computed. A
# floor would need a trustworthy terminal width, and the launcher has none: the
# split is relative and herdr owns the geometry, so the width that matters is
# not the launcher's own.
#
# 0.7, not 0.8, because the agents pane must be able to show its OWN controls.
# The `og agents` footer's full form ("q quit  r refresh  ^/v move  enter focus
# space expand  a access") is 64 display columns — the widest element the pane
# renders, now that Enter focuses and expand moved to Space. (The move keys are
# ASCII `^/v`, not ↑/↓, so 64 is the width in every locale, CJK included.) It no
# longer CLIPS at a narrower pane: `og_agents._footer` degrades it to a 45-column
# compact form and then to a 12-column key list, so every key stays discoverable
# at any width. The ratio is still 0.7 because 0.8 gave the pane only ~43 columns on the
# operator's 215-column terminal, where the footer would fall all the way to the
# bare key list; 0.7 leaves ~30% (~64 columns there), enough for the whole
# readable footer AND the 27-column QR. A pane that cannot show its own controls
# is broken.
AGENTS_PANE_RATIO = 0.7

# Statuses that mean "this session is the one to come back to". `idle`
# deliberately counts: a root reads idle for the whole time it waits for the
# human to type, which is precisely the session they want returned to. A failed
# or runner-offline session must not block a fresh start.
LIVE_STATUSES = frozenset({"running", "idle"})

# og-install.json keys. All have a default, so an install that predates one
# still launches.
AGENT_NAME_KEY = "agent_name"
AGENTS_PANE_KEY = "herdr_agents_pane"
SESSION_SPACE_CAP_KEY = "herdr_session_space_cap"
PROJECTS_DIR_KEY = "projects_dir"
DEFAULT_AGENT_NAME = "dev-lead"
# Where a RELATIVE `og chat --dir` lands. `~/projects` because that is where
# `og_repo_dir` (bin/og) already looks for a checkout, so the two halves of the
# installer agree on where work lives.
DEFAULT_PROJECTS_DIR = "~/projects"

# How long to wait for `herdr server` to answer a ping before giving up. Bounded
# on purpose: a launcher that blocks here is an `og start` that never finishes
# and never says why.
SERVER_WAIT_SECONDS = 15.0
SERVER_POLL_SECONDS = 0.25

# The launcher's OWN log, beside herdr's. See `launch_log_path` for why it is a
# separate file rather than an append to `log_path`.
LAUNCH_LOG_NAME = "herdr-launch.log"

# How long to hold the terminal before exec'ing herdr, when something degraded.
# Whole seconds, counted with a bounded loop rather than a wall-clock deadline so
# the hold cannot outlive its own budget on a loaded machine (and so a test can
# shorten it). See `_hold_for_problems` for why there is one at all.
DEGRADE_HOLD_SECONDS = 6


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


def session_space_cap(config: dict) -> int:
    """How many past-conversation spaces the bridge may keep open.

    0 MEANS NO LIMIT and 0 is the default, so an install that never answered (and
    one that predates the key) lists every non-archived root — the parity with
    the web sidebar that is the point of the feature. A POSITIVE value is a cap
    the bridge honours, trimming the oldest first (see og_herdr.DEFAULT_SPACE_CAP).
    Same shape as `agents_pane_enabled`: one key in `~/.omnigent/og-install.json`
    read through one function with a default.

    The reader is here, beside the other config readers, but the CONSUMER is the
    bridge (og_herdr), which counts the history spaces it already has and refuses
    new ones past this. Only a real, non-negative `int` is honoured; anything
    else — a missing key, a null, a string, a float, a negative — falls back to
    the default (no limit) rather than raising, because this feeds a background
    daemon that must not die over a hand-edited number. A bool is refused on
    purpose: `True` is an `int` in Python and would otherwise read as a cap of 1.
    """
    value = config.get(SESSION_SPACE_CAP_KEY, DEFAULT_SPACE_CAP)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_SPACE_CAP
    return value


def projects_dir(config: dict) -> Path:
    """The root a RELATIVE `og chat --dir` resolves against.

    `~` is expanded HERE and not by the installer, so a plan written by hand —
    or by an installer that predates the key — means the same thing as one
    written by `og setup`. Anything that is not a non-empty string falls back to
    the default rather than raising, on the `session_space_cap` rule: this is
    read on the way to exec'ing a REPL, and an unusable hand-edited value must
    not be the reason `og chat` will not start.
    """
    value = config.get(PROJECTS_DIR_KEY)
    if not isinstance(value, str) or not value.strip():
        value = DEFAULT_PROJECTS_DIR
    return Path(value.strip()).expanduser()


def resolve_chat_dir(path: str, config: dict) -> Path:
    """The directory `og chat --dir <path>` roots the session in.

    ABSOLUTE is used as given. RELATIVE resolves against the configured
    projects root (`projects_dir`), which is what makes `og chat --dir og` mean
    something from $HOME, where the interesting thing is a repo under
    ~/projects.

    A path that does not exist is REFUSED, never created. `omnigent run` roots
    the session at `Path.cwd().resolve()` (omnigent cli.py), so a missing
    directory would have to be made by something — and an `og chat` that
    silently mkdirs a typo is a session rooted in an empty directory with no
    error anywhere in it. Resolution also normalises the path (`.resolve()`), so
    what comes back is what `os.getcwd()` will report in the pane, which is
    the string the pending record below is keyed by and the string the bridge
    matches against.

    An EMPTY path is refused for the same reason. `Path("")` is `.` and
    `projects_dir / ""` is the projects root, so an empty `--dir` would resolve
    to somewhere plausible — a value the operator typed that silently means
    "not where I am", which is the one outcome this flag exists to prevent.
    Callers must pass an absent flag as None, never as "".
    """
    if not isinstance(path, str) or not path.strip():
        raise LaunchError(
            "--dir was given an empty value — omit it entirely to stay in the "
            "current directory")
    target = Path(path).expanduser()
    relative = not target.is_absolute()
    if relative:
        target = projects_dir(config) / target
    resolved = target.resolve()
    if not resolved.exists():
        raise LaunchError("--dir {0}: no such directory{1}".format(
            path,
            " — a relative path resolves against {0}".format(
                projects_dir(config)) if relative else ""))
    if not resolved.is_dir():
        raise LaunchError("--dir {0}: not a directory".format(path))
    return resolved


# ---------------------------------------------------------------------------
# which session, if any, this directory already has
# ---------------------------------------------------------------------------

def live_session(watcher, cwd: str) -> Optional[dict]:
    """The live ROOT session whose detail-row `workspace` is `cwd`, or None.

    Read through `og_herdr_watch.SessionWatcher` rather than by fetching the API
    directly, so pagination, `kind=any` and per-server token matching stay in one
    place — re-implementing any of them here is how a launcher and a bridge start
    disagreeing about what is live.

    The runner rule is the exception, and it is applied HERE rather than inherited
    from `poll_once`. That predicate answers the BRIDGE's question — "should this
    session get a space?" — and its answer is yes for a dead ROOT, on purpose: a
    past conversation becomes a history space (the spaces feature). So an event
    from `poll_once` is NOT the same thing as a session to attach to. A dead root
    reads `status: "idle"`, passes every filter below, and would otherwise be
    returned as "the live session"; `launch` would take case (B) and
    `_attach_there` would type an `omnigent attach` that dies with:

        Error: Session … has no online runner … its host is offline.

    Applying `runner_is_offline` here is what keeps `poll_once` free to project
    dead roots for the bridge. The unknown direction is chosen to match the error
    below: only an explicit `runner_online: False` is dead, so an absent or
    unreadable answer counts as LIVE. Guessing "none" on a session whose runner
    state we merely could not read is the one outcome that cannot be undone — it
    opens a second space and types a second `og chat` into a conversation the
    operator already had — while attaching to a session that turns out to be dead
    is a visible error the operator can simply re-run.

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
        # THE RUNNER RULE, re-applied here and NOT inherited from poll_once: a
        # dead root is projected on purpose (it becomes a history space in the
        # bridge), has status "idle", and so passes everything above. Handing it
        # back would send `launch` down case (B) and type an attach that dies. See
        # the docstring for why an unreadable runner answer counts as live.
        if runner_is_offline(session):
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
    agents_pane: bool
    env: dict
    state_path: Path
    # Extra environment for every pane this launch creates, handed to herdr on
    # the create call so it reaches the pane's shell. Empty means "send none".
    pane_env: dict = field(default_factory=dict)
    spawn: Callable[[], Any] = None  # start the mux's own server, if it is down
    log_path: Optional[Path] = None
    # What this launch could not do, in the order it happened. Filled by `report`
    # and read by `_hold_for_problems` — it is the only thing that makes "the
    # launch went fine" and "the launch went fine except for one pane" the same
    # code path right up until the terminal is handed to herdr.
    problems: list = field(default_factory=list)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


def launch_log_path(ctx: Launch) -> Optional[Path]:
    """The launcher's own log: `~/.omnigent/logs/herdr-launch.log`.

    A SIBLING of `ctx.log_path`, never `ctx.log_path` itself: that file is herdr's
    stdout and stderr (spawn_herdr_server appends to it), so an og warning written
    there reads as the multiplexer's — and an operator tailing herdr's log to find
    out why herdr misbehaved would find og's sentence and no answer. Derived from
    `log_path` rather than from $HOME so the two can never drift into different
    directories, and so a launch that redirects the log directory moves both.
    """
    if not ctx.log_path:
        return None
    return ctx.log_path.parent / LAUNCH_LOG_NAME


def launcher_log_path() -> Optional[Path]:
    """The launcher log derived from $OMNIGENT_HOME rather than from a launch.

    `launch_log_path` (the launch's own) derives from `ctx.log_path` so a launch
    that redirects the log directory moves the log with it. A caller with no
    launch behind it — `chat_pane`, which runs from a pane the operator opened
    by hand — has no `ctx` to derive from, and `~/.omnigent/logs` is where
    `build_launch` puts it anyway, so both spellings name one file.
    """
    base = Path(os.environ.get("OMNIGENT_HOME") or Path.home() / ".omnigent")
    return base / "logs" / LAUNCH_LOG_NAME


def log_line(ctx: Launch, line: str) -> None:
    """Append one timestamped line to the launcher log. Never raises.

    Best effort to exactly the degree the failure it describes: a log that
    cannot be written must not turn a degraded launch into a failed one. A
    launch with no log at all (a test, or an older caller that passes no
    `log_path`) simply has no durable channel, which is the old behaviour and
    not a reason to fail.
    """
    _append_log(launch_log_path(ctx), line)


def _append_log(path: Optional[Path], line: str) -> None:
    """One timestamped line in `path`, or nothing at all. Never raises."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{0} {1}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


def report(ctx: Launch, line: str) -> None:
    """Report a failure the launch SURVIVES, somewhere that outlives it.

    The defect this exists for: `warn` on its own is not a report, because the
    launch ends in `os.execvp("herdr")` and herdr's TUI takes the terminal into
    the alternate screen within milliseconds. Every warning that reached only the
    terminal was wiped before a human could read it — which is how "the agents
    pane never appeared" became a bug report with no cause. So a degradation goes
    three places, and the three have different jobs:

      1. `ctx.problems`, which `_hold_for_problems` re-surfaces immediately
         before the exec. Re-surfacing is not redundancy: `_open_space` prints
         `info("opened space ...")` AFTER the split, so by exec time the first
         copy has already scrolled off the top of the terminal.
      2. The launcher log, which outlives both the exec and the session. This is
         the part that answers "why did that not happen?" days later, on a
         machine the operator is no longer sitting at.
      3. The terminal, as it always was.

    Still non-fatal by construction: nothing here raises, and the caller returns
    to a working chat pane.
    """
    ctx.problems.append(line)
    log_line(ctx, line)
    warn(line)
    path = launch_log_path(ctx)
    if path:
        print("  (also in {0})".format(path))


def operator_is_watching() -> bool:
    """Whether a human is at this terminal, as far as this process can tell.

    The only test there is, and the reason the hold is not unconditional: a
    launcher under `og start herdr > run.log` still logs its problems and still
    attaches, it just does not sit there counting down for somebody who cannot
    see the countdown.
    """
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _hold_for_problems(ctx: Launch) -> None:
    """Leave the terminal alone long enough to READ a degradation.

    The second half of the fix, and the half that reaches a human in the moment:
    the log is what they find tomorrow, this is what tells them today that
    something is wrong. Neither alone is enough — a log nobody knows to open is
    the same void with a file in it.

    Bounded and non-blocking by construction, and skipped entirely when nothing
    degraded or nobody is watching, so the ordinary launch costs exactly what it
    cost before. It never raises: the chat pane is the session, and a cosmetic
    pane must not be able to stop it — nor, now, to stop it loudly.
    """
    if not ctx.problems or not operator_is_watching():
        return
    plural = "" if len(ctx.problems) == 1 else "s"
    warn("{0} thing{1} about this launch did not work — the chat pane itself "
         "is unaffected:".format(len(ctx.problems), plural))
    for line in ctx.problems:
        print("    {0}".format(line))
    print("  {0} takes this terminal in {1}s (ctrl-c stops the launch)".format(
        MUX_HERDR, DEGRADE_HOLD_SECONDS))
    sys.stdout.flush()
    for left in range(int(DEGRADE_HOLD_SECONDS), 0, -1):
        time.sleep(1.0)
        # \r rewrites the countdown in place; ESC[K erases what it overwrites,
        # without which a shorter number leaves the tail of the longer one.
        sys.stdout.write("\r  {0} takes this terminal in {1}s".format(
            MUX_HERDR, left - 1))
        sys.stdout.flush()
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


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

    KNOWN GAP, documented rather than fixed: this case NEVER opens the `og agents`
    pane, and says nothing when the space it is attaching to has none — only
    `_open_space` splits. So an operator whose directory already had a session
    gets no agents pane and no explanation, which is indistinguishable at the
    keyboard from case (A)'s split failing, and for the same reason: the launch
    says nothing either way. The likely triggers are a space whose agents pane
    was closed by hand, and a space opened before `herdr_agents_pane` existed.

    It is NOT a small fix, which is why it is written down instead of patched:
    the split needs a tab id and a root pane, and a focused EXISTING space has
    neither in the record this launcher keeps (the state file stores workspace_id,
    tab_id and pane_id, but a space created before the record existed, or one
    whose layout the operator has since changed, cannot be trusted to match) —
    so "add the pane if it is missing" needs an answer for which pane is the
    chat, and that question is a design decision rather than a line of code. It
    also has to be idempotent: the operator may have deliberately closed the
    agents pane, and a start that reopens it every time is its own annoyance.
    Splitting unconditionally is the safe half and could ship on its own.

    Until then `og start herdr --once` is the diagnostic: it prints what this
    launch did without attaching, and a silent attach is itself the answer.
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

    The space's first tab is given that same label. `workspace.create` labels
    only the WORKSPACE — it has no tab-label field — so the tab it makes
    implicitly would otherwise be unlabelled and show herdr's tab number. The
    operator asked for the tab to read the space's own name, so it does, and
    og_herdr's title-arrival rename carries it onto the title with the space.
    (`ctx.agent`, which this used to say, is the driving bundle's name; it read
    as a second, different name beside a space labelled from the directory.)

    The pending record is written BEFORE `og chat` is typed, and that order is
    the contract, not a detail. The bridge is already running (bin/og starts it
    before exec'ing this) and polls every few seconds, while `og chat` mints the
    session the instant it runs — so a record written after the send can lose
    the race, and a poll that sees a root session with no record for its
    directory opens a SECOND space (the duplicate-space bug this handshake
    exists to prevent). Every id the record needs is in the workspace.create
    reply, so there is nothing to wait for.
    """
    label = os.path.basename(ctx.cwd.rstrip(os.sep)) or ctx.cwd
    created = ctx.client.workspace_create(label=label, cwd=ctx.cwd, focus=True,
                                          env=ctx.pane_env)
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
    #
    # This runs BEFORE the pending record below, so it cannot leave a record
    # pointing at a workspace it just closed. (The one path that closes AFTER
    # the record — a failed `pane_run` — drops the record itself; see below.)
    if ws_id and not (tab_id and pane_id):
        _close_quietly(ctx, ws_id)
        raise LaunchError(
            "workspace.create returned {0}; expected workspace, tab and "
            "root_pane".format(sorted(created) or "an empty result"))
    if not ws_id:
        raise LaunchError(
            "workspace.create returned no workspace id: {0!r}".format(created))

    # The first tab reads the space's own label, not the agent bundle's name:
    # `workspace.create` cannot label the tab it makes, so without this it shows
    # herdr's tab number. See the docstring.
    ctx.client.tab_rename(tab_id, label)
    # A pending record, not a space record: the session this space is for does
    # not exist yet, so there is no id to key it by. og_herdr claims it by
    # directory when the session appears — and it must be on disk BEFORE the
    # chat is typed, or the bridge's poll can beat the write (see docstring).
    #
    # A record left behind by a failure AFTER this point is deliberate and
    # self-healing: the bridge prunes a pending record whose workspace is not in
    # workspace.list (og_herdr._prune_pending), so a space the operator closes
    # takes its record with it. The `pane_run` path below does not rest on that
    # — the space SURVIVES a failed send, and a live-but-empty space is exactly
    # what the bridge will not prune — so it drops the record itself.
    _record(ctx.state_path, "pending", ctx.cwd, {
        "workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
        "owner": OWNER_LAUNCHER, "cwd": ctx.cwd,
    })
    try:
        ctx.client.pane_run(pane_id, CHAT_COMMAND)
    except Exception:
        # No session was minted, so nothing will ever claim this record; drop it
        # and close the space it named (the create-missing-pane rule above:
        # "no stray empty spaces").
        _unrecord(ctx.state_path, "pending", ctx.cwd)
        _close_quietly(ctx, ws_id)
        raise
    if ctx.agents_pane:
        _split_agents(ctx, ws_id, tab_id, pane_id)
    info("opened space {0} for {1}".format(ws_id, ctx.cwd))
    return "attach" if attach else "done"


def _close_quietly(ctx: Launch, ws_id: str) -> None:
    """Close a space this launch opened but cannot use.

    Best effort by design: the launch is already failing or degrading, and a
    close that itself fails must not replace the real error. The id is still
    reported, because closing it by hand is then the only other way it goes
    away.
    """
    try:
        ctx.client.workspace_close(ws_id)
    except Exception as exc:
        report(ctx, "could not close the space {0} I just opened ({1}); close "
                    "it by hand".format(ws_id, exc))


def split_agents_pane(client, ws_id: str, tab_id: str, root_pane: str,
                      cwd: str, pane_env, known_panes=None) -> list:
    """Put `og agents` in a split beside `root_pane`; RETURN what failed.

    ONE implementation of the split, called by the three things that open a pane
    og expects to carry an `og agents` beside it: the launcher's `_open_space`
    (case A/D), the bridge's history/resume space (`og_herdr._add_history_space`)
    and `og chat` in a pane the operator opened by hand (`chat_pane`). The
    bridge never split, so resuming a past conversation from the spaces list came
    up without the pane a fresh `og start` opens — and a second copy of the
    split would only drift, leaving the next person to discover which one ran.

    `known_panes` is the set of pane ids the tab held BEFORE the split, for a
    caller whose tab may already have other panes in it — `None` means no
    baseline and keeps the difference-against-the-tab rule, which is exact only
    because those callers created the space and so have the one pane they just
    split. The two are different questions and neither is a third: an empty set
    is herdr answering "this tab had no panes", while a baseline that could not
    be read is `tab_pane_ids`' None, and the caller must not have split at all.
    See `_split_pane_id`.

    It RETURNS its failures rather than reporting them, so each caller can route
    a degradation through its own channel: the launcher's `_split_agents` sends
    each line through `report` (log + re-surface before the terminal is handed
    to herdr), the bridge appends them to its `reconcile` lines, and `chat_pane`
    prints and logs them. The strings are deliberately stable so the launcher's
    log-matching tests keep passing.
    """
    params = {"target_pane_id": root_pane, "direction": "right",
              "focus": False, "cwd": cwd, "ratio": AGENTS_PANE_RATIO}
    if pane_env:
        params["env"] = pane_env
    try:
        reply = client.call("pane.split", params)
    except Exception as exc:
        return ["could not open the og agents pane ({0}); the chat is up "
                "without it".format(exc)]
    pane_id = _split_pane_id(client, ws_id, tab_id, root_pane, reply, known_panes)
    if not pane_id:
        return ["herdr split the chat pane but reported no new pane id; "
                "`og agents` was not started there"]
    try:
        client.pane_run(pane_id, AGENTS_COMMAND)
    except Exception as exc:
        return ["could not start og agents in the split pane ({0})".format(exc)]
    return []


def _split_agents(ctx: Launch, ws_id: str, tab_id: str, root_pane: str) -> None:
    """Put `og agents` in a split beside the chat.

    Best effort by design: the chat pane is the session, and losing the whole
    launch because a cosmetic second pane could not be found is the wrong trade.
    That trade is kept — but every one of the three failures below now goes
    through `report`, so "best effort" no longer means "unobserved": each is
    recorded, logged, and re-surfaced before the terminal is handed to herdr.

    `pane.split` makes a pane too, so it takes the same pane environment as the
    space's own pane; the split runs `og agents`, which opens no browser, but a
    pane og creates under a mux launch should carry the map regardless.

    The split itself is `split_agents_pane`, shared with og_herdr's
    history/resume path so the two cannot drift; this launcher caller is the one
    that routes its returned lines through `report` (see the note there).
    """
    for line in split_agents_pane(ctx.client, ws_id, tab_id, root_pane,
                                  ctx.cwd, ctx.pane_env):
        report(ctx, line)


def _split_pane_id(client, ws_id: str, tab_id: str, root_pane: str,
                   reply: Any, known_panes=None) -> Optional[str]:
    """The pane `pane.split` just made, or None when it cannot be told.

    The reply does not name it: the schema's `pane.split` result is `{"type":
    "ok"}` — no pane, no id. So the new pane is found by DIFFERENCE against the
    tab. A reply that DOES carry the pane wins outright, since a server that
    starts naming it should not have its answer second-guessed.

    `known_panes` is the tab's panes as they were BEFORE the split, and it has
    two spellings that mean OPPOSITE things, which is the whole hazard here:

    * a set — including an EMPTY one — is herdr's answer. Every pane in the tab
      that is not in it is one herdr just made, and an empty set is a real
      answer ("this tab had no panes"), not a shrug.
    * None is NO BASELINE: either the caller made the space itself, so the tab
      holds exactly the pane being split and the difference is exact by
      construction, or the baseline could not be read and the caller decided
      not to split at all. Callers in the second position must not reach here.

    So a caller that does not know what the tab held must filter, not fall back
    to `others[0]`: the lowest-numbered other pane is only the right answer in
    the first case, and in the second it can be a pane the OPERATOR split for
    their own work.
    """
    if isinstance(reply, dict):
        found = _id_of(reply, "pane_id") or _id_of(reply.get("pane"), "pane_id")
        if found:
            return found
    try:
        panes = client.call("pane.list", {"workspace_id": ws_id})
    except Exception:
        return None
    others = sorted(
        str(pane.get("pane_id")) for pane in (panes or {}).get("panes", [])
        if isinstance(pane, dict) and pane.get("tab_id") == tab_id
        and pane.get("pane_id") and pane.get("pane_id") != root_pane)
    if known_panes is not None:
        fresh = [pane_id for pane_id in others if pane_id not in known_panes]
        return fresh[0] if fresh else None
    return others[0] if others else None


def tab_pane_ids(client, ws_id: str, tab_id: str) -> Optional[set]:
    """The pane ids herdr currently lists in a tab — or None, "could not ask".

    Read BEFORE a split so the pane that split makes can be told from the ones
    the operator had already. Three outcomes, kept apart the way
    `og_herdr.space_status` keeps a listing apart from a failed one:

    * a set — possibly EMPTY — is herdr's answer, and an empty set is a real one
      ("this tab has no panes"), not a shrug;
    * None is "the listing could not be read", and it is NOT an empty baseline.
      Collapsing the two is how a failed read turns into "every pane in the tab
      is new", which hands `og agents` to the lowest-numbered other pane — the
      operator's, in a space they made themselves.

    A caller given None must refuse to guess rather than pass it on: a missing
    agents pane is cosmetic, a command typed into somebody's pane is not.
    """
    try:
        panes = client.call("pane.list", {"workspace_id": ws_id})
    except Exception:
        return None
    return {str(pane.get("pane_id")) for pane in (panes or {}).get("panes", [])
            if isinstance(pane, dict) and pane.get("tab_id") == tab_id
            and pane.get("pane_id")}


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
    if space_status(ws_id, live_workspaces(ctx)) == SPACE_LIVE:
        return record, False
    if ws_id:
        info("the recorded space {0} is gone; adopting again".format(ws_id))
    return _adopt(ctx, session), True


def live_workspaces(ctx: Launch) -> dict:
    """`{workspace_id: row}` for every workspace herdr currently lists.

    A listing that cannot be read is an error rather than an empty answer: an
    empty one would read as "your space is gone" and adopt a second space over
    the first. The parse itself lives in og_herdr so this launcher and the
    bridge cannot drift into reading the same reply two different ways, and the
    verdict is `og_herdr.space_status`, which is also where that rule is
    stated.
    """
    try:
        listing = ctx.client.workspace_list()
    except Exception as exc:
        raise LaunchError("could not list herdr workspaces: {0}".format(exc))
    return workspace_index(listing)


def _adopt(ctx: Launch, session: dict) -> dict:
    """Give an existing session a space, and ATTACH it rather than chat.

    `og chat` would mint a NEW conversation — the one thing adoption exists to
    avoid — so the pane gets `omnigent attach --server <url> <session id>`,
    built by og_herdr so there is one spelling of it.

    The space's first tab is labelled with the same string as the space: the
    session's title when it has one, else the directory's basename. Same reason
    as `_open_space` — `workspace.create` cannot label the tab it makes.
    """
    sid = str(session.get("id") or "")
    label = session.get("title") or os.path.basename(ctx.cwd.rstrip(os.sep))
    cwd = session.get("workspace") or ctx.cwd
    created = ctx.client.workspace_create(label=str(label), cwd=str(cwd),
                                         focus=True, env=ctx.pane_env)
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
    ctx.client.tab_rename(tab_id, str(label))
    ctx.client.pane_run(pane_id, attach_command(ctx.server, sid))
    record = {"workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
              "owner": OWNER_LAUNCHER, "cwd": str(cwd)}
    _record(ctx.state_path, "spaces", sid, record)
    info("adopted session {0} into space {1}".format(sid, ws_id))
    return record


# ---------------------------------------------------------------------------
# the state file — og_herdr owns the format, this only reads and writes it
# ---------------------------------------------------------------------------

def _load(ctx: Launch) -> dict:
    state = load_state(ctx.state_path)
    return {"spaces": state.get("spaces") or {},
            "pending": state.get("pending") or {}}


def _record(state_path: Path, bucket: str, key: str, record: dict) -> None:
    """Put one record into the shared state file, through og_herdr's writer.

    Re-read immediately before writing: the file is not a lock and both this
    process and the bridge write it, whole and last-writer-wins (see
    og_herdr.save_state). A load at the top of the launch and a save here would
    silently drop whatever the bridge wrote in between.

    Takes the PATH rather than a `Launch` because the record now has two
    writers — the launcher's `_open_space` and `og chat`'s own pane
    (`record_chat_pane`) — and the re-read rule has to hold for both of them,
    not be re-implemented for whichever caller was written second.
    """
    state = load_state(state_path)
    state.setdefault(bucket, {})[key] = record
    save_state(state_path, state)


def _unrecord(state_path: Path, bucket: str, key: str) -> None:
    """Drop one record, re-reading first for the same reason `_record` does.

    Used when a record names a space that turned out to be unusable — the chat
    never started, so no session will ever claim it. Missing key is not an
    error: the bridge may have claimed it already, which is also a fine outcome.
    """
    state = load_state(state_path)
    state.setdefault(bucket, {}).pop(key, None)
    save_state(state_path, state)


def record_chat_pane(state_path: Path, cwd: str, workspace_id: str,
                     tab_id: str, pane_id: str) -> dict:
    """Record THIS pane as the one a session in `cwd` will be run in.

    The record `_open_space` writes, through the same writer, keyed by the same
    thing: the directory. `og chat` mints the session the instant it execs, and
    there is no session id yet to key it by, so the bridge claims this record
    when a root appears in this directory (og_herdr._claim_pending) and renames
    the space to the conversation's title — which is the entire point of an
    operator opening a space by hand, `cd`ing into a repo and typing `og chat`.

    Written BEFORE the exec for the same reason `_open_space` writes before it
    types: the bridge is already running and polls every few seconds, so a
    record written after the session exists can lose the race and the poll
    opens a second space.
    """
    record = {"workspace_id": workspace_id, "tab_id": tab_id,
              "pane_id": pane_id, "owner": OWNER_LAUNCHER, "cwd": cwd}
    _record(state_path, "pending", cwd, record)
    return record


def og_owns_pane(state_path: Path, workspace_id: str, tab_id: str,
                 pane_id: str) -> bool:
    """Is this pane one og opened for a chat, rather than one the operator made?

    `og chat` asks this to stay out of the launcher's way. `og start` types
    `og chat` into a pane `_open_space` has just made and then splits that same
    pane for `og agents` — so the process `pane_run` started runs the very code
    that splits beside it. Both halves doing it is how `og start` came to open
    THREE panes where the operator expects two.

    Both halves of the question are already written down before either acts:
    `_open_space` records the pane it is about to type into, and
    `_add_history_space` remembers it, keyed by ids a human typing in herdr
    cannot collide with. So the answer is a lookup in og's own state file, not a
    second opinion about panes.

    A pane listing cannot answer it, and the reason is worth keeping in view
    because it is the trap this function exists to avoid: `pane.list` returns
    only pane_id, tab_id and workspace_id — no command, no process — so "this
    tab already holds the `og agents` pane" and "this tab already holds a pane
    the operator split themselves" are the SAME answer. Reading the second as
    the first is how a guard would silently take the agents pane away from every
    `og chat` an operator runs in a tab they made, which is the case
    `known_panes` exists to protect. `og chat` can still tell panes apart from
    one another; it cannot tell what a pane is running.

    It fails OPEN. A state file that cannot be read — missing, truncated, a
    version this code does not know — is og having nothing to say, not og having
    said "mine", so the answer is no and the caller splits. That direction costs
    a cosmetic extra pane rather than a missing one, the same trade
    `split_agents_pane` makes when the listing fails after a split has already
    been issued.

    The tab is matched EXACTLY, and a record carrying NO `tab_id` does not
    match — not "the tab is unknown, so it must be this one". The lenience that
    clause used to grant is the one way this function can lose the pane it
    exists to protect, and it is found rather than reasoned out: a tab-less
    record reads as og's own in EVERY tab, so one stale or hand-edited record
    makes `og chat` stand down in a tab the launcher never touched — and
    standing down when nobody is splitting is the one failure the operator pays
    for with the agents pane itself, silently, with nothing degraded to show
    for it. Review also noted the supporting fact is not established (that
    pane ids can never be reused across tabs), which is exactly why the
    comparison cannot be loosened: a tab-less record is not evidence about any
    particular tab.

    No writer produces one, which is what makes the exact match strict rather
    than merely safe. `_open_space` closes the space and refuses when the create
    reply carried no tab, so the record it writes always has one;
    `record_chat_pane` is called from one place, with a `pane_identity` that is
    None unless all three ids are non-empty; and both `_add_space` and
    `_add_history_space` reach `_remember` only after rejecting the same partial
    create. A v1 state file upgraded in place DOES yield tab-less records —
    `_upgrade_v1` records the workspace/session pair and invents nothing, by
    design — so this path really does meet one. Those carry no `pane_id` either
    and so fail the pane test already, and refusing them is the direction this
    whole function already fails in. Do not read this as leniency to be
    restored.
    """
    try:
        state = load_state(state_path)
    except Exception:
        return False
    for bucket in ("pending", "spaces"):
        for record in (state.get(bucket) or {}).values():
            if not isinstance(record, dict):
                continue
            if (record.get("workspace_id") == workspace_id
                    and record.get("tab_id") == tab_id
                    and record.get("pane_id") == pane_id):
                return True
    return False


# ---------------------------------------------------------------------------
# `og chat`'s end of the same handshake — the other way a space gets a session
# ---------------------------------------------------------------------------
# The user's flow this exists for: "new space" in herdr, `cd` to the repo, `og
# chat`. No launcher ran, so nothing wrote the pending record the bridge adopts
# — and the bridge, seeing a root session appear in a directory it has no space
# for, opens a SECOND space for the conversation already on screen. So `og
# chat` writes the record itself, for the pane it is running in, and puts the
# agents pane beside it the way `_open_space` does — EXCEPT when the record says
# the launcher already opened this pane, because then the launcher is splitting
# it too and two halves doing one job is what opened three panes in a tab the
# operator expects to hold two. See `og_owns_pane` and `chat_pane`.
#
# It is a NO-OP outside a herdr pane — a plain `og chat` in an ordinary terminal
# must behave exactly as it always has, including never touching the state file.
# The marker is `HERDR_ENV=1`, herdr's own flag for "this process was spawned by
# a managed pane" (measured, not assumed: herdr's bundled agent skill, `herdr
# --skill`, states `test "${HERDR_ENV:-}" = 1` as the check before touching
# herdr, and the same pane context is what `is_inside` above already reads).

# The pane context herdr injects into every managed pane. Measured against
# herdr 0.9.4 the same way: `herdr --skill` documents HERDR_WORKSPACE_ID,
# HERDR_TAB_ID and HERDR_PANE_ID as the caller context it injects into each
# managed pane (and herdr's binary carries them as one contiguous env-table
# block, which is why they are a set rather than three guesses). Nothing here
# invents a name.
PANE_ID_KEYS = ("HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID")


def pane_identity(env: dict) -> Optional[tuple]:
    """`(workspace_id, tab_id, pane_id)` for the pane this process is in.

    None when herdr did not put all three in the environment. All three are
    required rather than defaulted: a pending record with a null tab or pane id
    is one the bridge would adopt and then fail to set up against, and that —
    a record naming a pane that does not exist — is worse than recording
    nothing, which costs the operator nothing but a second space they can
    close.
    """
    ids = tuple((env.get(key) or "").strip() for key in PANE_ID_KEYS)
    return ids if all(ids) else None


def _chat_client(environ: dict):
    """A herdr client for the pane this process is in.

    Built lazily and only when a split is actually wanted: `HerdrClient` resolves
    its socket from `$HERDR_SOCKET_PATH` / `$HERDR_SESSION` — both set by herdr
    in a pane — and connects on the first call, so a `chat_pane` with nothing to
    split must not construct one at all. `environ` is passed rather than read
    from `os.environ` so the socket resolves against the same environment the
    pane identity came from; in production the two are the same dictionary.
    """
    from og_herdr_client import HerdrClient
    return HerdrClient(socket_path=HerdrClient.resolve_socket_path(
        None, env=environ))


def chat_pane(argv=None, client=None, env=None) -> int:
    """`og chat`'s multiplexer half. Run by bin/og before it execs the REPL.

    `argv` takes `--dir <path>`; a None means NO arguments rather than this
    process's own command line — `main` is the only caller that reads
    `sys.argv`, and a helper that silently parsed whatever launched the
    interpreter is how a test ends up asserting on pytest's flags.

    The resolved directory is printed on **stdout** as data (bin/og captures it,
    `cd`s there and execs `omnigent run` from it) and every human-facing line
    goes to stderr, so the capture can never pick up a sentence.

    `client` is the herdr client, injected so no test opens a real socket; None
    means "build one if a split is actually needed".

    The agents pane is NOT opened unconditionally. On a fresh `og start` this
    process is running in the very pane `_open_space` is about to split, so
    splitting here as well left the operator a third pane; the launcher keeps the
    split and this half stands down when `og_owns_pane` says the pane is already
    og's. An operator-made pane has no such record and is split as it always was.

    Exit codes: 0 for the ordinary case, INCLUDING every degradation — the chat
    is the session and a cosmetic second pane must not be able to stop it, which
    is the trade `report`/`_split_agents` already makes. 2 only for `--dir`
    naming something that is not a directory, which is a refusal rather than a
    degradation: running the session somewhere the operator did not ask for is
    not a degraded version of what they asked for.
    """
    args = build_chat_parser().parse_args([] if argv is None else argv)
    environ = dict(os.environ) if env is None else dict(env)
    config = install_config()

    cwd = None
    # `is not None`, not truthiness: `--dir ""` is a value the operator typed
    # and must be REFUSED, not mistaken for the flag being absent — which is
    # what silently ran the chat in the current directory.
    if args.dir is not None:
        try:
            cwd = str(resolve_chat_dir(args.dir, config))
        except LaunchError as exc:
            print("error: {0}".format(exc), file=sys.stderr)
            return 2
        print(cwd)

    # Outside herdr there is nothing to record and nothing to split. Checked
    # here as well as in bin/og (which gates on the same variable to avoid
    # spawning python for every plain `og chat`): this is the end that OWNS the
    # handshake, so it cannot take the caller's word for it.
    if not is_inside(environ):
        return 0

    identity = pane_identity(environ)
    if identity is None:
        _chat_degrade(
            "HERDR_ENV says this is a herdr pane, but it carries none of "
            "{0}, so the chat was not recorded; the bridge will open its own "
            "space for it".format(", ".join(PANE_ID_KEYS)))
        return 0
    ws_id, tab_id, pane_id = identity

    # `os.getcwd()` and not `$PWD`: the record is claimed by EXACT match against
    # the session's own `workspace`, which `omnigent run` fills with
    # `str(Path.cwd().resolve())` (cli.py) — a physical path, where a shell's
    # `$PWD` is a logical one and the two part company under a symlink.
    here = cwd or os.getcwd()
    # Asked BEFORE the record below, which would otherwise answer it for us:
    # `record_chat_pane` writes exactly this record, so a lookup after it says
    # "og owns this pane" on every run, the operator's included.
    state = default_state_path()
    og_opened = og_owns_pane(state, ws_id, tab_id, pane_id)
    try:
        record_chat_pane(state, here, ws_id, tab_id, pane_id)
    except Exception as exc:
        # The record is what stops a second space appearing; failing to write it
        # is not worth refusing the chat over, but it must never be silent.
        _chat_degrade("could not record this pane for the bridge ({0})".format(exc))

    if not agents_pane_enabled(config):
        return 0

    if og_opened:
        # Somebody else is already splitting THIS pane. `og start` writes the
        # record above before it types `og chat`, and types `og chat` into the
        # pane it is about to split — so on a fresh launch this process is the
        # launcher's own second half, and both halves splitting is what put three
        # panes in a tab the operator expects to hold two. The launcher keeps the
        # split: it is the half that still holds the pane if this process never
        # reaches its own, and the half whose degradation reaches the operator
        # before the terminal is handed to herdr.
        #
        # Ordered, not raced: the record is on disk before `pane_run` can start
        # this process at all, so finding it here means the launcher is already
        # past `pane_run` and about to split. Not finding it means nobody wrote
        # one — an operator-made pane has no record until the line below writes
        # it, which is the split this half still owes.
        #
        # Checked before the client is built, so a launch pays for no socket at
        # all on this path.
        return 0

    opened = client if client is not None else _chat_client(environ)
    try:
        # Read the tab's panes BEFORE splitting: unlike a launch, this tab is one
        # the operator made and may have split themselves, and "the first pane
        # in this tab that is not the chat pane" is then their pane, not ours.
        # A listing that could not be read is not an empty tab (see
        # `tab_pane_ids`), and guessing from one would type `og agents` into
        # whatever the operator has — so NO split is made either: a pane nothing
        # ever runs in is the exact thing `_open_space` refuses to leave behind.
        baseline = tab_pane_ids(opened, ws_id, tab_id)
        if baseline is None:
            _chat_degrade(
                "could not read this tab's panes from herdr, so no `og agents` "
                "pane was opened beside the chat; run `og agents` in a pane "
                "yourself if you want one")
        else:
            for line in split_agents_pane(opened, ws_id, tab_id, pane_id, here,
                                          pane_env_for(environ),
                                          known_panes=baseline):
                _chat_degrade(line)
    except Exception as exc:
        _chat_degrade("could not open the og agents pane ({0}); the chat is up "
                      "without it".format(exc))
    finally:
        if client is None:
            try:
                opened.close()
            except Exception:
                pass
    return 0


def _chat_degrade(line: str) -> None:
    """Say what did not work, where it survives the REPL taking the terminal.

    Printed AND logged, for the reason `report` gives: `og chat` ends in an
    `exec omnigent run`, whose REPL takes over the terminal in the same way
    herdr's TUI does, so a line that reached only stderr is gone before anybody
    reads it. The log is the copy that answers "why did the agents pane never
    appear?" days later.
    """
    print("! " + line, file=sys.stderr)
    _append_log(launcher_log_path(), line)


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

# The second entry point, as a SUBCOMMAND rather than another `og start` flag:
# `og start` and `og chat` are different jobs with different callers (bin/og's
# `cmd_start` vs `cmd_chat`), and one flag set for both would make every
# `og start` flag mean something in a command that has none of them.
CHAT_PANE_COMMAND = "chat-pane"


def build_chat_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="og chat (herdr pane handshake)",
        description="Record the pane this `og chat` is running in, and open the "
                    "`og agents` pane beside it. Does nothing outside a herdr "
                    "pane. Prints the resolved --dir on stdout; everything a "
                    "human should read goes to stderr.")
    ap.add_argument("--dir", default=None,
                    help="directory to root the session in. Absolute paths are "
                         "used as given; a relative one resolves against the "
                         "install's projects_dir (default: {0}). Refused if it "
                         "is empty or does not exist.".format(DEFAULT_PROJECTS_DIR))
    return ap


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
                  server=args.server,
                  agents_pane=agents_pane_enabled(config), env=dict(os.environ),
                  state_path=default_state_path(),
                  pane_env=pane_env_for(dict(os.environ)),
                  spawn=spawn_herdr_server(binary, log_path),
                  log_path=log_path)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Dispatched on the subcommand BEFORE the `og start` parser sees it, so that
    # parser keeps exactly the flags it has always had — `og start herdr --once`
    # must not grow a vocabulary it does not want.
    if argv and argv[0] == CHAT_PANE_COMMAND:
        return chat_pane(argv[1:])
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
    # A degradation is not fatal and never becomes one — the chat pane IS the
    # session — but it is also not something the operator may never find out
    # about. Hand the terminal over only after they have had a moment to read it.
    # `--once` returned above: nothing takes the terminal in that mode, so the
    # text is already on screen and a hold would be pure waiting.
    _hold_for_problems(ctx)
    binary = args.bin or shutil.which(args.mux) or args.mux
    try:
        # exec does not flush Python's buffers. On a pipe stdout is block
        # buffered, so without this the warnings above are discarded by the exec
        # rather than by the TUI — the same loss by a different mechanism.
        sys.stdout.flush()
        os.execvp(binary, [binary])
    except OSError as exc:  # pragma: no cover - exec only fails if it vanished
        ctx.close()
        print("error: could not run {0}: {1}".format(binary, exc),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
