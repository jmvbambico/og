"""Tests for installer/og_init.py — the orchestration contract generator.

Two failure modes are covered here, because both were silent:

  1. `never_write` with no entries used to render an unparseable YAML list,
     which made the WHOLE contract file fail to load — so every gate and
     protected branch was silently ignored.
  2. Stack detection used to look at the repo root only, so a nested project
     detected nothing at all — which is what emptied `never_write` and
     triggered (1). The two compound, so they are tested together.

The later sections cover the decision surface (`--questions`), the marked
AGENTS.md block, and the interactive question flow.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
import og_init  # noqa: E402

BRANCHES = {"integration_base": "main", "protected": [], "task_prefix": "feature/"}


def _render(repo: Path) -> str:
    """Run the real detection + gate + render pipeline over a temp repo."""
    stacks = og_init.detect_stacks(repo)
    gates, high_risk, never_write, notes = og_init.build_gates(repo, stacks)
    return og_init.render(repo, BRANCHES, gates, high_risk, never_write, stacks, notes)


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _node_package(path: Path, name: str = "app") -> None:
    _write(path, json.dumps({"name": name, "scripts": {"build": "tsc", "test": "jest"}}))


def test_zero_stacks_renders_parseable_yaml():
    """Regression: the empty `never_write` fallback broke the whole file.

    `policies/omnigent_local_policies.py::_load_contract` swallows the parse
    error and returns None, so an unparseable contract means NO gate, NO
    protected branch and NO never_write rule — silently.
    """
    body = og_init.render(Path("/tmp/x"), BRANCHES, [], [], [], [], [])
    data = yaml.safe_load(body)
    assert data["never_write"] == [], "never_write must be an empty list, not None"
    assert data["gates"] is None or data["gates"] == []


def test_zero_stacks_contract_keeps_its_other_keys():
    body = og_init.render(Path("/tmp/x"), BRANCHES, [], [], [], [], [])
    data = yaml.safe_load(body)
    assert data["branches"]["integration_base"] == "main"
    assert data["merge_policy"]["enabled"] is False


def test_nested_repo_detects_both_stacks_with_prefixed_gates(tmp_path):
    repo = tmp_path / "mops"
    _write(repo / "console" / "src-tauri" / "Cargo.toml", "[package]\nname='x'\n")
    _node_package(repo / "console" / "package.json")

    body = _render(repo)
    data = yaml.safe_load(body)

    runs = [g["run"] for g in data["gates"]]
    assert any(r.startswith("cd -- console/src-tauri && ") for r in runs), runs
    assert any(r.startswith("cd -- console && ") for r in runs), runs
    # The Rust crate is NOT the repo root, so its gates must be prefixed.
    assert "cargo test" in " ".join(runs)
    assert not any(r == "cargo test" for r in runs)

    # never_write is prefixed too, and still parses.
    nw = data["never_write"]
    assert "console/src-tauri/target/**" in nw
    assert "console/node_modules/**" in nw


def test_root_marker_keeps_unprefixed_command(tmp_path):
    """No regression: a root-level marker must produce exactly today's command."""
    repo = tmp_path / "flat"
    _write(repo / "Cargo.toml", "[package]\nname='x'\n")

    stacks = og_init.detect_stacks(repo)
    assert [s["id"] for s in stacks] == ["rust"]
    assert stacks[0]["dir"] == ""

    gates, _, _, _ = og_init.build_gates(repo, stacks)
    runs = [g[1] for g in gates]
    assert "cargo test" in runs
    assert not any(r.startswith("cd ") for r in runs), runs

    data = yaml.safe_load(_render(repo))
    assert [g["run"] for g in data["gates"]] == runs


def test_skipped_directory_is_not_detected(tmp_path):
    """The walker must skip generated dirs AND still find a real nested one.

    With only skipped markers present, the OLD root-only implementation also
    returned [], so this test would have passed against the bug. The ordinary
    nested marker is what distinguishes the two implementations.
    """
    repo = tmp_path / "skips"
    _write(repo / "app" / "Cargo.toml", "[package]\n")
    _write(repo / "node_modules" / "pkg" / "Cargo.toml", "[package]\n")
    _write(repo / "target" / "Cargo.toml", "[package]\n")
    _write(repo / "vendor" / "Cargo.toml", "[package]\n")
    _write(repo / "dist" / "Cargo.toml", "[package]\n")
    _write(repo / "build" / "Cargo.toml", "[package]\n")
    _write(repo / ".venv" / "Cargo.toml", "[package]\n")
    _write(repo / "__pycache__" / "Cargo.toml", "[package]\n")

    found = og_init.detect_stacks(repo)
    assert [(s["id"], s["dir"]) for s in found] == [("rust", "app")]


def test_depth_beyond_bound_is_not_detected(tmp_path):
    repo = tmp_path / "deep"
    _write(repo / "a" / "b" / "c" / "d" / "Cargo.toml", "[package]\n")

    assert og_init.detect_stacks(repo) == []


def test_within_bound_is_detected(tmp_path):
    repo = tmp_path / "ok"
    _write(repo / "a" / "b" / "c" / "Cargo.toml", "[package]\n")

    stacks = og_init.detect_stacks(repo)
    assert [(s["id"], s["dir"]) for s in stacks] == [("rust", "a/b/c")]


def test_same_stack_in_two_dirs_yields_two_gates(tmp_path):
    repo = tmp_path / "multi"
    _write(repo / "one" / "Cargo.toml", "[package]\n")
    _write(repo / "two" / "Cargo.toml", "[package]\n")

    data = yaml.safe_load(_render(repo))
    runs = [g["run"] for g in data["gates"]]
    assert "cd -- one && cargo test" in runs
    assert "cd -- two && cargo test" in runs


def test_never_write_entries_do_not_repeat(tmp_path):
    repo = tmp_path / "dup"
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    _write(repo / "sub" / "pyproject.toml", "[project]\nname='y'\n")

    data = yaml.safe_load(_render(repo))
    nw = data["never_write"]
    assert len(nw) == len(set(nw))


@pytest.mark.parametrize("case", ["empty", "root", "nested", "skip", "deep"])
def test_everything_rendered_parses(tmp_path, case):
    repo = tmp_path / case
    if case == "root":
        _write(repo / "Cargo.toml", "[package]\n")
    elif case == "nested":
        _write(repo / "console" / "src-tauri" / "Cargo.toml", "[package]\n")
        _node_package(repo / "console" / "package.json")
    elif case == "skip":
        _write(repo / "app" / "Cargo.toml", "[package]\n")
        _write(repo / "node_modules" / "pkg" / "Cargo.toml", "[package]\n")
    elif case == "deep":
        _write(repo / "a" / "b" / "c" / "d" / "Cargo.toml", "[package]\n")

    body = _render(repo)
    data = yaml.safe_load(body)
    assert isinstance(data, dict)
    assert "gates" in data and "never_write" in data and "merge_policy" in data


# --- directory names are untrusted input -------------------------------------
#
# A directory name reaches the contract three ways: inside a shell command,
# inside a path glob, and inside a gate name. All three have to survive a
# name that is not a plain identifier.


def test_directory_with_space_is_quoted_for_the_shell(tmp_path):
    repo = tmp_path / "spaced"
    _write(repo / "console app" / "Cargo.toml", "[package]\n")

    body = _render(repo)
    data = yaml.safe_load(body)

    run = data["gates"][0]["run"]
    # The shell must see ONE directory argument, not two.
    assert shlex.split(run)[:3] == ["cd", "--", "console app"], run
    assert "cargo" in run


def test_directory_with_double_quote_renders_and_parses(tmp_path):
    repo = tmp_path / "quoted"
    _write(repo / 'we"ird' / "Cargo.toml", "[package]\n")

    body = _render(repo)
    data = yaml.safe_load(body)

    assert data["never_write"] == ['we"ird/target/**']
    run = data["gates"][0]["run"]
    assert shlex.split(run)[:3] == ["cd", "--", 'we"ird'], run
    assert "cargo" in run


def test_directory_with_newline_cannot_inject_yaml(tmp_path):
    repo = tmp_path / "newline"
    _write(repo / "evil\nnever_write: ['/etc']" / "Cargo.toml", "[package]\n")

    body = _render(repo)
    data = yaml.safe_load(body)

    assert data["never_write"] == ['evil\nnever_write: [\'/etc\']/target/**']
    assert "never_write" in data


def test_root_gate_name_is_unchanged(tmp_path):
    repo = tmp_path / "flat"
    _write(repo / "Cargo.toml", "[package]\n")

    data = yaml.safe_load(_render(repo))
    names = [g["name"] for g in data["gates"]]
    assert "unit" in names
    assert "build" in names
    assert not any(n.startswith("unit-") for n in names), names


def test_nested_gate_names_are_disambiguated(tmp_path):
    repo = tmp_path / "both"
    _node_package(repo / "package.json")
    _node_package(repo / "console" / "package.json")

    data = yaml.safe_load(_render(repo))
    names = [g["name"] for g in data["gates"]]

    # One name per command: the orchestrator quotes gates back by name.
    assert len(names) == len(set(names)), names
    assert "build" in names and "unit" in names
    assert "build-console" in names and "unit-console" in names


def test_nested_gate_name_slug_flattens_separators(tmp_path):
    repo = tmp_path / "deep"
    _write(repo / "console" / "src-tauri" / "Cargo.toml", "[package]\n")

    data = yaml.safe_load(_render(repo))
    names = [g["name"] for g in data["gates"]]
    assert "unit-console-src-tauri" in names, names


def test_render_refuses_to_write_invalid_yaml(monkeypatch):
    """The guard is the mechanism protecting everything above it."""
    def boom(_body):
        raise ValueError("simulated parse failure")
    monkeypatch.setattr(og_init.yaml, "safe_load", boom)

    with pytest.raises(SystemExit):
        og_init.render(Path("/tmp/x"), BRANCHES, [], [], [], [], [])


# --- a value must round-trip as the same STRING -----------------------------
#
# YAML 1.1 coerces bare words and numbers: `no` is False, `123` is an int,
# `2026-10-21` is a date. A contract whose `integration_base` is a boolean
# is as broken as one that does not parse — the merge gate compares branch
# names — so every value must come back out as the identical str.


COERCING_VALUES = ["no", "yes", "on", "off", "true", "null", "123", "1.5",
                    "0x1f", "2026-10-21", "main", "dev", "feature/", "y",
                    "N", "FALSE", "~", "1:30", ".inf", "1_000"]


@pytest.mark.parametrize("value", COERCING_VALUES)
def test_scalar_helper_round_trips_as_string(value):
    emitted = og_init._yaml_scalar(value)
    loaded = yaml.safe_load(f"k: {emitted}")["k"]
    assert loaded == value
    assert isinstance(loaded, str)


@pytest.mark.parametrize("value", COERCING_VALUES)
def test_render_keeps_values_as_strings(value):
    """The real render path, not just the helper."""
    branches = {"integration_base": value, "protected": [value],
                "task_prefix": value}
    body = og_init.render(Path("/tmp/x"), branches, [], [], [], [], [])
    data = yaml.safe_load(body)
    assert data["branches"]["integration_base"] == value
    assert data["branches"]["task_prefix"] == value
    assert data["branches"]["protected"] == [value]
    assert isinstance(data["branches"]["integration_base"], str)


def test_leading_dash_directory_uses_double_dash(tmp_path):
    """`cd -dash` would be read as an option by cd."""
    repo = tmp_path / "dash"
    _write(repo / "-dash" / "Cargo.toml", "[package]\n")

    data = yaml.safe_load(_render(repo))
    run = data["gates"][0]["run"]
    assert "--" in run.split()
    assert shlex.split(run)[:3] == ["cd", "--", "-dash"], run


def test_control_characters_do_not_reach_a_comment(tmp_path):
    """Comments get no quoting, so they must be stripped of control chars."""
    repo = tmp_path / "ctrl"
    _write(repo / "ev\x01il" / "Cargo.toml", "[package]\n")

    body = _render(repo)
    assert "\x01" not in body
    data = yaml.safe_load(body)
    assert "gates" in data


# --- the decision surface: `og init --questions` ---------------------------
#
# `install.sh --questions` hands an AI a schema instead of a transcript of
# interactive prompts. `og init` had no such surface, so an AI driving it had
# to scrape a terminal dialogue to learn the same facts.


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _git_repo(path: Path, extra_branches=()) -> Path:
    """A real git repo — `og init` refuses to run outside one."""
    path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"],
                   cwd=path, check=True, env=env)
    for b in extra_branches:
        subprocess.run(["git", "branch", b], cwd=path, check=False,
                       capture_output=True, env=env)
    return path


def _run_init(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["og init", *args])
    og_init.main()


def _no_prompt(monkeypatch):
    """Make any prompt a hard failure."""
    def boom(*a, **k):
        raise AssertionError("og init prompted, but this path must not ask")
    monkeypatch.setattr("builtins.input", boom)


def test_questions_emits_parseable_json_without_a_contract(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path / "fresh")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    _no_prompt(monkeypatch)

    _run_init(monkeypatch, "--questions", str(repo))

    payload = json.loads(capsys.readouterr().out)
    for key in ("detected", "current", "questions", "apply_with"):
        assert key in payload, payload.keys()
    assert payload["current"] is None
    assert payload["detected"]["stacks"][0]["id"] == "python"
    assert payload["detected"]["gates"], "the detected gates must be reported"
    assert payload["apply_with"].startswith("og init ")


def test_questions_reports_an_existing_contract_as_current(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path / "existing")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    _run_init(monkeypatch, str(repo))
    capsys.readouterr()

    _run_init(monkeypatch, "--questions", str(repo))
    payload = json.loads(capsys.readouterr().out)

    assert payload["current"] is not None
    assert payload["current"]["parses"] is True
    assert payload["current"]["contract"]["branches"]["integration_base"] == "main"


def test_questions_reports_a_contract_that_does_not_parse(tmp_path, monkeypatch, capsys):
    """A contract the loader cannot read is silently IGNORED, so say so."""
    repo = _git_repo(tmp_path / "broken")
    _write(repo / ".agents" / "orchestration.yaml", "branches: [oops\n")

    _run_init(monkeypatch, "--questions", str(repo))
    payload = json.loads(capsys.readouterr().out)

    assert payload["current"]["parses"] is False
    assert payload["current"]["contract"] is None
    assert payload["current"]["error"]


def test_questions_writes_nothing(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path / "readonly")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    before = sorted(p.name for p in repo.iterdir())

    _run_init(monkeypatch, "--questions", str(repo))
    capsys.readouterr()

    assert sorted(p.name for p in repo.iterdir()) == before
    assert not (repo / ".agents").exists()


def test_questions_offers_exactly_the_three_questions(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path / "q")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    _run_init(monkeypatch, "--questions", str(repo))

    payload = json.loads(capsys.readouterr().out)
    keys = [q["key"] for q in payload["questions"]]
    assert keys == ["branching_model", "gate_scopes", "gates"]
    for q in payload["questions"]:
        assert q["ask"], q


def test_questions_states_what_it_decided_itself(tmp_path, monkeypatch, capsys):
    """The values og must NOT ask about are reported, with the reason."""
    repo = _git_repo(tmp_path / "d")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    _run_init(monkeypatch, "--questions", str(repo))

    payload = json.loads(capsys.readouterr().out)
    decided = {d["key"]: d for d in payload["not_asked"]}
    assert decided["merge_policy.enabled"]["value"] is False
    assert "branches.task_prefix" in decided


# --- the marked AGENTS.md block ---------------------------------------------
#
# A repo with its own AGENTS.md used to print "left untouched" and never
# received the orchestration directives at all, even though the orchestrator
# reads AGENTS.md FIRST.


def _init_with_agents_md(tmp_path, monkeypatch, agents_text, *args):
    repo = _git_repo(tmp_path / "proj")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    if agents_text is not None:
        _write(repo / "AGENTS.md", agents_text)
    _no_prompt(monkeypatch)
    _run_init(monkeypatch, *args, str(repo))
    return repo


def test_block_is_appended_to_an_existing_agents_md(tmp_path, monkeypatch):
    repo = _init_with_agents_md(tmp_path, monkeypatch, "# Mine\n\nmine.\n")

    text = (repo / "AGENTS.md").read_text()
    assert text.startswith("# Mine\n\nmine.\n")
    assert text.count(og_init.BLOCK_BEGIN) == 1
    assert text.count(og_init.BLOCK_END) == 1
    assert "## Orchestration" in text
    assert ".agents/orchestration.yaml" in text


def test_block_is_idempotent(tmp_path, monkeypatch):
    """Re-running og init must UPDATE the block, never append a second one."""
    repo = _init_with_agents_md(tmp_path, monkeypatch, "# Mine\n\nmine.\n")
    first = (repo / "AGENTS.md").read_text()

    _run_init(monkeypatch, "--force", str(repo))
    second = (repo / "AGENTS.md").read_text()

    assert second.count(og_init.BLOCK_BEGIN) == 1
    assert second.count(og_init.BLOCK_END) == 1
    assert second == first, "the block must be replaced in place, not appended"


def test_block_leaves_the_rest_of_the_file_byte_identical(tmp_path, monkeypatch):
    original = "# Mine\n\nkeep this exactly.\n\n## Notes\n\nand this.\n"
    repo = _init_with_agents_md(tmp_path, monkeypatch, original)

    _run_init(monkeypatch, "--force", str(repo))
    text = (repo / "AGENTS.md").read_text()

    before = text.split(og_init.BLOCK_BEGIN)[0]
    after = text.split(og_init.BLOCK_END)[1]
    assert before == original
    # Nothing follows the block except its own line terminator.
    assert after == "\n"


def test_block_update_keeps_the_user_text_and_refreshes_the_block(tmp_path, monkeypatch):
    repo = _init_with_agents_md(tmp_path, monkeypatch, "# Mine\n\nmine.\n")
    (repo / "AGENTS.md").write_text(
        (repo / "AGENTS.md").read_text() + "\nuser added this later.\n")

    _run_init(monkeypatch, "--force", str(repo))
    text = (repo / "AGENTS.md").read_text()

    assert text.count(og_init.BLOCK_BEGIN) == 1
    assert "user added this later." in text


def test_stub_is_written_when_there_is_no_agents_md(tmp_path, monkeypatch):
    repo = _init_with_agents_md(tmp_path, monkeypatch, None)
    text = (repo / "AGENTS.md").read_text()
    assert "Trigger Phrases" in text
    assert og_init.BLOCK_BEGIN not in text


def test_no_agents_md_suppresses_both_the_stub_and_the_block(tmp_path, monkeypatch):
    repo = _init_with_agents_md(tmp_path, monkeypatch, "# Mine\n\nmine.\n",
                                "--no-agents-md")
    text = (repo / "AGENTS.md").read_text()
    assert og_init.BLOCK_BEGIN not in text
    assert text == "# Mine\n\nmine.\n"

    repo2 = _init_with_agents_md(tmp_path / "second", monkeypatch, None,
                                 "--no-agents-md")
    assert not (repo2 / "AGENTS.md").exists()


# --- the question flow -----------------------------------------------------


def test_no_tty_asks_nothing_and_writes_the_detected_contract(tmp_path, monkeypatch):
    """Non-interactive must keep today's behaviour exactly."""
    repo = _git_repo(tmp_path / "quiet")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    _no_prompt(monkeypatch)

    _run_init(monkeypatch, str(repo))

    stacks = og_init.detect_stacks(repo)
    gates, high_risk, never_write, notes = og_init.build_gates(repo, stacks)
    branches = og_init.detect_branches(repo)
    expected = og_init.render(repo, branches, gates, high_risk, never_write,
                              stacks, notes)
    assert (repo / ".agents" / "orchestration.yaml").read_text() == expected


def test_branching_detection_dev_proposes_gitflow(tmp_path):
    repo = _git_repo(tmp_path / "flow", extra_branches=["dev"])
    assert og_init.detect_branching_model(og_init.detect_branch_names(repo)) == "gitflow"


def test_branching_detection_only_main_proposes_github_flow(tmp_path):
    repo = _git_repo(tmp_path / "hub")
    assert og_init.detect_branching_model(og_init.detect_branch_names(repo)) == "github-flow"


def test_branching_detection_is_undecided_on_an_odd_layout(tmp_path):
    repo = _git_repo(tmp_path / "odd", extra_branches=["release/1"])
    assert og_init.detect_branching_model(og_init.detect_branch_names(repo)) is None


def test_questions_report_the_detected_branching_model(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path / "flow", extra_branches=["dev"])
    _run_init(monkeypatch, "--questions", str(repo))

    payload = json.loads(capsys.readouterr().out)
    q = payload["questions"][0]
    assert q["default"] == "gitflow"
    assert q["detected"] is True
    assert payload["detected"]["branches"]["branching_model"] == "gitflow"


def test_flow_does_not_ask_the_branching_model_when_it_is_detected():
    asked = []

    def ask(prompt, default=""):
        asked.append(prompt)
        return default

    answers = og_init.run_flow([("unit", "pytest", "worker")],
                               {"integration_base": "main", "protected": [],
                                "task_prefix": "feature/"},
                               {"main", "dev"}, ask=ask)
    assert answers["branching_model"] == "gitflow"
    assert len(asked) == 2, "only the scope and the gate confirmation remain"


def test_flow_asks_the_branching_model_when_the_layout_is_unclear():
    asked = []

    def ask(prompt, default=""):
        asked.append(prompt)
        return default

    answers = og_init.run_flow([], {"integration_base": "main", "protected": [],
                                    "task_prefix": "feature/"},
                               {"main", "release/1"}, ask=ask)
    assert answers["branching_model"] == "github-flow"
    assert any("Branching model" in p for p in asked)


def test_flow_marks_a_shared_gate_integration_only():
    def ask(prompt, default=""):
        if "shared database" in prompt and "e2e" in prompt:
            return "y"
        return default

    gates = [("unit", "pytest", "worker"), ("e2e", "docker compose up", None)]
    answers = og_init.run_flow(gates, {"integration_base": "main", "protected": [],
                                       "task_prefix": "feature/"},
                               {"main"}, ask=ask)
    by_name = {g["name"]: g for g in answers["gates"]}
    assert by_name["unit"]["scope"] == "worker"
    assert by_name["e2e"]["scope"] is None, "a shared gate is integration-only"


def test_flow_can_delete_a_gate():
    def ask(prompt, default=""):
        if "Which should be removed" in prompt:
            return "2"
        return default

    gates = [("unit", "pytest", "worker"), ("e2e", "npm run e2e", "worker")]
    answers = og_init.run_flow(gates, {"integration_base": "main", "protected": [],
                                       "task_prefix": "feature/"},
                               {"main"}, ask=ask)
    assert [g["name"] for g in answers["gates"]] == ["unit"]


def test_answers_move_a_gate_to_integration_only():
    base = [("unit", "pytest", "worker"), ("e2e", "npm run e2e", "worker")]
    branches, gates = og_init.apply_answers(
        {"gate_scopes": {"e2e": "integration"}},
        {"integration_base": "main", "protected": [], "task_prefix": "feature/"},
        base, {"main"})
    assert ("e2e", "npm run e2e", None) in gates
    assert ("unit", "pytest", "worker") in gates


def test_answers_set_the_protected_branches_for_the_model():
    names = {"main", "dev", "release/1"}
    branches, _ = og_init.apply_answers(
        {"branching_model": "gitflow"},
        {"integration_base": "dev", "protected": [], "task_prefix": "feature/"},
        [], names)
    assert branches["integration_base"] == "dev"
    assert "main" in branches["protected"]
    assert "release/1" in branches["protected"]


def test_plan_file_applies_without_asking(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "planned")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    plan = tmp_path / "answers.json"
    plan.write_text(json.dumps({"branching_model": "github-flow",
                                "gate_scopes": {"unit": "integration"}}))
    _no_prompt(monkeypatch)

    _run_init(monkeypatch, "--plan", str(plan), str(repo))

    data = yaml.safe_load((repo / ".agents" / "orchestration.yaml").read_text())
    unit = next(g for g in data["gates"] if g["name"] == "unit")
    assert "scope" not in unit, "an integration-only gate carries no scope"


def test_no_prompt_uses_the_defaults_even_on_a_tty(tmp_path, monkeypatch):
    """`--no-prompt` is what automation on a TTY should pass."""
    repo = _git_repo(tmp_path / "auto")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    monkeypatch.setattr(og_init, "interactive", lambda: True)
    _no_prompt(monkeypatch)

    _run_init(monkeypatch, "--no-prompt", str(repo))

    assert (repo / ".agents" / "orchestration.yaml").exists()


def test_a_tty_asks_the_questions(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "tty")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    monkeypatch.setattr(og_init, "interactive", lambda: True)
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", fake_input)

    _run_init(monkeypatch, str(repo))

    assert asked, "the flow must ask on a TTY"
    data = yaml.safe_load((repo / ".agents" / "orchestration.yaml").read_text())
    assert data["gates"]


def test_roster_reports_a_single_vendor_roster(tmp_path, monkeypatch):
    """`require_cross_vendor_review` is unenforceable on a one-vendor roster."""
    home = tmp_path / "omnigent"
    _write(home / "og-install.json", json.dumps({
        "coders": [{"id": "freebuff", "model": "deepseek/deepseek-v4-flash"}],
        "reviewer": [{"id": "opencode", "model": "opencode/deepseek-v4.1-flash-free"}],
    }))
    monkeypatch.setenv("OMNIGENT_HOME", str(home))

    roster = og_init.read_roster()
    assert roster is not None
    assert roster["vendors"] == ["deepseek"]
    assert roster["cross_vendor_review_possible"] is False


def test_roster_reports_a_cross_vendor_roster(tmp_path, monkeypatch):
    home = tmp_path / "omnigent"
    _write(home / "og-install.json", json.dumps({
        "coders": [{"id": "freebuff", "model": "deepseek/deepseek-v4-flash"}],
        "reviewer": [{"id": "codex", "model": "gpt-6-luna"}],
    }))
    monkeypatch.setenv("OMNIGENT_HOME", str(home))

    roster = og_init.read_roster()
    assert roster["cross_vendor_review_possible"] is True
    assert roster["vendors"] == ["deepseek", "openai"]


def test_questions_warn_when_a_cross_vendor_review_is_impossible(tmp_path, monkeypatch, capsys):
    home = tmp_path / "omnigent"
    _write(home / "og-install.json", json.dumps({
        "coders": [{"id": "freebuff", "model": "deepseek/deepseek-v4-flash"}],
        "reviewer": [{"id": "opencode", "model": "opencode/deepseek-v4.1-flash-free"}],
    }))
    monkeypatch.setenv("OMNIGENT_HOME", str(home))
    repo = _git_repo(tmp_path / "one")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")

    _run_init(monkeypatch, "--questions", str(repo))
    payload = json.loads(capsys.readouterr().out)

    assert payload["roster"]["cross_vendor_review_possible"] is False
    assert any("cross_vendor_review" in w for w in payload["warnings"])


# --- round 2: the marker boundary is a promise about someone else's file ---
#
# Two ways to break that promise, both real:
#
#   1. `Path.read_text()` translates CRLF to LF, so a CRLF-authored
#      AGENTS.md came back with every line ending rewritten — a whole-file
#      diff in a file og was only asked to add a block to.
#   2. A marker pair inside a fenced code block is an EXAMPLE of this
#      feature, not og's block. Replacing it ate the example and left the
#      fence unterminated.


BLOCK = og_init.orchestration_block({"integration_base": "main", "protected": []})


def test_crlf_file_keeps_its_line_endings(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_bytes(b"# Mine\r\n\r\nUser text here.\r\n")

    og_init.write_agents_block(p, BLOCK)
    after = p.read_bytes()

    assert b"\r\n" in after
    assert b"User text here.\r\n" in after
    assert b"\n\n" not in after.replace(b"\r\n", b"~"), "no bare LF was introduced"


def test_crlf_user_bytes_are_byte_identical_after_one_and_two_runs(tmp_path):
    original = b"# Mine\r\n\r\nUser text here.\r\n\r\nMore prose.\r\n"
    p = tmp_path / "AGENTS.md"
    p.write_bytes(original)

    og_init.write_agents_block(p, BLOCK)
    once = p.read_bytes()
    og_init.write_agents_block(p, BLOCK)
    twice = p.read_bytes()

    assert once == twice, "a rerun must be a no-op"
    # Every byte the user wrote, unchanged.
    for line in original.split(b"\r\n"):
        if line:
            assert line + b"\r\n" in once
    # The block went in with the file's own ending.
    assert b"og:orchestration:begin -->\r\n" in once
    assert b"og:orchestration:end -->\r\n" in once


def test_mixed_line_endings_are_not_normalised(tmp_path):
    """og must not "fix" a user's mixed file."""
    original = b"# Mine\r\n\nUser text here.\n\r\nMore prose.\n"
    p = tmp_path / "AGENTS.md"
    p.write_bytes(original)

    og_init.write_agents_block(p, BLOCK)
    after = p.read_bytes()

    assert after.startswith(b"# Mine\r\n\nUser text here.\n\r\nMore prose.\n")
    # The inserted block follows the DOMINANT ending, which here is LF.
    assert b"og:orchestration:begin -->\n" in after


def test_fenced_example_is_left_byte_identical():
    doc = ("# Mine\n\n```\n" + og_init.BLOCK_BEGIN + "\nexample content\n"
           + og_init.BLOCK_END + "\n```\n\nMORE USER TEXT\n")
    out = og_init.upsert_agents_block(doc, BLOCK)

    assert "example content" in out
    assert out.count("```") == 2, "the fence must not be left dangling"
    assert "MORE USER TEXT" in out
    # og's own block was appended, and it is the block that carries the
    # signature.
    assert og_init.BLOCK_SIGNATURE in out
    assert out.count(og_init.BLOCK_BEGIN) == 2, "the example and og's block"


def test_fenced_example_survives_a_rerun():
    doc = ("# Mine\n\n```\n" + og_init.BLOCK_BEGIN + "\nexample content\n"
           + og_init.BLOCK_END + "\n```\n\nMORE USER TEXT\n")
    once = og_init.upsert_agents_block(doc, BLOCK)
    twice = og_init.upsert_agents_block(once, BLOCK)

    assert twice == once
    assert "example content" in twice
    assert twice.count("```") == 2


def test_two_marker_pairs_in_one_file():
    """A quoted pair AND og's own block: only og's is replaced."""
    quoted = ("# Mine\n\n```\n" + og_init.BLOCK_BEGIN + "\nexample content\n"
              + og_init.BLOCK_END + "\n```\n")
    with_block = og_init.upsert_agents_block(quoted, BLOCK)
    # A second, unfenced, quoted pair AFTER og's block.
    doc = (with_block + "\nUNFENCED QUOTE\n" + og_init.BLOCK_BEGIN + "\n"
           + "user text\n" + og_init.BLOCK_END + "\n")
    out = og_init.upsert_agents_block(doc, BLOCK)

    assert "user text" in out, "an unsigned pair is not og's block"
    assert "example content" in out
    assert out.count(og_init.BLOCK_SIGNATURE) == 1


def test_orphan_begin_marker_without_a_signature_is_not_repaired():
    """A user's half-written marker pair must not lose their text."""
    doc = ("# Mine\n\n" + og_init.BLOCK_BEGIN + "\nuser text\n")
    out = og_init.upsert_agents_block(doc, BLOCK)

    assert "user text" in out
    assert og_init.BLOCK_SIGNATURE in out, "og's block is appended alongside"


def test_orphan_begin_marker_with_the_signature_is_repaired():
    """og's own truncated block is replaced, not duplicated."""
    doc = ("# Mine\n\n" + og_init.BLOCK_BEGIN + "\n" + og_init.BLOCK_SIGNATURE
           + "\n\n## Orchestration\n\nhalf a block\n")
    out = og_init.upsert_agents_block(doc, BLOCK)

    assert "half a block" not in out
    assert out.count(og_init.BLOCK_SIGNATURE) == 1
    assert og_init.upsert_agents_block(out, BLOCK) == out


def test_indented_marker_is_not_og_s_block():
    """An indented marker is an example, not a block to rewrite."""
    doc = "# Mine\n\n    " + og_init.BLOCK_BEGIN + "\n    user text\n"
    out = og_init.upsert_agents_block(doc, BLOCK)

    assert "user text" in out
    assert og_init.BLOCK_SIGNATURE in out


def test_crlf_run_through_main(tmp_path, monkeypatch):
    """The end-to-end path must not normalise the file either."""
    repo = _git_repo(tmp_path / "crlf")
    _write(repo / "pyproject.toml", "[project]\nname='x'\n")
    (repo / "AGENTS.md").write_bytes(b"# Mine\r\n\r\nUser text here.\r\n")
    _no_prompt(monkeypatch)

    _run_init(monkeypatch, str(repo))

    after = (repo / "AGENTS.md").read_bytes()
    assert b"User text here.\r\n" in after
    assert og_init.BLOCK_SIGNATURE.encode() in after
    assert b"og:orchestration:end -->\r\n" in after


# --- the gate confirmation default ------------------------------------------
#
# "Default to fewer" applies where the uncertainty actually is: a gate og
# detected and can vouch for is kept, a gate og flagged as probably wrong
# is removed unless the user says otherwise.


def test_a_legacy_block_without_the_signature_is_still_replaced():
    """A block written by an og from before the signature existed."""
    legacy = ("# Mine\n\n" + og_init.BLOCK_BEGIN + "\n\n## Orchestration\n\n"
              "old body\n" + og_init.BLOCK_END + "\n")
    out = og_init.upsert_agents_block(legacy, BLOCK)

    assert "old body" not in out
    assert out.count(og_init.BLOCK_BEGIN) == 1
    assert og_init.BLOCK_SIGNATURE in out
    assert og_init.upsert_agents_block(out, BLOCK) == out


def test_an_unfenced_unsigned_pair_is_not_replaced():
    """A quoted pair in prose is the user's text, not og's block."""
    doc = ("# Mine\n\n" + og_init.BLOCK_BEGIN + "\nthe user's own note\n"
           + og_init.BLOCK_END + "\n")
    out = og_init.upsert_agents_block(doc, BLOCK)

    assert "the user's own note" in out
    assert og_init.BLOCK_SIGNATURE in out, "og's block is appended, not merged"
    assert out.count(og_init.BLOCK_BEGIN) == 2
