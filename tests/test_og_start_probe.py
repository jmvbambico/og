"""Tests for installer/og_start_probe.py and the +1h mark default.

No network and no live ~/.omnigent: every probe runs a fake CLI written into
tmp_path and placed on the PATH the probe is given. The state file is always
an explicit tmp_path argument, so nothing touches the real install.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
import og_quota as q  # noqa: E402
import og_start_probe as sp  # noqa: E402
import og_stats as st  # noqa: E402

# Real wall clock: save_state prunes marks against the actual clock, so a
# fixed past date would be pruned before the assertion could read it.
NOW = datetime.now().astimezone()
REPO = Path(__file__).resolve().parent.parent


def _fake_cli(bin_dir: Path, name: str, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


def _install(path: Path, model: str | None = "opencode/mimo-v2.6-flash-free",
             agent: str = "opencode"):
    entry = {"id": agent, "priority": 1}
    if model:
        entry["model"] = model
    path.write_text(json.dumps({"coders": [entry]}))


def _registry(path: Path, patterns: list[str] | None = None, *,
              probe: str | None = None, argv: list[str] | None = None,
              model_prefer: str | None = "opencode/mimo-v2.6-flash-free",
              agent: str = "opencode",
              free_models: list[str] | None = None):
    model: dict = {"required": True}
    if model_prefer:
        model["prefer"] = model_prefer
    liveness: dict = {
        "argv": argv or ["opencode", "run", "-m", "{model}",
                         "Reply with exactly: ok. Do not use tools."],
        "failure_patterns": patterns or ["Rate limit exceeded"],
    }
    if free_models is not None:
        liveness["free_models"] = free_models
    path.write_text(json.dumps({"agents": [{
        "id": agent,
        "label": agent,
        "roles": ["coder"],
        "model": model,
        "quota": {
            "probe": probe,
            "note": "no queryable balance",
            "liveness": liveness,
        },
    }]}))


def _mark(state_path: Path, agent: str = "opencode", until=None):
    q.save_state({"version": 1, "agents": {},
                  "marks": {agent: {"state": "dry", "until": until,
                                    "reason": "stale", "at": q._iso(NOW)}}},
                 state_path)


def _ctx(bin_dir: Path, now: datetime = NOW,
         extra_env: dict | None = None, system: bool = False) -> q.Ctx:
    # By default only the fake bin dir is on PATH: a missing binary must be
    # genuinely missing, not found later in the machine's real PATH. Tests
    # whose fake CLI calls shell utilities (sleep, ps, touch) opt into the
    # system directories explicitly.
    path = str(bin_dir) + (":/usr/bin:/bin" if system else "")
    env = {"PATH": path}
    env.update(extra_env or {})
    return q.Ctx(env=env, now=lambda: now)


def _run(tmp_path, bin_dir, *, timeout=5.0, ctx=None, patterns=None,
         probe=None, argv=None, agent="opencode",
         model="opencode/mimo-v2.6-flash-free", prefer=None,
         free_models=None, system=False):
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    state = tmp_path / "og-quota.json"
    _install(inst, model, agent)
    if free_models is None:
        # Default for the success-path tests: the installed pin is listed.
        # Billing tests pass [] or an explicit list on purpose.
        free_models = [model] if model else []
    _registry(reg, patterns, probe=probe, argv=argv, agent=agent,
              model_prefer=prefer if prefer is not None else model,
              free_models=free_models)
    return sp.probe_start(inst, reg, state, timeout=timeout,
                          ctx=ctx or _ctx(bin_dir, system=system)), state


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                         capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


# ---------------------------------------------------------------------------
# liveness: the outcomes that matter
# ---------------------------------------------------------------------------

def test_ok_liveness_clears_an_unexpiring_mark(tmp_path):
    """The field bug: a mark with no expiry stayed dry forever. A healthy
    liveness reply must clear it and record last_probe_at/ok."""
    bin_dir = tmp_path / "bin"
    args_file = tmp_path / "args"
    _fake_cli(bin_dir, "opencode",
              f'printf "%s\\n" "$@" > "{args_file}"\necho ok\n')
    state = tmp_path / "og-quota.json"
    _mark(state)

    outcomes, state = _run(tmp_path, bin_dir)

    assert outcomes[0]["state"] == "ok"
    saved = q.load_state(state)
    assert "opencode" not in saved["marks"]
    rec = saved["agents"]["opencode"]
    assert rec["state"] == "ok" and rec["source"] == "liveness"
    assert rec["last_probe_at"]
    # the pin from the install (not a hardcoded id) reached the CLI
    assert args_file.read_text().splitlines() == [
        "run", "-m", "opencode/mimo-v2.6-flash-free",
        "Reply with exactly: ok. Do not use tools."]


def test_quota_string_marks_dry_with_the_parsed_expiry(tmp_path):
    """A known quota string is evidence of exhaustion: mark dry, and take the
    expiry from 'Resets in Xh Ym' rather than the +1h fallback."""
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode",
              "echo \"You've reached your 5-hour usage limit. "
              "Resets in 2h 41m\" >&2\nexit 1\n\n")
    state = tmp_path / "og-quota.json"

    outcomes, state = _run(tmp_path, bin_dir, patterns=[
        "You've reached your .*usage limit", "Rate limit exceeded"])

    assert outcomes[0]["state"] == "dry"
    saved = q.load_state(state)
    until = q._parse_iso(saved["marks"]["opencode"]["until"])
    assert until is not None
    assert timedelta(hours=2, minutes=40) <= until - NOW <= \
        timedelta(hours=2, minutes=42)
    # after the mark expires the stored record must not keep reading `dry`
    assert saved["agents"]["opencode"]["state"] == "unknown"


def test_successful_reply_with_a_quota_phrase_is_ok(tmp_path):
    """#5: a successful reply mentioning a quota phrase is NOT exhaustion.
    Only a FAILED request (non-zero or no reply) may be classified dry."""
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode",
              "echo ok\necho 'Rate limit exceeded' >&2\nexit 0\n")
    state = tmp_path / "og-quota.json"
    _mark(state)

    outcomes, state = _run(tmp_path, bin_dir)

    assert outcomes[0]["state"] == "ok"
    assert "opencode" not in q.load_state(state)["marks"]


def test_exit_zero_with_no_reply_and_a_quota_phrase_is_dry(tmp_path):
    """No successful reply + a quota string is still evidence of exhaustion."""
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode", "echo 'Rate limit exceeded' >&2\nexit 0\n")
    state = tmp_path / "og-quota.json"

    outcomes, state = _run(tmp_path, bin_dir)

    assert outcomes[0]["state"] == "dry"
    assert "opencode" in q.load_state(state)["marks"]


def test_missing_binary_is_unavailable_not_dry(tmp_path):
    """A broken install says nothing about capacity: never dry."""
    bin_dir = tmp_path / "empty"
    bin_dir.mkdir()
    state = tmp_path / "og-quota.json"

    outcomes, state = _run(tmp_path, bin_dir)

    assert outcomes[0]["state"] == "unavailable"
    assert "not found" in outcomes[0]["detail"]
    saved = q.load_state(state)
    assert saved["marks"] == {}
    assert saved["agents"]["opencode"]["state"] == "unknown"
    assert "not found" in saved["agents"]["opencode"]["detail"]


def test_timeout_is_error_not_dry(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode", "sleep 5\n")
    state = tmp_path / "og-quota.json"

    outcomes, state = _run(tmp_path, bin_dir, timeout=0.3, system=True)

    assert outcomes[0]["state"] == "error"
    assert "timed out" in outcomes[0]["detail"]
    assert q.load_state(state)["marks"] == {}


def test_timeout_kills_the_whole_process_group(tmp_path):
    """#1: a grandchild the CLI spawned must not survive the timeout."""
    bin_dir = tmp_path / "bin"
    pidfile = tmp_path / "grandchild.pid"
    _fake_cli(bin_dir, "opencode",
              f'sleep 60 &\necho $! > "{pidfile}"\nwait\n')
    ctx = _ctx(bin_dir, extra_env={"OG_PROBE_PID": str(pidfile)},
               system=True)

    # 1.5s: enough for the shell to start and background its grandchild even
    # on a loaded machine, short enough to keep the suite quick.
    outcomes, _ = _run(tmp_path, bin_dir, timeout=1.5, ctx=ctx)

    assert outcomes[0]["state"] == "error" and "timed out" in outcomes[0][
        "detail"]
    assert pidfile.exists(), "the fake CLI never started its grandchild"
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _running(pid):
        time.sleep(0.05)
    assert not _running(pid), f"grandchild {pid} survived the probe timeout"


def test_probe_runs_in_a_fresh_empty_temporary_directory(tmp_path):
    """#1: never the caller's cwd, and the directory is gone afterwards."""
    bin_dir = tmp_path / "bin"
    pwd_file = tmp_path / "pwd"
    _fake_cli(bin_dir, "opencode", f'pwd > "{pwd_file}"\necho ok\n')
    ctx = _ctx(bin_dir, extra_env={"OG_PROBE_PWD": str(pwd_file)},
               system=True)

    outcomes, _ = _run(tmp_path, bin_dir, ctx=ctx)

    assert outcomes[0]["state"] == "ok"
    probed_cwd = Path(pwd_file.read_text().strip())
    assert probed_cwd != Path.cwd()
    assert probed_cwd != tmp_path
    assert not probed_cwd.exists(), "TemporaryDirectory was not cleaned up"


def test_auth_error_is_reported_without_drying(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode",
              "echo 'not logged in: run opencode auth login' >&2\nexit 1\n")
    state = tmp_path / "og-quota.json"

    outcomes, state = _run(tmp_path, bin_dir)

    assert outcomes[0]["state"] == "error"
    assert "not logged in" in outcomes[0]["detail"]
    assert q.load_state(state)["marks"] == {}


def test_default_expiry_is_one_hour_when_no_reset_is_printed(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode", "echo 'Rate limit exceeded' >&2\nexit 1\n")
    state = tmp_path / "og-quota.json"

    outcomes, _ = _run(tmp_path, bin_dir)

    assert outcomes[0]["state"] == "dry"
    until = outcomes[0]["until"]
    assert timedelta(minutes=59) <= until - NOW <= timedelta(minutes=61)


# ---------------------------------------------------------------------------
# #2: a probe must never bill
# ---------------------------------------------------------------------------

def test_a_paid_pin_is_skipped_without_running_the_cli(tmp_path):
    marker = tmp_path / "ran"
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "cmd", f'touch "{marker}"\necho ok\n')

    outcomes, state = _run(
        tmp_path, bin_dir, agent="cmdcode", model="moonshotai/kimi-k3",
        prefer="moonshotai/kimi-k3", free_models=[],
        argv=["cmd", "-p", "Reply with exactly: ok. Do not use tools.",
              "--model", "{model}", "--skip-onboarding"],
        patterns=["Rate limit exceeded"])

    assert outcomes[0]["state"] == "skipped"
    assert "model not verified free: moonshotai/kimi-k3" in \
        outcomes[0]["detail"]
    assert not marker.exists(), "a paid probe was actually executed"
    assert q.load_state(state)["marks"] == {}


def test_unpinned_falls_back_to_a_paid_prefer_and_is_skipped(tmp_path):
    marker = tmp_path / "ran"
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode", f'touch "{marker}"\necho ok\n')

    outcomes, state = _run(tmp_path, bin_dir, model=None,
                           prefer="anthropic/claude-opus-5", free_models=[])

    assert outcomes[0]["state"] == "skipped"
    assert "model not verified free: anthropic/claude-opus-5" in \
        outcomes[0]["detail"]
    assert not marker.exists()
    assert q.load_state(state)["marks"] == {}


def test_a_free_looking_suffix_alone_is_not_authoritative(tmp_path):
    """(a) A paid model can be NAMED `...-free`; only the registry allow-list
    may authorise spending. The CLI must not run."""
    marker = tmp_path / "ran"
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode", f'touch "{marker}"\necho ok\n')

    outcomes, state = _run(tmp_path, bin_dir, model="opencode/paid-model-free",
                           free_models=[])

    assert outcomes[0]["state"] == "skipped"
    assert "model not verified free: opencode/paid-model-free" in \
        outcomes[0]["detail"]
    assert not marker.exists()
    assert q.load_state(state)["marks"] == {}


def test_an_allow_listed_id_is_probed(tmp_path):
    """(b) The only way a liveness request runs is an exact allow-list hit."""
    marker = tmp_path / "ran"
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode", f'touch "{marker}"\necho ok\n')

    outcomes, _ = _run(tmp_path, bin_dir, model="opencode/listed-free",
                       free_models=["opencode/listed-free"], system=True)

    assert outcomes[0]["state"] == "ok"
    assert marker.exists()


def test_free_models_is_an_exact_allow_list():
    row = {"quota": {"liveness": {"free_models": ["opencode/listed-free"]}}}
    assert sp._is_free_model(row, "opencode/listed-free")
    # no shape rule: any other id, however free-looking, is not authorised
    assert not sp._is_free_model(row, "opencode/other-free")
    assert not sp._is_free_model(row, "opencode/listed-free:free")
    assert not sp._is_free_model({}, "kilo/kilo-auto/free")
    assert not sp._is_free_model({"quota": {"liveness": {"free": True}}},
                                 "opencode/listed-free")


def test_a_model_template_without_any_model_is_skipped(tmp_path):
    """Nothing user-specific is invented: no pin and no prefer means no
    probe (and certainly not a billed one)."""
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode", "echo ok\n")
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    state = tmp_path / "og-quota.json"
    inst.write_text(json.dumps({"coders": [{"id": "opencode",
                                            "priority": 1}]}))
    # the registry's prefer is the installer's default pin, so to test the
    # empty case remove it from the row too
    _registry(reg, model_prefer=None, free_models=[])

    outcomes = sp.probe_start(inst, reg, state, ctx=_ctx(bin_dir))
    assert outcomes[0]["state"] == "skipped"
    assert "no model to verify as free" in outcomes[0]["detail"]


def test_registry_liveness_has_no_action_enabling_flags():
    """#1: probes are capability checks. No --yolo/--tools-all in any row, and
    every prompt forbids tools."""
    reg = json.loads((REPO / "installer" / "registry.json").read_text())
    for row in reg["agents"]:
        live = (row.get("quota") or {}).get("liveness") or {}
        argv = live.get("argv") or []
        assert "--yolo" not in argv, row["id"]
        assert "--tools-all" not in argv, row["id"]
        if argv:
            assert any("Do not use tools" in str(a) for a in argv), row["id"]


def test_registry_free_models_belong_to_their_agent_row():
    """(c) Every allow-listed id is an exact id owned by its own row: listed
    only there, prefixed by that agent's provider, and including the row's
    preferred pin whenever the row has an allow-list at all."""
    reg = json.loads((REPO / "installer" / "registry.json").read_text())
    claimed: dict[str, str] = {}
    allow_lists = 0
    for row in reg["agents"]:
        live = (row.get("quota") or {}).get("liveness") or {}
        ids = live.get("free_models") or []
        assert isinstance(ids, list), row["id"]
        if ids:
            allow_lists += 1
            assert row["model"]["prefer"] in ids, row["id"]
        for mid in ids:
            assert isinstance(mid, str) and mid == mid.strip() and mid
            assert mid.split("/")[0] == row["id"], (row["id"], mid)
            assert mid not in claimed, (mid, row["id"], claimed[mid])
            claimed[mid] = row["id"]
    # the two unmeasured-tier rows that document a free path have one;
    # Command Code/Kiro have none, so their paid pins stay skipped.
    assert allow_lists == 2
    assert claimed == {
        "opencode/mimo-v2.6-flash-free": "opencode",
        "opencode/deepseek-v4.1-flash-free": "opencode",
        "opencode/mimo-v2.5-free": "opencode",
        "kilo/kilo-auto/free": "kilo",
    }


# ---------------------------------------------------------------------------
# #3: reset parsing (days, combos, zero is not a match)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,delta", [
    ("Resets in 2d", timedelta(days=2)),
    ("Resets in 1d 3h", timedelta(days=1, hours=3)),
    ("Resets in 2h 41m", timedelta(hours=2, minutes=41)),
    ("Resets in 45m", timedelta(minutes=45)),
    ("Resets in 30s", timedelta(seconds=30)),
    ("You've reached your limit. Resets in 2 days 3 hours.", timedelta(
        days=2, hours=3)),
])
def test_reset_parsing_supports_days_and_combos(text, delta):
    assert sp._reset_from_text(text, NOW) == NOW + delta


@pytest.mark.parametrize("text", [
    "Resets in 0m", "Resets in 0d 0h", "Resets in", "soon",
    "Resets in soon",
])
def test_zero_or_empty_reset_is_not_a_match(text):
    assert sp._reset_from_text(text, NOW) is None


# ---------------------------------------------------------------------------
# measurable probes: the same mark lifecycle, using the existing probe
# ---------------------------------------------------------------------------

def test_measured_ok_clears_a_mark_and_measured_dry_writes_one(tmp_path,
                                                               monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = {"n": 0}

    def probe(params, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return q.make_record("ok", "measured", 80, 100, "percent", [],
                                 None, "stub", q._iso(NOW), "fine")
        return q.make_record("dry", "measured", 0, 100, "percent", [],
                             (NOW + timedelta(minutes=30)).isoformat(),
                             "stub", q._iso(NOW), "exhausted")

    monkeypatch.setitem(q.PROBES, "stub", probe)
    state = tmp_path / "og-quota.json"
    _mark(state)
    outcomes, state = _run(tmp_path, bin_dir, probe="stub")
    assert outcomes[0]["state"] == "ok"
    assert "opencode" not in q.load_state(state)["marks"]

    # a second run sees dryness; the mark carries the probe's reset time
    outcomes, state = _run(tmp_path, bin_dir, probe="stub")
    assert outcomes[0]["state"] == "dry"
    until = q._parse_iso(q.load_state(state)["marks"]["opencode"]["until"])
    assert until == NOW + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# #4: concurrent writers serialise and merge per agent
# ---------------------------------------------------------------------------

def test_update_state_serializes_two_writers(tmp_path):
    path = tmp_path / "og-quota.json"
    q.save_state({"version": 1, "agents": {}, "marks": {}}, path)
    entered, release = threading.Event(), threading.Event()

    def first(state):
        entered.set()
        assert release.wait(5)
        state.setdefault("marks", {})["a"] = {
            "state": "dry", "until": None, "reason": "a", "at": q._iso(NOW)}

    def second(state):
        state.setdefault("marks", {})["b"] = {
            "state": "dry", "until": None, "reason": "b", "at": q._iso(NOW)}

    t1 = threading.Thread(target=lambda: q.update_state(path, first))
    t1.start()
    assert entered.wait(5), "first writer never entered its mutation"
    t2 = threading.Thread(target=lambda: q.update_state(path, second))
    t2.start()
    time.sleep(0.2)
    assert t2.is_alive(), "second writer did not wait for the lock"
    release.set()
    t1.join(5)
    t2.join(5)
    assert set(q.load_state(path)["marks"]) == {"a", "b"}


def test_probe_merge_keeps_a_mark_written_while_probing(tmp_path):
    """A mark written by `og stats --mark` during the probe must survive: the
    final step reloads under the lock and merges per agent."""
    bin_dir = tmp_path / "bin"
    started = tmp_path / "started"
    _fake_cli(bin_dir, "opencode",
              f'touch "{started}"\nsleep 1\necho ok\n')
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    state = tmp_path / "og-quota.json"
    _install(inst)
    _registry(reg, free_models=["opencode/mimo-v2.6-flash-free"])
    q.save_state({"version": 1, "agents": {}, "marks": {}}, state)

    result = {}

    def run_probe():
        result["outcomes"] = sp.probe_start(
            inst, reg, state, timeout=5, ctx=_ctx(bin_dir, system=True))

    t = threading.Thread(target=run_probe)
    t.start()
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started.exists(), "probe CLI did not start"
    q.update_state(state, lambda s: s.setdefault("marks", {}).__setitem__(
        "claude", {"state": "dry", "until": None, "reason": "concurrent",
                   "at": q._iso(NOW)}))
    t.join(10)

    assert result["outcomes"][0]["state"] == "ok"
    marks = q.load_state(state)["marks"]
    assert "claude" in marks and marks["claude"]["reason"] == "concurrent"
    assert "opencode" not in marks  # the probe's ok still cleared its own


# ---------------------------------------------------------------------------
# og stats --mark: the default is now +1h
# ---------------------------------------------------------------------------

def test_mark_without_until_defaults_to_one_hour(tmp_path, monkeypatch,
                                                  capsys):
    state = tmp_path / "og-quota.json"
    monkeypatch.setattr(st, "OMNI", tmp_path)
    monkeypatch.setattr(q, "OMNI_STATE", state)

    st.main(["--mark", "opencode"])

    mark = q.load_state(state)["marks"]["opencode"]
    until = q._parse_iso(mark["until"])
    live_now = datetime.now().astimezone()
    assert timedelta(minutes=59) < until - live_now <= \
        timedelta(hours=1, minutes=1)
    assert "no expiry" not in capsys.readouterr().out
