#!/usr/bin/env python3
"""og audit — what actually happened in an orchestrator run.

Reads ~/.omnigent/chat.db (the transcripts; runner logs are httpx noise) and
the server log (where a parked approval or question shows as a slow hook
POST), and prints per session: harness, duration, tool mix, how many times
the worker asked instead of acting, malformed tool calls, output volume, and
its final report. Built from the questions a coder-CLI trial needs answered:
did it boot, did it act, did it stall, did it commit, what did it say.

  og audit            the most recent run (root session and its children)
  og audit <id>       a specific root or child session id (hex prefix ok)
  og audit --list     recent root sessions
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

OMNI = Path(os.environ.get("OMNIGENT_HOME", Path.home() / ".omnigent"))
DB = OMNI / "chat.db"
LOGS = OMNI / "logs" / "server"

T_MESSAGE, T_CALL, T_RESULT, T_RESOURCE = 1, 2, 3, 8
ASK_TOOLS = {"question", "askuserquestion", "ask_followup_question", "ask_user", "elicit"}

# A worker's own text (T_RESULT outputs and T_MESSAGE text) or its runner log
# can carry a quota-failure line that Omnigent never surfaces. Each entry is a
# (regex, hint, log_only) triple; the hint ends with the remedy, so `og audit`
# can point at the exact command to run instead of leaving the reader to guess.
#
# Where each string actually lands, read from ~/.omnigent/chat.db and the
# runner logs (see docs/TROUBLESHOOTING.md):
#   Kilo `Add credits to continue, or switch to a free model` — the harness
#     error item (type 5, {"source":"harness","code":"RuntimeError",
#     "message":"inner executor error: Internal error: ..."}), and echoed into
#     the child's T_RESULT output; the runner log repeats it verbatim.
#   freebuff `not enough Freebucks` — T_RESULT output and T_MESSAGE text.
#   OpenCode `Rate limit exceeded` — the opencode log (opencode-native/<hash>/
#     xdg-data/opencode/log/opencode.log). log_only: it also appears inside
#     source code a worker is merely reading, so scanning chat.db for it would
#     flag every opencode worker that happens to grep a rate-limit string.
SIGNATURES: list[tuple[re.Pattern, str, bool]] = [
    (re.compile(r"Add credits to continue, or switch to a free model"),
     "Kilo is out of credits — the pin never landed on the free router. "
     "Run `og stats --mark <id> dry --until +1h --reason \"Add credits to continue, "
     "or switch to a free model\"` and set `kilo/kilo-auto/free` as Kilo's default.",
     False),
    (re.compile(r"[Nn]ot enough Freebucks"),
     "freebuff's Freebucks pool is empty. Run `og stats --mark <id> dry --until +1h "
     "--reason \"not enough Freebucks\"`; `og stats` shows the last balance "
     "freebuff itself reported.",
     False),
    (re.compile(r"Rate limit exceeded"),
     "a rate limit hit. Run `og stats --mark <id> dry --until <reset> --reason "
     "\"Rate limit exceeded\"`; for OpenCode the log is at "
     "~/.omnigent/opencode-native/<hash>/xdg-data/opencode/log/opencode.log.",
     True),
    (re.compile(r"out of credits"),
     "out of credits. Run `og stats --mark <id> dry --until +1h --reason \"out of "
     "credits\"`.",
     False),
    (re.compile(r"usage cap", re.IGNORECASE),
     "a usage cap was hit. Run `og stats --mark <id> dry --until +1h --reason "
     "\"usage cap\"`.",
     False),
]


def signature_hints(text: str, log_only: bool = False) -> list[str]:
    """Quota-failure hints found in `text`, in SIGNATURES order.

    With `log_only=False` (the default) only the chat.db-facing signatures are
    matched — the opencode rate-limit line is log_only because it also shows up
    inside source code a worker is merely reading.
    """
    return [hint for rx, hint, lo in SIGNATURES if (lo == log_only) and rx.search(text)]


def ts(t: int | None) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else "-"


def dur(a: int | None, b: int | None) -> str:
    if not a or not b:
        return "-"
    s = b - a
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def hexid(b: bytes) -> str:
    return b.hex()


def connect() -> sqlite3.Connection:
    if not DB.exists():
        sys.exit(f"no chat.db at {DB}")
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def roots(con: sqlite3.Connection, limit: int = 10) -> list[tuple]:
    return con.execute(
        "select id, title, created_at, updated_at from conversations "
        "where parent_conversation_id is null order by updated_at desc limit ?", (limit,)
    ).fetchall()


def resolve(con: sqlite3.Connection, prefix: str) -> bytes:
    rows = con.execute("select id from conversations where hex(id) like ?",
                       (prefix.upper() + "%",)).fetchall()
    if len(rows) != 1:
        sys.exit(f"{len(rows)} sessions match {prefix!r}")
    return rows[0][0]


def tree(con: sqlite3.Connection, root: bytes) -> list[tuple]:
    return con.execute(
        "select id, title, parent_conversation_id, created_at, updated_at from conversations "
        "where root_conversation_id = ? order by created_at", (root,)
    ).fetchall()


def items(con: sqlite3.Connection, cid: bytes) -> list[tuple]:
    return con.execute(
        "select type, created_at, data from conversation_items where conversation_id = ? "
        "order by position", (cid,)
    ).fetchall()


def hook_waits() -> dict[str, list[tuple[str, float]]]:
    """session id -> [(hook kind, seconds)] for every parked approval/question."""
    out: dict[str, list[tuple[str, float]]] = {}
    pat = re.compile(r'"POST /v1/sessions/([0-9a-f]+)/hooks/((?:native-)?permission-request)'
                     r' HTTP/1.1" 200 OK ([0-9.]+)ms')
    for log in sorted(LOGS.glob("server-*.log"))[-6:]:
        try:
            for line in log.open(errors="replace"):
                m = pat.search(line)
                if m:
                    out.setdefault(m.group(1), []).append((m.group(2), float(m.group(3)) / 1000))
        except OSError:
            continue
    return out


def opencode_log_text(since: int | None) -> str:
    """Concatenated text of every recent OpenCode worker log.

    OpenCode's rate-limit failure never reaches chat.db — it lives in the
    worker's own log — so the signature scan has to read it separately.
    """
    if not since:
        return ""
    out: list[str] = []
    for log in (OMNI / "opencode-native").glob("*/xdg-data/opencode/log/opencode.log"):
        try:
            if log.stat().st_mtime < since - 60:
                continue
            out.append(log.read_text(errors="replace"))
        except OSError:
            continue
    return "\n".join(out)


def rate_limit_hint(since: int | None, label: str | None = None) -> str | None:
    """Explain the stall Omnigent 0.13 cannot: an OpenCode worker whose model
    call was rate-limited. opencode logs `stream error ... Rate limit exceeded`
    and disables retries under Omnigent, but the forwarder gets no
    `session.error`, so the turn never completes and the orchestrator --
    inbox-driven by design -- is never woken. Only the worker's own log says why.

    Restricted to opencode children. Previously this fired for ANY child with no
    tool calls whenever an opencode log anywhere in the window had a rate-limit
    line, which attributed a Kilo credit failure or a Cline boot failure to
    OpenCode. The audit label for an opencode child is `opencode` or
    `opencode-native` (see analyse()).
    """
    if not since:
        return None
    if not (label or "").lower().startswith("opencode"):
        return None
    hits = 0
    for log in (OMNI / "opencode-native").glob("*/xdg-data/opencode/log/opencode.log"):
        try:
            if log.stat().st_mtime < since - 60:
                continue
            text = log.read_text(errors="replace")
        except OSError:
            continue
        if "stream error" in text and ("Rate limit" in text or "rate limit" in text):
            hits += 1
    if hits:
        return ("an OpenCode worker log from this run says `Rate limit exceeded` — the "
                "pinned model is over its quota; Omnigent does not surface this, so the run "
                "stalls until you cancel it and re-pin (og setup) or wait for the reset")
    return None


def analyse(rows: list[tuple]) -> dict:
    calls: Counter = Counter()
    asks = invalid = user_turns = out_chars = 0
    agent = None
    first = last = None
    last_assistant = ""
    items_text = ""
    for t, at, data in rows:
        first = first or at
        last = at
        try:
            d = json.loads(data)
        except ValueError:
            continue
        if t == T_CALL:
            name = str(d.get("name", "?"))
            # ACP workers stamp a response id (resp_...) here, not a harness;
            # the resource event below carries the real terminal name instead.
            a = d.get("agent")
            if not agent and isinstance(a, str) and not a.startswith("resp_"):
                agent = a
            calls[name] += 1
            if name.lower().rsplit("__", 1)[-1] in ASK_TOOLS:
                asks += 1
            if name == "invalid":
                invalid += 1
        elif t == T_RESULT:
            out = str(d.get("output", ""))
            out_chars += len(out)
            items_text += out
        elif t == T_MESSAGE:
            if d.get("role") == "user":
                user_turns += 1
            elif d.get("role") == "assistant":
                text = "".join(c.get("text", "") for c in d.get("content", [])
                               if isinstance(c, dict))
                if text.strip():
                    last_assistant = text
                    items_text += text
        elif t == T_RESOURCE and not agent:
            meta = (d.get("resource") or {}).get("metadata") or {}
            agent = meta.get("terminal_name")
    return {"calls": calls, "asks": asks, "invalid": invalid, "turns": user_turns,
            "out_chars": out_chars, "agent": agent, "first": first, "last": last,
            "report": last_assistant, "items_text": items_text}


def show(con: sqlite3.Connection, root: bytes, full: bool) -> None:
    waits = hook_waits()
    for cid, title, parent, created, updated in tree(con, root):
        rows = items(con, cid)
        a = analyse(rows)
        role = "ROOT " if parent is None else "  ├─ "
        print(f"{role}{title or '(untitled)'}   [{hexid(cid)[:8]}]")
        label = a["agent"] or ("acp" if (title or "").startswith("coder_") else "?")
        print(f"       harness {label:<18} {ts(a['first'])} → {ts(a['last'])}  "
              f"({dur(a['first'], a['last'])})  turns {a['turns']}")
        top = ", ".join(f"{n} {c}" for n, c in a["calls"].most_common(8))
        print(f"       tools   {sum(a['calls'].values())}: {top or '-'}")
        flags = []
        if a["asks"]:
            flags.append(f"ASKED {a['asks']}x")
        if a["invalid"]:
            flags.append(f"{a['invalid']} malformed tool call(s)")
        for kind, secs in waits.get(hexid(cid), []):
            flags.append(f"parked {kind.replace('-request', '')} {secs:.0f}s")
        # A text-only reviewer legitimately answers without tools; a worker
        # that produced neither tool calls nor a report never acted. The
        # transcript cannot say whether that was a boot/silent-model failure
        # or an account quota stop (seen twice on Cursor: 0 turns, quota page
        # showing capped until Oct 19), so the hint names both and the check
        # instead of guessing.
        if not a["calls"] and parent is not None and not a["report"]:
            flags.append("NO TOOL CALLS — never acted (boot failure, silent "
                         "model failure, OR an account quota stop — check the "
                         "vendor's usage page, then og stats --mark <id> dry "
                         "--until <reset>)")
            hint = rate_limit_hint(a["first"], label)
            if hint:
                flags.append(hint)
        flags.extend(signature_hints(a["items_text"]))
        # An OpenCode worker can die to a rate limit without a single item in
        # chat.db — the failure lives in its own log. Scan it too, but only
        # for opencode children: the log is theirs, and attributing it to a
        # Kilo or Cline worker is a false alarm.
        if label and label.lower().startswith("opencode"):
            flags.extend(signature_hints(opencode_log_text(a["first"]), log_only=True))
        print(f"       output  {a['out_chars'] // 1000}k chars"
              + (f"   ⚠ {'; '.join(flags)}" if flags else ""))
        if parent is not None and a["report"]:
            rep = a["report"] if full else a["report"][:400].replace("\n", " ")
            print(f"       report  {rep}{'' if full else ('…' if len(a['report']) > 400 else '')}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(prog="og audit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", nargs="?", help="root or child session id (prefix ok)")
    ap.add_argument("--list", action="store_true", help="recent root sessions")
    ap.add_argument("--full", action="store_true", help="print each worker's full final report")
    args = ap.parse_args()
    con = connect()
    if args.list:
        for cid, title, created, updated in roots(con):
            n = con.execute("select count(*) from conversations where root_conversation_id = ? "
                            "and parent_conversation_id is not null", (cid,)).fetchone()[0]
            print(f"{hexid(cid)[:8]}  {ts(updated)}  {n:2d} workers  {title or '(untitled)'}")
        return
    if args.session:
        cid = resolve(con, args.session)
        root = con.execute("select root_conversation_id from conversations where id = ?",
                           (cid,)).fetchone()[0]
    else:
        rs = roots(con, 1)
        if not rs:
            sys.exit("no sessions")
        root = rs[0][0]
    show(con, root, args.full)


if __name__ == "__main__":
    main()
