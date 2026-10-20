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

A liveness probe must never bill. It runs only when the model is verifiably
free (an explicit `free: true`, or an id whose last segment is `free` /
ends in `-free` / ends in `:free`); a paid pin is reported `skipped: paid
model` and the mark is left alone. The request runs in a fresh empty temporary
directory, with stdin closed and its own session/process group, so it can
neither touch the caller's tree nor survive a timeout. No action-enabling
flags (`--yolo`, `--tools-all`, ...) are passed, and the prompt explicitly
forbids tools: the probe is a capability check, not a dispatch.

Results are bounded (one thread and one timeout per agent) and can never abort
a start: the caller ignores this process's exit status, and main() catches
everything. A missing binary, an auth error or a timeout is reported as
`unavailable`/`error`, never `dry` -- only a failed request carrying a known
quota string is evidence of exhaustion, because a broken probe says nothing
about capacity.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import og_quota as q  # noqa: E402
import og_stats as st  # noqa: E402

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MARK_TTL = timedelta(hours=1)
# Grace between SIGTERM and SIGKILL when a probe overruns. Short: `og start`
# is already waiting on this, and an agent CLI that ignores TERM for 2s is not
# going to become responsive in the next 10.
KILL_GRACE_S = 2.0

# "Resets in 2d", "1d 3h", "2h 41m", "45m", "30s", "2 hours 41 minutes".
_RESET_CLAUSE_RX = re.compile(r"resets?\s+in\s+([^.;\n]+)", re.IGNORECASE)
_RESET_PART_RX = re.compile(
    r"(\d+)\s*(d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:utes?)?)?"
    r"|s(?:ec(?:onds?)?)?)\b",
    re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


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
    """Parse every duration in a "Resets in ..." clause into an expiry.

    Supports days as well as the compact hour/minute/second forms and their
    combinations ("2d", "1d 3h", "2h 41m", "45m", "30s"). A zero total, or a
    clause with no parsable part, is NOT a match: the caller then falls back
    to +1h rather than writing an already-expired mark.
    """
    m = _RESET_CLAUSE_RX.search(text or "")
    if not m:
        return None
    seconds = 0
    for num, unit in _RESET_PART_RX.findall(m.group(1)):
        seconds += int(num) * _UNIT_SECONDS[unit[0].lower()]
    if seconds <= 0:
        return None
    return now + timedelta(seconds=seconds)


def _kill_tree(p: subprocess.Popen, sig: int) -> None:
    """Signal the probe's whole process group (it is a session leader).

    `start_new_session=True` makes the child's pid its pgid, so killpg reaches
    a grandchild the CLI spawned. After SIGTERM the caller gives a short
    grace, then this is called again with SIGKILL -- unconditionally, even if
    the leader already exited, so a TERM-ignoring grandchild still dies.
    """
    try:
        os.killpg(p.pid, sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        p.send_signal(sig)
    except OSError:
        pass


def _run_bounded(argv: list[str], env: dict[str, str], timeout: float,
                 cwd: str) -> tuple[int | None, str, str, bool]:
    """Run a probe with stdin closed, in `cwd`, killing its tree on timeout."""
    try:
        p = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=cwd,
            start_new_session=True)
    except OSError as e:
        return None, "", str(e), False
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err, False
    except subprocess.TimeoutExpired:
        _kill_tree(p, signal.SIGTERM)
        deadline = time.monotonic() + KILL_GRACE_S
        while time.monotonic() < deadline and p.poll() is None:
            time.sleep(0.05)
        _kill_tree(p, signal.SIGKILL)
        try:
            out, err = p.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return None, out, err, True


def _resolve_model(row: dict, entry: dict) -> str | None:
    model = (entry.get("params") or {}).get("model")
    if not model:
        # Registry `prefer` is the installer's own default pin; using it keeps
        # an unpinned install probeable without inventing a model here.
        model = (row.get("model") or {}).get("prefer")
    return str(model) if model else None


def _is_free_model(row: dict, model: str | None) -> bool:
    """True only when the pin is verifiably free -- a probe must never bill.

    Explicit registry truth (`quota.liveness.free` or `quota.free`) wins;
    otherwise the id's SHAPE decides, never a list of model names, so a new
    free id maps without a code change. Shapes seen in practice:
    `opencode/mimo-v2.6-flash-free`, `kilo/kilo-auto/free`,
    OpenRouter's `vendor/model:free`.
    """
    quota = row.get("quota") or {}
    liveness = quota.get("liveness") or {}
    if liveness.get("free") is True or quota.get("free") is True:
        return True
    if not model:
        return False
    m = str(model).strip().lower()
    if m.endswith(":free"):
        return True
    segs = [s for s in m.split("/") if s]
    last = segs[-1] if segs else m
    return last == "free" or last.endswith("-free")


def _liveness_plan(row: dict, entry: dict) -> tuple[list[str] | None, str]:
    """(argv, reason). argv is None when the probe must not run."""
    conf = ((row.get("quota") or {}).get("liveness") or {})
    template = conf.get("argv") or []
    if not template:
        return None, "no liveness probe configured"
    model = _resolve_model(row, entry)
    if not _is_free_model(row, model):
        if model:
            return None, f"skipped: paid model {model}"
        return None, "skipped: no model to verify as free"
    return [str(a).replace("{model}", model or "") for a in template], ""


def _probe_liveness(row: dict, entry: dict, ctx: q.Ctx,
                    now: datetime, timeout: float) -> dict:
    """One tiny real request through the agent's own CLI.

    Returns a small outcome dict: state is ok | dry | error | unavailable |
    skipped. (The persisted record keeps the schema's ok/dry/unknown states;
    this word is what the start line shows.)
    """
    argv, why = _liveness_plan(row, entry)
    if argv is None:
        return {"state": "unavailable" if why.startswith("no ") else "skipped",
                "detail": why}
    exe = _which(argv[0], ctx.env.get("PATH"))
    if exe is None:
        return {"state": "unavailable",
                "detail": f"unavailable: {argv[0]} not found on PATH"}
    argv[0] = exe
    with tempfile.TemporaryDirectory(prefix="og-probe-") as cwd:
        rc, out, err, timed_out = _run_bounded(argv, ctx.env, timeout, cwd)
    if timed_out:
        return {"state": "error",
                "detail": f"error: timed out after {timeout:g}s"}
    if rc is None and not out and not err:
        return {"state": "error", "detail": "error: could not start the probe"}
    text = f"{out or ''}\n{err or ''}"
    patterns = ((row.get("quota") or {}).get("liveness") or {}).get(
        "failure_patterns") or []
    matched = None
    for pat in patterns:
        try:
            matched = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        except re.error:
            continue
        if matched:
            break
    # A request SUCCEEDED when it exited 0 and printed something. Only a
    # failed request (non-zero, or no reply) carrying a quota string is
    # evidence of exhaustion: a successful reply that merely mentions a quota
    # phrase must clear a mark, not set one.
    succeeded = rc == 0 and bool((out or "").strip())
    if matched and not succeeded:
        msg = next((ln.strip() for ln in text.splitlines()
                    if matched.group(0) in ln), "") or _first_line(
                        matched.group(0))
        return {"state": "dry", "detail": "dry: " + (msg or "quota failure"),
                "expires_at": _reset_from_text(text, now)}
    if succeeded:
        return {"state": "ok", "detail": "ok (reply received)"}
    return {"state": "error",
            "detail": "error: " + (_first_line(text) or
                                   f"exit {rc if rc is not None else '?'}")}


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

    error/unavailable/skipped changes nothing: no evidence is not a licence to
    keep a stale mark, nor to clear one.
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
    under the state lock in probe_start.
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
        # real request AND the pin is verifiably free, verify it -- otherwise
        # "ok, assumed" stays a guess and a paid pin is never billed.
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
            elif live["state"] == "skipped":
                # Keep the assumed quota record; the skip is only a statement
                # about our willingness to spend money, not about capacity.
                live["detail"] = f"{outcome.get('detail', '')}; {live['detail']}"
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
    # Snapshot for the probes' 30-minute cache fallback. Probing happens with
    # NO lock held -- a 20s network call must not block a concurrent
    # `og stats --mark`; the locked merge below is what prevents lost updates.
    snapshot = q.load_state(state_path)
    prev = dict(snapshot.get("agents") or {})
    workers = min(16, max(1, len(ordered)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(
            lambda e: _probe_one(e, rows.get(e["agent"]) or {}, ctx, prev,
                                 now, timeout),
            ordered))

    outcomes: list[dict] = []

    def merge(fresh: dict) -> None:
        """Per-agent merge into the state read under the lock."""
        for agent_id, (o, rec) in zip((e["agent"] for e in ordered), results):
            if rec is not None:
                fresh.setdefault("agents", {})[agent_id] = rec
            _apply(agent_id, o, now, fresh)
            o["agent"] = agent_id
            outcomes.append(o)

    # Locked load->merge->save: a mark written by `og stats` while the probes
    # ran survives, and our own per-agent verdict is applied on top.
    q.update_state(state_path, merge, now=now)
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
