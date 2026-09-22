#!/usr/bin/env python3
"""og quota — per-agent quota/capacity probes (the engine behind `og stats`).

Each probe in PROBES inspects one agent's local credential store or one
vendor's usage endpoint and returns a record in a pinned schema (see
docs/STATS.md). Probes never raise: a failure becomes tier "unknown" with a
one-line detail, because a stats table that dies on the first missing
credential is worse than one that shows one unknown row.

Privacy contract: read-only, never prints or logs token values, and never
refreshes OAuth tokens — a refresh would rotate the user's real session.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

# Network probes must fail fast: og stats is run interactively before a
# dispatch decision, and an 8 s ceiling bounds the worst-case wall clock.
TIMEOUT_S = 8

# How long a last-good record stays reusable after a probe fails or 429s.
CACHE_TTL_S = 30 * 60

STATE_VERSION = 1


class Ctx:
    """Injectable side effects, so tests can stub network/keystore/sqlite.

    Defaults are the real implementations; tests pass lambdas that neither
    touch the network nor ~/.omnigent.
    """

    def __init__(self,
                 http: Callable[..., tuple[int, str]] | None = None,
                 now: Callable[[], datetime] | None = None,
                 env: dict[str, str] | None = None,
                 read_text: Callable[[Path], str] | None = None,
                 read_json: Callable[[Path], Any] | None = None,
                 run: Callable[..., Any] | None = None):
        self.http = http or _default_http
        self.now = now or datetime.now
        self.env = env if env is not None else dict(os.environ)
        self.read_text = read_text or _default_read_text
        self.read_json = read_json or _default_read_json
        self.run = run or subprocess.run


def _default_http(method: str, url: str, headers: dict, body: str | None,
                  timeout: float) -> tuple[int, str]:
    # urllib keeps this file dependency-free; requests is not a given on a
    # user machine and og_audit.py already set the stdlib-only precedent.
    import urllib.request
    import urllib.error
    data = body.encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001 — a probe never raises
        return 0, str(e)


def _default_read_text(path: Path) -> str:
    return path.read_text()


def _default_read_json(path: Path) -> Any:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# shared record helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat()


def unknown_record(detail: str) -> dict:
    return {"state": "unknown", "tier": "unknown", "remaining": None,
            "limit": None, "unit": None, "windows": [], "reset_at": None,
            "source": "none", "checked_at": _iso(datetime.now()),
            "detail": detail}


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def _aware(dt: datetime) -> datetime:
    """Attach the local zone to a naive clock so aware/naive mixes compare.

    Tests pass naive datetimes, production passes aware ones; assume local
    time either way (every timestamp here is produced by .astimezone()).
    """
    return dt if dt.tzinfo else dt.astimezone()


def _pct(x: Any) -> float | None:
    """Normalize a utilization to 0-100.

    Accepts either a fraction (0-1) or a percent (0-100): vendors disagree
    and the pinning spec lists both shapes (`utilization` vs
    `used_percentage`).
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v > 1.0:
        return v
    return v * 100.0


def _first(d: dict, *names: str) -> Any:
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None


def make_record(state: str, tier: str, remaining: float | None,
                limit: float | None, unit: str | None, windows: list[dict],
                reset_at: str | None, source: str, checked_at: str,
                detail: str) -> dict:
    return {"state": state, "tier": tier, "remaining": remaining,
            "limit": limit, "unit": unit, "windows": windows,
            "reset_at": reset_at, "source": source, "checked_at": checked_at,
            "detail": detail}


def _state_from_windows(state: str, windows: list[dict],
                        remaining: float | None) -> str:
    # dry is downstream of a mark OR exhaustion; the measured-exhaustion rule
    # lives here so every probe reports dry the same way (state schema pin).
    if any((w.get("used_percent") or 0) >= 100 for w in windows):
        return "dry"
    if remaining is not None and remaining <= 0:
        return "dry"
    return state


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------

def _probe_anthropic_oauth(params: dict, ctx: Ctx) -> dict:
    token, detail = _anthropic_token(ctx)
    if not token:
        return unknown_record(detail)
    status, body = ctx.http(
        "GET", "https://api.anthropic.com/api/oauth/usage",
        {"Authorization": f"Bearer {token}",
         "anthropic-beta": "oauth-2025-04-20",
         "User-Agent": "claude-code/2.0.0"},
        None, TIMEOUT_S)
    if status == 429:
        raise _RateLimited()
    if status != 200:
        return unknown_record(f"HTTP {status}")
    try:
        d = json.loads(body)
    except ValueError:
        return unknown_record("bad JSON from usage endpoint")
    # rate_limits.five_hour / seven_day: vendors flip between a 0-1
    # `utilization` and a 0-100 `used_percentage`; _pct takes both.
    # Live shape (2026-09-21, api/oauth/usage): windows sit at the TOP level
    # {"five_hour": {"utilization": 0-1, "resets_at": ISO, ...}, "seven_day":
    # {...}, ...} with no "rate_limits" wrapper, so accept both nestings.
    rl = d.get("rate_limits") or d
    windows: list[dict] = []
    for name, key in (("five_hour", "five_hour"), ("seven_day", "seven_day")):
        w = rl.get(key) or {}
        pct = _pct(_first(w, "utilization", "used_percentage"))
        if pct is None:
            continue
        windows.append({"name": name, "used_percent": pct,
                        "reset_at": w.get("resets_at")})
    if not windows:
        return unknown_record(f"unrecognized response shape: {list(d)}")
    # five_hour governs dispatch capacity: it is listed first and binds even
    # when seven_day shows less remaining (a far-off weekly reset must not
    # mask an imminent five-hour block).
    binding = windows[0]
    remaining = 100 - binding["used_percent"]
    return make_record(
        _state_from_windows("ok", windows, remaining), "measured",
        remaining, 100, "percent", windows, binding.get("reset_at"),
        "anthropic-oauth", _iso(ctx.now()),
        f"{len(windows)} window(s); {binding['name']} binding")


def _anthropic_token(ctx: Ctx) -> tuple[str | None, str]:
    """Keychain first, then the fallback file. Never refresh; a token that is
    expired or a non-OAuth sk-ant- API key is reported unknown instead.

    Returns (token, detail) — detail doubles as the reason when token is None.
    """
    try:
        r = ctx.run(["security", "find-generic-password", "-s",
                     "Claude Code-credentials", "-w"],
                    capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            d = json.loads(r.stdout)
            tok = (d.get("claudeAiOauth") or {}).get("accessToken")
            exp = (d.get("claudeAiOauth") or {}).get("expiresAt")
            if tok and (exp is None or exp > ctx.now().timestamp() * 1000):
                # Only sk-ant-api* is a long-lived API key. Genuine Claude
                # Code OAuth access tokens are sk-ant-oat01-* (with an
                # sk-ant-ort01-* refresh token beside them), so rejecting the
                # whole sk-ant- prefix would lock out real OAuth logins.
                if str(tok).startswith("sk-ant-api"):
                    return None, "sk-ant- API key in Keychain is not an OAuth token"
                return tok, "keychain"
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError,
            OSError, ValueError):
        pass
    cred = Path.home() / ".claude" / ".credentials.json"
    try:
        d = ctx.read_json(cred)
        oauth = d.get("claudeAiOauth") or {}
        tok, exp = oauth.get("accessToken"), oauth.get("expiresAt")
        if tok and not str(tok).startswith("sk-ant-api") and \
                (exp is None or exp > ctx.now().timestamp() * 1000):
            return tok, "file"
        return None, "no OAuth token"
    except FileNotFoundError:
        return None, "no OAuth token"


def _probe_codex_wham(params: dict, ctx: Ctx) -> dict:
    auth = _codex_token(ctx)
    # _codex_token returns a (token, account_id) pair; a missing token is
    # (None, None), which is truthy as a tuple, so check the token itself.
    if not auth or not auth[0]:
        return unknown_record("no codex/openai OAuth token")
    token, account_id = auth
    headers = {"Authorization": f"Bearer {token}"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    status, body = ctx.http(
        "GET", "https://chatgpt.com/backend-api/wham/usage",
        headers, None, TIMEOUT_S)
    if status == 429:
        raise _RateLimited()
    if status != 200:
        return unknown_record(f"HTTP {status}")
    try:
        d = json.loads(body)
    except ValueError:
        return unknown_record("bad JSON from wham endpoint")
    rl = d.get("rate_limit") or {}
    windows: list[dict] = []
    reset_at: str | None = None
    for name in ("primary_window", "secondary_window"):
        w = rl.get(name) or {}
        pct = _pct(_first(w, "used_percent", "used_percentage", "percent_used"))
        if pct is None:
            continue
        ra = _wham_reset(w, ctx.now())
        windows.append({"name": name.replace("_window", ""), "used_percent": pct,
                        "reset_at": ra})
    if not windows:
        return unknown_record(f"unrecognized response shape: {list(d)}")
    # primary governs dispatch capacity: it binds even when the secondary
    # window shows less remaining (same first-window rule as anthropic-oauth).
    binding = windows[0]
    remaining = 100 - binding["used_percent"]
    credits = d.get("credits") or {}
    detail = f"{len(windows)} window(s); {binding['name']} binding"
    if credits:
        bal = _first(credits, "balance")
        detail += ("; credits unlimited" if credits.get("unlimited")
                   else f"; credits {bal}" if bal is not None else "")
    return make_record(
        _state_from_windows("ok", windows, remaining), "measured",
        remaining, 100, "percent", windows, binding.get("reset_at"),
        "codex-wham", _iso(ctx.now()), detail)


def _wham_reset(w: dict, now: datetime) -> str | None:
    """wham reports reset_after_seconds (relative) or reset_at (epoch s) —
    prefer the absolute value, fall back to computing one."""
    ra = w.get("reset_at")
    if ra:
        try:
            return datetime.fromtimestamp(float(ra)).astimezone().isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    after = w.get("reset_after_seconds")
    if after:
        try:
            return (now + timedelta(seconds=float(after))).astimezone().isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _codex_token(ctx: Ctx) -> tuple[str | None, str | None]:
    """auth.json first, then opencode's auth store (same ChatGPT login, shared
    token). Returns (token, account_id)."""
    try:
        d = ctx.read_json(Path.home() / ".codex" / "auth.json")
        tokens = d.get("tokens") or {}
        if tokens.get("access_token"):
            return tokens["access_token"], tokens.get("account_id")
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        pass
    try:
        d = ctx.read_json(Path.home() / ".local/share/opencode/auth.json")
        for key in ("opencode/codex", "opencode/openai", "codex", "openai"):
            entry = d.get(key) or {}
            tok = entry.get("access") or entry.get("access_token")
            if tok:
                return tok, None
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        pass
    return None, None


def _probe_antigravity(params: dict, ctx: Ctx) -> dict:
    path = Path.home() / ".config/opencode/antigravity-accounts.json"
    try:
        d = ctx.read_json(path)
    except FileNotFoundError:
        return unknown_record("no antigravity-accounts.json")
    except (json.JSONDecodeError, OSError) as e:
        return unknown_record(f"unreadable: {e}")
    accounts = d.get("accounts") or []
    if not accounts:
        return unknown_record("no accounts in antigravity-accounts.json")
    checked = d.get("cachedQuotaUpdatedAt")
    checked_at = (datetime.fromtimestamp(float(checked)).astimezone().isoformat()
                  if checked else _iso(ctx.now()))
    families: dict[str, float] = {}
    windows: list[dict] = []
    for acct in accounts:
        cq = acct.get("cachedQuota") or {}
        for fam in ("claude", "gemini-pro", "gemini-flash"):
            q = cq.get(fam) or {}
            frac = q.get("remainingFraction")
            if frac is None:
                continue
            # Multiple accounts share one quota pool per family? Not known —
            # take the min as the binding constraint (pessimistic).
            pct = _pct(frac)
            if families.get(fam) is None or pct < families[fam]:
                families[fam] = pct
                rt = q.get("resetTime")
                if rt:
                    windows = [w for w in windows if w["name"] != fam]
                    windows.append({"name": fam, "used_percent": 100 - pct,
                                    "reset_at": rt})
    if not families:
        return unknown_record("no cachedQuota data in accounts")
    fam = min(families, key=families.get)
    remaining = families[fam]
    # worst first: the binding family heads the table row's window list.
    windows.sort(key=lambda w: w.get("used_percent", 0), reverse=True)
    return make_record(
        _state_from_windows("ok", windows, remaining), "measured",
        remaining, 100, "percent", windows, _reset_of(windows, fam),
        "antigravity", checked_at,
        f"{len(accounts)} account(s); binding family {fam}")


def _reset_of(windows: list[dict], name: str) -> str | None:
    for w in windows:
        if w["name"] == name:
            return w.get("reset_at")
    return None


def _probe_kilo_profile(params: dict, ctx: Ctx) -> dict:
    exe = None
    for cand in (ctx.env.get("PATH", ""), str(Path.home() / ".kilo/bin")):
        if not cand:
            continue
        for d in cand.split(os.pathsep):
            p = Path(d) / "kilo"
            if p.exists():
                exe = str(p)
                break
        if exe:
            break
    if not exe:
        return unknown_record("kilo binary not found (PATH or ~/.kilo/bin)")
    try:
        r = ctx.run([exe, "profile", "--json"], capture_output=True,
                    text=True, timeout=TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as e:
        return unknown_record(f"kilo profile failed: {e}")
    if r.returncode != 0:
        return unknown_record(f"kilo profile exit {r.returncode}")
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return unknown_record("kilo profile: bad JSON")
    bal = _first(d, "balance", "remaining_balance", "credits")
    if bal is None:
        return unknown_record(f"kilo profile: no balance field ({list(d)})")
    bal = float(bal)
    return make_record(
        "ok" if bal > 0 else "dry", "measured", bal, None, "credits", [],
        None, "kilo-profile", _iso(ctx.now()), "kilo profile --json")


def _probe_cursor_dashboard(params: dict, ctx: Ctx) -> dict:
    db = Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    if not db.exists():
        return unknown_record("no Cursor state.vscdb")
    token = None
    try:
        # file:...?mode=ro — sqlite would otherwise create/journal the file
        # (og_audit.py set this precedent for the same reason).
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "select value from ItemTable where key = 'cursorAuth/accessToken'"
            ).fetchone()
        finally:
            con.close()
        token = row[0] if row else None
    except sqlite3.Error as e:
        return unknown_record(f"state.vscdb unreadable: {e}")
    if not token:
        return unknown_record("no cursorAuth/accessToken in state.vscdb")
    status, body = ctx.http(
        "POST", "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
         "Connect-Protocol-Version": "1"},
        "{}", TIMEOUT_S)
    if status == 429:
        raise _RateLimited()
    if status != 200:
        return unknown_record(f"HTTP {status}")
    try:
        d = json.loads(body)
    except ValueError:
        return unknown_record("bad JSON from cursor dashboard")
    pu = d.get("planUsage") or {}
    total = _first(pu, "totalPercentUsed", "total_percent_used")
    if total is None:
        return unknown_record(f"unrecognized response shape: {list(d)}")
    total = _pct(total)
    auto = _first(pu, "autoPercentUsed", "auto_percent_used")
    api = _first(pu, "apiPercentUsed", "api_percent_used")
    start = pu.get("billingCycleStart")
    end = pu.get("billingCycleEnd") or pu.get("billingCycleEndMs")
    reset_at = (datetime.fromtimestamp(float(end) / 1000).astimezone().isoformat()
                if end else None)
    detail = f"auto {_pct(auto) if auto is not None else '?'}%"
    if api is not None:
        detail += f", api {_pct(api)}%"
    return make_record(
        _state_from_windows("ok", [{"name": "cycle", "used_percent": total,
                                    "reset_at": reset_at}], 100 - total),
        "measured", 100 - total, 100, "percent",
        [{"name": "cycle", "used_percent": total, "reset_at": reset_at}],
        reset_at, "cursor-dashboard", _iso(ctx.now()), detail)


def _probe_deepseek_balance(params: dict, ctx: Ctx) -> dict:
    key = ctx.env.get("DEEPSEEK_API_KEY")
    if not key:
        return unknown_record("DEEPSEEK_API_KEY not set")
    status, body = ctx.http(
        "GET", "https://api.deepseek.com/user/balance",
        {"Authorization": f"Bearer {key}"}, None, TIMEOUT_S)
    if status == 429:
        raise _RateLimited()
    if status != 200:
        return unknown_record(f"HTTP {status}")
    try:
        d = json.loads(body)
    except ValueError:
        return unknown_record("bad JSON from deepseek balance")
    infos = d.get("balance_infos") or []
    if not infos:
        return unknown_record("no balance_infos in response")
    info = infos[0]
    bal = _first(info, "total_balance")
    cur = info.get("currency", "CNY")
    try:
        bal = float(bal)
    except (TypeError, ValueError):
        return unknown_record("balance_infos[0].total_balance not numeric")
    return make_record(
        "ok" if (d.get("is_available", True) and bal > 0) else "dry",
        # the state schema pins only "usd" as a currency unit; a non-USD
        # account is still reported under it with the real currency in detail.
        "measured", bal, None, "usd", [], None, "deepseek-balance",
        _iso(ctx.now()),
        f"currency {cur}" + ("" if d.get("is_available", True) else "; unavailable"))


def _probe_launch_budget(params: dict, ctx: Ctx) -> dict:
    """freebuff's Freebucks balance, observed — never inferred.

    freebuff exposes no quota API, bills per hour at a per-model rate, and
    can be launched from anywhere (a human's own terminal, another tool), so
    counting this orchestrator's launches and subtracting a flat per-launch
    cost produced confident wrong numbers (2026-09-22: reported 15 left with
    0 in the pool, and a reviewer was dispatched into the failure). An
    inferred number presented as fact is worse than unknown, so this probe
    only reports a balance freebuff itself printed — newest match wins — and
    reports unknown otherwise. Provenance (2026-09-23): the observation is
    taken only from freebuff worker sessions' own error output (chat.db rows
    of `coder_freebuff:` conversations carrying blink's `freebuff:` prefix,
    or runner log lines with the harness error framing); anything else —
    including og's own reports — is ignored by design, because the probe's
    own output lands in chat.db and would otherwise re-observe itself.
    """
    daily = float(params.get("daily") or 0)
    if daily <= 0:
        return unknown_record("no daily budget configured in registry quota block")
    now = ctx.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = midnight + timedelta(days=1)
    home = OMNI_STATE.parent

    best: tuple | None = None  # (ts, remaining, limit, error_only, origin)
    db_ts, db_obs = _scan_chat_items(home / "chat.db")
    if db_obs is not None:
        best = (db_ts, *db_obs, "chat.db")
    log_ts, log_obs, log_name = _scan_runner_logs(home / "logs" / "runner")
    if log_obs is not None and (best is None or _aware(log_ts) > _aware(best[0])):
        best = (log_ts, *log_obs, log_name)

    if best is None or _aware(best[0]) < _aware(midnight):
        # No observation, or the newest one predates the daily reset: the
        # pool refills at local midnight, so a yesterday balance says nothing
        # about today. Limit and reset ARE known (registry daily param, next
        # local midnight); the balance is not.
        why = ("nothing has reported a Freebucks balance since the last reset"
               if best is None else
               "the newest reported balance predates today's reset")
        return make_record(
            "unknown", "unknown", None, daily, "freebucks", [],
            next_midnight.isoformat(), "launch-budget", _iso(now),
            f"freebuff exposes no quota API and {why}; "
            f"pool is {daily:g}/day resetting at local midnight")
    ts, remaining, limit, error_only, origin = best
    age = max(0.0, (_aware(now) - _aware(ts)).total_seconds())
    if error_only:
        what = "not enough Freebucks"
    elif limit is not None:
        what = f"{remaining:g}/{limit:g} Freebucks (daily meter)"
    else:
        what = f"{remaining:g} left"
    return make_record(
        "dry" if (remaining <= 0 or error_only) else "ok", "measured",
        remaining, daily if limit is None else limit, "freebucks", [],
        next_midnight.isoformat(), "launch-budget (observed)", _iso(ts),
        f"freebuff reported {what} {_age_str(age)} ago ({origin})")


# The balance as freebuff/blink itself emits it — every pattern retargeted to
# the real harness error framing seen verbatim on 2026-09-22/23:
#   `... (harness=acp): {... 'inner executor error: ACP session/new failed:
#   freebuff: not enough Freebucks — │ Not enough Freebucks — 5 Freebucks/hr
#   against 0 left. Enter opens plans. │ ...}'`
# (chat.db error payloads in `coder_freebuff:` sessions carry the same text.)
#
# PROVENANCE (2026-09-23 fix): conversation_items holds EVERY session's prose
# — dispatch prompts, reports, code reads, and this probe's own previous
# output — so a bare figure match re-observes the probe forever, always
# seconds old and never decaying to unknown. A figure counts only when
# preceded in the same payload/line by blink's `freebuff:` error prefix (or
# the harness framing that carries it). The TUI meters (`FREE · N/25`,
# `Session ended · N left`) were dropped outright: zero hits in any runner
# log, and every chat.db hit was our own prose quoting them — an unreachable
# pattern that can only false-positive is worse than no pattern.
_FREEBUCKS_PATTERNS: list[tuple] = [
    ("remaining", re.compile(r"against (\d+) left", re.IGNORECASE)),
    ("empty", re.compile(r"freebuff:\s*│?\s*not enough freebucks",
                          re.IGNORECASE)),
]

# Bounds for the scan: chat.db is 100 MB+ on a live machine and the probe
# budget is 8 s, so only the newest rows / newest logs are read.
_FREEBUCKS_SCAN_ROWS = 4000
_FREEBUCKS_SCAN_LOGS = 5


# Provenance prefixes: blink's error prefix, and the harness framing that
# carries it. A figure match counts only when one of these occurs EARLIER in
# the same payload/line — i.e. the text is something blink or the harness
# emitted, not prose quoting it.
_BLINK_PREFIX_RX = re.compile(r"freebuff:", re.IGNORECASE)
_HARNESS_FRAMING_RX = re.compile(
    r"ACP session/new failed|surfaced to UI as failed", re.IGNORECASE)
# Belt and braces on top of the title/prefix rules: the probe's own output is
# itself stored in chat.db (and echoed into runner logs), so reject any
# payload/line carrying it. WHY: without this, any future prose change that
# weakens the prefix rule would let the probe re-observe itself forever.
_SELF_MARKERS = ("freebuff reported", "_probe_launch_budget",
                 "launch-budget (observed)")


def _has_self_marker(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _SELF_MARKERS)


def _extract_freebucks(text: str, *,
                       extra_prefix_rx=None) -> tuple | None:
    """Newest provenanced Freebucks observation in `text`.

    Returns (remaining, limit, error_only). Later matches win (a turn that
    ends `against 0 left` after an earlier `not enough Freebucks` line
    reports the 0, not the error). A `remaining` figure counts only when a
    provenance prefix occurs earlier in the same text; the `empty` pattern
    carries its `freebuff:` prefix inline so it is provenanced by construction.
    Unprovenanced figures (quoted prose, regex source, bare mentions) yield
    None — no evidence, never zero.
    """
    text = text or ""
    marks = [m.start() for m in _BLINK_PREFIX_RX.finditer(text)]
    if extra_prefix_rx is not None:
        marks += [m.start() for m in extra_prefix_rx.finditer(text)]
    best: tuple | None = None  # (pos, remaining, limit, error_only)
    for kind, rx in _FREEBUCKS_PATTERNS:
        for m in rx.finditer(text):
            if kind == "empty":
                cand = (m.start(), 0.0, None, True)
            else:
                if not any(p < m.start() for p in marks):
                    continue
                cand = (m.start(), float(m.group(1)), None, False)
            if best is None or cand[0] >= best[0]:
                best = cand
    return None if best is None else best[1:]


def _epoch_to_aware(created: Any, now: datetime) -> datetime:
    """A chat.db created_at (epoch seconds) as an aware datetime; now on junk."""
    try:
        return datetime.fromtimestamp(float(created)).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return _aware(now)


def _scan_chat_items(db: Path) -> tuple:
    """Newest provenanced (ts, observation) in conversation_items, or (None, None).

    Only rows of `coder_freebuff:` worker sessions count — the join on
    conversations enforces that, so the orchestrator's own prompts, reports
    and code reads never qualify. Within those rows the figure must still
    carry the blink/harness prefix (see _extract_freebucks), and any payload
    carrying the probe's own output is rejected outright: the probe's output
    is itself stored in chat.db, and re-observing it would self-sustain a
    fresh-looking reading forever.

    Newest-first via rowid (append order ~ time order, no full-table sort);
    the first row with a match is the newest evidence. A missing table or an
    unreadable db is "no evidence", not an error — the probe reports unknown.
    """
    try:
        # read-only, same precedent as og_audit.py
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select ci.data, ci.created_at from conversation_items ci "
                "join conversations c on c.id = ci.conversation_id "
                "and c.workspace_id = ci.workspace_id "
                "where c.title like 'coder_freebuff:%' "
                "order by ci.rowid desc limit ?",
                (_FREEBUCKS_SCAN_ROWS,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None, None
    for data, created in rows:
        text = str(data or "")
        if _has_self_marker(text):
            continue
        obs = _extract_freebucks(text)
        if obs is not None:
            return _epoch_to_aware(created, datetime.now()), obs
    return None, None


def _extract_freebucks_lines(text: str) -> tuple | None:
    """Newest provenanced observation across log lines; later lines win.

    A line counts only when it carries the blink `freebuff:` prefix or the
    harness error framing — a log that merely echoes the orchestrator's
    prompt text must not qualify — and never when it carries the probe's
    own output wording.
    """
    best: tuple | None = None
    for line in (text or "").splitlines():
        if _has_self_marker(line):
            continue
        if not (_BLINK_PREFIX_RX.search(line)
                or _HARNESS_FRAMING_RX.search(line)):
            continue
        obs = _extract_freebucks(line, extra_prefix_rx=_HARNESS_FRAMING_RX)
        if obs is not None:
            best = obs
    return best


def _scan_runner_logs(logdir: Path) -> tuple:
    """Newest (ts, observation, origin) in the newest runner logs.

    Files newest-first by mtime; the first file with a match wins, and within
    it the last match (newest line) wins. The mtime stands in for the match's
    own timestamp — a log appended to after the match reads slightly newer
    than the line really is, which only ever makes the evidence look fresher.
    Returns (None, None, None) when nothing matches.
    """
    try:
        logs = sorted(logdir.glob("*.log"),
                      key=lambda p: p.stat().st_mtime,
                      reverse=True)[:_FREEBUCKS_SCAN_LOGS]
    except OSError:
        return None, None, None
    for lp in logs:
        try:
            ts = datetime.fromtimestamp(lp.stat().st_mtime).astimezone()
            obs = _extract_freebucks_lines(lp.read_text(errors="replace"))
        except OSError:
            continue
        if obs is not None:
            return ts, obs, "runner log"
    return None, None, None


def _age_str(age_s: float) -> str:
    if age_s < 90:
        return f"{age_s:.0f}s"
    if age_s < 3600:
        return f"{age_s / 60:.0f}m"
    if age_s < 86400:
        return f"{age_s / 3600:.1f}h"
    return f"{age_s / 86400:.1f}d"


class _RateLimited(Exception):
    """Internal control flow: probe hit 429, fall back to the cache."""


PROBES: dict[str, Callable[[dict, Ctx], dict]] = {
    "anthropic-oauth": _probe_anthropic_oauth,
    "codex-wham": _probe_codex_wham,
    "antigravity": _probe_antigravity,
    "kilo-profile": _probe_kilo_profile,
    "cursor-dashboard": _probe_cursor_dashboard,
    "deepseek-balance": _probe_deepseek_balance,
    "launch-budget": _probe_launch_budget,
}


# ---------------------------------------------------------------------------
# state file ($OMNIGENT_HOME/og-quota.json)
# ---------------------------------------------------------------------------

OMNI_STATE = Path(os.environ.get("OMNIGENT_HOME", Path.home() / ".omnigent")) / "og-quota.json"


def load_state(path: Path | None = None) -> dict:
    p = path or OMNI_STATE
    try:
        d = json.loads(p.read_text())
        if d.get("version") != STATE_VERSION:
            return {"version": STATE_VERSION, "agents": {}, "marks": {}}
        return d
    except FileNotFoundError:
        return {"version": STATE_VERSION, "agents": {}, "marks": {}}
    except (json.JSONDecodeError, OSError):
        return {"version": STATE_VERSION, "agents": {}, "marks": {}}


def save_state(state: dict, path: Path | None = None) -> None:
    p = path or OMNI_STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    # prune expired marks on write (schema pin) and dead agent entries
    now = datetime.now().astimezone()
    marks = {k: v for k, v in (state.get("marks") or {}).items() if _mark_active(v, now)}
    state = {"version": STATE_VERSION, "agents": state.get("agents") or {},
             "marks": marks}
    # atomic write: a crash mid-write must not leave a state file the next
    # run misreads as corrupt and silently discards.
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".og-quota")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            raise


def _mark_active(mark: dict, now: datetime) -> bool:
    until = mark.get("until")
    parsed = _parse_iso(until) if until else None
    # None until (or an unparseable one) means no expiry; normalize zones so
    # naive test clocks and aware production clocks compare.
    return parsed is None or _aware(parsed) > _aware(now)


# ---------------------------------------------------------------------------
# runner: fan out probes, merge marks, keep last-good records
# ---------------------------------------------------------------------------

def run_probes(requests: dict[str, dict], ctx: Ctx,
               state: dict | None = None) -> dict[str, dict]:
    """requests: agent_id -> {"probe": name, **params}. Returns agent_id ->
    merged record (marks applied, cache fallback on failure/429).

    Side effect: the UNMARKED probe records are written back into
    state["agents"], so the state file keeps the last-good measurement for
    --no-probe and the cached fallback; marks render on top and are never
    persisted as records."""
    state = state or {"version": STATE_VERSION, "agents": {}, "marks": {}}
    prev = state.get("agents") or {}
    marks = state.get("marks") or {}

    def one(agent_id: str, req: dict) -> tuple[str, dict, dict]:
        return agent_id, *_one_agent(agent_id, req, ctx, prev, marks)

    # Probes are independent (network + keystore); run them concurrently so a
    # slow endpoint cannot stretch the whole table past its 8 s-per-probe
    # budget. ThreadPoolExecutor over urllib is the stdlib-only way to get it.
    out: dict[str, dict] = {}
    raws: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(requests)))) as ex:
        for agent_id, view, raw in ex.map(lambda kv: one(*kv), requests.items()):
            out[agent_id] = view
            raws[agent_id] = raw
    state.setdefault("agents", {}).update(raws)
    return out


def _one_agent(agent_id: str, req: dict, ctx: Ctx, prev: dict,
               marks: dict) -> tuple[dict, dict]:
    """(view record, raw record). The view has the active mark applied for
    rendering; the raw is the unmarked probe result for persistence."""
    probe_name = req.get("probe")
    params = {k: v for k, v in req.items() if k != "probe"}
    # identity scope for every probe: a probe that reads shared state
    # (chat.db today, a shared dashboard tomorrow) filters to this agent.
    params["agent_id"] = agent_id
    raw = _run_one_probe(probe_name, params, ctx, prev)
    if raw is None:
        # probe absent: keep showing the last stored record (marks still apply);
        # tier unknown with the registry's quota.note only when never probed.
        base = (prev or {}).get(agent_id)
        raw = dict(base) if base is not None else \
            unknown_record(str(req.get("note") or "no quota probe configured"))
    return _apply_mark(agent_id, raw, marks, ctx), raw


def _run_one_probe(probe_name: str | None, params: dict, ctx: Ctx,
                   prev: dict) -> dict | None:
    if not probe_name:
        return None
    fn = PROBES.get(probe_name)
    if fn is None:
        return unknown_record(f"unknown probe {probe_name!r}")
    try:
        rec = fn(params, ctx)
    except _RateLimited:
        return _cached_or(probe_name, prev, ctx,
                          f"{probe_name} rate-limited (429)")
    except Exception as e:  # noqa: BLE001 — a probe never raises; the table
        # must survive one bad probe with a one-line detail (module docstring).
        return _cached_or(probe_name, prev, ctx, f"{probe_name} failed: {e}")
    if rec.get("state") == "unknown" and "no OAuth token" in str(rec.get("detail", "")):
        return rec  # auth-missing is not transient; do not mask with a stale cache
    return rec


def _cached_or(probe_name: str, prev: dict, ctx: Ctx,
               why: str) -> dict:
    """429/failure fallback: reuse the last good record for this probe if it
    is younger than CACHE_TTL_S, else report unknown."""
    for rec in (prev or {}).values():
        src = str(rec.get("source", ""))
        # strip any parenthetical suffix (" (cached)", " (observed)") so an
        # observed launch-budget record stays reusable like any other probe's
        if src.split(" (")[0] != probe_name:
            continue
        age = _age_s(rec.get("checked_at"), ctx)
        if age is not None and age <= CACHE_TTL_S:
            cached = dict(rec)
            cached["source"] = f"{probe_name} (cached)"
            cached["detail"] = f"{why}; reusing last good record"
            return cached
    return unknown_record(why)


def _age_s(iso: str | None, ctx: Ctx) -> float | None:
    dt = _parse_iso(iso) if iso else None
    if dt is None:
        return None
    return (_aware(ctx.now()) - _aware(dt)).total_seconds()


def _apply_mark(agent_id: str, rec: dict, marks: dict, ctx: Ctx) -> dict:
    """An active mark overrides the probe's state (schema pin: dry = active
    mark OR measured exhaustion). Expired marks are ignored here and pruned
    on the next save_state."""
    mark = marks.get(agent_id)
    if mark and _mark_active(mark, ctx.now()):
        rec = dict(rec)
        rec["state"] = mark.get("state", "dry")
        rec["source"] = "mark"
        reason = mark.get("reason") or ""
        rec["detail"] = (f"mark: {reason}" if reason else "mark")
    return rec


def merge_view(state: dict, ctx: Ctx) -> dict:
    """agents dict as stored: active marks re-applied over stored probe
    records, so readers of the state file see the same thing `og stats` does."""
    marks = state.get("marks") or {}
    out = {}
    for aid, rec in (state.get("agents") or {}).items():
        out[aid] = _apply_mark(aid, rec, marks, ctx)
    return out
