"""Tests for installer/og_init.py — the orchestration contract generator.

Two failure modes are covered here, because both were silent:

  1. `never_write` with no entries used to render an unparseable YAML list,
     which made the WHOLE contract file fail to load — so every gate and
     protected branch was silently ignored.
  2. Stack detection used to look at the repo root only, so a nested project
     detected nothing at all — which is what emptied `never_write` and
     triggered (1). The two compound, so they are tested together.
"""
from __future__ import annotations

import json
import shlex
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
