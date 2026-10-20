#!/usr/bin/env python3
"""Probe every installed agent once, at `og start` / `og restart`.

WHY THIS EXISTS (2026-10-13): a dry mark written by `og stats --mark` without
`--until` had NO expiry, and nothing ever overrode it for an agent whose quota
cannot be measured (OpenCode Zen free tier, Command Code). One "Zen rate limit
exceeded" mark kept two workers out of the roster for a week. A mark is a
snapshot of a moment; a start is a fresh moment, so it gets fresh evidence.

Two kinds of evidence:

* a registry row with a measurable `quota.probe` uses that probe, exactly as
  `og stats` does;
* a row with no queryable quota (or an "assumed" reading such as Kilo's free
  router) gets a LIVENESS probe: one tiny real request through the agent's own
  CLI on the model the install pinned. The command shape lives in the registry
  (`quota.liveness.argv`), the pin comes from `og-install.json`, and nothing
  user-specific is hardcoded here.

Results are bounded (one thread and one timeout per agent) and can never abort
a start: the caller ignores this process's exit status, and main() catches
everything. A missing binary, an auth error or a timeout is reported as
`unavailable`/`error`, never `dry` -- only a quota string is evidence of
exhaustion, because a broken probe says nothing about capacity.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import og_quota as q  # noqa: E402
import og_stats as st  # noqa: E402

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MARK_TTL = timedelta(hours=1)

# "Resets in 2h 41m", "resets in 45m", "Resets in 2 hours 41 minutes".
_RESET_RX = re.compile(
    r"resets?\s+in\s+"
    r"(?:(\d+)\s*h(?:ours?|rs?)?)?\s*"
    r"(?:(\d+)\s*m(?:in(?:utes?)?)?)?",
    re.IGNORECASE)


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat()


def _which(name: str, path_env: str | None) -> str | None:
    if not name:
        return None
    if os.sep in name:
        return name if os.access(name, os.X_OK) else None
    for d in (path_env or "").split(os.pathsep):
        if not d:
            continue
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _expiry(reset_at: str | None, now: datetime) -> datetime:
    """The reset time the vendor gave, or now + 1h when it gave none.

    A reset in the past is treated as "no reset": a mark that expires before
    it is written would clear itself on the next state write.
    """
    dt = q._parse_iso(reset_at) if reset_at else None
    if dt is not None and q._aware(dt) > q._aware(now):
        return dt
    return now + DEFAULT_MARK_TTL


def _reset_from_text(text: str, now: datetime) -> datetime | None:
    m = _RESET_RX.search(text or "")
    if not m:
        return None
    h, mi = int(m.group(1) or 0), int(m.group(2) or 0)
    if h == 0 and mi == 0:
        return None
    return now + timedelta(hours=h, minutes=mi)


def _liveness_argv(row: dict, entry: dict) -> tuple[list[str] | None, str]:
    """The registry's liveness argv with the install's pin substituted.

    Returns (argv, reason). argv is None when the probe cannot be formed --
    no liveness block, or a `{model}` template with nothing pinned.
    """
    conf = ((row.get("quota") or {}).get("liveness") or {})
    template = conf.get("argv") or []
    if not template:
        return None, "no liveness probe configured"
    model = (entry.get("params") or {}).get("model")
    if not model:
        # Registry `prefer` is the installer's own default pin; using it keeps
        # an unpinned install probeable without inventing a model here.
        model = ((row.get("model") or {}).get("prefer"))
    if any("{model}" in str(a) for a in template) and not model:
        return None, "no pinned model for the liveness probe"
    return [str(a).replace("{model}", str(model or "")) for a in template], ""


def _probe_liveness(row: dict, entry: dict, ctx: q.Ctx,
                    now: datetime, timeout: float) -> dict:
    """One tiny real request through the agent's own CLI.

    Returns a small outcome dict: state is ok | dry | error | unavailable.
    (The persisted record keeps the schema's ok/dry/unknown states; this word
    is what the start line shows.)
    """
    argv, why = _liveness_argv(row, entry)
    if argv is None:
        return {"state": "unavailable", "detail": why}
    exe = _which(argv[0], ctx.env.get("PATH"))
    if exe is None:
        return {"state": "unavailable",
                "detail": f"unavailable: {argv[0]} not found on PATH"}
    argv[0] = exe
    try:
        r = ctx.run(argv, capture_output=True, text=True, timeout=timeout,
                    stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"state": "error",
                "detail": f"error: timed out after {timeout:g}s"}
    except OSError as e:
        return {"state": "error", "detail": f"error: {e}"}
    out = f"{getattr(r, 'stdout', '') or ''}\n{getattr(r, 'stderr', '') or ''}"
    patterns = ((row.get("quota") or {}).get("liveness") or {}).get(
        "failure_patterns") or []
    for pat in patterns:
        try:
            m = re.search(pat, out, re.IGNORECASE | re.DOTALL)
        except re.error:
            continue
        if m:
            # The reset clause normally sits on the same line as the matched
            # quota string ("... usage limit. Resets in 2h 41m") but not
            # inside the match itself, so parse the whole output.
            msg = next((ln.strip() for ln in out.splitlines()
                        if m.group(0) in ln), "") or _first_line(m.group(0))
            return {"state": "dry", "detail": "dry: " + (msg or "quota failure"),
                    "expires_at": _reset_from_text(out, now)}
    if getattr(r, "returncode", 1) == 0:
        return {"state": "ok", "detail": "ok (reply received)"}
    return {"state": "error",
            "detail": "error: " + (_first_line(out) or
                                   f"exit {getattr(r, 'returncode', '?')}")}


def _outcome_expiry(outcome: dict, now: datetime) -> datetime:
    return outcome.get("expires_at") or _expiry(outcome.get("reset_at"), now)


def _record(state: str, tier: str, reset_at: str | None,
            now: datetime, detail: str) -> dict:
    rec = q.make_record(state, tier, None, None, None, [], reset_at,
                        "liveness", _iso(now), detail)
    # Explicit provenance requested by the start probe contract; checked_at
    # already carries the same instant for every other reader.
    rec["last_probe_at"] = _iso(now)
    return rec


def _apply(agent_id: str, outcome: dict, now: datetime,
           state: dict) -> dict:
    """Persist one outcome: ok clears a mark, dry writes one with an expiry.

    error/unavailable changes nothing: no evidence is not a licence to keep a
    stale mark, nor to clear one.
    """
    marks = state.setdefault("marks", {})
    if outcome["state"] == "ok":
        marks.pop(agent_id, None)
    elif outcome["state"] == "dry":
        exp = _outcome_expiry(outcome, now)
        marks[agent_id] = {
            "state": "dry", "until": _iso(exp),
            "reason": "start probe: " + outcome.get("detail", ""),
            "at": _iso(now)}
        outcome["until"] = exp
    return outcome


def _probe_one(entry: dict, row: dict, ctx: q.Ctx, prev: dict,
               now: datetime, timeout: float) -> tuple[dict, dict | None]:
    """Quota probe when the registry names one, liveness otherwise.

    Returns (outcome, record-to-persist). It deliberately does NOT touch the
    shared state: the callers run concurrently, and a probe's cache fallback
    iterates the previous records -- inserting into that dict mid-iteration
    would raise "dictionary changed size". Records are applied sequentially
    in probe_start.
    """
    agent_id = entry["agent"]
    probe = entry.get("probe")
    outcome: dict
    rec: dict | None
    if probe:
        params = dict(entry.get("params") or {})
        params["agent_id"] = agent_id
        raw = q._run_one_probe(probe, params, ctx, prev)
        raw["last_probe_at"] = _iso(now)
        rec = raw
        src = str(raw.get("source", ""))
        if raw.get("state") == "dry":
            outcome = {"state": "dry", "source": probe,
                       "reset_at": raw.get("reset_at"),
                       "detail": f"dry ({probe}): {raw.get('detail', '')}"}
        elif raw.get("state") == "ok" and "(cached)" not in src:
            outcome = {"state": "ok", "source": probe,
                       "detail": f"ok ({probe}) {raw.get('detail', '')}"}
        elif raw.get("state") == "ok":
            # A cached reading is not fresh evidence; show it, but leave any
            # existing mark alone -- only a live ok clears a mark.
            outcome = {"state": "cached", "source": probe,
                       "detail": f"ok ({probe}, cached) {raw.get('detail', '')}"}
        else:
            outcome = {"state": "unknown", "source": probe,
                       "detail": f"{raw.get('state', '?')} ({probe}): "
                                 f"{raw.get('detail', '')}"}
        # `assumed` means the probe identified the binding pool but measured
        # nothing (Kilo's free router). If the registry knows how to make a
        # real request, verify it -- otherwise "ok, assumed" stays a guess.
        if raw.get("tier") == "assumed":
            live = _probe_liveness(row, entry, ctx, now, timeout)
            if live["state"] in ("ok", "dry"):
                live["source"] = "liveness"
                live["detail"] = f"{outcome.get('detail', '')}; {live['detail']}"
                if live["state"] == "ok":
                    rec = _record("ok", "assumed", None, now, live["detail"])
                else:
                    rec = _record("unknown", "unknown",
                                  _iso(_outcome_expiry(live, now)), now,
                                  live["detail"])
                outcome = live
            else:
                # The quota probe could not measure and the liveness request
                # failed: report the failure, do not claim the assumed ok.
                rec = _record("unknown", "unknown", None, now, live["detail"])
                outcome = live
        return outcome, rec

    live = _probe_liveness(row, entry, ctx, now, timeout)
    live["source"] = "liveness"
    if live["state"] == "ok":
        rec = _record("ok", "assumed", None, now, live["detail"])
    elif live["state"] == "dry":
        rec = _record("unknown", "unknown", _iso(_outcome_expiry(live, now)),
                      now, live["detail"])
    else:
        rec = _record("unknown", "unknown", None, now, live["detail"])
    return live, rec


def _fmt(outcomes: list[dict]) -> str:
    width = max((len(o["agent"]) for o in outcomes), default=0)
    lines = []
    for o in outcomes:
        detail = o.get("detail", "")
        until = o.get("until")
        if until is not None:
            detail += f" (until {until.astimezone().strftime('%m-%d %H:%M')})"
        if len(detail) > 120:
            detail = detail[:119] + "…"
        lines.append(f"  {o['agent']:<{width}}  {o['state']:<11}  {detail}")
    return "\n".join(lines)


def probe_start(install_path: Path, registry_path: Path, state_path: Path,
                *, timeout: float = DEFAULT_TIMEOUT_S,
                ctx: q.Ctx | None = None,
                entries: list[dict] | None = None) -> list[dict]:
    """Probe the installed lineup concurrently and update the state file.

    Returns the per-agent outcomes in lineup order. Raises only on genuinely
    unreadable inputs (the caller turns that into a no-op).
    """
    now = ctx.now() if ctx is not None else datetime.now().astimezone()
    if entries is None:
        entries = st.lineup(install_path, registry_path)
    try:
        reg = json.loads(registry_path.read_text())
    except (OSError, ValueError):
        reg = {}
    rows = st._agents_map(reg)
    state = q.load_state(state_path)

    # One line per agent, in lineup order: the same agent can hold two roles
    # (coder + integrator), and a caller wants one verdict, not two probes
    # racing on the same CLI.
    seen: dict[str, dict] = {}
    ordered: list[dict] = []
    for e in entries:
        if e["agent"] in seen:
            continue
        seen[e["agent"]] = e
        ordered.append(e)

    ctx = ctx or q.Ctx(now=lambda: now)
    # Snapshot for the probes' 30-minute cache fallback; the shared dict is
    # only written in the sequential pass below.
    prev = dict(state.get("agents") or {})
    workers = min(16, max(1, len(ordered)))
    outcomes: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(
            lambda e: _probe_one(e, rows.get(e["agent"]) or {}, ctx, prev,
                                 now, timeout),
            ordered))
    for agent_id, (o, rec) in zip((e["agent"] for e in ordered), results):
        if rec is not None:
            state.setdefault("agents", {})[agent_id] = rec
        _apply(agent_id, o, now, state)
        o["agent"] = agent_id
        outcomes.append(o)
    q.save_state(state, state_path, now=now)
    return outcomes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="og start probe", add_help=True,
        description="Probe every installed agent and refresh dry marks.")
    ap.add_argument("--install", type=Path,
                    default=Path(os.environ.get(
                        "OMNIGENT_HOME", Path.home() / ".omnigent")) /
                    "og-install.json")
    ap.add_argument("--registry", type=Path,
                    default=Path(os.environ.get("OG_REGISTRY") or
                                 Path(__file__).resolve().parent /
                                 "registry.json"))
    ap.add_argument("--state", type=Path, default=q.OMNI_STATE)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help="per-agent probe ceiling in seconds (default 20)")
    args = ap.parse_args(argv)
    try:
        if not args.install.is_file():
            return 0  # nothing installed: start will say so itself
        outcomes = probe_start(args.install, args.registry, args.state,
                               timeout=args.timeout)
        if outcomes:
            print(_fmt(outcomes))
    except SystemExit as e:  # lineup() exits on a missing/bad catalog
        print(f"capacity probe skipped: {e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 -- a probe never fails a start
        print(f"capacity probe failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
