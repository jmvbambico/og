"""Tests for installer/og_start_probe.py and the +1h mark default.

No network and no live ~/.omnigent: every probe runs a fake CLI written into
tmp_path and placed on the PATH the probe is given. The state file is always
an explicit tmp_path argument, so nothing touches the real install.
"""
from __future__ import annotations

import json
import sys
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


def _fake_cli(bin_dir: Path, name: str, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


def _install(path: Path, model: str = "opencode/mimo-v2.6-flash-free"):
    path.write_text(json.dumps({
        "coders": [{"id": "opencode", "priority": 1, "model": model}],
    }))


def _registry(path: Path, patterns: list[str] | None = None, *,
              probe: str | None = None, argv: list[str] | None = None,
              model_prefer: str = "opencode/mimo-v2.6-flash-free"):
    path.write_text(json.dumps({"agents": [{
        "id": "opencode",
        "label": "OpenCode",
        "roles": ["coder"],
        "model": {"required": True, "prefer": model_prefer},
        "quota": {
            "probe": probe,
            "note": "no queryable balance",
            "liveness": {
                "argv": argv or ["opencode", "run", "-m", "{model}",
                                 "reply ok"],
                "failure_patterns": patterns or ["Rate limit exceeded"],
            },
        },
    }]}))


def _mark(state_path: Path, agent: str = "opencode", until=None):
    q.save_state({"version": 1, "agents": {},
                  "marks": {agent: {"state": "dry", "until": until,
                                    "reason": "stale", "at": q._iso(NOW)}}},
                 state_path)


def _ctx(bin_dir: Path, now: datetime = NOW) -> q.Ctx:
    # Only the fake bin dir on PATH: a missing binary must be genuinely
    # missing, not found later in the machine's real PATH.
    return q.Ctx(env={"PATH": str(bin_dir)}, now=lambda: now)


def _run(tmp_path, bin_dir, *, timeout=5.0, ctx=None, patterns=None,
         probe=None, argv=None):
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    state = tmp_path / "og-quota.json"
    _install(inst)
    _registry(reg, patterns, probe=probe, argv=argv)
    return sp.probe_start(inst, reg, state, timeout=timeout,
                          ctx=ctx or _ctx(bin_dir)), state


# ---------------------------------------------------------------------------
# liveness: the three outcomes that matter
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
        "run", "-m", "opencode/mimo-v2.6-flash-free", "reply ok"]


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

    outcomes, state = _run(tmp_path, bin_dir, timeout=0.3)

    assert outcomes[0]["state"] == "error"
    assert "timed out" in outcomes[0]["detail"]
    assert q.load_state(state)["marks"] == {}


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


def test_a_model_template_without_a_pin_is_unavailable(tmp_path):
    """Nothing user-specific is invented: an unpinned install cannot run a
    model-pinned liveness probe."""
    bin_dir = tmp_path / "bin"
    _fake_cli(bin_dir, "opencode", "echo ok\n")
    inst = tmp_path / "og-install.json"
    reg = tmp_path / "registry.json"
    state = tmp_path / "og-quota.json"
    inst.write_text(json.dumps({"coders": [{"id": "opencode",
                                            "priority": 1}]}))
    # the registry's prefer is the installer's default pin, so to test the
    # empty case remove it from the row too
    _registry(reg)
    data = json.loads(reg.read_text())
    data["agents"][0]["model"].pop("prefer")
    reg.write_text(json.dumps(data))

    outcomes = sp.probe_start(inst, reg, state, ctx=_ctx(bin_dir))
    assert outcomes[0]["state"] == "unavailable"
    assert "no pinned model" in outcomes[0]["detail"]


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
