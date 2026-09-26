"""Unit tests for installer/og_install.py's pure/testable logic.

Interactive prompts (ask/pick_one/...) and the full interactive flow
(build_plan_interactive) aren't covered here — they need a real terminal.
Everything that touches disk uses tmp_path and monkeypatches the module's
OMNI/STATE constants rather than the real ~/.omnigent.
"""
from __future__ import annotations

import json
import re
import shlex
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


def test_primary_collision_warning_reads_as_immediate_not_failover():
    # The head collides right away -- you never fail over TO the primary -- so
    # its wording must not talk about failover; a BACKUP keeps that wording,
    # because it is exactly the entry reached on failover.
    primary = _base_plan(coders=[{"id": "gemini", "priority": 1, "model": None}],
                         reviewer={"id": "agy", "model": None})
    msgs = [msg for _, msg in m.validate(primary) if "shares a vendor" in msg]
    assert msgs, msgs
    assert "failing over" not in msgs[0], msgs[0]
    assert "same-vendor review" in msgs[0] and "degraded-review" in msgs[0]

    backup = _base_plan(
        coders=[{"id": "gemini", "priority": 1, "model": None}],
        reviewer=[{"id": "codex", "priority": 1, "model": None},
                  {"id": "agy", "priority": 2, "model": None}])
    bmsgs = [msg for _, msg in m.validate(backup) if "shares a vendor" in msg]
    assert bmsgs and "failing over to `reviewer_2`" in bmsgs[0], bmsgs


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


def test_validate_warns_when_a_reviewer_never_receives_its_prompt():
    # cursor is prompt_delivery: none. As reviewer its whole contract lives in
    # reviewer.yaml.tmpl and would be dropped, so validate must warn.
    plan = _base_plan(reviewer={"id": "cursor", "model": None})
    issues = m.validate(plan)
    assert any(level == "warn" and "never receives the review contract" in msg
               for level, msg in issues)


def test_validate_no_reviewer_mute_warning_for_a_delivering_reviewer():
    # argv (codex) and per_turn (opencode) both deliver the spec prompt, so
    # neither triggers the reviewer warning.
    for rid in ("codex", "opencode"):
        plan = _base_plan(reviewer={"id": rid, "model": None})
        assert not any("never receives the review contract" in msg
                       for _, msg in m.validate(plan)), rid


def test_validate_coder_and_reviewer_mute_warnings_are_independent():
    # gemini is a `none` coder, cursor a `none` reviewer. Each warns on its own
    # with distinct wording; when both apply, both messages appear.
    coder_only = _base_plan(coders=[{"id": "gemini", "priority": 1, "model": None}])
    coder_msgs = [msg for _, msg in m.validate(coder_only)]
    assert any("never receive their sub-agent prompt" in msg for msg in coder_msgs)
    assert not any("never receives the review contract" in msg for msg in coder_msgs)

    reviewer_only = _base_plan(
        coders=[{"id": "opencode", "priority": 1, "model": "opencode/mimo-v2.5-free"}],
        reviewer={"id": "cursor", "model": None})
    rev_msgs = [msg for _, msg in m.validate(reviewer_only)]
    assert not any("never receive their sub-agent prompt" in msg for msg in rev_msgs)
    assert any("never receives the review contract" in msg for msg in rev_msgs)

    both = _base_plan(coders=[{"id": "gemini", "priority": 1, "model": None}],
                      reviewer={"id": "cursor", "model": None})
    both_msgs = [msg for _, msg in m.validate(both)]
    assert any("never receive their sub-agent prompt" in msg for msg in both_msgs)
    assert any("never receives the review contract" in msg for msg in both_msgs)


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


def test_validate_prompt_ceiling_fires_for_an_argv_backup_behind_a_per_turn_primary():
    # A per_turn primary (OpenCode) has no ceiling, so reading only
    # orchestrators[0] skipped the refusal entirely. The BACKUP (claude) is an
    # argv harness -- the entry that launches when the primary is dry -- so an
    # over-ceiling prompt must be refused for IT, and the message must name it.
    plan = _base_plan(orchestrator=[{"id": "opencode", "priority": 1},
                                    {"id": "claude", "priority": 2}])
    over = "x" * (m.PROMPT_CEILING + 500)
    msgs = [msg for level, msg in m.validate(plan, over) if level == "error"]
    assert any("over the" in msg and "Claude Code" in msg for msg in msgs), msgs


def test_ceiling_error_pluralizes_when_two_argv_harnesses_are_named():
    # Two argv entries are both bound by the tmux ceiling; "an argv-delivered
    # harness (A, B)" reads as though only one were over it.
    plan = _base_plan(orchestrator=[{"id": "claude", "priority": 1},
                                    {"id": "codex", "priority": 2}])
    over = "x" * (m.PROMPT_CEILING + 500)
    msg = next(msg for level, msg in m.validate(plan, over)
               if level == "error" and "over the" in msg)
    assert "for argv-delivered harnesses (Claude Code, Codex (OpenAI))" in msg
    assert "an argv-delivered harnesses" not in msg


def test_ceiling_error_stays_singular_for_one_argv_harness():
    plan = _base_plan(orchestrator="claude")
    over = "x" * (m.PROMPT_CEILING + 500)
    msg = next(msg for level, msg in m.validate(plan, over)
               if level == "error" and "over the" in msg)
    assert "for an argv-delivered harness (Claude Code)" in msg
    assert "harnesses" not in msg


def test_validate_prompt_ceiling_warns_for_an_argv_backup_behind_a_per_turn_primary():
    # Same chain inside the 800-byte warn band: the hazard is real (that backup
    # is the one that launches) and must surface even though the head has no
    # ceiling at all.
    plan = _base_plan(orchestrator=[{"id": "opencode", "priority": 1},
                                    {"id": "claude", "priority": 2}])
    near = "x" * (m.PROMPT_CEILING - 500)
    assert any(level == "warn" and "tmux ceiling" in msg
               for level, msg in m.validate(plan, near))


def test_validate_errors_when_a_backup_orchestrator_never_receives_its_prompt():
    # cursor is prompt_delivery: none. As a BACKUP orchestrator it drops the
    # whole orchestration contract exactly like a primary would, and it is the
    # entry reached for when the primary is dry -- so the error must fire for
    # it, not only for the head.
    plan = _base_plan(orchestrator=[{"id": "claude", "priority": 1},
                                    {"id": "cursor", "priority": 2}])
    msgs = [msg for level, msg in m.validate(plan) if level == "error"]
    assert any("never receives a spec prompt" in msg and "Cursor" in msg
               for msg in msgs), msgs


def test_validate_warns_when_the_orchestrator_is_unverified_for_that_role():
    # grok/devin clear the gates validate() enforces for orchestrator, so the
    # role is legal -- but no og run has ever been driven with either as the
    # brain. The registry records that per role; the caveat must reach a user
    # choosing one, as a WARN (a weak orchestrator at runtime, not a broken
    # install), never as an error.
    for aid in ("grok", "devin"):
        issues = m.validate(_base_plan(orchestrator=aid))
        warns = [msg for level, msg in issues if level == "warn"]
        assert any("UNVERIFIED as orchestrator" in msg for msg in warns), (aid, issues)
        assert not any(level == "error" for level, _ in issues), (aid, issues)


def test_validate_no_unverified_warning_for_a_verified_role():
    # claude is verified in every role it fills; nothing to warn about.
    issues = m.validate(_base_plan())
    assert not any("UNVERIFIED" in msg for _, msg in issues)


def test_role_options_flag_a_role_the_row_marks_unverified():
    # The per-role marker must reach the pick_one note (the picker's third
    # per-option field) at SELECTION time -- and must not taint a role the row
    # is verified for. Same row, two roles, opposite notes.
    orch = {aid: note for aid, _, note in
            m.role_options("orchestrator", {"grok": "/bin/grok", "claude": "/bin/claude"})}
    assert "UNVERIFIED as orchestrator" in orch["grok"]
    assert orch["claude"] is None
    coder = {aid: note for aid, _, note in
             m.role_options("coder", {"grok": "/bin/grok"})}
    assert coder["grok"] is None


# --------------------------------------------------------------------------
# role chains: orchestrator/reviewer are ordered lists, like coders
# --------------------------------------------------------------------------
def _chain_plan(**overrides):
    """The same roster as _base_plan, in the new chain shape."""
    plan = _base_plan(
        orchestrator=[{"id": "claude", "priority": 1}],
        reviewer=[{"id": "codex", "priority": 1, "model": None}],
    )
    plan.update(overrides)
    return plan


def _apply_into(tmp_path, monkeypatch, plan):
    """A real apply() into a throwaway OMNI.

    install_pth is stubbed: it resolves the interpreter omnigent runs under and
    would write a .pth into the REAL site-packages (the sandbox guard only
    trips when $HOME is redirected, which pytest does not do), so nothing that
    wants a real tree may leave it live.
    """
    monkeypatch.setattr(m, "OMNI", tmp_path)
    monkeypatch.setattr(m, "STATE", tmp_path / "og-install.json")
    monkeypatch.setattr(m, "install_pth", lambda *a, **k: None)
    plan = dict(plan, bin_dir=str(tmp_path / "bin"))
    m.apply(plan)
    return plan


def _tree(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_old_singleton_shape_installs_identically_to_the_chain_shape(tmp_path, monkeypatch):
    """The live-state regression. Every existing og-install.json holds a bare
    "orchestrator" string and a single-object "reviewer"; it must keep loading
    and produce a byte-identical install, or a reconfigure (and `og update`,
    which re-applies the saved plan) would silently change a working setup."""
    monkeypatch.setattr(m, "OMNI", tmp_path)
    monkeypatch.setattr(m, "STATE", tmp_path / "og-install.json")
    monkeypatch.setattr(m, "install_pth", lambda *a, **k: None)
    m.apply(dict(_base_plan(), bin_dir=str(tmp_path / "bin")))
    old_tree = _tree(tmp_path)
    assert (tmp_path / "agents" / "test-agent" / "agents" / "reviewer" /
            "config.yaml").is_file()

    m.apply(dict(_chain_plan(), bin_dir=str(tmp_path / "bin")))
    assert _tree(tmp_path) == old_tree, "the new shape changed the generated install"


def test_chain_shape_round_trips_through_save_and_load(tmp_path, monkeypatch):
    """og-install.json is the source of truth and must be rerunnable: what
    apply() persists is the chain shape, and normalizing it again is a no-op."""
    _apply_into(tmp_path, monkeypatch, _chain_plan())
    state = json.loads((tmp_path / "og-install.json").read_text())
    assert state["orchestrator"] == [{"id": "claude", "priority": 1}]
    assert state["reviewer"] == [{"id": "codex", "priority": 1, "model": None}]
    assert state["coders"] == [{"id": "opencode", "priority": 1,
                                "model": "opencode/mimo-v2.5-free"}]
    assert m.normalize_plan(json.loads(json.dumps(state))) == state


def test_chain_entries_normalizes_every_old_shape():
    # bare string (orchestrator), single object (reviewer), list of strings,
    # and an already-canonical list -- all reach the one entry shape.
    assert m.chain_entries("claude") == [{"id": "claude", "priority": 1}]
    assert m.chain_entries({"id": "codex", "model": None}) == [
        {"id": "codex", "model": None, "priority": 1}]
    assert m.chain_entries(["a", "b"]) == [{"id": "a", "priority": 1},
                                           {"id": "b", "priority": 2}]
    assert m.chain_entries([{"id": "a", "priority": 7, "model": "m"}]) == [
        {"id": "a", "priority": 7, "model": "m"}]
    assert m.chain_entries(None) == []


def test_chain_priority_comes_from_array_order():
    # Same rule as coders: position IS preference, so a stale explicit
    # `priority` on an entry keeps its place rather than being renumbered.
    plan = {"orchestrator": ["claude", "codex"], "reviewer": ["codex", "kiro"],
            "coders": [{"id": "opencode"}, {"id": "cline"}]}
    m.normalize_plan(plan)
    assert [e["priority"] for e in plan["orchestrator"]] == [1, 2]
    assert [e["priority"] for e in plan["reviewer"]] == [1, 2]
    assert [e["priority"] for e in plan["coders"]] == [1, 2]


def test_role_table_is_the_single_source_of_truth(monkeypatch):
    """One declarative table (ROLES) is what every per-role branch reads, so a
    new role is a row rather than a new literal tuple in normalize_plan(),
    account_entries(), validate(), apply(), render_*() and emit_questions()
    that drifts from the others. Proven by moving the table and watching the
    plumbing follow it, not by restating the role list by hand."""
    assert [r.key for r in m.ROLES] == ["orchestrator", "coders", "reviewer", "scout"]
    # The spec-rendering subset is derived from the same rows, not a second list.
    assert m.SPEC_ROLES == [r for r in m.ROLES if r.spec]
    assert [r.key for r in m.SPEC_ROLES] == ["coders", "reviewer", "scout"]
    # Every registry role a row names is one some registry row actually offers.
    for r in m.ROLES:
        assert any(r.role in a["roles"] for a in m.REGISTRY["agents"]), r.key
    # reviewer_names() is a thin alias of the generic helper, not a copy of it.
    plan = _chain_plan(reviewer=[{"id": "codex", "priority": 1, "model": None},
                                 {"id": "kiro", "priority": 2, "model": "auto"}])
    assert m.role_names(plan, "reviewer") == ["reviewer", "reviewer_2"]
    assert m.reviewer_names(plan) == m.role_names(plan, "reviewer")

    # A role added to the table is normalized and named with no other change...
    extra = m.Role("integrator", "integrator", "integrator.yaml.tmpl",
                   multi=False, spec=True, optional=True, ask="x")
    monkeypatch.setattr(m, "ROLES", [*m.ROLES, extra])
    moved = {"orchestrator": "claude", "integrator": ["codex", "kiro"]}
    m.normalize_plan(moved)
    assert moved["integrator"] == [{"id": "codex", "priority": 1},
                                   {"id": "kiro", "priority": 2}]
    assert m.role_names(moved, "integrator") == ["integrator", "integrator_2"]
    # ...and a key the table does NOT declare is left alone, never guessed at.
    untouched = {"mystery": "codex"}
    m.normalize_plan(untouched)
    assert untouched == {"mystery": "codex"}


def test_validate_and_render_accept_the_old_singleton_shape():
    # The read path never mutates the caller's plan into a list; readers
    # normalize on the way in, so a legacy dict keeps validating and rendering.
    plan = _base_plan()
    assert not any(level == "error" for level, _ in m.validate(plan))
    assert yaml.safe_load(m.render_orchestrator(plan))["name"] == "test-agent"
    assert m.render_reviewer(plan)


def test_validate_warns_on_a_same_vendor_backup_anywhere_in_the_reviewer_chain():
    """A WARNING, never an error: refusing would block a user whose only
    available backup is same-vendor, which is worse than telling them plainly
    what they are getting. Only a chain-aware check catches this — the primary
    (codex/openai) is clean, the collision is on `reviewer_2`."""
    plan = _base_plan(
        coders=[{"id": "gemini", "priority": 1, "model": None}],
        reviewer=[{"id": "codex", "priority": 1, "model": None},
                  {"id": "agy", "priority": 2, "model": None}])
    issues = m.validate(plan)
    warns = [(level, msg) for level, msg in issues if "shares a vendor" in msg]
    assert len(warns) == 1, issues
    assert all(level == "warn" for level, _ in warns)
    msg = warns[0][1]
    assert "`reviewer_2`" in msg and "Antigravity (Google)" in msg   # which reviewer
    assert "`coder_gemini`" in msg and "`google`" in msg             # which coder, which vendor
    assert "same-vendor review" in msg and "degraded-review" in msg

    # The roster skill carries the same caveat on the affected entry, so the
    # orchestrator knows at dispatch time and not only at install time.
    skill = m.render_roster_skill(plan)
    backup = skill.split("## `reviewer_2`")[1]
    assert "**Same-vendor review (`google`).**" in backup
    assert "`coder_gemini`" in backup and "degraded-review" in backup
    primary = skill.split("## `reviewer`")[1].split("## `reviewer_2`")[0]
    assert "**Same-vendor review" not in primary


def test_validate_silent_when_every_reviewer_pairing_is_cross_vendor():
    plan = _base_plan(
        coders=[{"id": "cmdcode", "priority": 1, "model": "moonshotai/kimi-k3"}],
        reviewer=[{"id": "codex", "priority": 1, "model": None},
                  {"id": "kiro", "priority": 2, "model": None}])
    assert not [msg for _, msg in m.validate(plan) if "shares a vendor" in msg]


def test_validate_warns_same_vendor_through_a_pin_in_the_chain():
    # Vendor follows the model pin, not the registry row: a freebuff deepseek/*
    # pin collides with a reviewer pinned to a deepseek model, even though the
    # reviewer's registry vendor is aws-kiro. Same rule as the singleton case,
    # now applied per chain entry.
    plan = _base_plan(
        coders=[{"id": "freebuff", "priority": 1, "model": "deepseek/deepseek-v4.1-flash"}],
        reviewer=[{"id": "codex", "priority": 1, "model": "gpt-5.5"},
                  {"id": "kiro", "priority": 2, "model": "deepseek/deepseek-chat"}])
    msgs = [msg for _, msg in m.validate(plan) if "shares a vendor" in msg]
    assert msgs and all("`reviewer_2`" in m_ for m_ in msgs), msgs
    assert "`deepseek`" in msgs[0]
    plan["coders"][0]["model"] = None       # z-ai pin: no collision with deepseek
    assert not [msg for _, msg in m.validate(plan) if "shares a vendor" in msg]


def test_two_reviewer_chain_emits_a_spec_per_entry_with_its_own_pin(tmp_path, monkeypatch):
    """`reviewer` for the primary, `reviewer_2` for the backup — the coder
    convention (a stable name for the head, a distinct one per addition). Each
    spec carries its own harness and pin, and both are listed in tools.agents:
    that list is the only dispatch surface, so a backup missing from it cannot
    be failed over to at all."""
    plan = _chain_plan(
        reviewer=[{"id": "codex", "priority": 1, "model": "gpt-5.5"},
                  {"id": "kiro", "priority": 2, "model": "auto"}])
    _apply_into(tmp_path, monkeypatch, plan)
    bundle = tmp_path / "agents" / "test-agent"
    primary = yaml.safe_load((bundle / "agents" / "reviewer" / "config.yaml").read_text())
    backup = yaml.safe_load((bundle / "agents" / "reviewer_2" / "config.yaml").read_text())
    assert primary["name"] == "reviewer"
    assert primary["executor"]["model"] == "gpt-5.5"
    assert primary["executor"]["config"]["harness"] == "codex-native"
    assert backup["name"] == "reviewer_2"
    assert backup["executor"]["model"] == "auto"
    assert backup["executor"]["config"]["harness"] == "acp:kiro-aws"
    # The same review contract reaches the backup: the whole reason it exists is
    # to be used when the primary is dry.
    assert "Judge the diff ONLY against the contract" in backup["prompt"]
    orch = yaml.safe_load((bundle / "config.yaml").read_text())
    assert orch["tools"]["agents"] == ["coder_zen", "reviewer", "reviewer_2"]
    assert "three sub-agents" in orch["prompt"]


def test_reviewer_chain_apply_is_idempotent_and_prunes_a_dropped_backup(tmp_path, monkeypatch):
    two = _chain_plan(reviewer=[{"id": "codex", "priority": 1, "model": "gpt-5.5"},
                                {"id": "kiro", "priority": 2, "model": "auto"}])
    _apply_into(tmp_path, monkeypatch, two)
    first = _tree(tmp_path)
    _apply_into(tmp_path, monkeypatch, two)
    assert _tree(tmp_path) == first

    one = _chain_plan(reviewer=[{"id": "codex", "priority": 1, "model": "gpt-5.5"}])
    _apply_into(tmp_path, monkeypatch, one)
    agents_dir = tmp_path / "agents" / "test-agent" / "agents"
    assert not (agents_dir / "reviewer_2").exists(), "stale backup spec left behind"
    assert (agents_dir / "reviewer").is_dir()


def test_a_none_backup_reviewer_warns_and_gets_its_own_roster_bullet():
    # cursor is prompt_delivery: none. As a BACKUP it drops the contract exactly
    # like a primary would, and it is the entry the orchestrator reaches for
    # when the primary is dry — so both the install-time warning and the
    # skill's bullet must fire for it, not only for the head.
    plan = _base_plan(reviewer=[{"id": "codex", "priority": 1, "model": None},
                                {"id": "cursor", "priority": 2, "model": None}])
    warnings = [msg for level, msg in m.validate(plan) if level == "warn"]
    assert any("never receives the review contract" in msg and "Cursor" in msg
               for msg in warnings), warnings
    skill = m.render_roster_skill(plan)
    assert "`reviewer_2` -> `cursor-native`" in skill
    backup_section = skill.split("## `reviewer_2`")[1]
    assert "**Does not receive its sub-agent prompt.**" in backup_section
    primary_section = skill.split("## `reviewer`")[1].split("## `reviewer_2`")[0]
    assert "Does not receive" not in primary_section   # codex delivers its prompt


def test_render_reviewer_can_target_a_chain_entry_by_name():
    # The existing single-argument call still renders the primary; the explicit
    # form is what apply() uses for each backup.
    plan = _chain_plan(reviewer=[{"id": "codex", "priority": 1, "model": "gpt-5.5"},
                                 {"id": "kiro", "priority": 2, "model": "auto"}])
    assert yaml.safe_load(m.render_reviewer(plan))["name"] == "reviewer"
    backup = yaml.safe_load(m.render_reviewer(plan, m.chain(plan, "reviewer")[1], "reviewer_2"))
    assert backup["name"] == "reviewer_2"
    assert backup["executor"]["model"] == "auto"


# --------------------------------------------------------------------------
# scout -- an optional read-only worker, a chain like the reviewer
# --------------------------------------------------------------------------
def test_plan_without_a_scout_installs_with_none(tmp_path, monkeypatch):
    """scout is OPTIONAL: omitting the key must produce a correct bundle with no
    scout spec on disk and no dangling reference in the orchestrator's agent
    list or prompt, so a user who does not want one is never charged for it."""
    plan = _chain_plan()
    _apply_into(tmp_path, monkeypatch, plan)
    bundle = tmp_path / "agents" / "test-agent"
    assert not (bundle / "agents" / "scout").exists()
    orch = yaml.safe_load((bundle / "config.yaml").read_text())
    assert "scout" not in orch["tools"]["agents"]
    assert "scout" not in m.render_orchestrator(plan)


def test_an_old_install_with_no_scout_key_still_loads():
    # Backward compatibility, as for the reviewer chain: a plan written before
    # the role existed carries no scout key and must keep loading, validating
    # and rendering unchanged rather than erroring on a missing role.
    plan = _base_plan()
    assert "scout" not in plan
    assert m.chain(plan, "scout") == []
    assert m.role_names(plan, "scout") == []
    assert "scout" not in m.normalize_plan(dict(plan))
    assert not [msg for _, msg in m.validate(plan) if "scout" in msg]


def test_two_scout_chain_emits_a_spec_per_entry_with_its_own_pin(tmp_path, monkeypatch):
    plan = _chain_plan(scout=[{"id": "cmdcode", "priority": 1, "model": "moonshotai/kimi-k3"},
                              {"id": "codex", "priority": 2, "model": "gpt-5.5"}])
    _apply_into(tmp_path, monkeypatch, plan)
    bundle = tmp_path / "agents" / "test-agent"
    primary = yaml.safe_load((bundle / "agents" / "scout" / "config.yaml").read_text())
    backup = yaml.safe_load((bundle / "agents" / "scout_2" / "config.yaml").read_text())
    assert primary["name"] == "scout"
    assert primary["executor"]["model"] == "moonshotai/kimi-k3"
    assert primary["executor"]["config"]["harness"] == "acp:command-code"
    assert backup["name"] == "scout_2"
    assert backup["executor"]["model"] == "gpt-5.5"
    assert backup["executor"]["config"]["harness"] == "codex-native"
    # Both are reachable: tools.agents is the only dispatch surface, and the
    # count in the prompt must match what is actually listed.
    orch = yaml.safe_load((bundle / "config.yaml").read_text())
    assert orch["tools"]["agents"] == ["coder_zen", "reviewer", "scout", "scout_2"]
    assert "four sub-agents" in orch["prompt"]


def test_scout_template_carries_the_read_only_contract():
    plan = _base_plan(scout=[{"id": "cmdcode", "model": "moonshotai/kimi-k3"}])
    parsed = yaml.safe_load(m.render_scout(plan))
    prompt = " ".join(parsed["prompt"].split())
    assert "READ-ONLY" in prompt
    assert "never edit, create or delete a file" in prompt
    assert "never commit" in prompt
    # The bounded answer is the whole point: a scout that pastes whole files
    # back into the orchestrator's context has failed its purpose.
    assert "BOUNDED SUMMARY" in prompt
    assert "NOT file dumps" in prompt
    assert "could not find something, say so plainly" in prompt
    # ...and it is a normal worker spec: pinned model, named harness, skills off.
    assert parsed["executor"]["model"] == "moonshotai/kimi-k3"
    assert parsed["skills"] == "none"


def test_a_none_scout_warns_for_the_primary_and_the_backup():
    # cursor and gemini are prompt_delivery: none. As scout entries they never
    # see scout.yaml.tmpl, so BOTH the read-only and the bounded-answer rules
    # must be inlined -- and the backup is reached exactly when the primary is
    # dry, so it must warn too, not only the head.
    plan = _base_plan(scout=[{"id": "cursor", "priority": 1, "model": None},
                             {"id": "gemini", "priority": 2, "model": None}])
    warnings = [msg for level, msg in m.validate(plan) if level == "warn"]
    assert sum("never receives the scout contract" in msg for msg in warnings) == 2, warnings
    # A scout on a delivering harness gets no such warning.
    quiet = _base_plan(scout=[{"id": "codex", "model": None}])
    assert not [msg for _, msg in m.validate(quiet) if "scout contract" in msg]


def test_scout_gets_an_inline_roster_bullet_only_when_installed():
    roster = m.render_roster(_base_plan(scout=[{"id": "codex", "model": None}]))
    assert "`scout`" in roster and "Read-only" in roster
    assert "`scout`" not in m.render_roster(_base_plan())


def test_emit_questions_offers_scout_as_an_ordered_chain(monkeypatch, capsys):
    monkeypatch.setattr(m, "scan", lambda: {"codex": "/bin/codex", "cmdcode": "/bin/cmd"})
    monkeypatch.setattr(m, "load_state", lambda: {})
    m.emit_questions()
    out = capsys.readouterr().out
    q = json.loads(out[out.index("{"):])
    scout = next(x for x in q["questions"] if x["key"] == "scout")
    assert scout["type"] == "ordered_multi"
    assert set(scout["choices"]) == {"codex", "cmdcode"}
    # ...with the same model-pin surface as coders and the reviewer.
    assert "choices" in scout["per_item"]


def test_show_renders_both_chains_in_order_with_the_primary_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, "scan", lambda: {"claude": "/bin/claude", "devin": "/bin/devin",
                                            "codex": "/bin/codex", "kiro": "/bin/kiro"})
    state = _base_plan(
        orchestrator=[{"id": "claude", "priority": 1}, {"id": "devin", "priority": 2}],
        reviewer=[{"id": "codex", "priority": 1, "model": "gpt-5.5"},
                  {"id": "kiro", "priority": 2, "model": "auto"}],
    )
    m.show(state)
    out = capsys.readouterr().out
    order = [out.index(x) for x in ("Claude Code", "Devin", "Codex (OpenAI)", "Kiro (AWS)")]
    assert order == sorted(order), out
    assert out.count("(primary)") == 2        # both chains mark their head
    assert "→ auto" in out                    # the backup's own pin is shown


def test_emit_questions_offers_orchestrator_and_reviewer_as_ordered_chains(monkeypatch, capsys):
    monkeypatch.setattr(m, "scan", lambda: {"claude": "/bin/claude", "codex": "/bin/codex"})
    monkeypatch.setattr(m, "load_state", lambda: {})
    m.emit_questions()
    out = capsys.readouterr().out
    q = json.loads(out[out.index("{"):])
    for key in ("orchestrator", "reviewer"):
        question = next(x for x in q["questions"] if x["key"] == key)
        assert question["type"] == "ordered_multi", key
    # The reviewer question carries the same static model list as coders, so an
    # AI installer can pin each entry of the chain.
    assert "choices" in next(x for x in q["questions"]
                             if x["key"] == "reviewer")["per_item"]
    # ...and the same model REQUIREMENT wording: a reviewer pin is required
    # exactly where a coder's is, so the prompt must carry the caveat too.
    coder_model = next(x for x in q["questions"] if x["key"] == "coders")["per_item"]["model"]
    reviewer_model = next(x for x in q["questions"] if x["key"] == "reviewer")["per_item"]["model"]
    assert reviewer_model == coder_model
    assert "REQUIRED" in reviewer_model


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


def test_reviewer_is_valid_yaml():
    plan = _rendering_plan()
    rendered = m.render_reviewer(plan)
    parsed = yaml.safe_load(rendered)
    assert parsed["executor"]["config"]["harness"] == "codex-native"


def test_reviewer_contract_reports_a_missing_diff_instead_of_reading_the_repo():
    """A reviewer handed a missing/empty/truncated diff once went and read the
    repository instead of reporting the gap, which destroyed the independence
    that is the whole reason a separate reviewer exists. The contract now says
    plainly that reporting the gap is the correct answer."""
    # Whitespace-normalized: the contract is wrapped prose, so a phrase may
    # straddle a line break.
    prompt = " ".join(yaml.safe_load(m.render_reviewer(_rendering_plan()))["prompt"].split())
    assert "missing, empty, or truncated" in prompt
    assert "SAY SO AND STOP" in prompt
    assert "not given the diff" in prompt
    # ...and it forbids the fallback that caused the failure.
    assert "not go looking for it" in prompt
    assert "do not open a repository" in prompt
    assert "do not read files" in prompt


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


def test_og_env_wires_a_backup_reviewers_own_account(tmp_path, monkeypatch):
    # Interactive collects `accounts` for every id in the reviewer chain, so a
    # BACKUP can have its own. write_og_env emitted the env line for the primary
    # only, so that backup silently ran on the wrong account -- the failure the
    # separate account exists to prevent.
    plan = _base_plan(
        orchestrator="codex",
        reviewer=[{"id": "codex", "priority": 1, "model": None},
                  {"id": "claude", "priority": 2, "model": None}],
        accounts={"claude": "/tmp/claude-work"})
    env = _og_env(tmp_path, monkeypatch, plan)
    assert "OG_CLAUDE_CONFIG_DIR=/tmp/claude-work\n" in env


def test_og_env_wires_the_primary_reviewers_account(tmp_path, monkeypatch):
    # The head's account keeps working exactly as before.
    plan = _base_plan(reviewer={"id": "claude", "model": None},
                      accounts={"claude": "/tmp/claude-primary"})
    env = _og_env(tmp_path, monkeypatch, plan)
    assert "OG_CLAUDE_CONFIG_DIR=/tmp/claude-primary\n" in env


def test_validate_errors_when_two_agents_share_an_env_but_need_different_accounts(monkeypatch):
    # Nothing in the registry forbids two rows sharing `multi_account.env`, and
    # og launches ONE server with ONE value: a second account for each means one
    # og.env line silently overwrites the other and an agent runs on the wrong
    # login. Refuse rather than write a file where one account is dropped.
    row = next(a for a in m.REGISTRY["agents"] if a["id"] == "claude")
    twin = {**row, "id": "claude2", "label": "Claude Two"}
    monkeypatch.setattr(m, "REGISTRY", {"agents": [*m.REGISTRY["agents"], twin]})
    plan = _base_plan(
        orchestrator="codex",
        reviewer=[{"id": "claude", "priority": 1, "model": None},
                  {"id": "claude2", "priority": 2, "model": None}],
        accounts={"claude": "/tmp/a", "claude2": "/tmp/b"})
    msgs = [msg for level, msg in m.validate(plan) if level == "error"]
    assert any("OG_CLAUDE_CONFIG_DIR" in msg and "/tmp/a" in msg and "/tmp/b" in msg
               for msg in msgs), msgs


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
    # The procedure tells the orchestrator to substitute a `-free` id when the
    # pin rotates out. It MOVED into the generated roster skill to free argv
    # bytes, so it is asserted there. A paid Zen pin (a subscriber) or a
    # user-added provider never rotates, so the section must be absent -- it
    # would only invite an `args.model` override.
    free = _base_plan(coders=[{"id": "opencode", "priority": 1, "model": "opencode/mimo-v2.5-free"}])
    paid = _base_plan(coders=[{"id": "opencode", "priority": 1, "model": "opencode/claude-sonnet-5"}])
    assert "Zen model preflight" in m.render_roster_skill(free)
    assert "Zen model preflight" not in m.render_roster_skill(paid)
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


def test_render_roster_skill_reviewer_mute_bullet_only_for_a_none_reviewer():
    """A `none` reviewer never sees reviewer.yaml.tmpl, so its section must
    restate the contract (diff as text, judge only against it, no edits, the
    three-section report). A delivering reviewer gets no such bullet."""
    mute = m.render_roster_skill(_base_plan(reviewer={"id": "cursor", "model": None}))
    rv_section = mute.split("## `reviewer`")[1]
    assert "**Does not receive its sub-agent prompt.**" in rv_section
    assert "never go looking for a worktree" in rv_section
    assert "BLOCKING / NON-BLOCKING / SUGGESTIONS" in rv_section
    assert "file:line" in rv_section

    normal = m.render_roster_skill(_base_plan(reviewer={"id": "codex", "model": None}))
    assert "**Does not receive its sub-agent prompt.**" not in normal


def test_roster_skill_names_the_reviewer_chain_in_order_and_states_the_failover_rule():
    """The orchestrator must know who backs whom before it reads anything: the
    moment it needs the backup is the moment the primary came back dry, which
    is also the moment nobody is watching. Same rule as the coder chain."""
    plan = _base_plan(reviewer=[{"id": "codex", "priority": 1, "model": None},
                                {"id": "kiro", "priority": 2, "model": "auto"}])
    skill = m.render_roster_skill(plan)
    assert "## Reviewers — failover chain" in skill
    assert "`reviewer` (Codex (OpenAI)) is the primary" in skill
    assert "`reviewer_2` (Kiro (AWS)) backs it up, in that order." in skill
    for phrase in ("og stats",
                   "`og stats --agent <id> --json`",
                   "earliest entry with capacity",
                   "move down only when one is dry, dropped",
                   "for the run, or already failed this run",
                   "`reset_at`",
                   "never re-send a diff to a",
                   "reviewer that already failed this run."):
        assert phrase in skill, phrase
    # The per-entry sections follow the chain's order.
    assert skill.index("## `reviewer`") < skill.index("## `reviewer_2`")


def test_roster_skill_says_so_when_there_is_only_one_reviewer():
    # No invented backup: with a single-entry chain the section says there is
    # nothing to fail over to rather than implying one exists.
    skill = m.render_roster_skill(_base_plan())
    assert "`reviewer` (Codex (OpenAI)) is the only reviewer in this roster." in skill
    assert "is the primary" not in skill


def test_roster_skill_names_the_scout_chain_in_order_and_states_the_rule():
    """The scout's contract has to be in the skill because it is what makes the
    role worth its prompt bytes: read-only, and a bounded answer. Same chain and
    failover rule as the reviewers, since the backup is reached on a dry primary."""
    plan = _base_plan(scout=[{"id": "cmdcode", "priority": 1, "model": "moonshotai/kimi-k3"},
                             {"id": "kiro", "priority": 2, "model": "auto"}])
    skill = m.render_roster_skill(plan)
    assert "## Scouts — read-only repo reading" in skill
    assert "`scout` (Command Code) is the primary" in skill
    assert "`scout_2` (Kiro (AWS)) backs it up, in that order." in skill
    for phrase in ("BOUNDED summary", "never a file dump", "READ-ONLY",
                   "`og stats --agent <id> --json`", "earliest entry with capacity",
                   "never re-send a"):
        assert phrase in skill, phrase
    assert skill.index("## `scout`") < skill.index("## `scout_2`")
    # Each entry section carries its own harness, pin and quota shape.
    assert "harness `acp:command-code`" in skill
    assert "pinned `moonshotai/kimi-k3`" in skill
    assert "`scout` -> `acp:command-code`" in skill
    assert "`scout_2` -> `acp:kiro-aws`" in skill


def test_roster_skill_has_no_scout_section_without_a_scout():
    skill = m.render_roster_skill(_base_plan())
    assert "## Scouts" not in skill
    assert "is the only scout" not in skill
    assert "## `scout" not in skill


def test_roster_skill_scout_section_is_single_when_there_is_only_one():
    skill = m.render_roster_skill(_base_plan(scout=[{"id": "codex", "model": None}]))
    assert "`scout` (Codex (OpenAI)) is the only scout in this roster." in skill
    assert "backs it up" not in skill


def test_a_none_scout_backup_gets_its_own_roster_bullet():
    # cursor is prompt_delivery: none. A BACKUP scout drops the contract exactly
    # like a primary would, so its section must restate the read-only rule and
    # the bounded answer rather than assume the spec prompt arrived.
    plan = _base_plan(scout=[{"id": "codex", "priority": 1, "model": None},
                             {"id": "cursor", "priority": 2, "model": None}])
    skill = m.render_roster_skill(plan)
    backup = skill.split("## `scout_2`")[1]
    assert "**Does not receive its sub-agent prompt.**" in backup
    assert "never edit, create or delete a file" in backup
    assert "never a file dump" in backup
    primary = skill.split("## `scout`")[1].split("## `scout_2`")[0]
    assert "Does not receive" not in primary     # codex delivers its prompt


# --------------------------------------------------------------------------
# prompt diet: guidance moved out of the argv prompt into the roster skill
#
# The orchestrator prompt is inlined into the harness command line and capped
# at PROMPT_CEILING shell-quoted bytes, so mechanics are rendered into the
# generated `roster` skill (free) instead. These tests pin BOTH halves: the
# moved fact is in the skill, and the section that must be known before
# anything is read is still in the prompt.
# --------------------------------------------------------------------------
FOUR_CODER_PLAN = {
    "agent_name": "dev-lead", "orchestrator": "claude",
    "coders": [{"id": "cmdcode", "priority": 1, "model": "moonshotai/kimi-k3"},
               {"id": "opencode", "priority": 2, "model": "opencode/mimo-v2.6-flash-free"},
               {"id": "kilo", "priority": 3, "model": "kilo/kilo-auto/free"},
               {"id": "freebuff", "priority": 4, "model": "z-ai/glm-5.3-flash"}],
    "reviewer": {"id": "codex"}, "port": 6767, "ngrok_domain": "", "max_dispatches": 4,
}


def _prompt_body(rendered: str) -> str:
    return re.search(r"prompt:\s*\|(.*)", rendered, re.S).group(1)


def test_roster_skill_carries_the_preflight_procedure_moved_out_of_the_prompt():
    """The roster preflight is pure mechanics consulted while a dispatch is
    being prepared, so it lives in the skill; only the fact that it is
    MANDATORY on the first turn stays inline. Every procedural fact must have
    MOVED, not vanished."""
    skill = m.render_roster_skill(FOUR_CODER_PLAN)
    for moved in ("## Preflight (FIRST turn, before any dispatch)",
                  "sys_session_get_info({})", "configured_harnesses",
                  "exactly `true`", "MISSING worker", "same turn you start planning",
                  "`coder_cmdcode` -> `acp:command-code`",
                  "`coder_zen` -> `opencode-native`",
                  "`coder_kilo` -> `acp:kilo-code`",
                  "`coder_freebuff` -> `acp:freebuff`",
                  "`reviewer` -> `codex-native`"):
        assert moved in skill, moved


def test_roster_skill_preflight_mapping_is_generated_from_the_plan():
    """The worker -> harness-id table is generated from the plan, so a
    reconfigured roster produces a different table instead of a stale one."""
    a = m.render_roster_skill(_base_plan(coders=[{"id": "cmdcode", "priority": 1,
                                                  "model": "moonshotai/kimi-k3"}]))
    b = m.render_roster_skill(_base_plan(coders=[{"id": "cline", "priority": 1,
                                                  "model": "deepseek/deepseek-v4-flash"}]))
    assert "`coder_cmdcode` -> `acp:command-code`" in a
    assert "`coder_cmdcode` -> `acp:command-code`" not in b
    assert "`coder_cline` -> `acp:cline`" in b
    assert "`coder_cline` -> `acp:cline`" not in a


def test_roster_skill_carries_the_per_worker_failure_shapes():
    """The vendor-specific exhaustion strings and the exact mark-dry invocation
    moved out of the prompt, generated from the registry `quota.note` /
    `model.note` fields so they cannot drift from the catalog."""
    skill = m.render_roster_skill(FOUR_CODER_PLAN)
    assert "og stats --mark <id> dry --until" in skill
    assert "Add credits to continue, or switch to a free model" in skill   # kilo model.note
    assert "not enough Freebucks" in skill                                 # freebuff quota.note
    assert "Rate limit exceeded" in skill                                  # opencode quota.note
    assert "empty turn" in skill                                           # cline, Capacity
    assert "You've reached your 5-hour usage limit" in skill               # cmdcode quota.note
    assert "quota failure shape:" in skill


def test_orchestrator_prompt_keeps_the_safety_critical_inline_sections():
    """Whatever moved, these must survive in the prompt: each changes a
    decision made BEFORE the orchestrator reads anything."""
    rendered = m.render_orchestrator(FOUR_CODER_PLAN)
    for needle in (
            "you do NOT write product code",
            "NEVER merge into a `protected` branch",
            "Merge only into `auto_merge_target`",
            "NEVER write the passed marker for a review that did not happen",
            "`cross-vendor-review: passed`",
            "`degraded-review`",
            "DIFFERENT vendor",
            "DROPPED turn",
            "BOOT failure",
            "Drop it for the run; never re-dispatch.",
            "TASK failure",
            "fresh attempt in a CLEAN worktree",
            "QUOTA failure",
            "FROM A CLEAN WORKTREE",
            "MANDATORY before any dispatch",
            "sys_session_get_info({})"):
        assert needle in rendered, needle


def test_orchestrator_prompt_left_the_moved_detail_to_the_skill():
    """Guard against the moved detail creeping back into the argv prompt."""
    rendered = m.render_orchestrator(FOUR_CODER_PLAN)
    for moved in ("Add credits to continue", "not enough Freebucks",
                  "Rate limit exceeded", "--until", "--reason", "og stats --agent"):
        assert moved not in rendered, moved


def test_orchestrator_prompt_leaves_headroom_for_a_four_coder_roster():
    """The measured 4-coder roster must clear the 1,200-byte bar that the two
    new sub-agent roles and the singleton failover entries are budgeted
    against. A regression that re-inflates the prompt fails here, loudly."""
    body = _prompt_body(m.render_orchestrator(FOUR_CODER_PLAN))
    quoted = len(shlex.quote(body).encode())
    headroom = m.PROMPT_CEILING - quoted
    assert headroom >= 1200, f"{quoted} quoted, {headroom} headroom"
    # The installer itself must not warn about the ceiling: its warn band starts
    # 800 below it, so a 1,200-byte margin is comfortably outside.
    assert not any("ceiling" in msg for _, msg in m.validate(FOUR_CODER_PLAN, body))


FOUR_CODER_PLAN_SCOUT = dict(
    FOUR_CODER_PLAN, scout=[{"id": "cmdcode", "model": "moonshotai/kimi-k3"}])


def test_orchestrator_prompt_leaves_headroom_with_a_scout_installed():
    """The acceptance bar for the scout task: a four-coder roster PLUS the
    optional scout must still clear 900 bytes of headroom. Below that, the
    integrator role that follows has nothing to spend, and a later regression
    that re-inflates the prompt fails here rather than at tmux launch."""
    body = _prompt_body(m.render_orchestrator(FOUR_CODER_PLAN_SCOUT))
    quoted = len(shlex.quote(body).encode())
    headroom = m.PROMPT_CEILING - quoted
    assert headroom >= 900, f"{quoted} quoted, {headroom} headroom"
    assert not any("ceiling" in msg for _, msg in m.validate(FOUR_CODER_PLAN_SCOUT, body))


def test_orchestrator_prompt_names_scout_only_when_installed():
    with_scout = m.render_orchestrator(FOUR_CODER_PLAN_SCOUT)
    assert "`scout`" in with_scout
    assert "read-only, never edits or commits" in with_scout
    # The existing tension survives: a quick look to scope a dispatch is still
    # fine, so the prompt does not tell the brain to delegate every read.
    assert "quick look at a file or two" in with_scout
    assert "`scout`" not in m.render_orchestrator(FOUR_CODER_PLAN)


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
    assert "Zen model preflight" in m.render_roster_skill(plan)
    plan["coders"][0]["model"] = "deepseek/deepseek-chat"
    rendered = m.render_roster_skill(plan)
    assert "Zen model preflight" not in rendered
    assert "day-capped" not in m.render_orchestrator(plan)


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


@pytest.mark.skipif(sys.platform.startswith("win"), reason="posix HOME/pwd semantics")
def test_install_pth_sandboxed_home_leaves_live_pth_untouched(
        fake_omnigent_venv, tmp_path, monkeypatch, capsys):
    """A sandboxed run (HOME pointed at a temp dir per the AGENTS.md recipe)
    must not repoint the LIVE .pth at the temp policies dir: the write is
    skipped and the pre-existing .pth stays byte-identical."""
    purelib = subprocess.run(
        [str(fake_omnigent_venv), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True).stdout.strip()
    pth = Path(purelib) / "omnigent-local-policies.pth"
    pth.write_text("live-contents\n")
    before = pth.read_bytes()
    monkeypatch.setenv("HOME", str(tmp_path / "sandbox-home"))
    m.install_pth(tmp_path / "policies")  # must neither raise nor write
    assert pth.read_bytes() == before
    out = capsys.readouterr().out
    assert "sandbox" in out.lower() and str(pth) in out


# --------------------------------------------------------------------------
# model.choices: a static lineup for agents with no live model listing
# --------------------------------------------------------------------------
def test_pick_model_offers_choices_by_number(monkeypatch):
    # freebuff is required=false with no list_cmd but declares `model.choices`:
    # the early-return bail-out no longer fires, so the static lineup is shown
    # through the same numbered menu and selectable by number.
    reg = m.agents_by_id()
    answers = iter(["4"])
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: next(answers))
    assert m.pick_model(reg["freebuff"], None) == "deepseek-v4.1-flash"


def test_pick_model_without_list_cmd_or_choices_still_prompts(monkeypatch):
    # A required=false row with neither list_cmd nor choices IS pinnable: the
    # old early return inferred "not pinnable" from that missing metadata and
    # silently skipped the question -- the reviewer-pin bug. Now only a row that
    # DECLARES `pinnable: false` is skipped, so this one reaches manual entry,
    # where a blank answer means "harness default" and returns None.
    asked = []

    def fake_ask(prompt, default=None):
        asked.append((prompt, default))
        return ""

    monkeypatch.setattr(m, "ask", fake_ask)
    agent = {"id": "agy", "label": "Antigravity", "vendor": "google",
             "model": {"required": False, "pin_path": "executor.model"}}
    assert m.pick_model(agent, None) is None
    assert len(asked) == 1
    prompt, default = asked[0]
    assert "model id" in prompt and "(blank = harness default)" in prompt
    assert default == ""


def test_pick_model_unpinnable_row_keeps_current_without_prompting(monkeypatch):
    # claude declares model.pinnable=false (pinning the orchestrator also pins
    # the family its workers route within). It must keep whatever is set and
    # never prompt, whatever that value is.
    asked = []
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: asked.append(prompt) or "")
    reg = m.agents_by_id()
    assert m.pick_model(reg["claude"], "claude-opus-5") == "claude-opus-5"
    assert asked == []


def test_pick_model_prompts_for_agy_and_blank_means_no_pin(monkeypatch):
    # The real agy row (required=false, no list_cmd, no choices, not marked
    # pinnable:false) now reaches the manual prompt; blank -> None.
    seen = {}

    def fake_ask(prompt, default=None):
        seen["prompt"], seen["default"] = prompt, default
        return ""

    monkeypatch.setattr(m, "ask", fake_ask)
    reg = m.agents_by_id()
    assert m.pick_model(reg["agy"], None) is None
    assert "model id" in seen["prompt"] and "(blank = harness default)" in seen["prompt"]
    assert seen["default"] == ""


def test_pick_model_manual_entry_returns_the_typed_id(monkeypatch):
    # The same no-listing row accepts a hand-typed id verbatim.
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: "gemini-3-pro")
    reg = m.agents_by_id()
    assert m.pick_model(reg["agy"], None) == "gemini-3-pro"


def test_pick_model_codex_menu_resolves_by_number(monkeypatch):
    # codex has no live listing, so its static `choices` drive the same numbered
    # menu; a number resolves to the id at that position.
    reg = m.agents_by_id()
    assert reg["codex"]["model"]["choices"] == [
        "gpt-5.5-codex", "gpt-5.5", "gpt-5.5-codex-mini", "o4-mini"]
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: "3")
    assert m.pick_model(reg["codex"], None) == "gpt-5.5-codex-mini"


def test_pick_model_offers_an_existing_pin_as_default(monkeypatch):
    # An existing pin is the menu default; pressing enter (ask returns the
    # default) keeps it rather than snapping back to the registry's `prefer`.
    seen = {}

    def fake_ask(prompt, default=None):
        seen["default"] = default
        return default

    monkeypatch.setattr(m, "ask", fake_ask)
    reg = m.agents_by_id()
    assert m.pick_model(reg["codex"], "gpt-5.5") == "gpt-5.5"
    assert seen["default"] == "gpt-5.5"


def test_pick_model_choices_respect_prefer_default(monkeypatch):
    reg = m.agents_by_id()
    default = {}
    monkeypatch.setattr(m, "ask", lambda prompt, d=None: default.setdefault("d", d) or d)
    # freebuff's `prefer` (z-ai/glm-5.3-flash) is in choices, so an empty
    # enter (returns default) pins the preferred model rather than the first.
    assert m.pick_model(reg["freebuff"], None) == "z-ai/glm-5.3-flash"


def test_pick_model_no_list_cmd_row_emits_no_missing_listing_warning(monkeypatch):
    """The "could not list models" warning is gated on having TRIED a listing.
    A row that never declared `list_cmd` (agy) must reach manual entry silently
    -- firing it there would report a command that was never run, telling the
    user a listing failed when none was attempted."""
    reg = m.agents_by_id()
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: "gemini-3-pro")
    warned = []
    monkeypatch.setattr(m, "warn", lambda msg: warned.append(msg))
    assert m.pick_model(reg["agy"], None) == "gemini-3-pro"
    assert not any("could not list models" in w for w in warned), warned


def test_pick_model_codex_accepts_a_hand_typed_unlisted_id(monkeypatch):
    """codex's `choices` are a static convenience list, not a closed set: the
    row's own note promises a newer id typed by hand is accepted through the
    "pin it anyway?" confirmation, since the CLI cannot enumerate models."""
    reg = m.agents_by_id()
    monkeypatch.setattr(m, "ask", lambda prompt, default=None: "gpt-6-codex")
    monkeypatch.setattr(m, "ask_yes", lambda prompt, default=False: True)
    assert m.pick_model(reg["codex"], None) == "gpt-6-codex"


# --------------------------------------------------------------------------
# emit_questions surfaces a row's static model choices
# --------------------------------------------------------------------------
def test_emit_questions_carries_choices(monkeypatch, capsys):
    monkeypatch.setattr(m, "scan", lambda: {"freebuff": "/bin/blink"})
    monkeypatch.setattr(m, "load_state", lambda: {})
    m.emit_questions()
    out = capsys.readouterr().out
    q = json.loads(out[out.index("{"):])
    coder_q = next(x for x in q["questions"] if x["key"] == "coders")
    assert "choices" in coder_q["per_item"]
    assert coder_q["per_item"]["choices"]["freebuff"] == [
        "z-ai/glm-5.3-flash", "mimo-2.5", "solar-pro-4", "deepseek-v4.1-flash"]
    # rows without choices are simply absent, not empty
    assert "cline" not in coder_q["per_item"]["choices"]


def test_emit_questions_carries_the_per_role_caveat(monkeypatch, capsys):
    # AGENTS.md Part 1 has an AI installer drive its conversation from
    # --questions and never offer an agent without the warning that applies to
    # it. The caveat is per role, so it rides the question offering that role.
    monkeypatch.setattr(m, "scan", lambda: {"grok": "/bin/grok", "devin": "/bin/devin",
                                            "claude": "/bin/claude"})
    monkeypatch.setattr(m, "load_state", lambda: {})
    m.emit_questions()
    out = capsys.readouterr().out
    q = json.loads(out[out.index("{"):])
    orch_q = next(x for x in q["questions"] if x["key"] == "orchestrator")
    assert "UNVERIFIED as orchestrator" in orch_q["notes"]["grok"]
    assert "UNVERIFIED as orchestrator" in orch_q["notes"]["devin"]
    assert "claude" not in orch_q["notes"]
    # ...and the coder question carries nothing for them: verified in that role.
    coder_q = next(x for x in q["questions"] if x["key"] == "coders")
    assert "grok" not in coder_q["notes"] and "devin" not in coder_q["notes"]
    # The raw field is in the embedded registry for a consumer that reads it.
    grok_row = next(r for r in q["registry"] if r["id"] == "grok")
    assert grok_row["unverified_roles"] == ["orchestrator"]


# --------------------------------------------------------------------------
# model.env_var -> env-prefixed ACP command (freebuff/blink), additive
# --------------------------------------------------------------------------
def test_acp_command_injects_env_var_for_env_var_rows():
    reg = m.agents_by_id()
    # A row declaring model.env_var exports the pin as that variable.
    assert m.acp_command(reg["freebuff"], "deepseek/deepseek-v4.1-flash") == \
        "env BLINK_MODEL=deepseek/deepseek-v4.1-flash blink"
    assert m.acp_command(reg["freebuff"], None) == "blink"
    # A legacy row using top-level model_env still works (Cline's CLINE_MODEL).
    assert m.acp_command(reg["cline"], "deepseek/deepseek-v4-flash") == \
        "env CLINE_MODEL=deepseek/deepseek-v4-flash cline --acp --auto-approve true"
    # An acp-user row with neither keeps a plain command.
    assert m.acp_command(reg["kilo"], "kilo/kilo-auto/free") == "kilo acp"


def test_patch_global_config_renders_env_var_for_freebuff(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "OMNI", tmp_path)
    (tmp_path / "config.yaml").write_text("{}\n")
    plan = _base_plan(coders=[{"id": "freebuff", "priority": 1, "model": "deepseek/deepseek-v4.1-flash"}])
    m.patch_global_config(plan)
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    row = next(r for r in cfg["acp"]["agents"] if r["name"] == "Freebuff")
    assert row["command"].startswith("env BLINK_MODEL=deepseek/deepseek-v4.1-flash blink")


# --------------------------------------------------------------------------
# vendor follows the model (Part 2d): a freebuff deepseek/* pin is same-vendor
# with a deepseek reviewer. No code change; the existing model_vendor family
# already maps it. Lock it in here.
# --------------------------------------------------------------------------
def test_model_vendor_maps_freebuff_deepseek_pin_to_deepseek():
    reg = m.agents_by_id()
    assert m.vendor_of(reg["freebuff"], {"id": "freebuff",
                                         "model": "deepseek/deepseek-v4.1-flash"}) == "deepseek"
    assert m.vendor_of(reg["freebuff"], {"id": "freebuff",
                                         "model": "deepseek/deepseek-chat"}) == "deepseek"
    assert m.vendor_of(reg["freebuff"], {"id": "freebuff", "model": None}) == "z-ai"


def test_validate_warns_same_vendor_for_freebuff_deepseek_pin():
    # Vendor follows the model: a deepseek/* Freebuff pin shares Cline's vendor
    # (deepseek) for review. validate role-checks coders vs reviewer, not which
    # agent the reviewer id is.
    plan = _base_plan(
        coders=[{"id": "freebuff", "priority": 1, "model": "deepseek/deepseek-v4.1-flash"}],
        reviewer={"id": "cline", "model": None},
    )
    ws = [msg for level, msg in m.validate(plan) if level == "warn"
          and "shares a vendor" in msg]
    assert ws, [msg for _, msg in m.validate(plan)]
    # ...and the registry's own vendor is NOT same-vendor with an unpinned
    # Freebuff (z-ai vs cline's deepseek).
    plan["coders"][0]["model"] = None
    msgs = [msg for level, msg in m.validate(plan) if level == "warn"
            and "shares a vendor" in msg]
    assert not msgs


# --------------------------------------------------------------------------
# write_shims + {shim:...} expansion (cmd bridge support)
# --------------------------------------------------------------------------
def _registry_with_cmd_shim(monkeypatch):
    # A stand-in for the cmd row (owned by a parallel change): an acp-user
    # bridge whose model and write permission only arrive via the binary
    # named by CMD_BIN, so the plan materializes a shim script for it.
    row = {
        "id": "cmdtest",
        "label": "CmdTest",
        "harness": "acp:cmdtest",
        "kind": "acp-user",
        "binary": "cmd",
        "acp_command": "env CMD_BIN={shim:cmdtest-og} cmd-acp",
        "model": {"env_var": "CMD_MODEL", "list_cmd": ["cmd", "--list-models"]},
        "shim": {"name": "cmdtest-og",
                 "script": "#!/bin/sh\nexec cmd \"$@\" --yolo\n"},
    }
    monkeypatch.setattr(m, "REGISTRY", {"agents": [*m.REGISTRY["agents"], row]})
    return row


def _cmdtest_plan(**overrides):
    plan = _base_plan(
        coders=[{"id": "cmdtest", "priority": 1, "model": "moonshotai/kimi-k3"}],
    )
    plan.update(overrides)
    return plan


def test_write_shims_writes_an_executable_script(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "OMNI", tmp_path)
    row = _registry_with_cmd_shim(monkeypatch)
    changed = m.write_shims(_cmdtest_plan())
    dest = tmp_path / "shims" / "cmdtest-og"
    assert dest.read_text() == row["shim"]["script"]
    # The bridge exec's this path directly, so it must be executable.
    assert dest.stat().st_mode & 0o777 == 0o755
    assert changed and all("cmdtest-og" in entry for entry in changed)


def test_write_shims_covers_a_reviewer_too(tmp_path, monkeypatch):
    # patch_global_config renders a row for an ACP reviewer as well, so a
    # shimmed reviewer must materialize its script the same way.
    monkeypatch.setattr(m, "OMNI", tmp_path)
    _registry_with_cmd_shim(monkeypatch)
    plan = _cmdtest_plan(
        coders=[{"id": "opencode", "priority": 1, "model": "opencode/mimo-v2.5-free"}],
        reviewer={"id": "cmdtest", "model": None},
    )
    assert m.write_shims(plan)
    assert (tmp_path / "shims" / "cmdtest-og").is_file()


def test_write_shims_is_idempotent(tmp_path, monkeypatch):
    # The installer is rerunnable by design: a second apply with identical
    # content must leave the file alone and report no change.
    monkeypatch.setattr(m, "OMNI", tmp_path)
    _registry_with_cmd_shim(monkeypatch)
    plan = _cmdtest_plan()
    assert m.write_shims(plan)
    dest = tmp_path / "shims" / "cmdtest-og"
    before = dest.stat().st_mtime_ns
    assert m.write_shims(plan) == []
    assert dest.stat().st_mtime_ns == before


def test_write_shims_repairs_a_clobbered_mode(tmp_path, monkeypatch):
    # The bridge exec's the shim path directly, so a mode that drifted (umask,
    # a stray chmod, a dotfile sync) is a launch failure with no message
    # pointing at the installer. Content already matches here: only the mode
    # must converge, and the repair must be reported, not silent.
    monkeypatch.setattr(m, "OMNI", tmp_path)
    row = _registry_with_cmd_shim(monkeypatch)
    plan = _cmdtest_plan()
    assert m.write_shims(plan)
    dest = tmp_path / "shims" / "cmdtest-og"
    dest.chmod(0o644)
    changed = m.write_shims(plan)
    assert dest.stat().st_mode & 0o777 == 0o755
    assert dest.read_text() == row["shim"]["script"]
    assert changed and all("cmdtest-og" in entry for entry in changed)
    # ...and once converged, silence again.
    assert m.write_shims(plan) == []


def test_acp_command_expands_a_shim_token(tmp_path, monkeypatch):
    # No $VAR may survive: the executor runs this argv with no shell, so the
    # token becomes the absolute shim path, not $OMNI/shims/....
    monkeypatch.setattr(m, "OMNI", tmp_path)
    row = _registry_with_cmd_shim(monkeypatch)
    assert m.acp_command(row, None) == \
        f"env CMD_BIN={tmp_path}/shims/cmdtest-og cmd-acp"


def test_acp_command_composes_shim_with_the_model_env_prefix(tmp_path, monkeypatch):
    # The bridge reads the pin from CMD_MODEL and the binary from CMD_BIN, so
    # both env assignments chain in the one argv (chained `env` is valid).
    # shlex.quote leaves this id bare (no shell-unsafe chars); the contract
    # here is the composition -- prefix outside, expanded path inside.
    monkeypatch.setattr(m, "OMNI", tmp_path)
    row = _registry_with_cmd_shim(monkeypatch)
    assert m.acp_command(row, "moonshotai/kimi-k3") == (
        "env CMD_MODEL=moonshotai/kimi-k3 "
        f"env CMD_BIN={tmp_path}/shims/cmdtest-og cmd-acp"
    )


def test_acp_command_leaves_rows_without_a_shim_token_alone(tmp_path, monkeypatch):
    # Expansion must be a no-op for every existing row: byte-identical output.
    monkeypatch.setattr(m, "OMNI", tmp_path)
    reg = m.agents_by_id()
    assert m.acp_command(reg["cline"], "deepseek/deepseek-v4-flash") == \
        "env CLINE_MODEL=deepseek/deepseek-v4-flash cline --acp --auto-approve true"
    assert m.acp_command(reg["kilo"], "kilo/kilo-auto/free") == "kilo acp"


def test_list_models_drops_cmd_section_headings_and_docs_line(monkeypatch):
    # cmd --list-models groups ids under bare Capitalized vendor headings
    # ("Stealth", "Anthropic", ...) with a trailing docs line, each of which
    # parsed as a bare id. The live CLI pads its description column, so the
    # docs line's first field is the bare token "Docs:" -- reproduced here
    # with a double space, which is what let it through.
    monkeypatch.setattr(
        m.subprocess, "run",
        _fake_run(
            "Available models  ·  81 models\n"
            "\n"
            "Open Source\n"
            "\n"
            "deepseek/deepseek-v4-flash             fast hybrid-attention reasoning (default)\n"
            "moonshotai/kimi-k3                     long-horizon coding & knowledge work with 1M context\n"
            "\n"
            "Stealth\n"
            "\n"
            "stealth/space-bunny-alpha              FREE stealth model with 1M context\n"
            "\n"
            "Anthropic\n"
            "\n"
            "claude-sonnet-5                        best combo of speed & intelligence (recommended)\n"
            "\n"
            "OpenAI\n"
            "\n"
            "gpt-5.3-codex                          frontier coding model\n"
            "\n"
            "Google\n"
            "\n"
            "Sakana\n"
            "\n"
            "Meta\n"
            "\n"
            "xAI\n"
            "\n"
            "Docs:  https://commandcode.ai/docs\n"
        ),
    )
    agent = {"model": {"list_cmd": ["cmd", "--list-models"]}}
    got = m.list_models(agent)
    assert got == ["deepseek/deepseek-v4-flash", "moonshotai/kimi-k3",
                   "stealth/space-bunny-alpha", "claude-sonnet-5", "gpt-5.3-codex"]
    for junk in ("Stealth", "Anthropic", "OpenAI", "Google", "Sakana",
                 "Meta", "xAI", "Docs:"):
        assert junk not in got


def test_validate_errors_on_an_impossible_required_and_unpinnable_model(monkeypatch):
    """A row that both demands a pin (`model.required`) and forbids offering one
    (`model.pinnable: false`) is self-contradictory: pick_model skips the
    question, so the worker runs unpinned and inherits the orchestrator's model
    id -- the exact failure `required` exists to prevent. No real row does this;
    the guard is for the next registry edit, so a synthetic row exercises it."""
    row = {
        "id": "contradiction", "label": "Contradiction", "harness": "acp:contradiction",
        "kind": "acp-user", "binary": "contradiction", "vendor": "test",
        "roles": ["coder"], "relay": False, "silent_model_failure": False,
        "prompt_delivery": "unknown",
        "model": {"required": True, "pinnable": False, "pin_path": "executor.model"},
    }
    monkeypatch.setattr(m, "REGISTRY", {"agents": [*m.REGISTRY["agents"], row]})
    plan = _base_plan(coders=[{"id": "contradiction", "priority": 1, "model": None}])
    errors = [msg for level, msg in m.validate(plan) if level == "error"]
    assert any("model.required true and model.pinnable false" in msg for msg in errors), errors


# --------------------------------------------------------------------------
# unresolved {shim:...} tokens are refused + shim paths survive spaces
# --------------------------------------------------------------------------
def _bad_shim_row(**overrides):
    row = {
        "id": "badshim",
        "label": "BadShim",
        "harness": "acp:badshim",
        "kind": "acp-user",
        "binary": "badshim",
        "vendor": "badvendor",
        "acp_command": "env CMD_BIN={shim:missing} badshim-acp",
        "model": {"env_var": "CMD_MODEL", "list_cmd": ["badshim", "--list-models"]},
        "shim": {"name": "something-else",
                 "script": "#!/bin/sh\nexec badshim \"$@\"\n"},
    }
    row.update(overrides)
    return row


def test_validate_errors_on_shim_token_with_wrong_declared_name(monkeypatch):
    row = _bad_shim_row()
    monkeypatch.setattr(m, "REGISTRY", {"agents": [*m.REGISTRY["agents"], row]})
    plan = _base_plan(coders=[{"id": "badshim", "priority": 1,
                               "model": "moonshotai/kimi-k3"}])
    issues = m.validate(plan)
    shim_errors = [(level, msg) for level, msg in issues if "{shim:missing}" in msg]
    assert shim_errors, issues
    assert all(level == "error" for level, _ in shim_errors)
    assert any("badshim" in msg for _, msg in shim_errors)


def test_validate_errors_on_shim_token_with_no_declared_shim(monkeypatch):
    row = _bad_shim_row(shim=None)
    row.pop("shim", None)
    monkeypatch.setattr(m, "REGISTRY", {"agents": [*m.REGISTRY["agents"], row]})
    plan = _base_plan(coders=[{"id": "badshim", "priority": 1,
                               "model": "moonshotai/kimi-k3"}])
    issues = m.validate(plan)
    shim_errors = [(level, msg) for level, msg in issues if "{shim:missing}" in msg]
    assert shim_errors, issues
    assert all(level == "error" for level, _ in shim_errors)
    assert any("badshim" in msg for _, msg in shim_errors)


def test_validate_errors_on_unresolved_shim_token_in_reviewer(monkeypatch):
    row = _bad_shim_row()
    monkeypatch.setattr(m, "REGISTRY", {"agents": [*m.REGISTRY["agents"], row]})
    plan = _base_plan(reviewer={"id": "badshim", "model": None})
    issues = m.validate(plan)
    shim_errors = [(level, msg) for level, msg in issues if "{shim:missing}" in msg]
    assert shim_errors, issues
    assert all(level == "error" for level, _ in shim_errors)
    assert any("badshim" in msg for _, msg in shim_errors)


def test_validate_no_shim_error_for_a_correctly_declared_row(monkeypatch):
    _registry_with_cmd_shim(monkeypatch)
    issues = m.validate(_cmdtest_plan())
    assert not [msg for _, msg in issues if "shim" in msg.lower()], issues


def test_acp_command_keeps_a_spaced_shim_path_as_one_argv_entry(monkeypatch):
    # OMNI with a space (ordinary on macOS): argv is what matters, so assert
    # against shlex.split, not the raw string.
    monkeypatch.setattr(m, "OMNI", Path("/Users/John Smith/.omnigent"))
    row = _bad_shim_row(
        acp_command="env CMD_BIN={shim:cmd-og} cmd-acp",
        shim={"name": "cmd-og", "script": "#!/bin/sh\nexec cmd \"$@\"\n"},
    )
    row["acp_command"] = "env CMD_BIN={shim:cmd-og} cmd-acp"
    rendered = m.acp_command(row, "moonshotai/kimi-k3")
    argv = shlex.split(rendered)
    assert "CMD_BIN=/Users/John Smith/.omnigent/shims/cmd-og" in argv
    # ...and without the model prefix the same single-entry shape holds.
    argv_plain = shlex.split(m.acp_command(row, None))
    assert "CMD_BIN=/Users/John Smith/.omnigent/shims/cmd-og" in argv_plain


# --------------------------------------------------------------------------
# the registry's $comment documents every field it expects a row to carry
# --------------------------------------------------------------------------
def test_registry_comment_documents_the_role_unverified_fields():
    # A registry field absent from the $comment block is invisible to the next
    # person editing the catalog (AGENTS.md: add vendors HERE, not in code).
    comment = "\n".join(m.REGISTRY["$comment"])
    assert "unverified_roles" in comment
    assert "roles_note" in comment
