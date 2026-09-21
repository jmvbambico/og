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
# list_models / search_models
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


def test_search_models_is_case_insensitive_substring():
    models = ["opencode/mimo-v2.5-free", "anthropic/claude-sonnet-5", "deepseek/deepseek-chat"]
    assert m.search_models(models, "CLAUDE") == ["anthropic/claude-sonnet-5"]
    assert m.search_models(models, "deep") == ["deepseek/deepseek-chat"]
    assert m.search_models(models, "nope") == []


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


def test_acp_command_bakes_the_pin_into_env_where_the_cli_reads_it():
    # Omnigent never delivers an ACP model pin (traced: initialize, session/new,
    # session/prompt only). Cline reads CLINE_MODEL at newSession; without it
    # every worker ran on claude-sonnet-5 usage-billing and returned an empty
    # turn. Kilo has no such variable and keeps its plain command.
    reg = m.agents_by_id()
    assert m.acp_command(reg["cline"], "deepseek/deepseek-v4-flash") == \
        "env CLINE_MODEL=deepseek/deepseek-v4-flash cline --acp --auto-approve true"
    assert m.acp_command(reg["cline"], None) == "cline --acp --auto-approve true"
    assert m.acp_command(reg["kilo"], "kilo/kilo-auto/free") == "kilo acp"


def test_patch_global_config_renders_the_env_prefixed_cline_command(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "OMNI", tmp_path)
    (tmp_path / "config.yaml").write_text("{}\n")
    plan = _base_plan(coders=[{"id": "cline", "priority": 1, "model": "deepseek/deepseek-v4-flash"}])
    m.patch_global_config(plan)
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    row = next(r for r in cfg["acp"]["agents"] if r["name"] == "Cline")
    assert row["command"].startswith("env CLINE_MODEL=deepseek/deepseek-v4-flash cline --acp")


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
# OpenCode worker config -- the `question` tool must not be able to park a run
# --------------------------------------------------------------------------
def test_opencode_worker_config_written_for_an_opencode_coder(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "OMNI", tmp_path)
    d = m.write_opencode_worker_config(_base_plan())
    assert d == tmp_path / "opencode"
    cfg = json.loads((d / "opencode.json").read_text())
    # `*: ask` FIRST, `question: deny` AFTER: OpenCode is last-match-wins, so
    # this order is what actually removes the tool while every other tool
    # stays on Omnigent's policy-engine route. Pin the order, not just the keys.
    assert list(cfg["permission"].items()) == [("*", "ask"), ("question", "deny")]
    assert "tools" not in cfg  # the `tools: {question: false}` form does not work


def test_opencode_worker_config_wires_code_intel_mcp_only_when_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "OMNI", tmp_path)
    monkeypatch.setattr(m.shutil, "which", lambda name: None)
    cfg = json.loads((m.write_opencode_worker_config(_base_plan()) / "opencode.json").read_text())
    assert "mcp" not in cfg
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/local/bin/" + name)
    cfg = json.loads((m.write_opencode_worker_config(_base_plan()) / "opencode.json").read_text())
    assert cfg["mcp"]["codegraph"]["command"] == ["codegraph", "serve", "--mcp"]
    assert all(s["type"] == "local" for s in cfg["mcp"].values())  # stdio, never docker


def test_opencode_worker_config_skipped_when_opencode_orchestrates(tmp_path, monkeypatch):
    # The dir applies to EVERY OpenCode session og launches; an OpenCode
    # orchestrator needs `question` for its plan gate.
    monkeypatch.setattr(m, "OMNI", tmp_path)
    plan = _base_plan(orchestrator="opencode")
    assert m.write_opencode_worker_config(plan) is None
    assert not (tmp_path / "opencode" / "opencode.json").exists()


def test_opencode_worker_config_removed_when_opencode_leaves_the_roster(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "OMNI", tmp_path)
    m.write_opencode_worker_config(_base_plan())
    plan = _base_plan(coders=[{"id": "cline", "priority": 1, "model": "x"}])
    assert m.write_opencode_worker_config(plan) is None
    assert not (tmp_path / "opencode" / "opencode.json").exists()


def test_og_env_points_at_the_opencode_worker_config(tmp_path, monkeypatch):
    env = _og_env(tmp_path, monkeypatch, _base_plan())
    assert f"OG_OPENCODE_CONFIG_DIR={tmp_path / 'opencode'}\n" in env
    env = _og_env(tmp_path, monkeypatch, _base_plan(orchestrator="opencode"))
    assert "OG_OPENCODE_CONFIG_DIR" not in env


def test_coder_prompt_carries_the_unattended_and_orientation_rules():
    # Both rules exist because of one session: a free-tier worker parked the
    # run twice on `question`, and re-read the same test file 50 times.
    plan = _rendering_plan()
    prompt = yaml.safe_load(m.render_coder(plan, plan["coders"][0]))["prompt"]
    assert "Never stop to ask" in prompt
    assert "code-intelligence tool" in prompt
    # Tool-agnostic (other users run GitNexus etc.) and never assumes one exists.
    assert "codegraph" not in prompt.lower()
    assert "do not install anything" in prompt
    # Workers never index a worktree themselves (the orchestrator seeds it).
    assert "do not build one" in prompt


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


# --------------------------------------------------------------------------
# platform detection + hints
# --------------------------------------------------------------------------
def test_host_os_reports_wsl_from_env(monkeypatch):
    monkeypatch.setattr(m.sys, "platform", "linux")
    monkeypatch.setattr(m.os, "name", "posix")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert m.host_os() == "wsl"


def test_host_os_reports_plain_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(m.sys, "platform", "linux")
    monkeypatch.setattr(m.os, "name", "posix")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    # /proc/version is read only for the "microsoft" marker; on a non-Linux
    # host it does not exist and the OSError branch must still say linux.
    assert m.host_os() in ("linux", "wsl")


def test_host_os_reports_windows(monkeypatch):
    monkeypatch.setattr(m.sys, "platform", "win32")
    assert m.host_os() == "windows"


def test_prereq_hints_are_brew_on_macos(monkeypatch):
    monkeypatch.setattr(m, "host_os", lambda: "macos")
    hints = {n: how for n, _, _, how, _ in m.prereqs()}
    assert hints["tmux"] == "brew install tmux"
    assert "brew" not in hints["omnigent"]


def test_only_runtime_tools_are_required_prereqs():
    """gh and ngrok are per-workflow (GitHub PRs, tunnelled access), not
    something og or Omnigent need to start -- --check must not exit 1 on them."""
    required = {n for n, _, _, _, req in m.prereqs() if req}
    assert required == {"omnigent", "python3", "tmux", "git"}


def test_check_exits_zero_without_gh_and_ngrok(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    for name in ("omnigent", "tmux", "git", "claude"):
        (fake / name).write_text("#!/bin/sh\nexit 0\n")
        (fake / name).chmod(0o755)
    # python3 must resolve too: point at the interpreter running the tests.
    (fake / "python3").symlink_to(sys.executable)
    env = {"OMNIGENT_HOME": str(tmp_path), "PATH": str(fake), "HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, str(REPO / "installer" / "og_install.py"), "--check"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gh" in result.stdout and "optional" in result.stdout


def test_prereq_hints_are_apt_on_linux_and_wsl(monkeypatch):
    for host in ("linux", "wsl"):
        monkeypatch.setattr(m, "host_os", lambda h=host: h)
        hints = {n: how for n, _, _, how, _ in m.prereqs()}
        assert "brew" not in " ".join(hints.values()), host
        assert hints["tmux"] == "sudo apt install tmux"
        assert "ngrok.com" in hints["ngrok"]


# --------------------------------------------------------------------------
# bin/og credential store (the `file` backend is the cross-platform floor)
# --------------------------------------------------------------------------
OG = REPO / "bin" / "og"


def _og(args, home, stdin="", **env):
    """Run bin/og against a throwaway HOME. `omnigent` is deliberately absent
    from PATH so server_running() reports "not running" and login stops short
    of minting a session."""
    full_env = {"HOME": str(home), "PATH": "/usr/bin:/bin", **env}
    return subprocess.run(
        ["bash", str(OG), *args], input=stdin, capture_output=True, text=True,
        timeout=30, env=full_env,
    )


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash script")
def test_og_login_file_backend_writes_0600_file(tmp_path):
    r = _og(["login"], tmp_path, stdin="kunerrrs\nhunter2\nhunter2\n",
            OG_CRED_BACKEND="file")
    assert r.returncode == 0, r.stderr
    cred = tmp_path / ".omnigent" / "og-credentials"
    assert cred.read_text() == "kunerrrs\nhunter2\n"
    assert cred.stat().st_mode & 0o777 == 0o600
    assert "security:" not in r.stderr  # the macOS-only tool must never be called


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash script")
def test_og_login_file_backend_is_chosen_on_wsl_without_override(tmp_path):
    # A fake uname says Linux and the WSL marker is set: the store must land
    # in the file even if a secret-tool happens to be on PATH, since WSL has
    # no Secret Service daemon for it to talk to.
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "uname").write_text("#!/bin/sh\necho Linux\n")
    (fake / "secret-tool").write_text("#!/bin/sh\necho called >&2; exit 1\n")
    for f in fake.iterdir():
        f.chmod(0o755)
    r = _og(["login"], tmp_path, stdin="u\np\np\n",
            PATH=f"{fake}:/usr/bin:/bin", WSL_DISTRO_NAME="Ubuntu")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".omnigent" / "og-credentials").read_text() == "u\np\n"
    assert "called" not in r.stderr


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash script")
def test_og_login_falls_back_to_file_when_keyring_store_fails(tmp_path):
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "uname").write_text("#!/bin/sh\necho Linux\n")
    (fake / "secret-tool").write_text("#!/bin/sh\nexit 1\n")
    for f in fake.iterdir():
        f.chmod(0o755)
    r = _og(["login"], tmp_path, stdin="u\np\np\n", PATH=f"{fake}:/usr/bin:/bin")
    assert r.returncode == 0, r.stderr
    assert "falling back to a 0600 file" in r.stdout
    assert (tmp_path / ".omnigent" / "og-credentials").read_text() == "u\np\n"
    # status must report where the credentials actually are, not the backend
    # it would have preferred.
    s = _og(["status"], tmp_path, PATH=f"{fake}:/usr/bin:/bin")
    assert "creds:    'u' — " in s.stdout and "og-credentials" in s.stdout


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash script")
def test_og_rejects_unknown_cred_backend(tmp_path):
    r = _og(["status"], tmp_path, OG_CRED_BACKEND="vault")
    assert r.returncode == 1
    assert "OG_CRED_BACKEND must be" in r.stderr


@pytest.mark.skipif(sys.platform.startswith("win"), reason="bash script")
def test_og_login_mints_a_session_from_file_credentials(tmp_path):
    """The read side: with the server 'up' (a stub /auth/login behind og's own
    pidfile), `og login` must read the file-backed credentials back and write
    auth_tokens.json in omnigent's record shape."""
    import http.server
    import threading

    class Stub(http.server.BaseHTTPRequestHandler):
        seen = {}

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            Stub.seen = json.loads(self.rfile.read(n))
            body = json.dumps({"token": "tok-123", "expires_in": 60}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        omni = tmp_path / ".omnigent"
        omni.mkdir()
        # og's own pidfile is consulted before `omnigent server status`; this
        # process is alive, so server_running() says yes without omnigent.
        (omni / "og-server.pid").write_text(str(__import__("os").getpid()))
        r = _og(["login"], tmp_path, stdin="kunerrrs\nhunter2\nhunter2\n",
                OG_CRED_BACKEND="file", OG_PORT=str(port))
        assert r.returncode == 0, r.stderr + r.stdout
        assert "session minted" in r.stdout
        assert Stub.seen == {"username": "kunerrrs", "password": "hunter2"}
        store = json.loads((omni / "auth_tokens.json").read_text())
        assert store[f"http://127.0.0.1:{port}"]["token"] == "tok-123"
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------
# multi-provider model listing + model-derived vendor
# --------------------------------------------------------------------------
OPENCODE_AGENT = {"id": "opencode", "label": "OpenCode", "vendor": "opencode-zen",
                  "model": {"list_cmd": ["opencode", "models"],
                            "prefer": "opencode/mimo-v2.5-free", "required": True}}


def test_grouped_models_features_auto_and_free_but_hides_nothing():
    """A DeepSeek key added with `opencode auth login` shows up as its own
    provider group. Router (`auto`) and free-tier ids float to the top of
    their group and such groups come first -- placement only; every paid id
    is still listed, in the tool's own order, for a subscriber to pin."""
    models = ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner", "opencode/glm-5",
              "opencode/mimo-v2.5-free", "opencode/gpt-5.4", "kilo/kilo-auto/free",
              "kilo/anthropic/claude-sonnet-5"]
    groups = m.grouped_models(OPENCODE_AGENT, models)
    assert [g[0] for g in groups] == ["opencode", "kilo", "deepseek"]
    assert groups[0][1] == ["opencode/mimo-v2.5-free", "opencode/glm-5", "opencode/gpt-5.4"]
    assert groups[1][1] == ["kilo/kilo-auto/free", "kilo/anthropic/claude-sonnet-5"]
    assert groups[2][1] == ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"]
    assert sum(len(g[1]) for g in groups) == len(models)


def test_zen_preflight_only_for_a_free_zen_pin():
    # The preflight tells the orchestrator to substitute a `-free` id when the
    # pin rotates out. A paid Zen pin (a subscriber) or a user-added provider
    # never rotates, so the section must be absent -- it would only invite an
    # `args.model` override.
    free = _base_plan(coders=[{"id": "opencode", "priority": 1, "model": "opencode/mimo-v2.5-free"}])
    paid = _base_plan(coders=[{"id": "opencode", "priority": 1, "model": "opencode/claude-sonnet-5"}])
    assert "Zen model preflight" in m.render_orchestrator(free)
    assert "Zen model preflight" not in m.render_orchestrator(paid)
    assert "day-capped" in m.render_roster(free) and "day-capped" not in m.render_roster(paid)


def test_render_roster_skill_names_quota_and_marks_dry_workers(monkeypatch):
    """The roster skill's Capacity section says how to read `og stats`, how to
    map `coder_<id>` -> `<id>`, how to skip a dry worker, and how to re-admit
    one after reset_at. Each worker line names its quota probe, or says the
    limit is not measurable when the registry row has no quota block or a null
    probe."""
    fake = {
        "agents": [
            {"id": "codex", "label": "Codex (OpenAI)", "harness": "codex-native",
             "vendor": "openai", "roles": ["reviewer"], "relay": False,
             "silent_model_failure": False, "prompt_delivery": "argv",
             "model": {"required": False}},
            {"id": "kilo", "label": "Kilo Code", "harness": "acp:kilo-code",
             "vendor": "kilo", "roles": ["coder"], "relay": False,
             "silent_model_failure": True, "prompt_delivery": "unknown",
             "model": {"required": True, "note": "kilo note"},
             "quota": {"probe": "kilo-profile"}},
            {"id": "opencode", "label": "OpenCode (Zen)", "harness": "opencode-native",
             "vendor": "opencode-zen", "roles": ["coder"], "relay": True,
             "silent_model_failure": False, "prompt_delivery": "per_turn",
             "model": {"required": True},
             "quota": {"probe": None, "note": "server-side, not queryable"}},
            {"id": "gemini", "label": "Gemini CLI", "harness": "qwen-native",
             "vendor": "google", "roles": ["coder"], "relay": False,
             "silent_model_failure": False, "prompt_delivery": "none",
             "model": {"required": False}},
        ]
    }
    monkeypatch.setattr(m, "REGISTRY", fake)
    plan = _base_plan(coders=[
        {"id": "kilo", "priority": 1, "model": "kilo/kilo-auto/free"},
        {"id": "opencode", "priority": 2, "model": "opencode/mimo-v2.5-free"},
        {"id": "gemini", "priority": 3, "model": None},
    ])
    skill = m.render_roster_skill(plan)

    for needle in ("## Capacity", "og stats --json", "coder_<id>", "reset_at",
                   "og stats --mark", "CLEAN worktree", "og stats --agent"):
        assert needle in skill, needle
    assert "quota: `kilo-profile`" in skill
    assert "quota: not measurable" in skill
    assert "quota: not measurable (no quota block in the registry)" in skill
    # The cline concurrency note is rendered only when the row lacks it.
    assert "One session at a time" not in skill
    # A reviewer line too, with its own quota shape.
    assert "## `reviewer`" in skill


def test_render_roster_skill_renders_cline_concurrency_note(monkeypatch):
    """The 'one session at a time' caveat is rendered for Cline unless the
    registry row already carries it in its model note."""
    with_note = {
        "agents": [
            {"id": "codex", "label": "Codex (OpenAI)", "harness": "codex-native",
             "vendor": "openai", "roles": ["reviewer"], "relay": False,
             "silent_model_failure": False, "prompt_delivery": "argv",
             "model": {"required": False}},
            {"id": "cline", "label": "Cline", "harness": "acp:cline", "vendor": "deepseek",
             "roles": ["coder"], "relay": False, "silent_model_failure": True,
             "prompt_delivery": "unknown",
             "model": {"required": True,
                       "note": "one session at a time — concurrent Cline sessions on one "
                               "login get cut mid-turn; never dispatch two tasks to "
                               "`coder_cline` in the same turn."},
             "quota": {"probe": "deepseek-balance"}},
        ]
    }
    without_note = {
        "agents": [
            {"id": "codex", "label": "Codex (OpenAI)", "harness": "codex-native",
             "vendor": "openai", "roles": ["reviewer"], "relay": False,
             "silent_model_failure": False, "prompt_delivery": "argv",
             "model": {"required": False}},
            {"id": "cline", "label": "Cline", "harness": "acp:cline", "vendor": "deepseek",
             "roles": ["coder"], "relay": False, "silent_model_failure": True,
             "prompt_delivery": "unknown",
             "model": {"required": True, "note": "some other note"},
             "quota": {"probe": "deepseek-balance"}},
        ]
    }
    monkeypatch.setattr(m, "REGISTRY", with_note)
    plan = _base_plan(coders=[{"id": "cline", "priority": 1, "model": "x"}])
    rendered = m.render_roster_skill(plan)
    # The row already carries the caveat in its model note, so the renderer
    # renders the note as-is and does NOT add a second, bolded copy of it.
    assert rendered.count("**One session at a time.**") == 0
    assert "one session at a time — concurrent Cline sessions on one login" in rendered
    assert "never dispatch two tasks to `coder_cline` in the same turn" in rendered
    monkeypatch.setattr(m, "REGISTRY", without_note)
    rendered = m.render_roster_skill(plan)
    assert rendered.count("**One session at a time.**") == 1
    assert "never dispatch two tasks to `coder_cline` in the same turn" in rendered


def test_pick_model_offers_other_providers_by_number(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run", _fake_run(
        "opencode/mimo-v2.5-free\nopencode/glm-5\ndeepseek/deepseek-chat\n"))
    answers = iter(["99", "3"])  # out-of-range number is rejected, not stored
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: next(answers))
    assert m.pick_model(OPENCODE_AGENT, None) == "deepseek/deepseek-chat"


def test_pick_model_default_ignores_a_rotated_prefer(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run", _fake_run(
        "opencode/glm-5\nopencode/nemotron-free\n"))
    seen = {}

    def fake_ask(prompt, default=None):
        seen["default"] = default
        return default

    monkeypatch.setattr(m, "ask", fake_ask)
    # The registry's `prefer` (mimo) is not in the list, so the default is the
    # tool's first id -- no tier is promoted over another.
    assert m.pick_model(OPENCODE_AGENT, None) == "opencode/glm-5"
    assert seen["default"] == "opencode/glm-5"


def test_pick_model_search_narrows_then_picks(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run", _fake_run(
        "opencode/glm-5\nopencode/claude-sonnet-5\ndeepseek/deepseek-chat\n"))
    answers = iter(["claude", "1"])
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: next(answers))
    assert m.pick_model(OPENCODE_AGENT, None) == "opencode/claude-sonnet-5"


def test_pick_model_accepts_a_full_id_the_tool_listed(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run", _fake_run("a/x\nb/y\n"))
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: "b/y")
    assert m.pick_model(OPENCODE_AGENT, None) == "b/y"


@pytest.mark.parametrize("model_id,vendor", [
    ("deepseek/deepseek-chat", "deepseek"),
    ("anthropic/claude-opus-5", "anthropic"),
    ("opencode/claude-sonnet-5", "anthropic"),
    ("opencode/mimo-v2.5-free", "xiaomi"),
    ("opencode/big-pickle", None),
    ("kilo/kilo-auto/free", None),
    ("gpt-5.3-codex", None),
    (None, None),
])
def test_model_vendor(model_id, vendor):
    assert m.model_vendor(model_id) == vendor


def test_vendor_of_falls_back_to_registry():
    assert m.vendor_of(OPENCODE_AGENT, {"id": "opencode", "model": "opencode/big-pickle"}) == "opencode-zen"
    assert m.vendor_of(OPENCODE_AGENT, {"id": "opencode", "model": "deepseek/deepseek-chat"}) == "deepseek"


def test_validate_flags_same_vendor_through_a_reseller():
    """Zen's Claude reviewed by Claude Code is same-vendor review, however
    the bill reads."""
    plan = _rendering_plan()
    plan["coders"] = [{"id": "opencode", "priority": 1, "model": "opencode/claude-sonnet-5"}]
    plan["reviewer"] = {"id": "claude", "model": None}
    msgs = [msg for level, msg in m.validate(plan) if level == "warn"]
    assert any("shares a vendor" in msg for msg in msgs), msgs
    plan["coders"][0]["model"] = "deepseek/deepseek-chat"
    msgs = [msg for level, msg in m.validate(plan) if level == "warn"]
    assert not any("shares a vendor" in msg for msg in msgs), msgs


def test_zen_preflight_only_for_zen_pins():
    plan = _rendering_plan()
    plan["coders"] = [{"id": "opencode", "priority": 1, "model": "opencode/mimo-v2.5-free"}]
    assert "Zen model preflight" in m.render_orchestrator(plan)
    plan["coders"][0]["model"] = "deepseek/deepseek-chat"
    rendered = m.render_orchestrator(plan)
    assert "Zen model preflight" not in rendered
    assert "day-capped" not in rendered


# --------------------------------------------------------------------------
# logins that yield no models (OpenCode auth store cross-check)
# --------------------------------------------------------------------------
def _opencode_agent_with_auth(tmp_path, monkeypatch, entries: dict) -> dict:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    store = tmp_path / "opencode" / "auth.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({k: {"type": v, "key": "x"} for k, v in entries.items()}))
    return {"id": "opencode", "label": "OpenCode", "vendor": "opencode-zen",
            "model": {"list_cmd": ["opencode", "models"],
                      "auth_file": "$XDG_DATA_HOME/opencode/auth.json",
                      "oauth_builtin": ["openai"],
                      "oauth_plugins": {"anthropic": "opencode-anthropic-auth"}}}


def test_auth_providers_reads_the_store_via_xdg(tmp_path, monkeypatch):
    agent = _opencode_agent_with_auth(tmp_path, monkeypatch, {"deepseek": "api", "anthropic": "oauth"})
    assert m.auth_providers(agent) == {"deepseek": "api", "anthropic": "oauth"}


def test_auth_providers_empty_without_store_or_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert m.auth_providers({"model": {"auth_file": "$XDG_DATA_HOME/opencode/auth.json"}}) == {}
    assert m.auth_providers({"model": {}}) == {}


def test_unlisted_logins_explains_each_idle_credential(tmp_path, monkeypatch):
    """Reproduces the report: OAuth sessions in auth.json that `opencode
    models` never lists. Each idle login gets the reason that matches how
    OpenCode loads providers (API keys directly; OAuth only via a plugin)."""
    agent = _opencode_agent_with_auth(tmp_path, monkeypatch, {
        "opencode": "api",      # listed -> no note
        "deepseek": "api",      # API key but absent -> disabled/stale catalog
        "anthropic": "oauth",   # known plugin -> name it
        "openai": "oauth",      # built-in plugin -> session expired
        "google": "oauth",      # no plugin known -> say so
    })
    notes = {pid: (kind, why) for pid, kind, why in
             m.unlisted_logins(agent, ["opencode/glm-5", "opencode/mimo-free"])}
    assert "opencode" not in notes
    assert notes["deepseek"][0] == "api" and "disabled_providers" in notes["deepseek"][1]
    assert "opencode-anthropic-auth" in notes["anthropic"][1]
    assert "auth login" in notes["openai"][1]
    assert "none is built in" in notes["google"][1]


def test_unlisted_logins_silent_when_everything_is_listed(tmp_path, monkeypatch):
    agent = _opencode_agent_with_auth(tmp_path, monkeypatch, {"deepseek": "api"})
    assert m.unlisted_logins(agent, ["deepseek/deepseek-chat"]) == []


def test_pick_model_warns_about_idle_logins(tmp_path, monkeypatch):
    agent = _opencode_agent_with_auth(tmp_path, monkeypatch, {"anthropic": "oauth"})
    monkeypatch.setattr(m.subprocess, "run", _fake_run("opencode/mimo-free\n"))
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: default)
    warned = []
    monkeypatch.setattr(m, "warn", lambda msg: warned.append(msg))
    assert m.pick_model(agent, None) == "opencode/mimo-free"
    assert any("anthropic: logged in (oauth)" in w for w in warned), warned


def test_list_models_keeps_the_cli_error_for_the_warning(monkeypatch):
    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom\nplugin install failed\n")
    monkeypatch.setattr(m.subprocess, "run", failing)
    assert m.list_models({"model": {"list_cmd": ["opencode", "models"]}}) == []
    assert "exit 1" in m.LAST_LIST_ERROR and "plugin install failed" in m.LAST_LIST_ERROR


def test_list_models_reports_a_timeout(monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
    monkeypatch.setattr(m.subprocess, "run", slow)
    assert m.list_models({"model": {"list_cmd": ["opencode", "models"]}}) == []
    assert "timed out" in m.LAST_LIST_ERROR


# --------------------------------------------------------------------------
# install_pth targets omnigent's interpreter, not the installer's
# --------------------------------------------------------------------------
@pytest.fixture
def fake_omnigent_venv(tmp_path, monkeypatch):
    """A real venv standing in for omnigent's, with an `omnigent` entry point
    whose shebang names the venv python -- the shape uv tool / pipx / pip
    --user all produce. PATH holds only that entry point's directory plus
    the system dirs, and `uv` is absent, so resolution must go via the
    shebang."""
    import venv
    root = tmp_path / "tools" / "omnigent"
    venv.create(root, with_pip=False, symlinks=True)
    py = root / "bin" / "python"
    binroot = tmp_path / "bin"
    binroot.mkdir()
    entry = binroot / "omnigent"
    entry.write_text(f"#!{py}\nimport sys\nprint('fake omnigent')\n")
    entry.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binroot}:/usr/bin:/bin")
    monkeypatch.setattr(m.shutil, "which", lambda n: str(entry) if n == "omnigent" else None)
    return py


@pytest.mark.skipif(sys.platform.startswith("win"), reason="posix venv layout")
def test_omnigent_python_follows_the_entry_point_shebang(fake_omnigent_venv):
    assert m.omnigent_python() == fake_omnigent_venv


@pytest.mark.skipif(sys.platform.startswith("win"), reason="posix venv layout")
def test_install_pth_lands_in_omnigent_site_packages_and_imports(fake_omnigent_venv, tmp_path):
    """Regression for the deny-everything install: the .pth used to go to
    site.getsitepackages() of the INSTALLER's interpreter. Here the test
    interpreter and the fake omnigent venv differ, so only a fix that asks
    omnigent's python for its site-packages can pass."""
    pol_dir = tmp_path / "policies"
    pol_dir.mkdir()
    (pol_dir / "omnigent_local_policies.py").write_text("merge_gate = lambda **kw: None\n")
    m.install_pth(pol_dir)
    purelib = subprocess.run(
        [str(fake_omnigent_venv), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True).stdout.strip()
    pth = Path(purelib) / "omnigent-local-policies.pth"
    assert pth.read_text() == str(pol_dir) + "\n"
    assert str(tmp_path) in purelib  # the fake venv, not this interpreter's
    out = subprocess.run([str(fake_omnigent_venv), "-c",
                          "import omnigent_local_policies as x; print(x.__file__)"],
                         capture_output=True, text=True, check=True).stdout
    assert str(pol_dir) in out


@pytest.mark.skipif(sys.platform.startswith("win"), reason="posix venv layout")
def test_install_pth_dies_when_the_module_cannot_import(fake_omnigent_venv, tmp_path):
    pol_dir = tmp_path / "policies"
    pol_dir.mkdir()  # no module file inside -> import must fail -> die
    with pytest.raises(SystemExit):
        m.install_pth(pol_dir)


def test_install_pth_dies_without_an_omnigent_interpreter(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "omnigent_python", lambda: None)
    with pytest.raises(SystemExit):
        m.install_pth(tmp_path)
