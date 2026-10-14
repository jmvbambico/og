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

The pending record is written BEFORE `og chat` is typed. The bridge is already
running (bin/og starts it first) and polls every few seconds, so a record
written AFTER the send can lose that race — the poll sees a root session with
no record for its directory and opens a second space. See `_open_space`.

Everything here is a view. `og start` works without a multiplexer at all, so a
failure to open a space is reported and the og server is left running.

Read by tests through injected fakes: the herdr client, the session watcher, the
host id this machine is known by, and the spawn of the mux's own server. Nothing
in this module connects to a herdr socket at import time, and no test has to.
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
DEFAULT_AGENT_NAME = "dev-lead"

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


# ---------------------------------------------------------------------------
# which session, if any, this directory already has
# ---------------------------------------------------------------------------

def local_host_id(watcher) -> Optional[str]:
    """This machine's `host_id` according to the Omnigent server, or None.

    `GET /v1/hosts` answers `{"hosts": [...]}`, and the entry whose `status` is
    "online" is the machine this process is on — the same read `ensure_project`
    in bin/og does before it pins a project to a host, deliberately in the same
    shape (`status`, then `host_id`), because two answers to "which host am I"
    that disagree would pin a session to one machine and then go looking for it on
    another.

    Asked THROUGH the watcher rather than with a urllib call of our own, for the
    reason `live_session` reads its listing that way: its `_get_json` already
    carries the base URL and this server's token, and `--server` can name a
    server we were not handed a token for. (`_get_json` is private; promoting it
    to a public method would be a change in og_herdr_watch, which this fix
    deliberately does not make.)

    Every failure — a 404, a refused connection, a payload in a shape we do not
    recognise, a host row with no id — is None rather than an exception, and the
    catch is deliberately broad for that reason. The rule below is ADDITIVE: with
    no id of our own there is nothing to compare a session's against, so the
    honest answer is "I do not know which host I am". The worst a bug in these
    lines can do is leave the launch behaving exactly as it did before the filter
    existed — never fail an `og start` the operator is waiting on, and never hide
    a local session.
    """
    try:
        payload = watcher._get_json("{0}/v1/hosts".format(watcher.base_url))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    hosts = payload.get("hosts")
    if not isinstance(hosts, list):
        return None
    online = next((host for host in hosts
                   if isinstance(host, dict) and host.get("status") == "online"),
                  None)
    host_id = online.get("host_id") if online else None
    return host_id if isinstance(host_id, str) and host_id else None


def session_on_another_host(session: dict, host_id: Optional[str]) -> bool:
    """Whether this session is known to belong to a DIFFERENT machine.

    Only a POSITIVE answer skips a session, and it takes two known-and-different
    ids to produce one. Every other combination keeps the session, for the same
    reason `runner_is_offline` reads an unreadable answer as live: a skip we had
    no evidence for opens a SECOND space and types a second `og chat` into a
    conversation the operator already had — the one mistake nothing undoes —
    while attaching to a session that is not ours is a line on screen they can
    read and re-run. An endpoint we could not read must never get to decide that
    a local session is foreign.

    So, exhaustively:

    * `host_id` is None — our own lookup failed, found nothing, or found no
      online host. We do not know who we are, so nothing is filtered and the
      launch behaves exactly as it did before this rule existed.
    * the session carries no `host_id`, or a null / non-string one — we do not
      know who IT is. Same verdict.
    * both present and equal — ours, keep.
    * both present and different — someone else's, skip.

    The failure this exists for, which nothing above it could catch: a session
    running on a work-mac, in the SAME project path as this one (`~/projects/og`
    is not an unusual directory to have checked out twice), turned up in the
    local `kind=any` listing, matched `workspace == cwd`, passed the status and
    runner checks, and was handed back as "the live session". `runner_is_offline`
    cannot see that by design — it is True only for an explicit
    `runner_online: False`, and a session on a machine that is simply not here
    reports nothing at all, which that predicate deliberately reads as live. So
    `launch` took case (B), `_adopt` typed the attach, and the pane said:

        Error: Session … has no online runner … its host is offline.
    """
    if not host_id:
        return False
    theirs = session.get("host_id")
    if not isinstance(theirs, str) or not theirs:
        return False
    return theirs != host_id


def live_session(watcher, cwd: str,
                 host_id: Optional[str] = None) -> Optional[dict]:
    """The live ROOT session whose detail-row `workspace` is `cwd`, or None.

    `host_id` is this machine's, INJECTED — resolved once per launch by
    `launch` and handed in like the herdr client and the watcher, so no test
    reaches a network for it. It defaults to None, which is the UNKNOWN state,
    and unknown filters nothing (see `session_on_another_host`): a caller that
    could not resolve an id gets the pre-filter behaviour rather than a silent
    skip of everything.

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

    The HOST rule is the second filter the listing cannot decide on its own, and
    it is applied for the same reason and in the same direction. A session on
    another machine whose project path is the SAME directory is a real shape —
    two checkouts of one repo, one machine asleep — and it passes everything
    above: the listing is server-wide, `workspace` is a path rather than a
    machine, and `runner_is_offline` is silent about a host that is merely not
    here. It was handed back as "the live session", `launch` took case (B), and
    `_adopt` typed an attach that dies with the same line quoted above. Unlike
    the runner, this one HAS an answer to ask — `/v1/hosts`, which already says
    which host is this one (see `local_host_id`) — so it is a real comparison
    rather than a heuristic. Both unknowns still keep the session, for the reason
    the paragraph above gives: a hosts endpoint we could not read must not be
    able to hide a local session. See `session_on_another_host` for the full
    truth table.

    The session's OWN `host_id` arrives the same way its directory does — on the
    DETAIL row, merged in by `og_herdr_watch._enrich` — because the LISTING row
    carries no `host_id` at all (measured; see `og_herdr_watch.DETAIL_FIELDS`).
    That is worth stating plainly here: a filter reading `session.get("host_id")`
    off an event the watcher did not enrich it with reads "unknown" on every
    single session, and unknown means "do not filter" — which is this rule
    silently doing nothing.

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
        # THE HOST RULE, last of the filters and for the same reason as the
        # runner one: a session belonging to another machine is not something
        # this pane can attach to, whatever its status and path say. LAST on
        # purpose — only a session that is otherwise exactly ours is worth a
        # line of terminal — and `continue` rather than `return`, so a foreign
        # row sitting in front of the real one does not hide it.
        if session_on_another_host(session, host_id):
            info("skipping session {0} (host {1} is not this machine)".format(
                session.get("id"), session.get("host_id")))
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


def log_line(ctx: Launch, line: str) -> None:
    """Append one timestamped line to the launcher log. Never raises.

    Best effort to exactly the degree the failure it describes is: a log that
    cannot be written must not turn a degraded launch into a failed one. A
    launch with no log at all (a test, or an older caller that passes no
    `log_path`) simply has no durable channel, which is the old behaviour and
    not a reason to fail.
    """
    path = launch_log_path(ctx)
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
    # Resolved ONCE and injected, like the herdr client and the watcher: the host
    # id is a fact about the MACHINE, not about any one session, so asking per
    # candidate would put the same request to the same endpoint up to a hundred
    # times to get one answer. `live_session` is called from exactly here.
    session = live_session(ctx.watcher, ctx.cwd,
                           host_id=local_host_id(ctx.watcher))
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
    _record(ctx, "pending", ctx.cwd, {
        "workspace_id": ws_id, "tab_id": tab_id, "pane_id": pane_id,
        "owner": OWNER_LAUNCHER, "cwd": ctx.cwd,
    })
    try:
        ctx.client.pane_run(pane_id, CHAT_COMMAND)
    except Exception:
        # No session was minted, so nothing will ever claim this record; drop it
        # and close the space it named (the create-missing-pane rule above:
        # "no stray empty spaces").
        _unrecord(ctx, "pending", ctx.cwd)
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
                      cwd: str, pane_env) -> list:
    """Put `og agents` in a split beside `root_pane`; RETURN what failed.

    ONE implementation of the split, called by the two things that open a pane
    og expects to carry an `og agents` beside it: the launcher's `_open_space`
    (case A/D) and the bridge's history/resume space
    (`og_herdr._add_history_space`). The bridge never split, so resuming a past
    conversation from the spaces list came up without the pane a fresh `og start`
    opens — and a second copy of the split would only drift, leaving the next
    person to discover which one ran.

    It RETURNS its failures rather than reporting them, so each caller can route
    a degradation through its own channel: the launcher's `_split_agents` sends
    each line through `report` (log + re-surface before the terminal is handed
    to herdr), and the bridge appends them to its `reconcile` lines. The strings
    are deliberately stable so the launcher's log-matching tests keep passing.
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
    pane_id = _split_pane_id(client, ws_id, tab_id, root_pane, reply)
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
        panes = client.call("pane.list", {"workspace_id": ws_id})
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


def _unrecord(ctx: Launch, bucket: str, key: str) -> None:
    """Drop one record, re-reading first for the same reason `_record` does.

    Used when a record names a space that turned out to be unusable — the chat
    never started, so no session will ever claim it. Missing key is not an
    error: the bridge may have claimed it already, which is also a fine outcome.
    """
    state = load_state(ctx.state_path)
    state.setdefault(bucket, {}).pop(key, None)
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
                  server=args.server,
                  agents_pane=agents_pane_enabled(config), env=dict(os.environ),
                  state_path=default_state_path(),
                  pane_env=pane_env_for(dict(os.environ)),
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
