"""Tests for installer/og_quota.py and installer/og_stats.py.

No network and no live ~/.omnigent: every test injects a stub http/read/run
through Ctx and points the state file at tmp_path via OMNIGENT_HOME (module
constant q.OMNI_STATE) or explicit paths. The launch-budget probe runs against
a tmp sqlite with the chat.db schema's conversation columns.
"""
from __future__ import annotations

import json
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

    def http(method, url, headers, body, timeout):
        return 429, "{}"

    rec = q._run_one_probe("anthropic-oauth", {}, ctx(http=http, now=lambda: NOW),
                           prev.get("agents") or {})
    assert rec["source"] == "anthropic-oauth (cached)"
    assert rec["remaining"] == 60
    assert rec["detail"].startswith("anthropic-oauth rate-limited")


def test_anthropic_429_without_cache_is_unknown(tmp_path, monkeypatch):
    fake_home(tmp_path, monkeypatch)

    def http(method, url, headers, body, timeout):
        return 429, "{}"

    rec = q._run_one_probe("anthropic-oauth", {}, ctx(http=http, now=lambda: NOW), {})
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
# launch-budget
# --------------------------------------------------------------------------

def _chatdb(path: Path, rows: list[tuple[str, int]]):
    con = sqlite3.connect(path)
    con.execute("create table conversations (id blob, title text, "
                "created_at int, updated_at int, parent_conversation_id blob, "
                "root_conversation_id blob)")
    for i, (title, created) in enumerate(rows):
        con.execute("insert into conversations values (?,?,?,?,?,?)",
                    (bytes([i + 1]), title, created, created, None,
                     bytes([i + 1])))
    con.commit()
    con.close()


def test_launch_budget_counts_today_only(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "OMNI_STATE", tmp_path / "og-quota.json")
    db = tmp_path / "chat.db"
    today = int(NOW.timestamp())
    yesterday = int((NOW - timedelta(days=1)).timestamp())
    _chatdb(db, [
        ("coder_opencode: implement x", today - 60),
        ("coder_opencode: implement y", today - 30),
        ("coder_claude: review", today - 20),
        ("orchestrator run", today - 10),        # not coder_*
        ("coder_opencode: yesterday", yesterday),  # not today
    ])
    rec = q._probe_launch_budget({"daily": 10, "per_launch": 0.5},
                                 ctx(now=lambda: NOW))
    assert rec["tier"] == "inferred"
    assert rec["remaining"] == pytest.approx(10 - 3 * 0.5)
    assert rec["unit"] == "launches"
    assert rec["state"] == "ok"
    assert rec["reset_at"]  # next local midnight


def test_launch_budget_exhausted_is_dry(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "OMNI_STATE", tmp_path / "og-quota.json")
    db = tmp_path / "chat.db"
    _chatdb(db, [(f"coder_a: run {i}", int(NOW.timestamp()) - i - 1)
                 for i in range(4)])
    rec = q._probe_launch_budget({"daily": 2, "per_launch": 1},
                                 ctx(now=lambda: NOW))
    assert rec["state"] == "dry" and rec["remaining"] == pytest.approx(-2)


def test_launch_budget_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "OMNI_STATE", tmp_path / "og-quota.json")
    rec = q._probe_launch_budget({"daily": 10, "per_launch": 1}, ctx())
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
    assert timedelta(0) < until - NOW.replace(tzinfo=until.tzinfo) <= \
        timedelta(hours=2, minutes=1)
    st.main(["--clear", "opencode"])
    assert "opencode" not in q.load_state(state_path)["marks"]


def test_cli_bad_until_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "OMNI", tmp_path)
    with pytest.raises(SystemExit):
        st.main(["--mark", "opencode", "--until", "next week"])
