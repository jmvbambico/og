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

Read by tests through injected fakes: the herdr client, the session watcher, the
host id this machine is known by, and the spawn of the mux's own server. Nothing
in this module connects to a herdr socket at import time, and no test has to.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from og_herdr import (
    DEFAULT_SPACE_CAP,
    OWNER_LAUNCHER,
    SPACE_LIVE,
    _id_of,
    # The note of which herdr tab carries which agent, written by the bridge
    # when it creates a tab and read here to move the operator to it. Named in
    # og_herdr so the writer and this reader cannot drift about the file or its
    # shape — see `remember_agent_tab` for why herdr cannot answer it.
    # The two ROLES a note can be keyed by. `TAB_ROLE_MAIN` is the agent's
    # Omnigent view (a root's own pane), `TAB_ROLE_CLI` the vendor-CLI tab
    # `focus_agent_terminal` opens on demand — the distinction that keeps Enter
    # off a root's chat pane. Named in og_herdr so the writer and this reader
    # cannot drift.
    TAB_ROLE_CLI,
    TAB_ROLE_MAIN,
    agent_tab_record,
    agent_tabs_path,
    attach_command,
    # Whether a harness is hosted as a real terminal TUI is a REGISTRY fact
    # (`kind`), so it is read from the registry rather than inferred from the
    # harness id's spelling — one source, no drift. See og_herdr.harness_kinds.
    harness_has_terminal,
    harness_labels,
    # The Omnigent home is resolved by ONE function in og_herdr, so this
    # launcher's daemon records and its state file cannot be read from two
    # different homes by a sandboxed test that redirected only one of them.
    default_home,
    default_state_path,
    load_state,
    # The writer of the note `focus_agent_terminal` reads back, in its
    # COMPARE-AND-SET form. Load-bearing here, twice over: the return says whether
    # the write LANDED (a vendor-CLI tab whose note did not land is closed again
    # rather than left to be duplicated), and the "existing" half decides the
    # winner when two panes race to open one agent's tab — so both never win and
    # leave a duplicate. See `remember_agent_tab_if_absent`.
    remember_agent_tab_if_absent,
    # The pane environment is stated ONCE, in og_herdr, next to the pane
    # commands it is about — the bridge opens panes too and had no copy of the
    # rule at all, which is how `og start herdr` left panes that opened a
    # browser. Imported here, not redefined: see og_herdr.pane_env_for.
    pane_env_for,
    space_status,
    state_update,
    workspace_index,
)

# The runner rule, imported rather than re-implemented: `poll_once` applies it
# for a SUB-AGENT but deliberately withholds it from a ROOT (a dead root is a
# history space, the spaces feature), so `live_session` is the one caller that
# has to ask it again — see the note there. The watcher is stdlib-only and
# imports nothing of ours at module load, so this is a cheap, acyclic import.
from og_herdr_watch import _server_key, runner_is_offline

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
# The `og agents` footer's full form is 50 display columns — the widest element
# the pane renders. (The move keys are the East Asian AMBIGUOUS ↑/↓, charged two
# columns by `_display_width`, so 50 is the width in every locale, CJK included.)
# Below that it DEGRADES rather than clips: `og_agents._footer` picks a 30-column
# compact form, then a 20-column bare key list, so every key stays discoverable at
# any width (only under 20 columns does the key list itself clip). The ratio is
# 0.7, not 0.8, because 0.8 gave the pane only ~43 columns on the operator's
# 215-column terminal — too narrow for the full form — while 0.7 leaves ~30%
# (~64 columns there), enough for the FULL footer form AND the 27-column QR. A
# pane that cannot show its own controls is broken.
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

# Where this machine's OWN host daemon records what it is: one JSON file per
# daemon og has started here, named by a config signature rather than by
# anything unique. See `_daemon_host_ids` — this is the source that identifies
# us rather than inferring it from a list of every host the account has.
DAEMON_DIR = "daemons"

# The keys a daemon record may state its server under. All three are read
# because which one is populated depends on mode and version — `resolved_server_url`
# is null on a server-mode record — and a record that answers under any of them
# is ours to use.
DAEMON_URL_KEYS = ("server_url", "target", "resolved_server_url")

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

def local_host_id(watcher, home=None) -> Optional[str]:
    """THIS machine's `host_id`, or None when nothing can say.

    Two sources, in this order, because they answer different questions.

    1. THIS MACHINE'S OWN DAEMON RECORD (`_daemon_host_ids`) — preferred, and
       the only source that is an IDENTITY rather than an inference:
       `<OMNIGENT_HOME>/daemons/*.json` is written by this machine's host daemon
       and states its host_id directly.
    2. `GET /v1/hosts` (`online_host_id`) — a fallback, accepted ONLY when
       exactly one host in the list is online.
    3. None: UNKNOWN. `session_on_another_host` filters nothing with it, so the
       launch behaves exactly as it did before any of this.

    Why the daemon record is asked FIRST, which is the whole point of the
    ordering: `/v1/hosts` answers "every host this ACCOUNT has, across every
    machine", so "the first online entry" is not an identity test — it is a
    position in a list this process has no stake in. The user this bug came from
    genuinely runs two machines, and on theirs the other one sorted first: every
    `og start herdr` then judged THIS machine's own sessions foreign, skipped
    them, and opened a brand new space each time. That is the unrecoverable
    direction (a second space, and a second `og chat` typed into a conversation
    the operator already had) arriving through the very filter added to prevent
    it, and it is strictly worse than the attach that dies.

    So ambiguity is not resolved by guessing — it resolves to UNKNOWN. Two online
    hosts and no daemon record is exactly the case where the list cannot tell us
    who we are, and "I do not know" is the only answer that cannot cost the
    operator a conversation. When the id IS known the rule is a real comparison
    and a foreign session is skipped as it should be.

    `home` is the seam, mirroring `SessionWatcher.discover_token(base_url,
    home=None)`: tests point it at a tmp directory rather than reading the
    developer's real `~/.omnigent`. Both helpers below swallow every failure —
    a missing directory, an unreadable file, a payload in a shape we do not
    recognise — and the catch is deliberately broad, because this whole lookup is
    ADDITIVE: the worst a bug here can do is leave the launch behaving exactly as
    it did before the filter existed. An exception escaping would kill the
    `og start` the operator is waiting on, which is worse than either bug.

    One thing that is NOT a fall-through: records that CONTEST the question (they
    name this server with two different machines). Our own evidence contradicting
    itself is exactly when "let the account-wide list answer instead" would be
    worst, so that is unknown, and it stays unknown no matter how tidy the API
    looks.
    """
    claimed = _daemon_host_ids(watcher.base_url, home)
    if len(claimed) == 1:
        return claimed.pop()
    if claimed:
        return None  # contested: see above
    return online_host_id(watcher)


def _daemon_host_ids(base_url: str, home=None) -> set:
    """Every host_id this machine's daemon records claim, for THIS server.

    `<OMNIGENT_HOME>/daemons/*.json` is written by this machine's host daemon
    (`omnigent host status` prints the same `host_id`), so a record is an answer
    from the machine itself rather than an inference about it.

    The FILE NAME is a config signature, not a key, so there can be several
    records — one per daemon og has started here, and a restarted daemon writes a
    new signature — and a record states which server it belongs to under any of
    `server_url`, `target` or `resolved_server_url` (which of the three is
    populated depends on mode and age, so all three are read). Only records
    naming THIS server are returned: with `--server` pointing elsewhere, this
    machine's daemon record is not the answer.

    The answer is a SET, not a value, because the caller has to tell three states
    apart and a string cannot: one id (we know), several (our own records
    disagree — unknown), none (nobody said — fall through to `/v1/hosts`).

    URLs are compared through `og_herdr_watch._server_key`, the same canonical
    form that decides which token belongs to which server: scheme, host case,
    port and path, with any userinfo dropped and a trailing slash gone, so
    `http://127.0.0.1:6767` and `http://127.0.0.1:6767/` are one server.
    Re-deriving those rules here is how two reads of "which server" would start
    disagreeing, which is the drift this module is written against.

    Every failure — no directory, an unreadable file, a record that is not an
    object, one with no usable `host_id`, none matching this server — simply
    contributes nothing. Nothing here raises: this lookup only ever sharpens a
    filter, so it must not be able to stop a launch.
    """
    base = Path(home) if home else default_home()
    try:
        paths = sorted((base / DAEMON_DIR).glob("*.json"))
    except (OSError, TypeError, ValueError):
        return set()
    wanted = _server_key(base_url)
    found = set()
    for path in paths:
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # unreadable or not JSON: this record says nothing
        if not isinstance(record, dict):
            continue
        if not any(_server_key(record[key]) == wanted
                   for key in DAEMON_URL_KEYS
                   if isinstance(record.get(key), str) and record[key]):
            continue
        host_id = record.get("host_id")
        if isinstance(host_id, str) and host_id:
            found.add(host_id)
    return found


def online_host_id(watcher) -> Optional[str]:
    """The host_id from `/v1/hosts`, but ONLY when exactly one host is online.

    The request goes through the WATCHER, never a urllib call of our own, for the
    reason `live_session` reads its listing that way: its `_get_json` already
    carries the base URL and this server's token, and `--server` can name a
    server we were not handed a token for. (`_get_json` is private; promoting it
    to a public method would be a change in og_herdr_watch, which this fix
    deliberately does not make.)

    `status == "online"` is the entry the project pin in bin/og reads, and it is
    the same field this fallback reads — but the PIN can afford a guess and this
    cannot, because here a wrong answer silently skips the operator's own
    session. So the fallback requires there to BE one online host: zero means the
    machine has not brought its host up (absence, not verdict), and more than one
    means the list is describing several machines and position in it identifies
    nothing (see `local_host_id` for the two-machine failure that decides this).
    Both are unknown, and unknown filters nothing.
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
    online = [host for host in hosts
              if isinstance(host, dict) and host.get("status") == "online"]
    if len(online) != 1:
        return None
    host_id = online[0].get("host_id")
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
    # The server is ensured BEFORE the session branch, not after it. Case (B)
    # reads herdr's workspace listing (`_attach_there` -> `space_for` ->
    # `live_workspaces`) to find the session's recorded space, and that listing
    # needs the socket: with the server down it raises, and `live_workspaces`
    # turns that into "could not list herdr workspaces: ...". When this call sat
    # below the `if session:` return, a case-(B) launch — outside the
    # multiplexer, a live session already in $PWD — took the attach and died on
    # that unreadable listing without ever starting the server it was invoked to
    # start. Case (A) is unaffected: no session means the same two statements
    # run in the same order.
    ensure_server(ctx)
    if session:
        return _attach_there(ctx, session)
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


def _agents_pane_already_split(baseline: Optional[set], pane_id: str) -> bool:
    """Is there a pane beside this one — the half of "is the agents pane there"
    that herdr can actually answer.

    ONE of the two halves only. `pane.list` returns pane_id, tab_id and
    workspace_id and nothing else — no command, no process — so this says a
    SECOND PANE EXISTS and never that it is `og agents`. A live agents pane and
    a shell the operator split for their own work are the same answer, which is
    why `og_owns_pane`'s record still has to agree before this is consulted at
    all. Existence is the right level to ask at: a wrong YES costs a third pane
    (the v0.18.1 defect) and a wrong NO costs the operator the pane they asked
    for.

    THE LIMITATION, and nothing here recovers it: a pane whose `og agents`
    CRASHED is still a pane. A space whose agents pane died reads exactly like a
    healthy one, so it stands down and is not repaired — the operator's remedy
    is to close the dead pane and re-run. Telling the two apart would take
    pane CONTENT, and pane contents are not in the protocol; `pane.read` would
    put a running command's output in front of a decision that runs before the
    REPL has drawn anything.

    `None` is "could not ask" and it answers NO. That is the OPPOSITE direction
    from what `chat_pane` does with the very same value a few lines below, and
    both are right because they are two different questions. Here it is "is the
    agents pane already there?", and not knowing must never be read as "it is" —
    an unreadable listing must not be what costs the operator the pane. Below it
    is "which pane is the one herdr is about to make?", and guessing from an
    unknown baseline is how `og agents` ends up typed into a pane somebody is
    working in, so that one ends in no split at all (see `tab_pane_ids`). One
    cosmetic pane missing beats one shell taken over, in both directions.
    """
    if baseline is None:
        return False
    return bool(baseline - {pane_id})


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

    Takes the PATH rather than a `Launch` because the record has two writers —
    the launcher's `_open_space` and `og chat`'s own pane (`record_chat_pane`)
    — and the rule below has to hold for both of them, not be re-implemented
    for whichever caller was written second.

    ONE TRANSACTION, re-read included: load, change, save, all under the lock
    `og_herdr.state_lock` takes (see `state_update`). The re-read is the old half
    of this rule and it is not enough on its own — it stops a load taken at the
    top of the launch from erasing what the bridge wrote in between, and it
    leaves a window between the load and the save that a writer in the middle of
    its own read-decide-write can fall into, which is exactly what the bridge's
    claim now is. Taking the same lock here is what makes the two serialise:
    whichever commits first, the second reads the first's file and writes on top
    of it. Nothing here is long — no herdr call, no HTTP — so holding it costs
    the other process microseconds, not a round trip.
    """
    def put(state: dict) -> dict:
        state.setdefault(bucket, {})[key] = record
        return state

    state_update(state_path, put)


def _unrecord(state_path: Path, bucket: str, key: str) -> None:
    """Drop one record, in the same locked transaction `_record` uses.

    Used when a record names a space that turned out to be unusable — the chat
    never started, so no session will ever claim it. Missing key is not an
    error: the bridge may have claimed it already, which is also a fine outcome
    and the one the lock makes certain of — a drop cannot land between the
    bridge's read and its claim and erase a record the bridge is about to move
    into `spaces`.
    """
    def drop(state: dict) -> dict:
        state.setdefault(bucket, {}).pop(key, None)
        return state

    state_update(state_path, drop)


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
                 pane_id: str) -> Optional[dict]:
    """The record that names this pane — None when og has none.

    The question is the one the boolean this first returned answered: "is this
    pane one og opened for a chat, rather than one the operator made?" A matched
    record IS that yes, and it comes back WHOLE rather than as its `owner` alone,
    because the caller has two things to read off it and one of them can be
    absent. Returning the record is what keeps None meaning exactly one thing —
    nobody matched, so this is not og's pane — where returning the owner alone
    overloaded it: a record that matched all three ids but carried no `owner`
    came back as the same None as no record at all, so the caller could not tell
    "somebody else's pane, split it" from "og's own pane whose writer said
    nothing, so ask the tab before splitting". Both are real and they are
    opposites; see `chat_pane` for what it does with each. A truthy dict still
    reads as a plain yes, so `if og_owns_pane(...)` is unchanged in meaning.

    The owner rides along because it answers a question neither the boolean nor
    the pane list could — "IS A SECOND HALF STILL COMING?" — which is why it is
    read rather than merely detected; see below.

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

    A pane listing cannot answer it on its own, and the reason is worth keeping
    in view because it is the trap this function exists to avoid: `pane.list`
    returns only pane_id, tab_id and workspace_id — no command, no process — so
    "this tab already holds the `og agents` pane" and "this tab already holds a
    pane the operator split themselves" are the SAME answer. Reading the second
    as the first is how a guard would silently take the agents pane away from
    every `og chat` an operator runs in a tab they made, which is the case
    `known_panes` exists to protect. `og chat` can still tell panes apart from
    one another; it cannot tell what a pane is running.

    Which is why this answer is only ever HALF the guard. The caller pairs it
    with `_agents_pane_already_split`, which asks herdr whether the tab holds a
    pane beside this one — existence, not identity. Neither half is
    sufficient alone: this one says og opened the pane and not that the split
    that was supposed to follow it ever happened (the defect that left a resumed
    history space without its agents pane), and the other cannot say a pane is
    og's at all.

    IS A SECOND HALF STILL COMING? — that, and not "did the split work", is what
    `owner` answers, and it is why the record is returned rather than a bare yes.
    A record is written when a space is CREATED and says nothing about the split
    that is supposed to follow it; what it carries is WHO wrote it, and that is
    what decides the two cases the caller must treat differently. `OWNER_LAUNCHER`
    is the launcher's own record: `_open_space` writes it, types `og chat` into
    the pane and issues the split IN THE SAME CALL, so by the time this process
    reads the file the split is already in flight — a second half IS coming, and
    the caller stands down on the record alone, with no pane listing and no race
    to lose. `OWNER_HISTORY`/`OWNER_BRIDGE` are records a half wrote when it
    CREATED the space and then returned; nobody is coming, so the caller must ask
    herdr whether the split happened and make it if it did not (see
    `_agents_pane_already_split`).

    Every writer sets `owner` and none invents a missing one: `_open_space` and
    `record_chat_pane` on the launcher's side, `_remember` (defaulting to
    `OWNER_BRIDGE`) for the bridge's, and `_claim_pending` carries the value
    through a claim unchanged — so a matching record normally has one. A record
    that has none is nonetheless a shape the caller must survive: it is stale or
    hand-edited, since nothing current writes one, and it can still match on all
    three ids. The MATCH is the fact that matters — it proves og opened this
    pane — so the record comes back and the caller reads the absent owner as
    "nobody promised a second half, so ask the tab", never as "not og's, split".
    Returning the whole record is what makes that distinction expressible at all;
    see `chat_pane`.

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
        return None
    for bucket in ("pending", "spaces"):
        for record in (state.get(bucket) or {}).values():
            if not isinstance(record, dict):
                continue
            if (record.get("workspace_id") == workspace_id
                    and record.get("tab_id") == tab_id
                    and record.get("pane_id") == pane_id):
                return record
    return None


# ---------------------------------------------------------------------------
# `og chat`'s end of the same handshake — the other way a space gets a session
# ---------------------------------------------------------------------------
# The user's flow this exists for: "new space" in herdr, `cd` to the repo, `og
# chat`. No launcher ran, so nothing wrote the pending record the bridge adopts
# — and the bridge, seeing a root session appear in a directory it has no space
# for, opens a SECOND space for the conversation already on screen. So `og
# chat` writes the record itself, for the pane it is running in, and puts the
# agents pane beside it the way `_open_space` does — EXCEPT when og's own record
# says a second half is coming or one already came: the LAUNCHER's record means
# its split is in flight in the same call (stand down on it alone), and a
# HISTORY/BRIDGE record — or a matching record whose writer left no owner at all,
# which the match still proves is og's — means the split, if any, happened at
# space creation, so the tab is asked and the split is made only when no second
# pane is there. Two halves doing one job is what opened three panes in a tab the
# operator expects to hold two. See `og_owns_pane`, `_agents_pane_already_split`
# and `chat_pane`.
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
    split and this half stands down. WHICH record `og_owns_pane` returns decides
    how: the launcher's own record means the split is in flight in that same call
    (stand down on the record alone, BEFORE any client is built, no pane listing
    and no socket to race), while a `history`/`bridge` record — and a matching
    record whose writer left no owner, which the match still proves is og's —
    was written at space CREATION by a half that has since finished, so the tab is
    asked whether a pane is really beside this one and the split is made only when
    it is not. An operator-made pane has no such record and is split as it always
    was, and an og-owned pane whose split never happened — the agents pane switched
    on after the space opened, a split that failed, a pane the operator closed — is
    split now rather than left permanently without one. See
    `_agents_pane_already_split` for what that check can and cannot see, and
    `og_owns_pane` for why the two owners differ.

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
    owned = og_owns_pane(state, ws_id, tab_id, pane_id)
    try:
        record_chat_pane(state, here, ws_id, tab_id, pane_id)
    except Exception as exc:
        # The record is what stops a second space appearing; failing to write it
        # is not worth refusing the chat over, but it must never be silent.
        _chat_degrade("could not record this pane for the bridge ({0})".format(exc))

    if not agents_pane_enabled(config):
        return 0

    if owned is not None and owned.get("owner") == OWNER_LAUNCHER:
        # THE LAUNCHER'S OWN RECORD, and the half that must NOT be raced — stood
        # down HERE, above the client construction, so the launcher path opens NO
        # socket at all. This is the hot path (every fresh `og start` pane) and
        # v0.18.1 returned before building a client; a connection here is pure
        # waste, and every guarded build is one more thing that can raise on the
        # way to a chat that was never going to need it. `owner` answers "IS A
        # SECOND HALF STILL COMING?", not "did the split work": `_open_space`
        # writes this record, types `og chat` and issues its split IN THE SAME
        # CALL, so the split is already in flight by the time this process
        # finishes exec'ing python — a second half IS coming. Standing down on the
        # record alone is therefore deterministic and costs no socket round trip;
        # asking herdr where the tab's panes are would be a RACE, because the pane
        # that split is making need not be listed yet, and losing it means
        # splitting here too — the three-pane bug this whole path exists to
        # prevent.
        #
        # The price, accepted and paid here: a LAUNCHER space whose split FAILED
        # is not repaired by this half. It does not need to be. The launcher
        # reports its own split failure (`report`, in `_split_agents`), and that
        # line re-surfaces before the terminal is handed to herdr, so the operator
        # is TOLD. The HISTORY/BRIDGE path below has no such channel — its
        # degradation is only a reconcile line — which is exactly why that path
        # gets the pane check and this one does not.
        return 0

    try:
        # Constructing a client cannot connect — it resolves a socket path and
        # waits — but it CAN raise (an unimportable `og_herdr_client`, a home
        # directory `Path.home()` cannot resolve), and `bin/og` treats any
        # non-zero exit from this helper as `exit`, taking the chat down with
        # it. Cosmetic pane versus the session the operator asked for is not a
        # close call, so a client that cannot even be built degrades and the
        # chat starts. Unchanged in practice on the split path, and it is what
        # keeps the creation-half stand-down — which reaches here for the first
        # time now that it asks herdr — from being able to stop a chat at all.
        # The launcher path does not reach here at all; it stands down above.
        opened = client if client is not None else _chat_client(environ)
    except Exception as exc:
        _chat_degrade("could not reach herdr to open the og agents pane "
                      "({0}); the chat is up without it".format(exc))
        return 0
    try:
        # ONE listing, read before the remaining decision and used by both of its
        # halves: whether to stand down (is there a pane beside this one?) and, if
        # not, what the tab held BEFORE the split. Hoisted so the split path is
        # still exactly one round trip — and not reached at all on the launcher
        # path above, which stands down on the record alone.
        #
        # Read BEFORE splitting: unlike a launch, this tab may be one the
        # operator made and split themselves, and "the first pane in this tab
        # that is not the chat pane" is then their pane, not ours.
        baseline = tab_pane_ids(opened, ws_id, tab_id)
        if owned is not None and _agents_pane_already_split(baseline, pane_id):
            # THE CREATION HALF, and also every matching record that promises no
            # second half. A `history`/`bridge` record is written when a space is
            # CREATED, by a half that has since finished, and says nothing about
            # whether the split that was supposed to follow it ever happened:
            # `og_herdr._add_history_space` remembers the pane and then splits
            # best-effort (`_split_agents` returns early when `herdr_agents_pane`
            # is off, which the operator can turn on afterwards), a split can fail
            # into a reconcile line nobody read, and the operator can close the
            # pane. NOBODY is coming to split again, so the tab is asked and only
            # a pane that is demonstrably there counts — otherwise `og chat
            # --resume` into a past conversation comes back with no agents pane at
            # all — no error, nothing degraded, nothing to notice.
            #
            # A record that matched all three ids but carried NO `owner` is the
            # same case from the other end, and it is why `og_owns_pane` returns
            # the whole record: the match PROVES og opened this pane, and nothing
            # in it says a second half is coming, so the tab is asked rather than
            # split on an assumption. Splitting on an unknown owner while a
            # second pane is already there is a DUPLICATE agents pane — the exact
            # bug this guard exists to kill. "No record at all" is still the other
            # direction and still needs no listing: not og's, so the split is
            # simply made.
            #
            # The cost is bounded in the only direction that matters: the worst
            # answer this can give is a YES about a pane that is not `og agents`
            # (see `_agents_pane_already_split`), which costs a third pane, and
            # it needs a record AND a second pane for that to happen — never a
            # pane count on its own.
            return 0

        if baseline is None:
            # A listing that could not be read is not an empty tab (see
            # `tab_pane_ids`), and guessing from one would type `og agents` into
            # whatever the operator has — so NO split is made: a pane nothing
            # ever runs in is the exact thing `_open_space` refuses to leave
            # behind. Note that this is the OTHER use of the same `None`, and it
            # points the other way on purpose; `_agents_pane_already_split` says
            # why.
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
# the agent's own terminal: attaching a vendor-CLI tab to the native harness TUI
# ---------------------------------------------------------------------------
# herdr gives an agent ONE pane, the Omnigent chat view. The ACTUAL harness (the
# real Claude Code / codex / opencode TUI) is hosted by Omnigent as an
# interactive terminal inside a PRIVATE tmux server, one server per harness
# session, and is invisible from herdr. This section RESOLVES that terminal so
# `focus_agent_terminal` can open a tab attached to it.
#
# `OMNIGENT_TMUX_SOCK` is popped from the environment for security, so the only
# handle left is on disk, under
#
#     <tmp>/omnigent-<uid>/<harness>/<internal_id>/bridge.json
#     <tmp>/omnigent-<uid>/<harness>/<internal_id>/tmux.json
#
# `bridge.json` names the og session (`conversation_id` / `active_session_id`);
# its SIBLING `tmux.json` carries the socket path and the tmux target. The
# `<internal_id>` directory is OPAQUE — NOT the og session id — so a session can
# only be resolved to its terminal by SCANNING, which is what
# `resolve_harness_terminal` does. There is no registry of these anywhere else,
# and `<tmp>` is read from `$TMPDIR` at call time (never frozen at import) so a
# test can redirect it — the same rule `install_config` follows for
# `$OMNIGENT_HOME`.
OMNIGENT_TMP_PREFIX = "omnigent-"
BRIDGE_FILENAME = "bridge.json"
TMUX_FILENAME = "tmux.json"
# The `bridge.json` keys that name the og session, tried in this order.
BRIDGE_SESSION_KEYS = ("conversation_id", "active_session_id")
# `tmux.json` omits a target only if its writer changes; `main` is what every
# record measured so far carries, so it is the honest default rather than a guess.
DEFAULT_TMUX_TARGET = "main"

# The client flags the vendor-CLI attach runs with. SPELLED OUT HERE BECAUSE THEY
# ARE THE SAFETY PROPERTY, not a cosmetic choice — this is the one thing in this
# feature that can harm a LIVE agent:
#
#   * THE SIZING HAZARD. tmux sizes a window to the clients attached to it, and
#     the live harness sessions run at the operator's own terminal size. An
#     ordinary client attaching from our (usually smaller) pane WOULD resize the
#     window and visibly corrupt the display of a working agent — and no test in
#     this suite could catch it, because it only happens against a real socket.
#     `ignore-size` is tmux's own answer: "the client does not affect the size of
#     other clients", so the running agent's geometry is untouched.
#   * The attach must be INTERACTIVE. Omnigent's web bridge deliberately keeps
#     `ignore-size` and does NOT pass `-r` (read-only) because tmux >= 3.7
#     rejects `send-keys` from a read-only client. We want the real vendor CLI
#     to be typeable; dropping `read-only` gives up the "no second hand" guard
#     in exchange for interactivity. The sizing guard `ignore-size` remains.
#
# `ignore-size` alone is interactive and non-resizing. Verified against tmux
# 3.8 (`man tmux`, attach-session): "-f sets a comma-separated list of client
# flags ... ignore-size the client does not affect the size of other clients ...
# read-only the client is read-only". See `test_the_cli_attach_cannot_resize_or_type`.
TMUX_ATTACH_FLAGS = "ignore-size"


def omnigent_tmp_root(environ: Optional[dict] = None) -> Optional[Path]:
    """`<tmp>/omnigent-<uid>`, the per-user root of Omnigent's runtime records.

    Read from `$TMPDIR` NOW rather than frozen at import, exactly as
    `install_config` reads `$OMNIGENT_HOME`: a test (or an operator in a sandbox)
    redirects the variable, and a constant could not follow it. None when the uid
    cannot be read at all (a non-POSIX platform), which the caller reads as "no
    terminal", never as an error.
    """
    env = os.environ if environ is None else environ
    tmp = env.get("TMPDIR") or tempfile.gettempdir()
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover - POSIX only in practice
        return None
    if not tmp:
        return None
    return Path(tmp) / (OMNIGENT_TMP_PREFIX + str(uid))


def resolve_harness_terminal(session_id: Optional[str],
                             environ: Optional[dict] = None) -> Optional[tuple]:
    """`(socket_path, tmux_target)` for the native harness TUI behind a session.

    None when there is nothing to attach to, and NEVER an exception: a missing
    directory, an unreadable or malformed `bridge.json`/`tmux.json`, no session
    match, a missing target, or a socket path that is no longer a socket on disk
    all read as "no terminal". A STALE record — a `tmux.json` whose socket is
    gone — is treated as ABSENT rather than attached to, because attaching to a
    dead socket is exactly how a pane opens onto nothing.
    """
    if not session_id:
        return None
    root = omnigent_tmp_root(environ)
    if root is None:
        return None
    try:
        harness_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return None
    for harness_dir in harness_dirs:
        try:
            session_dirs = sorted(p for p in harness_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for session_dir in session_dirs:
            if not _bridge_names_session(session_dir / BRIDGE_FILENAME, session_id):
                continue
            terminal = _read_terminal(session_dir / TMUX_FILENAME)
            if terminal is not None:
                return terminal
    return None


def _bridge_names_session(path: Path, session_id: str) -> bool:
    """Whether the `bridge.json` at `path` names `session_id`. Never raises.

    `_read_json` keeps the never-raise contract (it swallows OSError and
    ValueError), but discovery must not crash even if a read seam fails — the
    test injects a `PermissionError` at that seam to prove the resolver never
    raises. A bridge we cannot read names no session, so it is skipped and the
    scan keeps looking.
    """
    try:
        record = _read_json(path)
    except Exception:
        return False
    if not isinstance(record, dict):
        return False
    return any(record.get(key) == session_id for key in BRIDGE_SESSION_KEYS)


def _read_terminal(path: Path) -> Optional[tuple]:
    """`(socket_path, target)` from a `tmux.json`, or None. Never raises.

    The socket is checked ON DISK, not merely present in the record: a stale
    `tmux.json` names a socket the harness server has already unlinked, and that
    is indistinguishable from a usable one until you try to attach to it.
    """
    record = _read_json(path)
    if not isinstance(record, dict):
        return None
    socket_path = record.get("socket_path")
    if not isinstance(socket_path, str) or not socket_path:
        return None
    if not _is_socket(socket_path):
        return None
    target = record.get("tmux_target")
    if not isinstance(target, str) or not target:
        target = DEFAULT_TMUX_TARGET
    return (socket_path, target)


def _read_json(path: Path) -> Any:
    """One JSON file, or None for anything unreadable. Never raises."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _is_socket(path: str) -> bool:
    """Whether `path` is a socket on disk RIGHT NOW — the stale-record test."""
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# the runner's view of a session's terminals — the AUTHORITATIVE path
# ---------------------------------------------------------------------------
# The filesystem scan above resolves `claude-native` and nothing else, and that
# is measured rather than suspected: the claude-native harness writes a
# `tmux.json` beside its `bridge.json`, while `codex-native` writes NO
# `tmux.json` at all — its `bridge.json` is an MCP config — so a codex
# sub-agent has no on-disk record for the scan to find and is unresolvable by
# it today (see omnigent/runtime/harnesses/codex_native/bridge.py).
#
# The Omnigent web UI never guesses at files. It asks the RUNNER for a
# session's terminal RESOURCES — `GET /v1/sessions/{id}/resources/terminals`,
# the route its terminal tabs are built from — and each terminal's metadata
# carries the tmux socket and target. The runner owns the registry that
# launched the terminal, so that answer is authoritative; the scan is a
# FALLBACK kept for the case the runner cannot be reached at all.
RUNNER_TERMINALS_ROUTE = "/v1/sessions/{0}/resources/terminals"
# The resource `type` this reads. The same listing carries `environment`
# resources (the default one and a per-terminal one) whose metadata has no
# `tmux_socket`; naming the type says so once rather than depending on a
# missing key.
TERMINAL_RESOURCE_TYPE = "terminal"
# Where the runner's terminal metadata carries the tmux coordinates
# (omnigent.entities.session_resources.terminal_resource_view; measured against
# a live server). Deliberately NOT `socket_path`/`tmux_target`: that is the
# spelling of OUR `tmux.json`, and the two schemas are read by two functions
# because they are two different documents.
RESOURCE_SOCKET_KEY = "tmux_socket"
RESOURCE_TARGET_KEY = "tmux_target"

# og's server is reached by URL, and the URL is derived from the install, not
# invented here: bin/og exports `OG_PORT` from og.env and `og setup` records the
# same number as `port` in og-install.json. `OG_SERVER` is the explicit override
# (and the seam a test uses); `OG_PORT` is next because og.env is what bin/og
# actually reads; the install plan is the fallback for a caller with neither.
RUNNER_URL_ENV = "OG_SERVER"
PORT_CONFIG_KEY = "port"

# How long the runner lookup may take before it reads as "no answer". BOUNDED
# on purpose, and this is the one thing here that is not just a copy of the
# watcher's transport: `SessionWatcher`'s own opener passes no timeout at all,
# and this call happens on a keypress in `og agents`, so a server that is up
# but wedged would hang the pane forever — strictly worse than the tab not
# opening. The timeout is carried by an OPENER rather than by the watcher so
# the shared transport keeps its own behaviour.
RUNNER_TIMEOUT_SECONDS = 3.0


def runner_base_url(env: Optional[dict] = None) -> str:
    """The Omnigent server a session's terminal resources are asked of.

    `OG_SERVER` wins outright (the explicit override, and the seam a test uses
    so it never reaches a developer's live server). `OG_PORT` is next: it is what
    bin/og sources from og.env and what its own server is started on. The install
    plan's `port` is the same number for an install that never exported it, and
    `DEFAULT_SERVER` is the last resort so a caller with no environment at all
    still asks somewhere plausible rather than raising.
    """
    env = os.environ if env is None else env
    explicit = env.get(RUNNER_URL_ENV)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().rstrip("/")
    port = env.get("OG_PORT")
    if isinstance(port, str) and port.strip().isdigit():
        return "http://127.0.0.1:{0}".format(port.strip())
    configured = install_config().get(PORT_CONFIG_KEY)
    if isinstance(configured, int) and not isinstance(configured, bool) \
            and configured > 0:
        return "http://127.0.0.1:{0}".format(configured)
    return DEFAULT_SERVER


def _runner_opener(timeout: float):
    """A urlopen bound to `timeout`, for `SessionWatcher(opener=...)`.

    The transport and the auth stay the watcher's (`_get_json` adds this
    server's bearer token and nothing else does); only the patience is ours.
    """
    def open_request(request):
        return urllib.request.urlopen(request, timeout=timeout)

    return open_request


def _runner_watcher(base_url: str):
    """A watcher for `base_url`, carrying that server's token. Lazy import."""
    from og_herdr_watch import SessionWatcher

    return SessionWatcher(
        base_url=base_url,
        token=SessionWatcher.discover_token(base_url),
        opener=_runner_opener(RUNNER_TIMEOUT_SECONDS))


def resolve_runner_terminal(session_id: Optional[str],
                            env: Optional[dict] = None,
                            watcher=None) -> Optional[tuple]:
    """`(socket_path, tmux_target)` from the runner's terminal resources, or None.

    NEVER raises, and the contract is absolute: a runner that is absent, an HTTP
    error, a timeout, malformed JSON, no terminal resource for the session, a
    `socket_path` that is no longer a socket on disk — every one reads as "no
    terminal", never as an exception. `og agents` is a view an operator leaves
    open all day, so this must not be able to kill the pane on a keypress.

    `watcher` is the injection seam: a caller (or a test) can hand in something
    with `base_url` and `_get_json`, which is the same shape `SessionWatcher`
    already presents and the same method the launcher's `/v1/hosts` lookup rides
    (see `online_host_id`) — one transport, one place this server's token is
    added. None means "build the real one", and a build that fails is "no
    terminal" like every other miss.

    A stale entry is SKIPPED rather than fatal: a listing that names two
    terminals, one of them dead, still resolves the live one. The scan checks
    the socket on disk for exactly this reason (see `_read_terminal`).
    """
    if not session_id:
        return None
    if watcher is None:
        try:
            watcher = _runner_watcher(runner_base_url(env))
        except Exception:
            return None
    try:
        base = str(getattr(watcher, "base_url", "") or runner_base_url(env))
        url = base.rstrip("/") + RUNNER_TERMINALS_ROUTE.format(
            urllib.parse.quote(str(session_id), safe=""))
        payload = watcher._get_json(url)
    except Exception:
        return None
    return _terminal_from_resources(payload)


def _terminal_from_resources(payload: Any) -> Optional[tuple]:
    """`(socket_path, target)` out of a terminal-resources page, or None.

    The page is a `PaginatedList` (`{"object": "list", "data": [resource, …]}`).
    Every shape here is read defensively because this parses a document this
    module does not own: a top level that is not an object, no `data`, an entry
    that is not an object, metadata that is not one, a `tmux_socket` that is not
    a non-empty string — each is skipped, and a page with nothing usable in it is
    `None` rather than an exception.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict) or item.get("type") != TERMINAL_RESOURCE_TYPE:
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        socket_path = metadata.get(RESOURCE_SOCKET_KEY)
        if not isinstance(socket_path, str) or not socket_path:
            continue
        if not _is_socket(socket_path):
            continue
        target = metadata.get(RESOURCE_TARGET_KEY)
        if not isinstance(target, str) or not target:
            target = DEFAULT_TMUX_TARGET
        return (socket_path, target)
    return None


def terminal_behind(session_id: Optional[str],
                           env: Optional[dict] = None,
                           watcher=None) -> Optional[tuple]:
    """The harness terminal behind a session: the runner's answer, else the scan.

    THE ORDER IS THE POINT. The runner knows which tmux server it launched for
    this session and answers for every native harness, codex included; the
    filesystem scan knows only the harnesses that write a `tmux.json`, so asking
    it first would leave the codex case broken for no reason. It is kept as the
    FALLBACK, tried only when the runner gave no answer, because the runner is
    not always reachable (a bridge running against a server that has since
    stopped, a `og agents` pane on a machine whose runner is down) and the scan
    resolves a live `claude-native` session with no network at all.

    Never raises, for the reasons both halves give.
    """
    terminal = resolve_runner_terminal(session_id, env, watcher)
    if terminal is not None:
        return terminal
    return resolve_harness_terminal(session_id, env)


def read_only_attach_command(socket_path: str, target: str) -> str:
    """The command a vendor-CLI tab runs to attach to an agent's harness.

    The flags come from `TMUX_ATTACH_FLAGS` and carry the resize/typing safety
    trade-off documented there (non-resizing via `ignore-size`, interactive).
    The socket and target are shell-quoted because both reach a shell in the
    pane: a `$TMPDIR` with a space in it is unusual but not impossible, and an
    unquoted path would silently split the command.
    """
    return "tmux -S {0} attach -f {1} -t {2}".format(
        shlex.quote(socket_path), TMUX_ATTACH_FLAGS, shlex.quote(target))


# ---------------------------------------------------------------------------
# move the operator to an agent's tab: Enter in `og agents`
# ---------------------------------------------------------------------------
# What `focus_agent_terminal` says when it cannot move the operator. One line
# each and DISTINCT on purpose, so "this can never work here" reads differently
# from "try again"; short so they survive a narrow pane's `_clip`.
NOTICE_FOCUS_OUTSIDE_HERDR = "focus: not inside herdr (no HERDR_ENV)"
NOTICE_FOCUS_NO_AGENT = "focus: no agent on this row"
# The THIRD answer `harness_has_terminal` can give, kept distinct rather than
# collapsed into either branch: with an unknown harness og cannot say whether
# the pane in this tab runs the real vendor CLI or a REPL, so it will not CREATE
# one. It is returned only when no `main` tab is already on record for the agent
# — a recorded tab is a fact og holds, and the harness kind is not needed to
# focus it (the v0.20.0 report: a worker arrived as an unknown harness while its
# own tab sat visibly beside the pane).
NOTICE_FOCUS_UNKNOWN_HARNESS = ("focus: {0} is not a harness og knows, so its "
                                "tab is unverified")
NOTICE_FOCUS_NO_TAB = "focus: no herdr tab recorded for this agent"
# A NATIVE agent whose harness terminal cannot be resolved: the vendor CLI is
# gone (the harness exited, the runner is offline, the codex-native record does
# not exist), so there is no real CLI to show. Distinct from NO_TAB, which is
# "we never noted a tab", and from the unknown-harness line: this one means "the
# harness is one you should be able to attach to, and there is nothing there".
NOTICE_FOCUS_NO_TERMINAL = ("focus: no live terminal for this agent (its harness "
                            "may have exited)")
# Nowhere to put the tab: the agent's space is not in og's notes or the state
# file, so og cannot open a tab for it without guessing at a space id.
NOTICE_FOCUS_NO_SPACE = "focus: this agent has no herdr space to open a tab in"
# The tab was opened and the note recording it did NOT land. The tab is closed
# again before this is returned (see `_open_vendor_cli_tab`), so what the
# operator reads is the whole story: nothing was left on screen for the next
# Enter to duplicate. See the commit that fixed the same shape twice before —
# an unrecorded pane is a duplicate waiting to happen.
NOTICE_FOCUS_NOT_RECORDED = "focus: the new CLI tab could not be recorded"
# The SAME failure, one worse: the note did not land AND the recovery close
# failed. The tab is on screen with nothing recording it, which is exactly how
# the next Enter opens a SECOND one. og cannot force herdr to release a tab, so
# this is a MITIGATION, not a cure — say so plainly so the operator can close it
# by hand. This is the PR #39 lesson (`NOTICE_CLI_NOT_RECORDED_OR_CLOSED`) carried
# over from the mirror: a close that silently fails must not be reported as a
# clean "not recorded".
NOTICE_FOCUS_NOT_RECORDED_OR_CLOSED = (
    "focus: a CLI tab was left open (could not record or close it)")
NOTICE_FOCUS_FAILED = "focus: {0}"


def focus_agent_terminal(session_id: str, harness: Optional[str],
                         root_session_id: str = "",
                         env: Optional[dict] = None,
                         client=None) -> Optional[str]:
    """Focus the tab showing this agent's real CLI. None on success, else why.

    THE INTERFACE `og agents` USES for Enter. `session_id` is the SELECTED row's
    own session (root or worker), `root_session_id` that row's root when it has
    one, and `harness` the row's harness id — the `og agents` pane resolves all
    three off the row it has (a root's `harness` comes from its detail fetch, a
    worker's from the install's agent spec). `client` and `env` are the test
    seams; None builds the real herdr client from `env`.

    Returns None when the operator's herdr UI moved, or a short one-line notice
    saying why it did not — the same shape and the same contract as
    `focus_in_herdr` in `og_agents`, and for the same reason: this pane is left
    open all day, so a herdr that is absent, a socket that is down, a socket
    that WILL NOT CONNECT and a state file that cannot be read must each become
    a notice rather than a traceback that kills the view. It NEVER raises, and
    the formatting is guarded too, so a failure whose own message cannot be
    rendered still comes back as text.

    WHY A TAB AND NOT `agent.focus`. herdr's `agent.get`/`agent.list` carry a
    `tab_id`, but nothing links an OMNIGENT session id to one of those records:
    `agent.get {target}` resolves a live agent NAME or the PANE id hosting the
    agent, and a session id answers `agent_not_found` — measured against the
    live server, on both the CLI and the socket API (see
    `og_herdr.remember_agent_tab`, which is where og records the tab it minted
    for exactly this reason). A TAB ID, unlike a session id, is something herdr
    will act on directly, so `tab.focus` is the one call that can work.

    WHAT IT REACHES, per harness kind, and this is the whole decision:

      * **native** (`claude-native`, `codex-native`, …) — the agent's REAL vendor
        CLI, in a tab of its own. If that tab is not there yet it is OPENED HERE,
        on demand, in the agent's space: a tab the bridge never made, holding the
        same interactive `tmux attach` a native worker's harness terminal needs
        (`read_only_attach_command`).
        A launcher-created ROOT therefore behaves exactly like a bridge-created
        one — the operator gets the agent's CLI in a NEW tab and the conversation's
        own pane is left exactly as it was. THE MAIN PANE IS NEVER REPURPOSED.
      * **ACP** (`acp:*`) — the agent's own og chat view, `omnigent attach`, which
        is what that agent's tab already holds (the same view `og chat`'s
        down-arrow menu gives). Nothing is created.
      * **unknown** — the third answer from `harness_has_terminal`, NOT collapsed
        into either branch: og cannot say what the row's tab is, so it never
        CREATES one and never guesses. It still focuses a `main` tab it has
        ALREADY recorded for the agent — where the tab is a fact og holds the
        kind decides nothing — and declines with its own line only when no tab is
        on record.

    The tab is found from og's own records, keyed by ROLE (`og_herdr.TAB_ROLE_*`):
    `TAB_ROLE_CLI` is the vendor-CLI tab an agent may have, `TAB_ROLE_MAIN` the
    tab holding its Omnigent view. For an ACP agent the `main` note is read first,
    then the state file's `spaces[session_id].tab_id` (a root whose space the
    LAUNCHER opened — the bridge only CLAIMED it, so it never made a tab to note),
    then the root's tab for a worker whose own note is gone. For a native agent the
    `cli` note is read, and the state file's record is NOT used as a fallback: that
    tab is the Omnigent view (`_pane_command` notes a root's own tab as `main`
    precisely so this stays true), and sending the operator there is the failure
    this whole role distinction exists to prevent. Notes are dropped when the panes
    go (`Bridge._forget_tab`), so a tab id is never aimed at after herdr re-used it.

    IDEMPOTENCE IS THE PROPERTY THAT MATTERS MOST HERE: pressing Enter on the same
    agent twice must land on the SAME tab. That is what the `cli` note buys — the
    second call finds the note and focuses it, and `tab.create` is not reached. And
    the mirror of it: if the note write does NOT land, the tab that was just opened
    is CLOSED again (`NOTICE_FOCUS_NOT_RECORDED`), because a tab on screen that
    nothing records is exactly how the next Enter opens a second one — the
    duplicate-pane defect this project has already paid for twice.

    The focus itself is ONE call. Measured against a live server: `tab.focus` also
    raises the WORKSPACE holding the tab (see `og_herdr_client.tab_focus`), so no
    `workspace.focus` is sent first — agents routinely live in a space other than
    the one on screen, and one call moves both.
    """
    environ = dict(os.environ) if env is None else dict(env)
    if not session_id:
        return NOTICE_FOCUS_NO_AGENT
    # Inside the guard, and before anything else: this pane must render in ANY
    # terminal, so a probe that raises has to become a notice like every other
    # failure rather than killing the view on a keypress.
    try:
        inside = is_inside(environ)
    except Exception:
        return NOTICE_FOCUS_OUTSIDE_HERDR
    if not inside:
        return NOTICE_FOCUS_OUTSIDE_HERDR
    # Both reads are guarded even though neither is documented as raising: this
    # function's "NEVER raises" is absolute, and these are the two seams that
    # touch something outside it. `harness_has_terminal` reads the registry (a
    # catalog that is not this module's), and `default_state_path` resolves
    # $OMNIGENT_HOME through `Path.home()`, which a sandboxed pane can leave
    # unresolvable. A failure in either is exactly "og cannot tell" and "there is
    # no tab to find" — the notices this already has — never a traceback that
    # kills the view.
    try:
        kind = harness_has_terminal(harness)
    except Exception:
        kind = None

    try:
        state_path = default_state_path()
        tabs_path = agent_tabs_path(state_path)
    except Exception:
        return NOTICE_FOCUS_NO_TAB

    terminal = None
    tab_id = None
    if kind is True:
        tab_id = _noted_tab(tabs_path, session_id, TAB_ROLE_CLI)
        if not tab_id:
            # No vendor-CLI tab yet: resolve the terminal FIRST, so an
            # unresolvable one is an honest notice and never an empty tab.
            try:
                terminal = terminal_behind(session_id, environ)
            except Exception:
                terminal = None
            if terminal is None:
                return NOTICE_FOCUS_NO_TERMINAL
    else:
        # ACP (`kind is False`) and unknown (`kind is None`) share ONE lookup: the
        # `main` tab og has ALREADY recorded for this agent. The harness KIND only
        # decides which tab to PREFER and whether a vendor-CLI tab may be CREATED
        # — it is not needed to focus a tab on record. Declining an unknown
        # harness even when its tab was right there is the v0.20.0 report: a
        # worker's listing carries no harness, so Enter arrived with "" and
        # refused the agent's own visible tab. The `tab_id` truthy-check below
        # keeps `_open_vendor_cli_tab` unreachable for an unknown harness: og
        # cannot know a vendor TUI is behind it, and opening a tab onto nothing is
        # the failure this third answer exists to prevent — focusing a recorded
        # tab is not creating one.
        tab_id = _main_tab_id(tabs_path, state_path, session_id, root_session_id)
        if not tab_id:
            if kind is None:
                label = harness if isinstance(harness, str) and harness.strip() \
                    else "this agent"
                return _focus_line(NOTICE_FOCUS_UNKNOWN_HARNESS, label)
            return NOTICE_FOCUS_NO_TAB

    # ONE client for the whole call, built only once there is something to do
    # with it — and closed here when we built it. Constructing one does not
    # connect (see `_chat_client`).
    try:
        opened = client if client is not None else _chat_client(environ)
    except Exception as exc:
        return _focus_line(NOTICE_FOCUS_FAILED, exc)
    try:
        if not tab_id:
            notice, tab_id = _open_vendor_cli_tab(
                session_id, harness, environ, opened, tabs_path, state_path,
                terminal, root_session_id)
            if notice is not None:
                return notice
        opened.tab_focus(tab_id)
    except Exception as exc:
        # Belt and braces: every step above turns its own failure into a notice,
        # and anything that got past them must still not kill the pane.
        return _focus_line(NOTICE_FOCUS_FAILED, exc)
    finally:
        if client is None:
            try:
                opened.close()
            except Exception:
                pass
    return None


def _noted_tab(tabs_path: Path, session_id: str, role: str) -> Optional[str]:
    """The tab id noted for one agent and role, or None. Never raises."""
    try:
        record = agent_tab_record(tabs_path, session_id, role)
    except Exception:
        return None
    tab_id = record.get("tab_id") if isinstance(record, dict) else None
    return tab_id if isinstance(tab_id, str) and tab_id else None


def _main_tab_id(tabs_path: Path, state_path: Path, session_id: str,
                 root_session_id: str = "") -> Optional[str]:
    """The tab holding an agent's Omnigent view, or None. Never raises.

    The sidecar first — written by whoever created the tab, and dropped when its
    pane goes — then the state file, which is where the LAUNCHER records the space
    it opened for `og chat`. That record is the only trace of a root whose space
    the bridge merely CLAIMED. `root_session_id` is the last resort, for a worker
    whose own notes are gone: the conversation it is working in, which is at least
    the right space. Both are og's own records — herdr cannot be asked (measured;
    see `og_herdr.remember_agent_tab`).

    THE ROOT FALLBACK IS DELIBERATE AND REVIEWED, not an oversight. An independent
    reviewer read it as violating "never touch the main pane"; the user ruled to
    KEEP it, and the reasoning is recorded here so it is not "fixed" into a dead
    end where selecting a reaped ACP agent does nothing at all. FOCUSING an
    existing tab is not REPURPOSING it: og never retypes, retargets, splits or
    closes the main pane, so landing the operator on the root conversation when a
    reaped agent's own view is gone is a benign last resort — the root IS the
    conversation that agent works in, the nearest true view left to show. Only the
    ACP branch reaches this; a NATIVE agent never comes here (its view is the
    vendor CLI; see `focus_agent_terminal` and the `cli`/`main` role split).
    """
    tab_id = _noted_tab(tabs_path, session_id, TAB_ROLE_MAIN)
    if tab_id:
        return tab_id
    try:
        spaces = load_state(state_path).get("spaces") or {}
    except Exception:
        return None
    if not isinstance(spaces, dict):
        return None
    for candidate in (session_id, root_session_id):
        if not isinstance(candidate, str) or not candidate:
            continue
        record = spaces.get(candidate)
        tab_id = record.get("tab_id") if isinstance(record, dict) else None
        if isinstance(tab_id, str) and tab_id:
            return tab_id
    return None


def _agent_space(tabs_path: Path, state_path: Path, session_id: str,
                 root_session_id: str = "") -> tuple:
    """`(workspace_id, cwd)` of the space an agent's tabs live in, or (None, None).

    The `main` note carries the workspace id, and for a WORKER that IS its root's
    space (the bridge records a worker's tab against the space it created it in),
    so the note normally answers this on its own. The state file's record is the
    fallback for a root whose space the launcher opened; its `cwd` rides along
    because a tab has to be created with one.

    Never raises: an unreadable state file is "no space known", which the caller
    turns into a notice rather than a tab in a guessed space.
    """
    try:
        noted = agent_tab_record(tabs_path, session_id, TAB_ROLE_MAIN)
    except Exception:
        noted = {}
    ws_id = noted.get("workspace_id") if isinstance(noted, dict) else None
    if isinstance(ws_id, str) and ws_id:
        return ws_id, None
    try:
        spaces = load_state(state_path).get("spaces") or {}
    except Exception:
        spaces = {}
    if not isinstance(spaces, dict):
        spaces = {}
    for candidate in (session_id, root_session_id):
        if not isinstance(candidate, str) or not candidate:
            continue
        record = spaces.get(candidate)
        if not isinstance(record, dict):
            continue
        ws_id = record.get("workspace_id")
        if isinstance(ws_id, str) and ws_id:
            cwd = record.get("cwd")
            return ws_id, cwd if isinstance(cwd, str) and cwd else None
    return None, None


def _cli_tab_label(harness: Optional[str], session_id: str) -> str:
    """What the on-demand vendor-CLI tab is called.

    The harness's product name ("Claude Code") when the registry names it, which
    is the same string the agent column shows, so the new tab reads as that
    agent's CLI beside it; the session id's short form otherwise. The ` (cli)`
    suffix is what tells it apart at a glance from the agent's own tab, which
    carries the conversation title.
    """
    name = harness_labels().get(harness) if isinstance(harness, str) else None
    if not isinstance(name, str) or not name:
        name = str(session_id)[:8]
    return "{0} (cli)".format(name)


def _open_vendor_cli_tab(session_id: str, harness: Optional[str], environ: dict,
                         opened, tabs_path: Path, state_path: Path,
                         terminal: tuple, root_session_id: str = "") -> tuple:
    """Open an agent's vendor-CLI tab, note it, and return `(notice, tab_id)`.

    ONE tab in the agent's space, running the interactive attach that
    `read_only_attach_command` builds — the one command the tab attach uses, so
    there is one spelling of the safety trade-off (`ignore-size`, and NOT `-r`;
    see `TMUX_ATTACH_FLAGS`).

    `focus=False` on the create and `tab.focus` afterwards, deliberately: the
    create must not move the operator's screen on its own, and the focus call is
    also what raises the workspace (measured), so doing both would be two jumps.

    `root_session_id` is the worker's root when it has one: a worker with no note
    of its own and no space record of its OWN still lives in its ROOT's space, so
    it is passed on to `_agent_space` (omitting it made Enter answer "no space"
    for exactly that case).

    THE NOTE IS A COMPARE-AND-SET, NOT A CHECK-THEN-WRITE. Two `og agents` panes
    can both see "no cli note" and both create a tab; `remember_agent_tab_if_absent`
    decides the winner INSIDE the sidecar's lock, at the write, so only one tab is
    ever recorded. The loser's tab is closed and the winner's id returned, so the
    focus lands on one tab and nothing is orphaned.

    EVERY failure becomes a notice, and any tab this call opened but could not
    use is CLOSED AGAIN before returning: an on-screen tab nothing records is how
    the next Enter opens a second one. There are THREE such paths — a partial
    create, a failed `pane_run`, and a compare-and-set that did not claim — and
    all three go through `_close_tab_or_report`, so if the close ALSO fails the
    operator gets `NOTICE_FOCUS_NOT_RECORDED_OR_CLOSED` instead of a generic
    failure with an orphan left on screen. That is the PR #38/#39 lesson (og
    cannot force herdr to release a tab, so the notice is a mitigation) made
    structural rather than repeated at each call site, where it was missed twice.
    """
    ws_id, cwd = _agent_space(tabs_path, state_path, session_id, root_session_id)
    if not ws_id:
        return NOTICE_FOCUS_NO_SPACE, None
    try:
        command = read_only_attach_command(*terminal)
    except Exception as exc:
        # `terminal` is a `(socket, target)` pair, always two strings when
        # `terminal_behind` supplies it — but this sits OUTSIDE the create's try,
        # and `read_only_attach_command` unpacks and shell-quotes it, so a
        # malformed tuple (wrong arity, a non-string member) would escape. Guarded
        # so "every failure becomes a notice" stays literally true, and so nothing
        # is created for a terminal og cannot name.
        return _focus_line(NOTICE_FOCUS_FAILED, exc), None
    try:
        created = opened.tab_create(
            ws_id, cwd or os.getcwd(), _cli_tab_label(harness, session_id),
            focus=False, env=pane_env_for(environ))
    except Exception as exc:
        # A `tab.create` that TIMES OUT may still have LANDED server-side: the
        # client's default is 10s and `_read_frame` raises `HerdrError("timeout")`,
        # so a failure here can mean a tab now EXISTS that this call never saw the
        # id of. There is then nothing to close and nothing to note, so the result
        # is a rare orphan tab the operator must close by hand — and a later Enter,
        # finding no note, opens another. It is NOT SPECIFIC TO THIS PATH: every
        # create in og has the same shape (`_add_tab`, `_open_space`), because the
        # herdr protocol has no idempotency key and the client cannot ask whether
        # its create landed.
        #
        # RECONCILING BY LABEL IS DELIBERATELY NOT DONE. `_cli_tab_label` is the
        # harness PRODUCT name ("Claude Code (cli)"), not unique per session, so
        # listing tabs and matching it could adopt or close ANOTHER agent's tab —
        # a wrong-tab bug, worse than the rare orphan it would fix. The real fix
        # belongs in the herdr protocol (an idempotency key, or a
        # create-if-absent), not here. Recorded so the next reader does not "fix"
        # this with exactly that label reconcile.
        return _focus_line(NOTICE_FOCUS_FAILED, exc), None
    if not isinstance(created, dict):
        created = {}
    tab_id = _id_of(created.get("tab"), "tab_id")
    pane_id = _id_of(created.get("root_pane"), "pane_id")
    if not (tab_id and pane_id):
        # A tab without a pane is an empty tab og created and cannot use: close
        # whatever came back, the same rule `_open_space` applies to a partial
        # create. `tab_id` may be falsy — herdr returned no tab at all — and then
        # there is nothing to orphan, so the plain failure is the whole truth.
        notice = _focus_line(NOTICE_FOCUS_FAILED,
                             "herdr returned no tab or pane id")
        if not tab_id:
            return notice, None
        return _close_tab_or_report(opened, tab_id, (notice, None))
    try:
        opened.pane_run(pane_id, command)
    except Exception as exc:
        return _close_tab_or_report(
            opened, tab_id, (_focus_line(NOTICE_FOCUS_FAILED, exc), None))
    claimed, winner = remember_agent_tab_if_absent(
        tabs_path, session_id, tab_id, ws_id, role=TAB_ROLE_CLI)
    if claimed:
        return None, tab_id
    # Not claimed: either a second pane opened this agent's tab first (there is a
    # WINNER) or the note could not be written. Either way OUR tab is not the one
    # on record, so it is closed — through the one close-and-report helper, which
    # names a left-open tab when the close fails instead of a clean "not recorded".
    return _close_tab_or_report(
        opened, tab_id,
        (None, winner) if winner else (NOTICE_FOCUS_NOT_RECORDED, None))


def _close_tab_or_report(opened, tab_id: str, closed_result: tuple) -> tuple:
    """Close a tab this call opened; return the `(notice, tab_id)` its fate fits.

    THE ONE PLACE `_open_vendor_cli_tab` CLOSES A TAB IT OPENED AND COULD NOT USE,
    AND THE ONE PLACE THAT PICKS THE NOTICE. `closed_result` is what to hand back
    when herdr DID close the tab. When the close FAILS the operator is told a tab
    was left open instead (`NOTICE_FOCUS_NOT_RECORDED_OR_CLOSED`), because a tab on
    screen with nothing recording it is exactly how the next Enter opens a second
    one — a defect class that has now appeared three times (PR #38, PR #39, and the
    CAS path it was first fixed on). Structural so it cannot recur a fourth: every
    close-and-report in that function goes through here, so no caller can close a
    tab and forget to say it was left open.

    A FALSY `tab_id` is deliberately NOT routed here — there is no tab to orphan,
    so a caller keeps its plain notice (see the partial-create branch).
    """
    if _close_tab_quietly(opened, tab_id):
        return closed_result
    return NOTICE_FOCUS_NOT_RECORDED_OR_CLOSED, None


def _close_tab_quietly(opened, tab_id: str) -> bool:
    """Close a tab this call just opened, best effort. Never raises.

    Returns True when herdr closed it, False when it did not. THE RETURN IS
    LOAD-BEARING: a tab on screen that nothing records is how the next Enter opens
    a second one, so a caller whose note did not land must know whether its own
    recovery close worked — when it did not, the operator is told
    (`NOTICE_FOCUS_NOT_RECORDED_OR_CLOSED`) instead of being left to discover the
    orphan. That is the PR #39 lesson, kept from the mirror.

    Callers close a FAILED attempt through `_close_tab_or_report`, which is the one
    place that pairs this return with the notice; calling this bare and dropping
    the bool is the defect that helper exists to make impossible.

    NOT `_close_quietly`, which is a different thing that already exists further
    up this file: the LAUNCHER's own "close the space I just opened", taking a
    ctx and a workspace id. Two same-named module-level functions would mean the
    later one silently wins for every caller — the launcher's space cleanup would
    have started calling `tab.close` on a workspace id.
    """
    try:
        opened.tab_close(tab_id)
    except Exception:
        return False
    return True


def _focus_line(template: str, value) -> str:
    """One notice line. NEVER raises — even while formatting."""
    try:
        return template.format(value)
    except Exception:
        return template.split("{")[0].strip() or template


# ---------------------------------------------------------------------------
# the multiplexer's own server
# ---------------------------------------------------------------------------

def ensure_server(ctx: Launch) -> None:
    """Make sure the multiplexer's server is answering, starting it if not.

    Only reached when we are OUTSIDE it. Bounded: a `herdr server` that never
    opens its socket is reported, never waited on, because an `og start` that
    hangs forever here would look like a hang in og rather than in herdr.
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
