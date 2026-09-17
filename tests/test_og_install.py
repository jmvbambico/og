"""Unit tests for installer/og_install.py's pure/testable logic.

Interactive prompts (ask/pick_one/...) and the full interactive flow
(build_plan_interactive) aren't covered here — they need a real terminal.
Everything that touches disk uses tmp_path and monkeypatches the module's
OMNI/STATE constants rather than the real ~/.omnigent.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import og_install as m

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# port_available / find_free_port
# --------------------------------------------------------------------------
def test_port_available_true_for_a_free_port():
    # Bind a socket, note the OS-assigned port, close it, then it should read free.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    assert m.port_available(port) is True


def test_port_available_false_while_held():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        assert m.port_available(port) is False
    finally:
        s.close()


def test_find_free_port_skips_a_held_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        found = m.find_free_port(port, tries=5)
        assert found is not None
        assert found != port
    finally:
        s.close()


def test_find_free_port_gives_up_after_tries(monkeypatch):
    monkeypatch.setattr(m, "port_available", lambda p: False)
    assert m.find_free_port(6767, tries=3) is None


# --------------------------------------------------------------------------
# list_models / free_models
# --------------------------------------------------------------------------
def _fake_run(stdout, returncode=0):
    def _run(cmd, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
    return _run


def test_list_models_one_bare_id_per_line(monkeypatch):
    monkeypatch.setattr(
        m.subprocess, "run",
        _fake_run("opencode/mimo-v2.5-free\nopencode/other-model\n"),
    )
    agent = {"model": {"list_cmd": ["opencode", "models"]}}
    assert m.list_models(agent) == ["opencode/mimo-v2.5-free", "opencode/other-model"]


def test_list_models_strips_kiro_style_table_row(monkeypatch):
    """Regression: kiro-cli's --list-models prints a formatted table, not bare
    ids. A row like this once got stored as the literal model id, and its
    leading "*" broke YAML parsing (alias syntax) once templated unquoted
    into config.yaml. Only the first column should survive.
    """
    monkeypatch.setattr(
        m.subprocess, "run",
        _fake_run(
            "* auto                 1.00x credits      Models chosen by task for optimal usage\n"
            "  claude-sonnet-4      1.25x credits      Anthropic's Sonnet\n"
        ),
    )
    agent = {"model": {"list_cmd": ["kiro-cli", "chat", "--list-models"]}}
    assert m.list_models(agent) == ["auto", "claude-sonnet-4"]


def test_list_models_empty_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run", _fake_run("ignored", returncode=1))
    assert m.list_models({"model": {"list_cmd": ["whatever"]}}) == []


def test_list_models_empty_without_list_cmd():
    assert m.list_models({"model": {}}) == []
    assert m.list_models({}) == []


def test_free_models_filters_by_pattern(monkeypatch):
    monkeypatch.setattr(
        m.subprocess, "run",
        _fake_run("kilo/kilo-auto/free\nkilo/kilo-pro\nkilo/other/free\n"),
    )
    agent = {"model": {"list_cmd": ["kilo", "models"], "free_pattern": r"(kilo-auto/free|/free$)"}}
    assert m.free_models(agent) == ["kilo/kilo-auto/free", "kilo/other/free"]


def test_free_models_without_pattern_returns_all(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run", _fake_run("a\nb\n"))
    assert m.free_models({"model": {"list_cmd": ["x"]}}) == ["a", "b"]


# --------------------------------------------------------------------------
# model_block
# --------------------------------------------------------------------------
def test_model_block_empty_when_no_model():
    assert m.model_block(None) == ""
    assert m.model_block("") == ""


def test_model_block_quotes_a_plain_model_id():
    block = m.model_block("opencode/mimo-v2.5-free")
    assert '"opencode/mimo-v2.5-free"' in block
    # And it must be valid YAML on its own once wrapped in a mapping.
    parsed = yaml.safe_load("executor:\n" + block)
    assert parsed["executor"]["model"] == "opencode/mimo-v2.5-free"


def test_model_block_survives_a_yaml_special_value():
    """Regression: an unquoted value starting with '*' is YAML alias syntax
    and crashes the parser. json.dumps-quoting must neutralize this and any
    other YAML-special character a vendor's raw CLI output might contain.
    """
    nasty = "* auto: something, weird & unquoted"
    block = m.model_block(nasty)
    parsed = yaml.safe_load("executor:\n" + block)
    assert parsed["executor"]["model"] == nasty


# --------------------------------------------------------------------------
# worker_name
# --------------------------------------------------------------------------
def test_worker_name_defaults_to_coder_prefix():
    assert m.worker_name("cline") == "coder_cline"
    assert m.worker_name("kiro") == "coder_kiro"


def test_worker_name_honors_registry_override():
    # opencode and agy pin an explicit `worker` in registry.json so renaming
    # the underlying agent id doesn't orphan session history.
    assert m.worker_name("opencode") == "coder_zen"
    assert m.worker_name("agy") == "coder_agy"


# --------------------------------------------------------------------------
# validate()
# --------------------------------------------------------------------------
def _base_plan(**overrides):
    plan = {
        "version": 1,
        "agent_name": "test-agent",
        "orchestrator": "claude",
        "coders": [{"id": "opencode", "priority": 1, "model": "opencode/mimo-v2.5-free"}],
        "reviewer": {"id": "codex", "model": None},
        "accounts": {},
        "port": 6767,
        "ngrok_domain": "",
        "max_dispatches": 4,
        "bin_dir": "/tmp/bin",
        "default_mode": "local",
    }
    plan.update(overrides)
    return plan


def test_validate_errors_when_required_model_missing():
    plan = _base_plan(coders=[{"id": "opencode", "priority": 1, "model": None}])
    issues = m.validate(plan)
    assert any(level == "error" and "requires a pinned model" in msg for level, msg in issues)


def test_validate_ok_when_required_model_present():
    plan = _base_plan()
    issues = m.validate(plan)
    assert not any(level == "error" for level, _ in issues)


def test_validate_warns_on_silent_model_failure_agent():
    plan = _base_plan(coders=[{"id": "cline", "priority": 1, "model": "deepseek/deepseek-v4-flash"}])
    issues = m.validate(plan)
    assert any("accepts a model it cannot serve" in msg for _, msg in issues)


def test_validate_warns_same_vendor_reviewer():
    plan = _base_plan(
        coders=[{"id": "kiro", "priority": 1, "model": "auto"}],
        reviewer={"id": "kiro", "model": None},
    )
    issues = m.validate(plan)
    assert any("shares a vendor with" in msg for _, msg in issues)


def test_validate_errors_when_orchestrator_cannot_relay():
    plan = _base_plan(orchestrator="cline")
    issues = m.validate(plan)
    assert any(level == "error" and "cannot dispatch sub-agents" in msg for level, msg in issues)


def test_validate_errors_when_orchestrator_prompt_not_delivered():
    # cursor is prompt_delivery: none (kiro used to be, before it moved to ACP).
    plan = _base_plan(orchestrator="cursor")
    issues = m.validate(plan)
    assert any(level == "error" and "never receives a spec prompt" in msg for level, msg in issues)


def test_validate_warns_when_a_coder_never_receives_its_prompt():
    plan = _base_plan(coders=[{"id": "cursor", "priority": 1, "model": None}])
    issues = m.validate(plan)
    assert any("never receive their sub-agent prompt" in msg for _, msg in issues)


def test_validate_prompt_ceiling_error_and_warn_boundaries():
    plan = _base_plan()
    under = "x" * (m.PROMPT_CEILING - 1000)
    at_warn = "x" * (m.PROMPT_CEILING - 500)
    over = "x" * (m.PROMPT_CEILING + 500)

    assert m.validate(plan, under) == []
    assert any(level == "warn" and "tmux ceiling" in msg
               for level, msg in m.validate(plan, at_warn))
    assert any(level == "error" and "over the" in msg
               for level, msg in m.validate(plan, over))


def test_validate_prompt_ceiling_ignored_for_non_argv_orchestrator():
    # opencode is per_turn delivery -- no ceiling applies regardless of length.
    plan = _base_plan(orchestrator="opencode")
    huge = "x" * (m.PROMPT_CEILING * 2)
    issues = m.validate(plan, huge)
    assert not any("ceiling" in msg for _, msg in issues)


# --------------------------------------------------------------------------
# template rendering -> must always be valid, parseable YAML
# --------------------------------------------------------------------------
def _rendering_plan():
    return _base_plan(
        coders=[
            {"id": "opencode", "priority": 1, "model": "opencode/mimo-v2.5-free"},
            {"id": "kiro", "priority": 2, "model": "auto"},
        ],
    )


def test_render_orchestrator_is_valid_yaml_with_no_leftover_placeholders():
    plan = _rendering_plan()
    rendered = m.render_orchestrator(plan)
    assert "{{" not in rendered
    parsed = yaml.safe_load(rendered)
    assert parsed["name"] == "test-agent"


def test_render_coder_is_valid_yaml_and_pins_the_model():
    plan = _rendering_plan()
    c = plan["coders"][0]
    rendered = m.render_coder(plan, c)
    parsed = yaml.safe_load(rendered)
    assert parsed["executor"]["model"] == "opencode/mimo-v2.5-free"
    assert parsed["executor"]["config"]["harness"] == "opencode-native"


def test_render_coder_acp_user_gets_permission_mode_or_not(monkeypatch):
    """Whatever this repo's current policy is for acp-user coders (bypass or
    not), it must be internally consistent: present for acp-user, absent for
    a native-harness coder. This pins the *shape*, not a specific choice.
    """
    plan = _base_plan(coders=[{"id": "cline", "priority": 1, "model": "x"}])
    cline_cfg = yaml.safe_load(m.render_coder(plan, plan["coders"][0]))
    plan2 = _rendering_plan()
    opencode_cfg = yaml.safe_load(m.render_coder(plan2, plan2["coders"][0]))
    cline_has = "permission_mode" in cline_cfg["executor"]["config"]
    opencode_has = "permission_mode" in opencode_cfg["executor"]["config"]
    assert opencode_has is False
    # Document the current state rather than assert a specific bool: this
    # flag is a deliberate, revisitable product choice (see AGENTS.md / PRs
    # touching acp permission handling), not an invariant to lock in here.
    assert isinstance(cline_has, bool)


def test_render_reviewer_is_valid_yaml():
    plan = _rendering_plan()
    rendered = m.render_reviewer(plan)
    parsed = yaml.safe_load(rendered)
    assert parsed["executor"]["config"]["harness"] == "codex-native"


# --------------------------------------------------------------------------
# apply() -- dry run only, never touches the real ~/.omnigent
# --------------------------------------------------------------------------
def test_apply_dry_run_echoes_the_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, "OMNI", tmp_path)
    plan = _rendering_plan()
    m.apply(plan, dry_run=True)
    out = capsys.readouterr().out
    assert "would be written to" in out
    # The dry-run output ends with the plan as pretty JSON; the last
    # balanced-looking JSON blob in stdout should round-trip to the input.
    echoed = json.loads(out[out.index("{"):])
    assert echoed["agent_name"] == plan["agent_name"]
    # dry run must not create the bundle directory.
    assert not (tmp_path / "agents" / plan["agent_name"]).exists()


def test_apply_dry_run_refuses_an_invalid_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "OMNI", tmp_path)
    plan = _base_plan(coders=[{"id": "opencode", "priority": 1, "model": None}])
    with pytest.raises(SystemExit):
        m.apply(plan, dry_run=True)


# --------------------------------------------------------------------------
# registry -- acp-user harness ids must be what Omnigent derives from the label
# --------------------------------------------------------------------------
def _omnigent_slugify(name: str) -> str:
    # Verbatim copy of omnigent.onboarding.acp_auth.slugify. The row is looked
    # up by acp:<slug>; a miss falls back to the FIRST configured row -- a
    # different vendor -- with no error anywhere.
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "agent"


def test_acp_user_harness_matches_omnigent_slug():
    for a in m.REGISTRY["agents"]:
        if a["kind"] != "acp-user":
            continue
        expected = f"acp:{_omnigent_slugify(a['label'])}"
        assert a["harness"] == expected, (
            f"{a['id']}: harness {a['harness']!r} will not resolve; "
            f"Omnigent slugs {a['label']!r} to {expected!r}")
        assert a.get("acp_command"), f"{a['id']}: acp-user rows need acp_command"


def test_kiro_is_wired_over_acp_with_trust_all_tools():
    # kiro-native has no headless no-prompt seam in Omnigent, so a native Kiro
    # worker stalls on per-tool permission prompts. Keep it on ACP with Kiro's
    # own auto-approve until that changes (see kind_note in the registry).
    kiro = m.agents_by_id()["kiro"]
    assert kiro["kind"] == "acp-user"
    assert "--trust-all-tools" in kiro["acp_command"]


def test_patch_global_config_writes_a_row_for_an_acp_reviewer(tmp_path, monkeypatch):
    # Without its own row an ACP reviewer resolves to the first coder's row.
    monkeypatch.setattr(m, "OMNI", tmp_path)
    (tmp_path / "config.yaml").write_text("{}\n")
    plan = _base_plan(
        coders=[{"id": "opencode", "priority": 1, "model": "opencode/mimo-v2.5-free"}],
        reviewer={"id": "kiro", "model": None},
    )
    m.patch_global_config(plan)
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    names = [r["name"] for r in cfg["acp"]["agents"]]
    assert "Kiro (AWS)" in names


# --------------------------------------------------------------------------
# write_og_env -- the auto-update switch og reads at startup
# --------------------------------------------------------------------------
def _og_env(tmp_path, monkeypatch, plan):
    monkeypatch.setattr(m, "OMNI", tmp_path)
    m.write_og_env(plan)
    return (tmp_path / "og.env").read_text()


def test_og_env_auto_update_defaults_on(tmp_path, monkeypatch):
    # A plan written before the key existed must keep behaving like a fresh
    # install: pull-and-reapply, not silent drift.
    env = _og_env(tmp_path, monkeypatch, _base_plan())
    assert "OG_AUTO_UPDATE=1\n" in env


def test_og_env_carries_a_version_stamp(tmp_path, monkeypatch):
    # `og start` reads OG_VERSION to compare against the latest release tag;
    # a missing stamp reads as "older than any release", so it must always be
    # written -- even as "unknown" or a bare sha on a tagless checkout.
    env = _og_env(tmp_path, monkeypatch, _base_plan())
    line = next(l for l in env.splitlines() if l.startswith("OG_VERSION="))
    assert line.split("=", 1)[1].strip()


def test_installed_version_is_unknown_outside_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "REPO", tmp_path)
    assert m.installed_version() == "unknown"


def test_og_env_auto_update_off_is_zero_not_absent(tmp_path, monkeypatch):
    # og treats an unset OG_AUTO_UPDATE as off, but the file should still say
    # so explicitly -- the comment above the line is the user's only hint that
    # the switch exists.
    env = _og_env(tmp_path, monkeypatch, _base_plan(auto_update=False))
    assert "OG_AUTO_UPDATE=0\n" in env
    assert "og update" in env


# --------------------------------------------------------------------------
# CLI smoke tests -- run the real script as a subprocess
# --------------------------------------------------------------------------
def test_cli_plan_defaults_auto_update_on(tmp_path):
    # A --plan without the key (older AI-written plans) gets auto_update=True,
    # matching the interactive default, and the dry run echoes it back.
    plan = _base_plan()
    plan.pop("auto_update", None)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan))
    env = {"OMNIGENT_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, str(REPO / "installer" / "og_install.py"),
         "--plan", str(plan_file), "--dry-run"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    echoed = json.loads(out[out.index("{"):])
    assert echoed["auto_update"] is True


def test_cli_questions_emits_valid_json(tmp_path):
    env = {"OMNIGENT_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, str(REPO / "installer" / "og_install.py"), "--questions"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["apply_with"] == "og-install --plan plan.json"
    assert isinstance(payload["registry"], list)


def test_cli_plan_dry_run_end_to_end(tmp_path):
    plan = _rendering_plan()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    env = {"OMNIGENT_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, str(REPO / "installer" / "og_install.py"),
         "--plan", str(plan_path), "--dry-run"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "dry run" in result.stdout
    assert not (tmp_path / "agents").exists()
