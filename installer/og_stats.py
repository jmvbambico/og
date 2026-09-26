#!/usr/bin/env python3
"""og stats — per-agent quota/capacity reporter.

Builds the lineup from og-install.json (orchestrator, coders by priority,
reviewers, scouts and integrators in chain order), finds each agent's quota
probe from the registry row's `quota` block, runs probes concurrently, and
prints one row per agent.
Probes live in og_quota.py; this CLI owns presentation and the mark commands.

Privacy: read-only everywhere, never prints token values, never refreshes
tokens (see docs/STATS.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import og_quota as q  # noqa: E402

OMNI = Path(os.environ.get("OMNIGENT_HOME", Path.home() / ".omnigent"))
INSTALL = OMNI / "og-install.json"
# test seam: point OG_REGISTRY at a different catalog (e.g. a branch where
# registry.json already carries quota blocks) without editing this repo
REGISTRY_ENV = "OG_REGISTRY"
REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY = REPO_ROOT / "installer" / "registry.json"


def _now() -> datetime:
    return datetime.now().astimezone()


# ---------------------------------------------------------------------------
# lineup: who is installed, in dispatch order
# ---------------------------------------------------------------------------

def _agents_map(reg: dict) -> dict:
    """Catalog rows by id. The real installer/registry.json carries agents as
    a LIST of {"id": ..., "quota": {...}, ...} rows; accept an {id: row} dict
    too (the shape the unit tests write)."""
    agents = reg.get("agents") or {}
    if isinstance(agents, list):
        return {r.get("id"): r for r in agents
                if isinstance(r, dict) and r.get("id")}
    return agents


def _priority_key(p) -> tuple:
    """Sort key for a coder's `priority`.

    A hand-edited og-install.json can carry a null (or a string) priority, and
    the old `key=lambda c: c.get("priority", 999)` then raised
    `TypeError: '<' not supported between instances of 'int' and 'NoneType'`,
    taking the whole lineup down right before a dispatch decision. Real (int)
    priorities sort first; anything else sorts after them, and because sorted()
    is stable the malformed entries keep their original array order.
    """
    if isinstance(p, int) and not isinstance(p, bool):
        return (0, p)
    return (1, 0)


def lineup(install_path: Path, registry_path: Path) -> list[dict]:
    """[{role, agent, priority, probe, params, note}] in dispatch order:
    orchestrator, coders by priority, reviewers, scouts and integrators in
    chain order."""
    try:
        inst = json.loads(install_path.read_text())
    except FileNotFoundError:
        sys.exit(f"no install state at {install_path} — run og setup first")
    except json.JSONDecodeError as e:
        sys.exit(f"{install_path} is not valid JSON: {e}")
    try:
        reg = json.loads(registry_path.read_text())
    except FileNotFoundError:
        sys.exit(f"no registry at {registry_path} (set {REGISTRY_ENV} to override)")
    except json.JSONDecodeError as e:
        sys.exit(f"{registry_path} is not valid JSON: {e}")

    def entry(agent_id, role: str, priority: int | None) -> dict | None:
        # A malformed entry -- a bare number, a null, an object with no id --
        # names no agent, and a row for it would carry agent=None and crash the
        # table render. og stats runs interactively before a dispatch decision,
        # so a hand-edited file must skip the bad entry, never trace back.
        if not isinstance(agent_id, str):
            return None
        row = _agents_map(reg).get(agent_id) or {}
        quota = row.get("quota") or {}
        probe = quota.get("probe")
        params = {k: v for k, v in quota.items() if k not in ("probe", "note")}
        return {"role": role, "agent": agent_id, "priority": priority,
                "probe": probe, "params": params,
                "note": quota.get("note") or ""}

    def ids(value) -> list:
        """The agent ids in a role's plan value.

        A role is an ORDERED chain, so it may be a list of entries; the older
        shapes (a bare id string, a single {"id": ...} object) still appear in
        state files written before the chain existed and must keep reporting.
        A hand-edited file can also carry a malformed element -- `"reviewer":
        [5]` used to raise AttributeError on `5.get`. Anything that is neither
        an id string nor an object carrying one is skipped, never guessed at.
        """
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [value["id"]] if isinstance(value.get("id"), str) else []
        if isinstance(value, list):
            out = []
            for e in value:
                if isinstance(e, str):
                    out.append(e)
                elif isinstance(e, dict) and isinstance(e.get("id"), str):
                    out.append(e["id"])
            return out
        return []

    entries: list[dict] = []

    def add(agent_id, role: str, priority: int | None) -> None:
        e = entry(agent_id, role, priority)
        if e is not None:
            entries.append(e)

    for aid in ids(inst.get("orchestrator")):
        add(aid, "orchestrator", None)
    coders = inst.get("coders")
    if not isinstance(coders, list):
        # A hand-edited "coders": 5 is not iterable and used to raise
        # TypeError: 'int' object is not iterable. `og stats` runs interactively
        # right before a dispatch decision, so it must skip what it cannot read.
        coders = []
    for c in sorted((c for c in coders if isinstance(c, dict)),
                    key=lambda c: _priority_key(c.get("priority", 999))):
        add(c.get("id"), "coder", c.get("priority"))
    # One row per reviewer, in chain order: this is the probe surface the
    # orchestrator reads to pick the earliest entry with capacity, so a backup
    # that is absent here cannot be failed over to on evidence.
    for aid in ids(inst.get("reviewer")):
        add(aid, "reviewer", None)
    # Same rule for the scout chain, appended after the reviewers: the earliest
    # scout WITH CAPACITY is the one dispatched, so an absent entry cannot be
    # chosen on evidence and the failover the roster skill promises is lost.
    for aid in ids(inst.get("scout")):
        add(aid, "scout", None)
    # Same rule again for the integrator chain, appended after the scouts: the
    # earliest integrator with capacity is the one dispatched, so a backup
    # missing here cannot be failed over to on evidence.
    for aid in ids(inst.get("integrator")):
        add(aid, "integrator", None)
    return entries


# ---------------------------------------------------------------------------
# marks
# ---------------------------------------------------------------------------

def _parse_until(s: str | None, now: datetime) -> str | None:
    """ISO8601, or +2h/+30m/+1d style relative offsets. Returns local ISO."""
    if not s:
        return None
    m = re.fullmatch(r"\+(\d+)([smhd])", s.strip(), re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return (now + delta).isoformat()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # astimezone() on a naive dt assumes local time, so this also fixes
        # the zone for naive input (do_mark compares against an aware now).
        return dt.astimezone().isoformat()
    except ValueError:
        raise SystemExit(f"--until: not ISO8601 or +Nh/+Nm/+Nd/+Ns: {s!r}")


def do_mark(agent_id: str, until: str | None, reason: str,
            state_path: Path) -> None:
    state = q.load_state(state_path)
    now = _now()
    marks = state.setdefault("marks", {})
    mark = {"state": "dry", "until": _parse_until(until, now),
            "reason": reason, "at": now.isoformat()}
    if mark["until"] and _q_parse(mark["until"]) <= now:
        # an already-expired mark is equivalent to clearing; store nothing
        marks.pop(agent_id, None)
    else:
        marks[agent_id] = mark
    state.setdefault("agents", {}).setdefault(agent_id, {
        **q.unknown_record("marked without a probe result"),
        "tier": "unknown"})
    q.save_state(state, state_path)
    u = mark["until"] or "no expiry"
    print(f"mark {agent_id}: dry until {u}" + (f" ({reason})" if reason else ""))


def _q_parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def do_clear(agent_id: str, state_path: Path) -> None:
    state = q.load_state(state_path)
    removed = (state.get("marks") or {}).pop(agent_id, None)
    q.save_state(state, state_path)
    print(f"clear {agent_id}: {'removed' if removed else 'no active mark'}")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

COLUMNS = ["role", "agent", "state", "remaining", "reset", "tier", "source",
           "age", "detail"]


def _fmt_remaining(rec: dict) -> str:
    if rec.get("remaining") is None:
        return "-"
    unit = rec.get("unit") or ""
    val = rec["remaining"]
    body = f"{val:g}" if isinstance(val, (int, float)) else str(val)
    if unit == "percent":
        limit = rec.get("limit")
        lim = f"{limit:g}" if isinstance(limit, (int, float)) else ""
        return f"{body}/{lim or '100'}%"
    return f"{body} {unit}".strip()


def _fmt_reset(rec: dict, now: datetime) -> str:
    s = rec.get("reset_at")
    if not s:
        return "-"
    dt = q._parse_iso(s)
    if not dt:
        return s
    # normalize both clocks: tests pass naive datetimes, production aware.
    dt, now = q._aware(dt), q._aware(now)
    local = dt.astimezone()
    delta = dt - now
    secs = delta.total_seconds()
    if secs <= 0:
        rel = "now"
    elif secs < 3600:
        rel = f"in {secs // 60:.0f}m"
    elif secs < 86400:
        rel = f"in {secs / 3600:.1f}h"
    else:
        rel = f"in {secs / 86400:.1f}d"
    return f"{local.strftime('%m-%d %H:%M')} ({rel})"


def _fmt_age(rec: dict, now: datetime) -> str:
    now = q._aware(now)

    class _C:  # minimal shim: _age_s only calls ctx.now, and the render clock
        now = staticmethod(lambda: now)  # must be the same one used for reset
    age = q._age_s(rec.get("checked_at"), _C())
    if age is None:
        return "-"
    if age < 90:
        return f"{age:.0f}s"
    if age < 3600:
        return f"{age / 60:.0f}m"
    if age < 86400:
        return f"{age / 3600:.1f}h"
    return f"{age / 86400:.1f}d"


def render_table(rows: list[dict], now: datetime) -> str:
    cells: list[list[str]] = []
    for r in rows:
        rec = r["rec"]
        cells.append([
            r["role"], r["agent"], rec.get("state", "?"),
            _fmt_remaining(rec), _fmt_reset(rec, now), rec.get("tier", "?"),
            rec.get("source", "-"), _fmt_age(rec, now),
            rec.get("detail", "")])
    widths = [max(len(c[i]) for c in cells) if cells else 0
              for i in range(len(COLUMNS))]
    # cap detail so one chatty probe cannot push the table sideways
    dwidth = min(widths[8], 60)
    widths[8] = dwidth
    out = ["  ".join(c.ljust(w) for c, w in zip(COLUMNS, widths)),
           "  ".join("-" * w for w in widths)]
    for c in cells:
        detail = c[8]
        c = list(c)
        c[8] = detail[:dwidth - 1] + "…" if len(detail) > dwidth else detail
        out.append("  ".join(f.ljust(w) for f, w in zip(c, widths)))
    return "\n".join(out)


def render_json(rows: list[dict], checked_at: datetime) -> str:
    agents = {}
    for r in rows:
        rec = dict(r["rec"])
        rec["role"] = r["role"]
        rec["priority"] = r["priority"]
        agents[r["agent"]] = rec
    return json.dumps({"version": q.STATE_VERSION,
                       "checked_at": checked_at.isoformat(),
                       "agents": agents}, indent=2)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_rows(entries: list[dict], state_path: Path, *, no_probe: bool,
               only: str | None, fresh: bool = False) -> list[dict]:
    state = q.load_state(state_path)
    now = _now()
    recs: dict[str, dict] = {}
    requests: dict[str, dict] = {}
    for e in entries:
        if only and e["agent"] != only:
            continue
        aid = e["agent"]
        if no_probe:
            # offline view: stored record if any, never a network call
            stored = (state.get("agents") or {}).get(aid)
            recs[aid] = stored or q.unknown_record("no stored record (--no-probe)")
        elif e["probe"]:
            requests[aid] = {"probe": e["probe"], **e["params"], "note": e["note"]}
        else:
            recs[aid] = q.unknown_record(e["note"] or "no quota probe configured")
    if not no_probe and requests:
        # run_probes applies marks and the 30 min cache fallback per record
        # for the view, and stashes the UNMARKED probe records in
        # state["agents"]; save persists those, never a mark overlay.
        recs.update(q.run_probes(requests, _ctx_from(now), state))
        q.save_state(state, state_path)
    # merged view: re-apply marks over everything shown
    merged = q.merge_view({"agents": recs, "marks": state.get("marks") or {}},
                          _ctx_from(now))
    return [{"role": e["role"], "agent": e["agent"], "priority": e["priority"],
             "rec": merged.get(e["agent"]) or q.unknown_record("-")}
            for e in entries if not only or e["agent"] == only]


def _ctx_from(now: datetime) -> q.Ctx:
    return q.Ctx(now=lambda: now)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="og stats", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit the merged view as JSON")
    ap.add_argument("--all", action="store_true",
                    help="include every registry agent, not just the installed lineup")
    # --mark takes an optional trailing state word so the CLI accepts its own
    # documented syntax: the orchestrator prompt, the generated roster skill,
    # the fanout skill and docs/STATS.md all run
    # `og stats --mark <id> dry --until ...`. nargs="+" (not a positional)
    # keeps the word attached to --mark: a bare positional would also match
    # `og stats dry` with no --mark at all.
    ap.add_argument("--mark", nargs="+", metavar="AGENT_ID",
                    help="mark an agent dry: --mark AGENT_ID [dry] (dry-run: prints, writes state)")
    ap.add_argument("--until", metavar="WHEN",
                    help="with --mark: ISO8601 or +2h/+30m/+1d (default: no expiry)")
    ap.add_argument("--reason", default="", help="with --mark: why it is dry")
    ap.add_argument("--clear", metavar="AGENT_ID", help="remove an agent's mark")
    ap.add_argument("--no-probe", action="store_true",
                    help="print stored records without probing (offline)")
    ap.add_argument("--agent", metavar="AGENT_ID", help="show one agent only")
    args = ap.parse_args(argv)

    state_path = OMNI / "og-quota.json"
    registry_path = Path(os.environ.get(REGISTRY_ENV) or REGISTRY)

    if args.mark:
        if args.clear:
            ap.error("--mark and --clear are mutually exclusive")
        if len(args.mark) == 1:
            mark_id = args.mark[0]
        elif len(args.mark) == 2 and args.mark[1] == "dry":
            mark_id = args.mark[0]
        else:
            ap.error("--mark takes AGENT_ID [dry]; the optional state must be the literal 'dry'")
        do_mark(mark_id, args.until, args.reason, state_path)
        return
    if args.clear:
        do_clear(args.clear, state_path)
        return

    if args.all:
        try:
            reg = json.loads(registry_path.read_text())
        except FileNotFoundError:
            sys.exit(f"no registry at {registry_path}")
        entries = []
        for aid, row in sorted(_agents_map(reg).items()):
            quota = row.get("quota") or {}
            role = row.get("role") or "/".join(row.get("roles") or []) or "-"
            entries.append({"role": role, "agent": aid,
                            "priority": None, "probe": quota.get("probe"),
                            "params": {k: v for k, v in quota.items()
                                       if k not in ("probe", "note")},
                            "note": quota.get("note") or ""})
    else:
        entries = lineup(INSTALL, registry_path)

    rows = build_rows(entries, state_path, no_probe=args.no_probe,
                      only=args.agent, fresh=False)
    if args.json:
        print(render_json(rows, _now()))
    else:
        print(render_table(rows, _now()))


if __name__ == "__main__":
    main()
