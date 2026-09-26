"""Tests for installer/og_quota.py and installer/og_stats.py.

No network and no live ~/.omnigent: every test injects a stub http/read/run
through Ctx and points the state file at tmp_path via OMNIGENT_HOME (module
constant q.OMNI_STATE) or explicit paths. The launch-budget probe runs against
a tmp sqlite with the chat.db conversation_items columns plus tmp runner logs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
import og_quota as q  # noqa: E402
import og_stats as st  # noqa: E402


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

def ok_http(body: dict):
    return lambda method, url, headers, body_, timeout: (200, json.dumps(body))


def ctx(http=None, now=None, env=None, read_json=None, read_text=None,
        run=None) -> q.Ctx:
    kwargs = {}
    if http:
        kwargs["http"] = http
    if now:
        kwargs["now"] = now
    if env is not None:
        kwargs["env"] = env
    if read_json:
        kwargs["read_json"] = read_json
    if read_text:
        kwargs["read_text"] = read_text
    if run:
        kwargs["run"] = run
    return q.Ctx(**kwargs)


NOW = datetime(2026, 9, 21, 10, 30, 0)


def fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


# --------------------------------------------------------------------------
# anthropic-oauth
# --------------------------------------------------------------------------

def test_anthropic_happy_keychain(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    monkeypatch.setattr(q, "OMNI_STATE", tmp_path / "state.json")
    cred = {"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9999999999999}}

    def run(cmd, **kw):
        assert cmd == ["security", "find-generic-password", "-s",
                       "Claude Code-credentials", "-w"]
        r = type("R", (), {})()
        r.returncode, r.stdout = 0, json.dumps(cred)
        return r

    calls = []

    def http(method, url, headers, body, timeout):
        calls.append((method, url, headers))
        return 200, json.dumps({"rate_limits": {
            "five_hour": {"utilization": 0.4, "resets_at": "2026-09-21T12:00:00Z"},
            "seven_day": {"used_percentage": 20, "resets_at": "2026-09-25T00:00:00Z"},
        }})

    rec = q._probe_anthropic_oauth({}, ctx(http=http, now=lambda: NOW, run=run))
    assert rec["state"] == "ok" and rec["tier"] == "measured"
    assert rec["remaining"] == pytest.approx(60.0)  # five_hour binds (60 < 80)
    assert rec["unit"] == "percent"
    assert {w["name"] for w in rec["windows"]} == {"five_hour", "seven_day"}
    # never leaks the token into the record
    assert "tok" not in json.dumps(rec)
    h = calls[0][2]
    assert h["Authorization"] == "Bearer tok" and \
        h["anthropic-beta"] == "oauth-2025-04-20"


def test_anthropic_missing_auth(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)

    def run(cmd, **kw):
        r = type("R", (), {})()
        r.returncode, r.stdout = 1, ""
        return r

    rec = q._probe_anthropic_oauth({}, ctx(run=run))
    assert rec["state"] == "unknown" and "no OAuth token" in rec["detail"]


def test_anthropic_top_level_shape(tmp_path, monkeypatch):
    # live api/oauth/usage nests five_hour/seven_day at the top level, with
    # no "rate_limits" wrapper (observed 2026-09-21).
    fake_home(tmp_path, monkeypatch)

    def run(cmd, **kw):
        r = type("R", (), {})()
        r.returncode, r.stdout = 0, json.dumps(
            {"claudeAiOauth": {"accessToken": "tok",
                               "expiresAt": 9999999999999}})
        return r

    rec = q._probe_anthropic_oauth({}, ctx(
        http=ok_http({"five_hour": {"utilization": 0.08,
                                    "resets_at": "2026-09-22T00:40:00+07:00"},
                      "seven_day": {"utilization": 0.5,
                                    "resets_at": "2026-09-25T00:00:00Z"}}),
        now=lambda: NOW, run=run))
    assert rec["state"] == "ok" and rec["tier"] == "measured"
    assert rec["remaining"] == pytest.approx(92.0)  # five_hour binds
    assert rec["reset_at"] == "2026-09-22T00:40:00+07:00"


def test_anthropic_sk_ant_key_is_not_oauth(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    cred = {"claudeAiOauth": {"accessToken": "sk-ant-api03-x", "expiresAt": 9999999999999}}

    def run(cmd, **kw):
        r = type("R", (), {})()
        r.returncode, r.stdout = 0, json.dumps(cred)
        return r

    rec = q._probe_anthropic_oauth({}, ctx(run=run))
    assert rec["state"] == "unknown" and "sk-ant-" in rec["detail"]


def test_anthropic_uses_cache_on_429(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    state_path = tmp_path / "state.json"
    good = q.make_record("ok", "measured", 60, 100, "percent",
                         [{"name": "five_hour", "used_percent": 40,
                           "reset_at": None}], None, "anthropic-oauth",
                         q._iso(NOW - timedelta(minutes=5)), "prior")
    q.save_state({"version": 1, "agents": {"a": good}, "marks": {}}, state_path)
    prev = q.load_state(state_path)

    # hermetic keychain: the probe must reach HTTP for the 429 path to run,
    # independent of whatever credential this machine happens to hold.
    def run(cmd, **kw):
        r = type("R", (), {})()
        r.returncode, r.stdout = 0, json.dumps(
            {"claudeAiOauth": {"accessToken": "tok",
                               "expiresAt": 9999999999999}})
        return r

    def http(method, url, headers, body, timeout):
        return 429, "{}"

    rec = q._run_one_probe("anthropic-oauth", {},
                           ctx(http=http, now=lambda: NOW, run=run),
                           prev.get("agents") or {})
    assert rec["source"] == "anthropic-oauth (cached)"
    assert rec["remaining"] == 60
    assert rec["detail"].startswith("anthropic-oauth rate-limited")


def test_anthropic_429_without_cache_is_unknown(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)

    def run(cmd, **kw):
        r = type("R", (), {})()
        r.returncode, r.stdout = 0, json.dumps(
            {"claudeAiOauth": {"accessToken": "tok",
                               "expiresAt": 9999999999999}})
        return r

    def http(method, url, headers, body, timeout):
        return 429, "{}"

    rec = q._run_one_probe("anthropic-oauth", {},
                           ctx(http=http, now=lambda: NOW, run=run), {})
    assert rec["state"] == "unknown" and "429" in rec["detail"]


def test_expired_cache_not_reused(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    good = q.make_record("ok", "measured", 60, 100, "percent", [], None,
                         "anthropic-oauth",
                         q._iso(NOW - timedelta(minutes=45)), "prior")
    rec = q._run_one_probe(
        "anthropic-oauth", {},
        ctx(http=lambda *a: (_ for _ in ()).throw(AssertionError("no call")),
            now=lambda: NOW),
        {"a": good})
    assert rec["state"] == "unknown"


# --------------------------------------------------------------------------
# codex-wham
# --------------------------------------------------------------------------

def _codex_files(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text(json.dumps(
        {"tokens": {"access_token": "ctok", "account_id": "acct-1"}}))
    return home


def test_codex_happy(tmp_path, monkeypatch):
    home = _codex_files(tmp_path, monkeypatch)
    seen = {}

    def http(method, url, headers, body, timeout):
        seen.update(headers)
        return 200, json.dumps({"rate_limit": {
            "primary_window": {"used_percent": 25,
                               "reset_after_seconds": 3600,
                               "reset_at": 1789980000},
            "secondary_window": {"used_percent": 80},
        }, "credits": {"unlimited": False, "balance": 42.5}})

    rec = q._probe_codex_wham({}, ctx(http=http, now=lambda: NOW))
    assert seen["Authorization"] == "Bearer ctok"
    assert seen["ChatGPT-Account-Id"] == "acct-1"
    assert rec["state"] == "ok"
    assert rec["remaining"] == pytest.approx(75.0)  # primary binds
    assert rec["detail"].endswith("credits 42.5")
    assert "ctok" not in json.dumps(rec)


def test_codex_missing_auth(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    rec = q._probe_codex_wham({}, ctx())
    assert rec["state"] == "unknown" and "no codex" in rec["detail"]


def test_codex_opencode_fallback(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    oc = home / ".local/share/opencode"
    oc.mkdir(parents=True)
    (oc / "auth.json").write_text(json.dumps(
        {"opencode/codex": {"access": "otok", "refresh": "r"}}))
    rec = q._probe_codex_wham({}, ctx(
        http=lambda *a: (200, json.dumps({"rate_limit": {
            "primary_window": {"used_percent": 10}}})),
        now=lambda: NOW))
    assert rec["state"] == "ok" and rec["source"] == "codex-wham"


# --------------------------------------------------------------------------
# antigravity
# --------------------------------------------------------------------------

def test_antigravity_happy(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    cfg = home / ".config/opencode"
    cfg.mkdir(parents=True)
    updated = NOW.timestamp() - 600
    (cfg / "antigravity-accounts.json").write_text(json.dumps({
        "cachedQuotaUpdatedAt": updated,
        "accounts": [{"cachedQuota": {
            "claude": {"remainingFraction": 0.9, "resetTime": "2026-09-22T00:00:00Z"},
            "gemini-pro": {"remainingFraction": 0.1, "resetTime": "2026-09-22T00:00:00Z"},
            "gemini-flash": {"remainingFraction": 0.5},
        }}]}))
    rec = q._probe_antigravity({}, ctx(now=lambda: NOW))
    assert rec["tier"] == "measured"
    assert rec["remaining"] == pytest.approx(10.0)  # min across families
    assert rec["checked_at"] == q._iso(datetime.fromtimestamp(updated))
    assert rec["windows"][0]["name"] == "gemini-pro"


def test_antigravity_missing(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    rec = q._probe_antigravity({}, ctx())
    assert rec["state"] == "unknown"


# --------------------------------------------------------------------------
# kilo-profile
# --------------------------------------------------------------------------

def test_kilo_happy(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    kilo = tmp_path / "bin"
    kilo.mkdir()
    (kilo / "kilo").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", str(kilo))

    def run(cmd, **kw):
        assert cmd[1:] == ["profile", "--json"]
        r = type("R", (), {})()
        r.returncode, r.stdout = 0, json.dumps({"balance": 12.5})
        return r

    rec = q._probe_kilo_profile({}, ctx(env={"PATH": str(kilo)}, run=run))
    assert rec["remaining"] == 12.5 and rec["unit"] == "credits"
    assert rec["state"] == "ok"


def test_kilo_missing_binary(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)
    rec = q._probe_kilo_profile({}, ctx(env={"PATH": ""}))
    assert rec["state"] == "unknown" and "kilo binary" in rec["detail"]


# --------------------------------------------------------------------------
# cursor-dashboard
# --------------------------------------------------------------------------

def test_cursor_happy(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    db = home / "Library/Application Support/Cursor/User/globalStorage"
    db.mkdir(parents=True)
    con = sqlite3.connect(db / "state.vscdb")
    con.execute("create table ItemTable (key text, value blob)")
    con.execute("insert into ItemTable values ('cursorAuth/accessToken', 'curtok')")
    con.commit()
    con.close()
    end_ms = int((NOW + timedelta(days=10)).timestamp() * 1000)

    def http(method, url, headers, body, timeout):
        assert method == "POST" and body == "{}"
        assert headers["Connect-Protocol-Version"] == "1"
        return 200, json.dumps({"planUsage": {
            "totalPercentUsed": 55, "autoPercentUsed": 50,
            "apiPercentUsed": 5, "billingCycleEnd": end_ms}})

    rec = q._probe_cursor_dashboard({}, ctx(http=http, now=lambda: NOW))
    assert rec["remaining"] == pytest.approx(45.0)
    assert rec["windows"][0]["used_percent"] == 55
    assert "curtok" not in json.dumps(rec)


def test_cursor_no_token(tmp_path, monkeypatch):
    home = fake_home(tmp_path, monkeypatch)
    db = home / "Library/Application Support/Cursor/User/globalStorage"
    db.mkdir(parents=True)
    con = sqlite3.connect(db / "state.vscdb")
    con.execute("create table ItemTable (key text, value blob)")
    con.commit()
    con.close()
    rec = q._probe_cursor_dashboard({}, ctx())
    assert rec["state"] == "unknown"


# --------------------------------------------------------------------------
# deepseek-balance
# --------------------------------------------------------------------------

def test_deepseek_happy(tmp_path, monkeypatch):
    def http(method, url, headers, body, timeout):
        assert headers["Authorization"] == "Bearer k1"
        return 200, json.dumps({"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "8.50"}]})

    rec = q._probe_deepseek_balance({}, ctx(env={"DEEPSEEK_API_KEY": "k1"},
                                            http=http))
    assert rec["remaining"] == 8.5 and rec["unit"] == "usd"
    assert "currency CNY" in rec["detail"]


def test_deepseek_no_key(tmp_path, monkeypatch):
    rec = q._probe_deepseek_balance({}, ctx(env={}))
    assert rec["state"] == "unknown" and "DEEPSEEK_API_KEY" in rec["detail"]


# --------------------------------------------------------------------------
# launch-budget: observed balance or unknown, never inferred
# --------------------------------------------------------------------------

FREEBUFF_TITLE = "coder_freebuff:test"
ORCH_TITLE = "Agent quota and delegation"  # the orchestrator's own session


# SELF-MATCH HYGIENE (same invariant as installer/og_quota.py): a worker
# session cat-ing THIS file must not become evidence, so the blink prefix,
# the pool noun, and the per-hour rate unit are never written contiguously
# here — every verbatim error frame below is assembled from these fragments
# at runtime. The tests assert exactly what they did before; only the way
# the literal is spelled changed.
_BLINK = "free" + "buff:"
_FB = "Free" + "bucks"
_FB_HR = "Free" + "bucks/hr"


def _frame(left: int) -> str:
    """The verbatim live error frame (2026-09-22/23), assembled from pieces."""
    return ("ACP session/new failed: " + _BLINK + " not enough " + _FB +
            " \u2014 \u2502 Not enough " + _FB + " \u2014 5 " + _FB_HR +
            f" against {left} left. Enter opens plans. \u2502")


def _err(left: int) -> str:
    """Real-shape blink error payload (verbatim framing, 2026-09-22/23)."""
    return '{"error": "' + _frame(left) + '"}'


def _itemsdb(path: Path, rows: list[tuple], title: str = FREEBUFF_TITLE):
    """chat.db-shaped db: conversations + conversation_items, ids joined.

    Each row is (data, created) or (data, created, title); the join in
    _scan_chat_items resolves the title per row.
    """
    con = sqlite3.connect(path)
    con.execute("create table conversations "
                "(id blob, workspace_id bigint, title varchar(768))")
    con.execute("create table conversation_items (id blob, "
                "conversation_id blob, response_id text, created_at int, "
                "position int, data text, search_text text, "
                "workspace_id bigint)")
    cids: dict[str, bytes] = {}

    def cid_for(t: str) -> bytes:
        if t not in cids:
            cids[t] = bytes([len(cids) + 1]) * 16
            con.execute("insert into conversations values (?,?,?)",
                        (cids[t], 0, t))
        return cids[t]

    for i, row in enumerate(rows):
        data, created = row[0], row[1]
        t = row[2] if len(row) > 2 else title
        con.execute("insert into conversation_items values (?,?,?,?,?,?,?,?)",
                    (bytes([(i % 250) + 1]), cid_for(t), f"r{i}", created, i,
                     data, data[:200], 0))
    con.commit()
    con.close()


def _mkhome(tmp_path: Path, monkeypatch) -> Path:
    """Point OMNI_STATE at tmp so chat.db + logs/runner resolve under it."""
    monkeypatch.setattr(q, "OMNI_STATE", tmp_path / "og-quota.json")
    return tmp_path


def _wlog(home: Path, name: str, text: str, age_s: float) -> Path:
    d = home / "logs" / "runner"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text)
    ts = NOW.timestamp() - age_s
    os.utime(p, (ts, ts))
    return p


def _logline(dt: datetime, text: str) -> str:
    """A realistic stamped runner log line (stamps carry no year)."""
    return f"ERROR {dt.strftime('%m-%d %H:%M:%S')}.000 runner.app | {text}\n"


def test_launch_budget_against_left_from_chat_db(tmp_path, monkeypatch):
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 14 * 60
    _itemsdb(home / "chat.db", [
        ('{"output": "old line, nothing"}', ts - 3600),
        (_err(0), ts),
    ])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["tier"] == "measured"
    assert rec["source"] == "launch-budget (observed)"
    assert rec["remaining"] == 0
    assert rec["state"] == "dry"  # remaining 0 -> dry
    assert rec["limit"] == 25 and rec["unit"] == "freebucks"
    assert rec["reset_at"]  # next local midnight
    assert "0 left" in rec["detail"] and "chat.db" in rec["detail"]
    assert q._parse_iso(rec["checked_at"]) is not None


def test_launch_budget_prefixed_not_enough_is_dry(tmp_path, monkeypatch):
    """`freebuff: not enough` with the blink prefix means an empty pool."""
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 60
    _itemsdb(home / "chat.db",
             [('{"error": "' + _BLINK + " Not enough " + _FB + '"}', ts)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["remaining"] == 0
    assert rec["state"] == "dry"
    assert "not enough" in rec["detail"].lower()


def test_launch_budget_orchestrator_session_is_ignored(tmp_path, monkeypatch):
    """The SAME error text in the orchestrator's session is not an
    observation — it is prose about freebuff, not output from freebuff."""
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 60
    _itemsdb(home / "chat.db", [(_err(0), ts)], title=ORCH_TITLE)
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["tier"] == "unknown"
    assert rec["remaining"] is None


def test_launch_budget_own_output_is_ignored(tmp_path, monkeypatch):
    """The probe's own detail wording lands in chat.db; re-observing it
    would self-sustain a fresh-looking reading forever."""
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 5
    _itemsdb(home / "chat.db",
             [("freebuff reported not enough Freebucks 9m ago (chat.db)",
               ts)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_own_source_is_ignored(tmp_path, monkeypatch):
    """A payload with the probe's source code is rejected even when it sits
    next to a real error — the self-marker rule backing up the
    title/prefix/structure rules."""
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 5
    _itemsdb(home / "chat.db",
             [("def _probe_launch_budget(params, ctx): " + _err(0), ts)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_bare_not_enough_is_ignored(tmp_path, monkeypatch):
    """A bare `not enough` with no `freebuff:` prefix is quoted prose, not
    an observation — no evidence, never zero."""
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 60
    _itemsdb(home / "chat.db",
             [('{"output": "Not enough Freebucks"}', ts)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_tui_meters_are_ignored(tmp_path, monkeypatch):
    """The dropped TUI patterns match nothing, even with a prefix nearby —
    they only ever appeared in prose we quoted ourselves."""
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 60
    _itemsdb(home / "chat.db", [
        ('{"error": "freebuff: done FREE \u00b7 8/25 Freebucks daily"}', ts),
        ('{"error": "freebuff: Session ended \u00b7 12 Freebucks left"}',
         ts + 1),
    ])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_newest_provenanced_wins(tmp_path, monkeypatch):
    """Newest-wins among rows that pass provenance; a newer row that does
    not (orchestrator title here) must not shadow an older observation."""
    home = _mkhome(tmp_path, monkeypatch)
    now = int(NOW.timestamp())
    _itemsdb(home / "chat.db", [
        (_err(10), now - 7200),
        (_err(3), now - 600),
        (_err(99), now - 60, ORCH_TITLE),
    ])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["remaining"] == 3
    assert "chat.db" in rec["detail"]


def test_launch_budget_source_window_without_rate_clause_is_unknown(
        tmp_path, monkeypatch):
    """Neither the probe's source nor THIS test file may read as evidence.

    The reviewer's exact scenario: a window of og_quota.py's own source —
    pattern pieces plus surrounding prose — inside a worker session must
    yield unknown. The structural rule (rate clause adjacent to the figure)
    does the work here, NOT the self-marker rule: the window is asserted to
    carry no self marker and to contain a `freebuff:` prefix occurrence, so
    only the missing full frame can explain the unknown.

    WHY this test exists: this orchestrator routinely dispatches workers to
    review this very file, so its source (pattern strings included) lands in
    worker sessions — and the same holds for this test file, whose verbatim
    frames are assembled from fragments at runtime for exactly that reason.
    If a future edit reintroduces a quotable full frame into either file,
    this test fails closed. Both files are read generically (q.__file__ and
    __file__) so a rename cannot silently skip one.
    """
    home = _mkhome(tmp_path, monkeypatch)
    src = Path(q.__file__).read_text()
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines) if "_FB_RATE_UNIT =" in l)
    end = next(i for i, l in enumerate(lines) if "_BLINK_PREFIX_RX =" in l)
    window = "\n".join(lines[start:end + 1])
    assert "freebuff:" in window.lower()  # prefix present, as in the review
    assert not q._has_self_marker(window)  # ... but no self marker
    # rate unit never contiguous (spelled via the runtime value, not the
    # literal, so this assertion itself does not introduce the literal)
    assert q._FB_RATE_UNIT.lower() not in window.lower()
    assert q._extract_freebucks(window) is None
    assert q._extract_freebucks(src) is None
    tsrc = Path(__file__).read_text()
    assert _FB_HR.lower() not in tsrc.lower()  # same, for this file
    assert q._extract_freebucks(tsrc) is None
    ts = int(NOW.timestamp())
    _itemsdb(home / "chat.db", [(window, ts - 60), (tsrc, ts - 59)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_human_prose_with_figure_is_unknown(tmp_path,
                                                          monkeypatch):
    """Human prose carrying the prefix and a bare figure — but no rate
    clause — is not an observation."""
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 60
    _itemsdb(home / "chat.db",
             [('{"output": "freebuff: it died with against 0 left again"}',
               ts)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_verbatim_live_error_is_observed(tmp_path, monkeypatch):
    """The verbatim live frame (rate clause adjacent to the figure) is the
    load-bearing match: observed, remaining 0, dry."""
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 60
    _itemsdb(home / "chat.db", [(_frame(0), ts)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["tier"] == "measured"
    assert rec["remaining"] == 0
    assert rec["state"] == "dry"


def test_launch_budget_stale_stamped_line_in_fresh_file_is_unknown(
        tmp_path, monkeypatch):
    """A genuine error stamped yesterday, in a log touched seconds ago (a
    later append), must NOT read as fresh: the line's own stamp is the
    evidence time, and it decays across midnight."""
    home = _mkhome(tmp_path, monkeypatch)
    _wlog(home, "runner-fresh.log",
          _logline(NOW - timedelta(days=1, hours=1),
                   "ACP session/new failed: " + _BLINK + " not enough " +
                   _FB + " \u2014 5 " + _FB_HR + " against 0 left."),
          age_s=5)
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_fresh_stamped_line_is_observed(tmp_path, monkeypatch):
    """A line stamped minutes ago in a recently-touched file reads with its
    real (fresh) age."""
    home = _mkhome(tmp_path, monkeypatch)
    _wlog(home, "runner-fresh.log",
          _logline(NOW - timedelta(minutes=5),
                   "ACP session/new failed: " + _BLINK + " not enough " +
                   _FB + " \u2014 5 " + _FB_HR + " against 5 left."),
          age_s=5)
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["tier"] == "measured"
    assert rec["remaining"] == 5 and rec["state"] == "ok"


def test_launch_budget_unstamped_log_line_is_skipped(tmp_path, monkeypatch):
    """A figure on a line with no parsable stamp is no evidence — even when
    the file mtime is now. Falling back to mtime would make stale errors
    look seconds old after any later append."""
    home = _mkhome(tmp_path, monkeypatch)
    _wlog(home, "runner-nostamp.log",
          "ACP session/new failed: " + _BLINK + " not enough " + _FB +
          " \u2014 5 " + _FB_HR + " against 0 left.\n",
          age_s=5)
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_junk_created_at_is_skipped(tmp_path, monkeypatch):
    """A row with an unparsable created_at is skipped, not treated as
    just-observed: the newer junk-dated error must not shadow the older
    valid one, and junk alone must read unknown (never permanently fresh)."""
    home = _mkhome(tmp_path, monkeypatch)
    now = int(NOW.timestamp())
    _itemsdb(home / "chat.db", [
        (_err(10), now - 7200),
        (_err(0), "junk"),
    ])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["remaining"] == 10  # junk row skipped, older valid row wins

    home2 = _mkhome(tmp_path, monkeypatch)
    (home2 / "chat.db").unlink()
    _itemsdb(home2 / "chat.db", [(_err(0), "junk")])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_underscore_title_is_not_a_worker(tmp_path,
                                                        monkeypatch):
    """`_` is a single-char LIKE wildcard: a `coderXfreebuff:` session must
    not join as a worker even when it carries a real error."""
    home = _mkhome(tmp_path, monkeypatch)
    ts = int(NOW.timestamp()) - 60
    _itemsdb(home / "chat.db", [(_err(0), ts, "coderXfreebuff:e")])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_run_probes_injects_agent_id(tmp_path, monkeypatch):
    seen = {}

    def stub(params, c):
        seen.update(params)
        return q.make_record("ok", "measured", 1, 2, "x", [], None,
                             "stub-id-probe", q._iso(NOW), "fine")

    monkeypatch.setitem(q.PROBES, "stub-id-probe", stub)
    q.run_probes({"freebuff": {"probe": "stub-id-probe", "daily": 25}},
                 ctx(now=lambda: NOW),
                 {"version": 1, "agents": {}, "marks": {}})
    assert seen["agent_id"] == "freebuff"


def test_launch_budget_harness_error_from_runner_log(tmp_path, monkeypatch):
    home = _mkhome(tmp_path, monkeypatch)
    _wlog(home, "runner-a.log",
          _logline(NOW - timedelta(seconds=180),
                   "turn surfaced to UI as failed "
                   "for 8ad3 (harness=acp): {'code': 'runner_error', 'message': "
                   "'inner executor error: ACP session/new failed: " +
                   _BLINK + " not enough " + _FB + " \u2014 5 " + _FB_HR +
                   " against 8 left.'}\n"),
          age_s=180)
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["tier"] == "measured"
    assert rec["source"] == "launch-budget (observed)"
    assert rec["remaining"] == 8 and rec["limit"] == 25
    assert rec["state"] == "ok"
    assert "runner log" in rec["detail"]


def test_launch_budget_runner_log_prompt_echo_is_ignored(tmp_path,
                                                         monkeypatch):
    """A log line that merely echoes prompt/report prose (no blink prefix,
    no harness framing) must not qualify, even with a figure in it."""
    home = _mkhome(tmp_path, monkeypatch)
    _wlog(home, "runner-a.log",
          _logline(NOW - timedelta(seconds=60),
                   "dispatched coder_freebuff with prompt: Session ended \u00b7 "
                   "12 Freebucks left is the meter to watch\n"),
          age_s=60)
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_newest_wins_across_sources(tmp_path, monkeypatch):
    home = _mkhome(tmp_path, monkeypatch)
    _itemsdb(home / "chat.db",
             [(_err(10), int(NOW.timestamp()) - 7200)])
    _wlog(home, "runner-b.log",
          _logline(NOW - timedelta(seconds=600),
                   "turn surfaced to UI as failed (harness=acp): "
                   "ACP session/new failed: " + _BLINK + " not enough " +
                   _FB + " \u2014 5 " + _FB_HR + " against 3 left."),
          age_s=600)
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["remaining"] == 3
    assert "runner log" in rec["detail"]


def test_launch_budget_newer_db_beats_older_log(tmp_path, monkeypatch):
    home = _mkhome(tmp_path, monkeypatch)
    _wlog(home, "runner-c.log",
          _logline(NOW - timedelta(seconds=7200),
                   "ACP session/new failed: " + _BLINK + " not enough " +
                   _FB + " \u2014 5 " + _FB_HR + " against 2 left."),
          age_s=7200)
    _itemsdb(home / "chat.db",
             [(_err(20), int(NOW.timestamp()) - 600)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["remaining"] == 20
    assert "chat.db" in rec["detail"]


def test_launch_budget_stale_across_midnight_is_unknown(tmp_path, monkeypatch):
    """Yesterday's balance says nothing about today's refilled pool."""
    home = _mkhome(tmp_path, monkeypatch)
    yesterday = int((NOW - timedelta(days=1)).timestamp())
    _itemsdb(home / "chat.db", [(_err(20), yesterday)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["tier"] == "unknown"
    assert rec["remaining"] is None
    assert rec["limit"] == 25  # the pool size IS known
    assert rec["reset_at"]  # next local midnight IS known
    assert "no quota API" in rec["detail"]


def test_launch_budget_nothing_found_is_unknown_not_a_number(tmp_path,
                                                             monkeypatch):
    """No observation anywhere: unknown with a null remaining, never 25-0."""
    home = _mkhome(tmp_path, monkeypatch)
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["tier"] == "unknown"
    assert rec["remaining"] is None
    assert rec["limit"] == 25 and rec["reset_at"]
    # a db whose rows carry no figure is the same: no evidence, not zero
    _itemsdb(home / "chat.db",
             [('{"output": "hello world"}', int(NOW.timestamp()) - 60)])
    rec = q._probe_launch_budget({"daily": 25}, ctx(now=lambda: NOW))
    assert rec["state"] == "unknown" and rec["remaining"] is None


def test_launch_budget_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "OMNI_STATE", tmp_path / "og-quota.json")
    rec = q._probe_launch_budget({"daily": 10}, ctx())
    assert rec["state"] == "unknown"


def test_launch_budget_no_params(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "OMNI_STATE", tmp_path / "og-quota.json")
    rec = q._probe_launch_budget({}, ctx())
    assert rec["state"] == "unknown"


# --------------------------------------------------------------------------
# runner: state, marks, merged view
# --------------------------------------------------------------------------

def test_probe_never_raises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(q.PROBES, "anthropic-oauth", boom)
    out = q.run_probes({"a": {"probe": "anthropic-oauth"}},
                       ctx(now=lambda: NOW), {"version": 1, "agents": {}, "marks": {}})
    assert out["a"]["state"] == "unknown" and "kaboom" in out["a"]["detail"]


def test_unknown_probe_name(tmp_path):
    out = q.run_probes({"a": {"probe": "nope"}}, ctx(now=lambda: NOW),
                       {"version": 1, "agents": {}, "marks": {}})
    assert out["a"]["state"] == "unknown" and "unknown probe" in out["a"]["detail"]


def test_mark_overrides_probe_state(tmp_path):
    rec = q.make_record("ok", "measured", 80, 100, "percent", [], None,
                        "anthropic-oauth", q._iso(NOW), "fine")
    out = q.run_probes(
        {"a": {"probe": None}},
        ctx(now=lambda: NOW),
        {"version": 1, "agents": {"a": rec},
         "marks": {"a": {"state": "dry", "until": None, "reason": "Saving quota for tomorrow", "at": q._iso(NOW)}}})
    # probe=None means no probe fn -> cached/unknown path, but mark applies
    assert out["a"]["state"] == "dry" and out["a"]["source"] == "mark"
    assert "Saving quota for tomorrow" in out["a"]["detail"]


def test_expired_mark_ignored_and_pruned(tmp_path):
    rec = q.make_record("ok", "measured", 80, 100, "percent", [], None,
                        "anthropic-oauth", q._iso(NOW), "fine")
    expired = (NOW - timedelta(hours=1)).isoformat()
    state = {"version": 1, "agents": {"a": rec},
             "marks": {"a": {"state": "dry", "until": expired,
                             "reason": "old", "at": expired}}}
    out = q.run_probes({"a": {"probe": None}}, ctx(now=lambda: NOW), state)
    assert out["a"]["state"] == "ok"  # mark ignored
    state_path = tmp_path / "state.json"
    q.save_state(state, state_path)
    assert "a" not in q.load_state(state_path)["marks"]  # pruned on write


def test_probe_run_persists_raw_record_not_mark(tmp_path, monkeypatch):
    monkeypatch.setitem(q.PROBES, "stub-ok",
                        lambda params, c: q.make_record(
                            "ok", "measured", 80, 100, "percent", [], None,
                            "stub-ok", q._iso(NOW), "fine"))
    good = q.make_record("ok", "measured", 80, 100, "percent", [], None,
                         "stub-ok", q._iso(NOW), "fine")
    state = {"version": 1, "agents": {"a": good},
             "marks": {"a": {"state": "dry", "until": None,
                             "reason": "hold", "at": q._iso(NOW)}}}
    out = q.run_probes({"a": {"probe": "stub-ok"}},
                       ctx(now=lambda: NOW), state)
    # the rendered view shows the mark, but the stored record stays raw
    assert out["a"]["state"] == "dry" and out["a"]["source"] == "mark"
    raw = state["agents"]["a"]
    assert raw["state"] == "ok" and raw["source"] == "stub-ok"


def test_mark_clear_no_probe_restores_probe_record(tmp_path):
    state_path = tmp_path / "og-quota.json"
    good = q.make_record("ok", "measured", 60, 100, "percent", [], None,
                         "anthropic-oauth", q._iso(NOW), "fine")
    q.save_state(
        {"version": 1, "agents": {"claude": good},
         "marks": {"claude": {"state": "dry", "until": None,
                              "reason": "hold", "at": q._iso(NOW)}}},
        state_path)
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    _write_install(inst)
    _write_registry(reg, {"claude": {"probe": "anthropic-oauth"}})
    rows = st.build_rows(st.lineup(inst, reg), state_path,
                         no_probe=True, only=None)
    marked = [r for r in rows if r["agent"] == "claude"][0]["rec"]
    assert marked["state"] == "dry" and marked["source"] == "mark"
    state = q.load_state(state_path)
    state.get("marks", {}).pop("claude", None)
    q.save_state(state, state_path)
    rows = st.build_rows(st.lineup(inst, reg), state_path,
                         no_probe=True, only=None)
    rec = [r for r in rows if r["agent"] == "claude"][0]["rec"]
    assert rec["state"] == "ok" and rec["source"] == "anthropic-oauth"


def test_cached_fallback_survives_mark(tmp_path, monkeypatch):
    good = q.make_record("ok", "measured", 80, 100, "percent", [], None,
                         "anthropic-oauth", q._iso(NOW), "fine")
    state = {"version": 1, "agents": {"a": good},
             "marks": {"a": {"state": "dry", "until": None,
                             "reason": "hold", "at": q._iso(NOW)}}}

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setitem(q.PROBES, "anthropic-oauth", boom)
    out = q.run_probes({"a": {"probe": "anthropic-oauth"}},
                       ctx(now=lambda: NOW), state)
    # view still shows the mark, but the stored record is the cached probe
    # result, never a mark overlay
    assert out["a"]["state"] == "dry" and out["a"]["source"] == "mark"
    assert state["agents"]["a"]["source"] == "anthropic-oauth (cached)"
    # once the mark is cleared the cached record is reachable again
    state["marks"].pop("a")
    out = q.run_probes({"a": {"probe": "anthropic-oauth"}},
                       ctx(now=lambda: NOW), state)
    assert out["a"]["source"] == "anthropic-oauth (cached)"


def test_state_atomic_and_versioned(tmp_path):
    p = tmp_path / "sub" / "og-quota.json"
    q.save_state({"version": 1, "agents": {"a": {}}, "marks": {}}, p)
    assert q.load_state(p)["version"] == 1
    p.write_text("{not json")
    assert q.load_state(p) == {"version": 1, "agents": {}, "marks": {}}
    # no temp files left behind
    assert not list(p.parent.glob(".og-quota*"))


# --------------------------------------------------------------------------
# og_stats CLI: lineup, table, json, marks, registry override
# --------------------------------------------------------------------------

def _write_install(path: Path):
    path.write_text(json.dumps({
        "orchestrator": {"id": "claude"},
        "coders": [{"id": "opencode", "priority": 1},
                   {"id": "kilo", "priority": 2}],
        "reviewer": {"id": "codex"},
    }))


def _write_registry(path: Path, quota: dict | None = None):
    agents = {}
    for aid in ("claude", "opencode", "kilo", "codex"):
        agents[aid] = {"label": aid, "roles": ["coder"]}
        if quota and aid in quota:
            agents[aid]["quota"] = quota[aid]
    path.write_text(json.dumps({"agents": agents}))


def test_lineup_order(tmp_path):
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    _write_install(inst)
    _write_registry(reg)
    rows = st.lineup(inst, reg)
    assert [(r["role"], r["agent"]) for r in rows] == [
        ("orchestrator", "claude"), ("coder", "opencode"),
        ("coder", "kilo"), ("reviewer", "codex")]


def test_lineup_list_registry(tmp_path):
    # the real installer/registry.json carries agents as a list of rows
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    _write_install(inst)
    reg.write_text(json.dumps({"agents": [
        {"id": "claude", "roles": ["coder"],
         "quota": {"probe": "anthropic-oauth"}},
        {"id": "opencode", "roles": ["coder"]},
        {"id": "kilo", "roles": ["coder"]},
        {"id": "codex", "roles": ["coder"]},
    ]}))
    rows = st.lineup(inst, reg)
    assert [(r["role"], r["agent"]) for r in rows] == [
        ("orchestrator", "claude"), ("coder", "opencode"),
        ("coder", "kilo"), ("reviewer", "codex")]
    assert [r for r in rows if r["agent"] == "claude"][0]["probe"] == \
        "anthropic-oauth"


def test_lineup_expands_orchestrator_and_reviewer_chains(tmp_path):
    """A role is an ordered chain now. `og stats` is the evidence the
    orchestrator reads to pick the earliest reviewer with capacity, so every
    entry must get its own row -- a backup missing here cannot be failed over
    to on anything better than a guess."""
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    inst.write_text(json.dumps({
        "orchestrator": [{"id": "claude", "priority": 1},
                         {"id": "codex", "priority": 2}],
        "coders": [{"id": "opencode", "priority": 1}],
        "reviewer": [{"id": "codex", "priority": 1},
                     {"id": "kiro", "priority": 2}],
    }))
    _write_registry(reg)
    rows = st.lineup(inst, reg)
    assert [(r["role"], r["agent"]) for r in rows] == [
        ("orchestrator", "claude"), ("orchestrator", "codex"),
        ("coder", "opencode"),
        ("reviewer", "codex"), ("reviewer", "kiro")]


def test_lineup_skips_a_malformed_chain_entry(tmp_path):
    # og stats runs interactively before a dispatch decision, so a hand-edited
    # og-install.json must skip a bad entry, not trace back: `"reviewer": [5]`
    # used to raise AttributeError on `5.get`. None/{}/int/list elements, a
    # scalar role value, and a dict with no id all have to be tolerated.
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    inst.write_text(json.dumps({
        "orchestrator": [None, "claude", {}, 5, ["x"]],
        "coders": [{"id": "opencode", "priority": 1}],
        "reviewer": [5, None, {}, "codex"],
    }))
    _write_registry(reg)
    rows = st.lineup(inst, reg)
    assert [(r["role"], r["agent"]) for r in rows] == [
        ("orchestrator", "claude"), ("coder", "opencode"), ("reviewer", "codex")]
    # no malformed entry may reach the renderer as a None agent
    assert all(isinstance(r["agent"], str) for r in rows)

    # A non-list, non-str, non-dict role value is simply empty.
    inst.write_text(json.dumps({"orchestrator": 5, "coders": [],
                                "reviewer": {"id": "codex"}}))
    assert [r["agent"] for r in st.lineup(inst, reg)] == ["codex"]


def test_lineup_malformed_entries_survive_the_table_render(tmp_path):
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    inst.write_text(json.dumps({
        "orchestrator": [5],
        "coders": [],
        "reviewer": [{"id": "codex", "priority": 1}, None],
    }))
    _write_registry(reg)
    rows = st.build_rows(st.lineup(inst, reg), tmp_path / "s.json",
                         no_probe=True, only=None)
    table = st.render_table(rows, NOW)   # crashed on a None agent before
    assert "codex" in table


def test_lineup_reports_the_scout_chain_in_order(tmp_path):
    """scout is an ordered failover chain, exactly like reviewer. `og stats` is
    the evidence the orchestrator reads to pick the earliest scout with capacity,
    so an entry absent here cannot be failed over to on anything but a guess."""
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    inst.write_text(json.dumps({
        "orchestrator": {"id": "claude"},
        "coders": [{"id": "opencode", "priority": 1}],
        "reviewer": {"id": "codex"},
        "scout": [{"id": "cmdcode", "priority": 1},
                  {"id": "kiro", "priority": 2}],
    }))
    _write_registry(reg)
    rows = st.lineup(inst, reg)
    assert [(r["role"], r["agent"]) for r in rows] == [
        ("orchestrator", "claude"), ("coder", "opencode"),
        ("reviewer", "codex"),
        ("scout", "cmdcode"), ("scout", "kiro")]


def test_lineup_tolerates_a_non_iterable_coders_value(tmp_path):
    # A hand-edited "coders": 5 raised TypeError: 'int' object is not iterable.
    # og stats runs interactively right before a dispatch decision, so it must
    # skip what it cannot read rather than trace back.
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    _write_registry(reg)
    for bad in (5, None, "opencode", {"id": "opencode"}):
        inst.write_text(json.dumps({"orchestrator": {"id": "claude"},
                                    "coders": bad,
                                    "reviewer": {"id": "codex"}}))
        assert [r["agent"] for r in st.lineup(inst, reg)] == ["claude", "codex"]


def test_lineup_skips_unreadable_coder_elements_and_bad_priorities(tmp_path):
    # None/{} elements name no agent; a null priority used to raise TypeError:
    # '<' not supported between instances of 'int' and 'NoneType' in the sort.
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    _write_registry(reg)
    inst.write_text(json.dumps({
        "orchestrator": {"id": "claude"},
        "coders": [None, {}, {"id": "opencode", "priority": None},
                   {"id": "kilo", "priority": 2}],
        "reviewer": {"id": "codex"},
    }))
    rows = st.lineup(inst, reg)
    assert all(isinstance(r["agent"], str) for r in rows)
    assert [r["agent"] for r in rows if r["role"] == "coder"] == ["kilo", "opencode"]


def test_lineup_non_int_priorities_sort_after_real_ones(tmp_path):
    # sorted() is stable, so malformed (null/str) priorities land AFTER every
    # int priority but keep their original array order among themselves.
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    _write_registry(reg)
    inst.write_text(json.dumps({
        "orchestrator": {"id": "claude"},
        "coders": [{"id": "opencode", "priority": None},
                   {"id": "kilo", "priority": "high"},
                   {"id": "codex", "priority": 2},
                   {"id": "claude", "priority": 1}],
        "reviewer": {"id": "codex"},
    }))
    rows = st.lineup(inst, reg)
    assert [r["agent"] for r in rows if r["role"] == "coder"] == [
        "claude", "codex", "opencode", "kilo"]


def test_table_and_json_shape(tmp_path, capsys):
    state_path = tmp_path / "og-quota.json"
    rec = q.make_record("ok", "measured", 60, 100, "percent",
                        [{"name": "five_hour", "used_percent": 40,
                          "reset_at": NOW.isoformat()}],
                        NOW.isoformat(), "anthropic-oauth", q._iso(NOW), "d")
    q.save_state({"version": 1, "agents": {"claude": rec}, "marks": {}},
                 state_path)
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    _write_install(inst)
    _write_registry(reg, {"claude": {"probe": "anthropic-oauth"}})

    rows = st.build_rows(
        st.lineup(inst, reg), state_path, no_probe=True, only=None)
    table = st.render_table(rows, NOW)
    for col in ("role", "agent", "state", "remaining", "reset", "tier",
                "source", "age", "detail"):
        assert col in table
    assert "claude" in table and "anthropic-oauth" in table

    js = json.loads(st.render_json(rows, NOW))
    assert js["version"] == 1 and "checked_at" in js
    a = js["agents"]["claude"]
    assert a["role"] == "orchestrator" and a["priority"] is None
    for k in ("state", "tier", "remaining", "limit", "unit", "windows",
              "reset_at", "source", "checked_at", "detail"):
        assert k in a


def test_absent_probe_uses_note(tmp_path):
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    _write_install(inst)
    _write_registry(reg, {"kilo": {"probe": None, "note": "no public quota API"}})
    rows = st.build_rows(st.lineup(inst, reg), tmp_path / "s.json",
                         no_probe=False, only=None)
    kilo = [r for r in rows if r["agent"] == "kilo"][0]["rec"]
    assert kilo["state"] == "unknown" and kilo["tier"] == "unknown"
    assert kilo["detail"] == "no public quota API"


def test_cli_mark_and_clear(tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "og-quota.json"
    monkeypatch.setattr(st, "OMNI", tmp_path)
    monkeypatch.setattr(q, "OMNI_STATE", state_path)
    st.main(["--mark", "opencode", "--until", "+2h",
             "--reason", "saving for the big run"])
    assert "mark opencode" in capsys.readouterr().out
    state = q.load_state(state_path)
    until = q._parse_iso(state["marks"]["opencode"]["until"])
    # +2h is relative to the wall clock at mark time, not the fixed NOW the
    # probe tests use (do_mark reads the real clock via st._now()).
    live_now = datetime.now().astimezone()
    assert timedelta(0) < until - live_now <= \
        timedelta(hours=2, minutes=1)
    st.main(["--clear", "opencode"])
    assert "opencode" not in q.load_state(state_path)["marks"]


def test_cli_bad_until_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "OMNI", tmp_path)
    with pytest.raises(SystemExit):
        st.main(["--mark", "opencode", "--until", "next week"])


def test_cli_mark_with_and_without_dry_state(tmp_path, monkeypatch, capsys):
    # The orchestrator prompt, the roster/fanout skills and docs all run
    # `og stats --mark <id> dry ...`; the bare `--mark <id>` form must mark
    # the agent identically.
    state_path = tmp_path / "og-quota.json"
    monkeypatch.setattr(st, "OMNI", tmp_path)
    monkeypatch.setattr(q, "OMNI_STATE", state_path)
    st.main(["--mark", "kilo", "--until", "+1h", "--reason", "quota"])
    capsys.readouterr()
    st.main(["--mark", "cline", "dry", "--until", "+1h", "--reason", "quota"])
    assert "mark cline" in capsys.readouterr().out
    marks = q.load_state(state_path)["marks"]
    for aid in ("kilo", "cline"):
        assert marks[aid]["state"] == "dry"
        assert marks[aid]["reason"] == "quota"
        assert marks[aid]["until"] is not None
    st.main(["--clear", "kilo"])
    st.main(["--clear", "cline"])


def test_cli_mark_bad_state_exits(tmp_path, monkeypatch):
    # The optional trailing word is the literal `dry`; anything else names
    # the accepted value in the usage error.
    monkeypatch.setattr(st, "OMNI", tmp_path)
    with pytest.raises(SystemExit):
        st.main(["--mark", "opencode", "wet"])
