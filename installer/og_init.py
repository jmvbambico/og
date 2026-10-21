#!/usr/bin/env python3
"""og init — scaffold a repo's orchestration contract.

The agent bundle is global and project-agnostic; every project fact comes from
the repo itself. This writes the two files the orchestrator reads:

  .agents/orchestration.yaml   machine-readable: branches, gates, never_write,
                               merge policy
  AGENTS.md                    the human-readable constitution (only a stub —
                               it is yours to write)

Nothing here is copied from another project. The schema is fixed; every value
is either detected from this repo or left as an explicit TODO, because a gate
command guessed wrong is worse than one absent: the orchestrator quotes the
contract back and would confidently run the wrong thing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import yaml
from pathlib import Path

C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[0;32m", "y": "\033[0;33m",
     "r": "\033[0;31m", "c": "\033[0;36m", "x": "\033[0m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = dict.fromkeys(C, "")


def ok(m): print(f"{C['g']}✓{C['x']} {m}")
def info(m): print(f"{C['c']}→{C['x']} {m}")
def warn(m): print(f"{C['y']}!{C['x']} {m}")
def die(m): print(f"{C['r']}✗{C['x']} {m}", file=sys.stderr); raise SystemExit(1)


def run(args, cwd=None):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# --------------------------------------------------------------------------
# Toolchain detection.
#
# `marker` decides whether a stack is present. `gates` are the commands a
# worker or the integration branch runs. `scope: worker` means every
# implementer runs it in its own worktree; anything without a scope runs ONCE
# on the integration branch — which is how a suite that shares a database or a
# port avoids being run concurrently by parallel workers.
# --------------------------------------------------------------------------
STACKS = [
    {
        "id": "go", "label": "Go", "marker": "go.mod",
        "gates": [("vet", "go vet ./...", "worker"),
                  ("build", "go build ./...", "worker"),
                  ("unit", "go test -count=1 -failfast ./...", "worker")],
        "optional": [("lint", "golangci-lint run ./... --timeout=2m", None, "golangci-lint"),
                     ("security", "gosec -quiet ./...", None, "gosec")],
        "high_risk": ["go.mod", "go.sum"],
        "never_write": ["vendor/**"],
    },
    {
        "id": "node", "label": "Node / TypeScript", "marker": "package.json",
        "gates": [("build", "{pm} run build", "worker"),
                  ("unit", "{pm} test", "worker")],
        "optional": [("lint", "{pm} run lint", None, None),
                     ("typecheck", "{pm} run typecheck", None, None)],
        "high_risk": ["package.json", "{lockfile}"],
        "never_write": ["node_modules/**", "dist/**", "build/**"],
    },
    {
        "id": "python", "label": "Python", "marker": "pyproject.toml",
        "gates": [("unit", "pytest -x -q", "worker")],
        "optional": [("lint", "ruff check .", None, "ruff"),
                     ("typecheck", "mypy .", None, "mypy")],
        "high_risk": ["pyproject.toml", "poetry.lock", "requirements.txt"],
        "never_write": [".venv/**", "**/__pycache__/**"],
    },
    {
        "id": "rust", "label": "Rust", "marker": "Cargo.toml",
        "gates": [("build", "cargo build", "worker"),
                  ("unit", "cargo test", "worker")],
        "optional": [("lint", "cargo clippy -- -D warnings", None, "cargo")],
        "high_risk": ["Cargo.toml", "Cargo.lock"],
        "never_write": ["target/**"],
    },
]

# Paths that are high-risk in ANY repo, for reasons that do not depend on the
# stack. The last two matter most: they are the agent's own gate, and an agent
# must never be able to weaken the rules it is judged by.
UNIVERSAL_HIGH_RISK = [
    (".github/workflows/**", "CI definition"),
    ("**/migrations/**", "schema changes are expensive to reverse"),
    ("AGENTS.md", "the constitution itself"),
    (".agents/**", "an agent must never weaken its own gate"),
]


# Directories that never contain a project we want to gate, and that are large
# enough to make the walk expensive. `build`/`dist` are here because they are
# generated output dirs (they are already in the Node stack's never_write);
# `.venv`/`__pycache__` are the Python equivalents. `vendor` is Go's vendored
# dependencies, not a project of its own.
SKIP_DIRS = {"node_modules", "target", ".git", "vendor", "dist", "build",
             ".venv", "__pycache__"}

# How far below the repo root a marker may live. Monorepos and nested
# layouts (a Rust crate under `console/src-tauri/`) are the common case that
# a root-only search misses entirely.
MAX_MARKER_DEPTH = 3


def _walk_project_dirs(repo: Path, max_depth: int = MAX_MARKER_DEPTH):
    """Yield (dir, relative_prefix) for the repo root and its subdirectories.

    Hidden directories are skipped wholesale (that covers `.git`, `.github`,
    `.agents` and `.venv`); symlinked directories are skipped too, so the walk
    cannot loop on a cycle.
    """
    yield repo, ""
    frontier = [(repo, "", 0)]
    while frontier:
        cur, rel, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        try:
            entries = sorted(cur.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for e in entries:
            if e.is_symlink() or not e.is_dir():
                continue
            if e.name in SKIP_DIRS or e.name.startswith("."):
                continue
            child_rel = f"{rel}/{e.name}" if rel else e.name
            yield e, child_rel
            frontier.append((e, child_rel, depth + 1))


def detect_stacks(repo: Path) -> list:
    """Find every stack marker in the repo, at any depth up to the bound.

    A root-only search was the root cause of the empty-contract bug: a nested
    project (`console/src-tauri/Cargo.toml`) detected no stack at all, which
    left `never_write` empty, which made `render()` emit the invalid fallback
    below. The two failures compound, so they are fixed together.

    Each returned dict is the stack definition plus:
      `dir`  the repo-relative directory the marker was found in ("" = root)
      `root` the absolute directory the marker was found in
    """
    found = []
    for base, rel in _walk_project_dirs(repo):
        for s in STACKS:
            if (base / s["marker"]).exists():
                item = dict(s)
                item["dir"] = rel
                item["root"] = base
                found.append(item)
    found.sort(key=lambda s: (0 if not s["dir"] else 1, s["dir"], s["id"]))
    return found


def _stack_label(st: dict) -> str:
    """Human label for a stack, disambiguated by directory when nested."""
    d = st.get("dir") or ""
    return f'{st["label"]} ({d})' if d else st["label"]


# A directory name read from the filesystem is now an untrusted input channel
# into the contract: it ends up in a shell command, in a path glob, and in a
# gate name. Any of those can break the contract in a way nobody notices.
# The helpers below are the two places where that input is made safe.
#
# Escaped scalars are emitted as JSON double-quoted strings: JSON strings are
# a strict subset of YAML's double-quoted scalar syntax (same escapes, same
# quoting), and `json.dumps` is stdlib, so this costs no dependency.


def _yaml_scalar(value: str) -> str:
    """Render a string as a YAML scalar, quoted unless the plain form is exact.

    The plain form is kept only when it loads back as THE SAME STRING, and
    that is checked by handing it to the YAML parser rather than by a
    hand-rolled character class. PyYAML's YAML 1.1 resolver coerces bare
    words and numbers: `no` loads as False, `2026-10-21` as a date, `123` as
    an int. A contract whose `integration_base` is a boolean is as broken as
    one that does not parse — the merge gate compares branch names — and a
    regex that tries to enumerate the resolver's cases will eventually
    disagree with it. Verifying against the parser cannot drift.
    """
    try:
        loaded = yaml.safe_load(f"k: {value}")
    except Exception:
        loaded = None
    if (isinstance(loaded, dict) and isinstance(loaded.get("k"), str)
            and loaded["k"] == value):
        return value
    return json.dumps(value)


def _yaml_quoted(value: str) -> str:
    """Render a string as a YAML double-quoted scalar, always quoted."""
    return json.dumps(value)


def _gate_slug(rel: str) -> str:
    """Turn a repo-relative directory into a readable name suffix.

    `console/src-tauri` -> `console-src-tauri`; `console app` -> `console-app`.
    """
    return re.sub(r"[^A-Za-z0-9]+", "-", rel).strip("-")


# C0 control characters and DEL have no business in a YAML comment, and some
# of them make the parser reject the whole document.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _one_line(value) -> str:
    """Flatten a value so it cannot break out of a YAML comment.

    A directory named `x\\nnever_write:` would otherwise inject a line of
    YAML into the middle of the generated comment block. Comments are the one
    place in the file with no quoting, so the value is flattened and stripped
    of control characters instead.
    """
    flat = " ".join(str(value).splitlines()).strip()
    return _CONTROL_CHARS.sub("", flat)


def node_pm(repo: Path) -> tuple:
    for lock, pm in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                     ("bun.lockb", "bun"), ("package-lock.json", "npm")):
        if (repo / lock).exists():
            return pm, lock
    return "npm", "package-lock.json"


def node_scripts(repo: Path) -> set:
    import json
    try:
        return set((json.loads((repo / "package.json").read_text()).get("scripts") or {}))
    except Exception:
        return set()


def detect_branch_names(repo: Path) -> set:
    """Every branch name in the repo, local and remote-tracking.

    Kept separate from `detect_branches` because the branching MODEL is a
    different question from the branch layout: the layout is what the contract
    needs, the model is what the user has to confirm when the layout does not
    answer it.
    """
    listing = run(["git", "for-each-ref", "--format=%(refname:short)",
                   "refs/heads", "refs/remotes/origin"], repo) or ""
    names = set()
    for b in listing.splitlines():
        b = b.strip()
        if not b:
            continue
        names.add(b.split("/", 1)[-1] if b.startswith("origin/") else b)
    return names


def detect_branches(repo: Path) -> dict:
    """Read the real branch layout instead of assuming gitflow."""
    out = {"integration_base": None, "protected": [], "task_prefix": "feature/"}
    head = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo)
    default = head.rsplit("/", 1)[-1] if head else None
    branches = detect_branch_names(repo)
    if not default:
        default = "main" if "main" in branches else ("master" if "master" in branches else None)

    # A `dev`/`develop` branch alongside the default branch is the signal for a
    # gitflow-style repo, where integration happens off dev and the default
    # branch is a release target. Otherwise integrate straight onto the default.
    integ = next((b for b in ("dev", "develop") if b in branches), None)
    out["integration_base"] = integ or default
    out["protected"] = sorted({b for b in (default, "staging", "production", "release")
                               if b and b in branches and b != out["integration_base"]})
    if default and default not in out["protected"] and default != out["integration_base"]:
        out["protected"].append(default)
    prefixes = [b.split("/")[0] + "/" for b in branches if "/" in b
                and b.split("/")[0] in ("feature", "feat", "bugfix", "fix", "chore")]
    if prefixes:
        out["task_prefix"] = max(set(prefixes), key=prefixes.count)
    return out


def build_gates(repo: Path, stacks: list) -> tuple:
    gates, high_risk, never_write, notes = [], [], [], []
    used_names = set()

    def gate_name(base: str, slug: str) -> str:
        """A gate name that identifies exactly one command.

        A nested gate is named after its directory (`unit-console`), because
        the orchestrator quotes gates back by name and two `unit` gates are
        ambiguous. A root-level gate keeps its original name.
        """
        if not slug:
            used_names.add(base)
            return base
        candidate = f"{base}-{slug}"
        n = 2
        while candidate in used_names:
            candidate = f"{base}-{slug}-{n}"
            n += 1
        used_names.add(candidate)
        return candidate

    for st in stacks:
        # A stack found in a subdirectory runs its commands THERE, so the
        # emitted command is prefixed with a `cd` into that directory. A
        # root-level stack keeps the exact command it has always had — no
        # `cd ./ &&` prefix.
        rel = st.get("dir") or ""
        base = st.get("root") or repo
        # The directory name comes from the filesystem, so it is quoted for the
        # shell: `cd console app && ...` would be parsed as `cd` with two
        # arguments and fail at gate-run time — silently, because the
        # contract itself still parses. The `--` separator is there for the
        # same reason: a directory named `-dash` would otherwise be read by
        # `cd` as an option. Do not drop it.
        prefix = f"cd -- {shlex.quote(rel)} && " if rel else ""
        # A gate name must identify ONE command, because the orchestrator
        # quotes gates back by name. Two directories each with a `unit` gate
        # would be ambiguous, so a nested gate is named after its directory.
        # A root-level gate keeps its original name (`unit` stays `unit`).
        slug = _gate_slug(rel) if rel else ""
        if rel and not slug:
            # A directory made only of characters the slug drops (`!!!`)
            # still needs a suffix, or its gate would collide with the
            # root-level one.
            slug = "dir"
        subs = {}
        if st["id"] == "node":
            pm, lock = node_pm(base)
            subs = {"pm": pm, "lockfile": lock}
            scripts = node_scripts(base)
            # A package.json carrying neither `build` nor `test` is tooling
            # (a linter, a hook, a docs site), not an application stack.
            # Emitting `npm test` for it would hand the orchestrator a gate
            # that always fails, which is worse than having no gate.
            if not ({"build", "test"} & scripts):
                notes.append("package.json has no build/test script — treated as "
                             "tooling, not a stack; no Node gates emitted")
                continue
        for name, cmd, scope in st["gates"]:
            cmd = cmd.format(**subs) if subs else cmd
            if st["id"] == "node" and name in ("build", "unit"):
                key = "build" if name == "build" else "test"
                if key not in scripts:
                    notes.append(f"package.json has no `{key}` script — "
                                 f"`{prefix}{cmd}` will fail")
            gates.append((gate_name(name, slug), prefix + cmd, scope))
        for name, cmd, scope, binary in st["optional"]:
            cmd = cmd.format(**subs) if subs else cmd
            if binary and not _which(binary):
                notes.append(f"{binary} not installed — `{name}` gate omitted")
                continue
            if st["id"] == "node" and name in ("lint", "typecheck") and name not in scripts:
                continue
            gates.append((gate_name(name, slug), prefix + cmd, scope))
        for h in st["high_risk"]:
            h = h.format(**subs) if subs else h
            high_risk.append(f"{rel}/{h}" if rel else h)
        for nw in st["never_write"]:
            never_write.append(f"{rel}/{nw}" if rel else nw)
    # A suite that talks to a database or binds a port must run ONCE on the
    # integration branch, never per-worker. It cannot be detected reliably, but
    # a conventionally-named directory is a strong enough hint to ask about.
    for d in ("tests/integration", "test/integration", "integration_tests",
              "tests/e2e", "e2e"):
        if (repo / d).is_dir():
            notes.append(
                f"{d}/ looks like a shared-state suite, but the worker-scoped gate "
                f"above runs the whole tree. Split it: narrow the `unit` gate to the "
                f"unit packages, and add a separate no-scope gate for {d}/ so it runs "
                f"once on the integration branch instead of in every worktree.")
            break
    # Two stacks can describe the same command (the same marker in two
    # directories). Keep one entry per distinct command, but never collapse
    # different directories into one gate — each project needs its own run.
    seen = set()
    deduped = []
    for g in gates:
        if g in seen:
            continue
        seen.add(g)
        deduped.append(g)
    return deduped, list(dict.fromkeys(high_risk)), list(dict.fromkeys(never_write)), notes


def _which(b):
    from shutil import which
    return which(b)


def render(repo: Path, branches: dict, gates: list, high_risk: list,
           never_write: list, stacks: list, notes: list) -> str:
    name = repo.name
    stack_label = " + ".join(_stack_label(s) for s in stacks) or "unknown"
    L = []
    L.append(f"# Machine-readable orchestration contract for {_one_line(name)}.")
    L.append("# Consumed by the global og orchestrator agent. AGENTS.md remains the")
    L.append("# human-readable constitution and takes precedence on anything this file")
    L.append("# does not cover.")
    L.append("#")
    L.append(f"# Generated by `og init` from a {_one_line(stack_label)} repo. Every value below was")
    L.append("# detected or left as a TODO — nothing is inherited from another project.")
    L.append("# Review it: a wrong gate command is worse than a missing one, because the")
    L.append("# orchestrator quotes this contract back and runs exactly what it says.")
    L.append("")
    L.append("branches:")
    ib = branches["integration_base"]
    L.append(f"  # Where finished work lands. Workers branch from here and the batched")
    L.append(f"  # integration branch merges back into it.")
    L.append(f"  integration_base: {_yaml_scalar(ib or 'TODO')}")
    L.append("  # Never merged into by an agent; promotions are a human's job.")
    if branches["protected"]:
        # A flow sequence has its own rules — a comma or a bracket inside an
        # unquoted element splits it — so the elements are always quoted.
        L.append("  protected: ["
                 + ", ".join(_yaml_quoted(b) for b in branches["protected"]) + "]")
    else:
        # Falling back to [main] here would be actively wrong when main IS the
        # integration base: merge_gate DENYs merges into a protected branch, so
        # the contract would forbid the only merge it also mandates.
        L.append("  # No release branch found alongside the integration base, so nothing")
        L.append(f"  # here is protected and agents may merge into `{_one_line(ib)}` directly.")
        L.append("  # If that is not what you want, add a `dev` branch and make it the")
        L.append(f"  # integration_base, leaving `{_one_line(ib)}` protected below.")
        L.append("  protected: []")
    L.append(f"  task_prefix: {_yaml_scalar(branches['task_prefix'])}")
    L.append("")
    L.append("gates:")
    L.append("  # scope: worker  -> runs in EACH implementer's worktree")
    L.append("  # no scope       -> runs ONCE on the integration branch.")
    L.append("  #")
    L.append("  # Put anything that shares state — a database, a fixed port, a seeded")
    L.append("  # fixture — in the no-scope tier. Parallel workers running it")
    L.append("  # concurrently would corrupt each other.")
    if not gates:
        L.append("  # TODO: no toolchain was detected in this repo, so there are NO gates.")
        L.append("  # Until you add some, nothing verifies a worker's output and every")
        L.append("  # merge is unguarded. Add your real commands, e.g.")
        L.append("  # - {name: unit, run: make test, scope: worker}")
        L.append("  # - {name: e2e,  run: make e2e}          # no scope: integration only")
    for nm, cmd, scope in gates:
        L.append(f"  - name: {_yaml_scalar(nm)}")
        L.append(f"    run: {_yaml_scalar(cmd)}")
        if scope:
            L.append(f"    scope: {_yaml_scalar(scope)}")
    L.append("")
    L.append("never_write:")
    L.append("  # Paths an implementer must never touch.")
    if never_write:
        for p in dict.fromkeys(never_write):
            L.append(f"  - {_yaml_quoted(p)}")
    else:
        # The empty case must still parse. A key with only comments beneath
        # it parses as `null`, and the first consumer that iterates it gets a
        # TypeError; the `[]` makes it an empty list instead. Nothing reads
        # `never_write` today — it is advisory, consumed by the orchestrator
        # agent rather than by the merge-gate policy — so this is
        # pre-emptive, but it costs one line and removes a whole class of
        # failure.
        L.append('  # e.g. - "vendor/**"')
        L.append("  []")
    L.append("")
    L.append("merge_policy:")
    L.append("  # Enforced by the `merge_gate` policy, not by prompt text. ALLOW only when")
    L.append("  # every condition holds; DENY for protected branches and force-pushes; ASK")
    L.append("  # for everything else INCLUDING anything it cannot determine.")
    L.append("  #")
    L.append("  # enabled: false means every merge is a human decision. That is the safe")
    L.append("  # starting point — turn it on once you trust the gates on real PRs.")
    L.append("  enabled: false")
    L.append(f"  auto_merge_target: {_yaml_scalar(ib or 'TODO')}")
    L.append("  # Thresholds are a starting point; recalibrate from real PR history.")
    L.append("  max_diff_lines: 400")
    L.append("  max_diff_files: 15")
    L.append("  # Any path here forces the human gate regardless of gate results.")
    L.append("  high_risk_paths:")
    for p in dict.fromkeys(high_risk):
        L.append(f"    - {_yaml_quoted(p)}")
    for p, why in UNIVERSAL_HIGH_RISK:
        L.append(f"    - {_yaml_quoted(p)}{' ' * max(1, 26 - len(p))}# {why}")
    L.append("  require_no_test_count_decrease: true")
    L.append("  # The gate cannot observe review itself (that is session state, not repo")
    L.append("  # state), so it reads the PR body: `cross-vendor-review: passed` permits")
    L.append("  # auto-merge; `degraded-review` always forces the human gate.")
    L.append("  require_cross_vendor_review: true")
    if notes:
        L.append("")
        L.append("# og init could not verify these — check before relying on them:")
        for n in notes:
            L.append(f"#   - {_one_line(n)}")
    L.append("")
    body = "\n".join(L)
    # Mechanism over prompt: validate the contract we just generated instead
    # of trusting it. A single malformed byte here does not break one key —
    # it breaks the WHOLE file. `policies/omnigent_local_policies.py`
    # (`_load_contract`) catches the parse error and returns None, so every
    # gate, protected branch and never_write rule is silently ignored: the
    # user believes they have a contract, and they have none. Refusing loudly
    # is the only honest outcome.
    try:
        yaml.safe_load(body)
    except Exception as e:
        die(f"og init generated invalid YAML ({e}); this is an og bug, please report it")
    return body


AGENTS_STUB = """# {name} Constitution

The human-readable contract for anyone — person or agent — working in this
repo. This file is AUTHORITATIVE: where it and `.agents/orchestration.yaml`
disagree, this wins.

`og init` wrote this stub. Fill it in; the orchestrator reads it before
planning, and an empty section means it will ask you instead of guessing.

## Trigger Phrases

Short phrases that mean a specific workflow rather than a literal request.
Delete this table if you have none.

| Phrase | Action |
|--------|--------|
| _e.g. "new alignment"_ | _what should happen_ |

## Core Principles

What matters in this codebase and why. Be specific — "write tests" is not
actionable; "every handler needs an integration test that exercises the real
database" is.

## Development Workflow

### Branching

Detected: integration base `{integration_base}`, task branches `{task_prefix}<slug>`.
Protected: {protected}.

### Before a PR

What must be true before review. The machine-checkable parts belong in
`.agents/orchestration.yaml` under `gates`; describe the judgement calls here.

## File Placement Rules

Where new code goes, and what must not be touched.

## Human Checkpoints

What an agent must stop and ask about rather than decide. Anything
irreversible, anything that claims something about reality (a status, a
release), and anything touching money or customer data.
"""


# --------------------------------------------------------------------------
# The decision schema (`--questions`) and the interactive flow.
#
# `install.sh --questions` hands an AI a machine-readable schema instead of a
# transcript of interactive prompts. `og init` had no such surface, so an AI
# driving it had to scrape a terminal dialogue to learn the same facts — the
# asymmetry that made this whole feature undrivable by anything but a human
# at a keyboard. `--questions` closes it: the same shape, measured from THIS
# repo.
#
# The rule the flow follows is og's own: measure first, ask only what is
# genuinely the user's to answer. Everything og can detect, og decides and
# says so out loud; only the three questions below are asked, and only the
# ones the answer to which changes what is written.
# --------------------------------------------------------------------------

BRANCHING_MODELS = ["gitflow", "github-flow", "trunk-release", "custom"]

BRANCHING_LABELS = {
    "gitflow": "GitFlow — integration on develop, releases cut from main",
    "github-flow": "GitHub Flow — short-lived branches merged into main",
    "trunk-release": "Trunk + release branches — main plus release/*",
    "custom": "Custom — I will set the branches by hand",
}


def detect_branching_model(names: set):
    """The branching model the branch layout implies, or None if it is unclear.

    A `dev`/`develop` branch is a gitflow repo; a repo whose only branch is
    `main` is github flow. Anything in between — several unrelated branches,
    no default branch at all — is genuinely ambiguous, and that is the only
    case where the question is worth asking.
    """
    if names & {"dev", "develop"}:
        return "gitflow"
    if names and names <= {"main", "master"}:
        return "github-flow"
    return None


# --- the installed roster, for the cross-vendor review note ----------------
#
# `require_cross_vendor_review` is true in every generated contract, but the
# gate can only see a cross-vendor review in the PR body. Whether one is
# POSSIBLE depends on the installed roster, which lives in the installer's
# state file, not in the repo. Reporting it here is the difference between
# the user learning it at review time and learning it now.
MODEL_FAMILIES = (("claude", "anthropic"), ("sonnet", "anthropic"),
                  ("opus", "anthropic"), ("haiku", "anthropic"),
                  ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
                  ("gemini", "google"), ("deepseek", "deepseek"),
                  ("qwen", "alibaba"), ("llama", "meta"),
                  ("mistral", "mistral"), ("kimi", "moonshot"),
                  ("grok", "xai"))


def _model_vendor(model):
    if not model:
        return None
    low = str(model).lower()
    for needle, vendor in MODEL_FAMILIES:
        if needle in low:
            return vendor
    return None


def read_roster():
    """The installed agents and their vendors, or None if there is no install.

    Cheap by construction: one JSON file the installer already wrote, plus
    the registry next to this script. The model-family heuristic is
    deliberately conservative — when it cannot place a model, the registry
    vendor is used.
    """
    state_path = Path(os.environ.get("OMNIGENT_HOME", Path.home() / ".omnigent")) \
        / "og-install.json"
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        return None
    try:
        registry = {a["id"]: a for a in
                    (json.loads((Path(__file__).resolve().parent / "registry.json")
                                .read_text()).get("agents") or [])}
    except Exception:
        registry = {}
    entries = []
    # The state file's own key names, mapped to the role they mean: the
    # installer writes `coders`, not `coder`.
    for state_key, role in (("coders", "coder"), ("reviewer", "reviewer")):
        for e in (state.get(state_key) or []):
            if not isinstance(e, dict):
                continue
            reg = registry.get(e.get("id")) or {}
            vendor = _model_vendor(e.get("model")) or reg.get("vendor")
            entries.append({"role": role, "id": e.get("id"),
                            "model": e.get("model"), "vendor": vendor})
    coder_vendors = {e["vendor"] for e in entries
                     if e["role"] == "coder" and e["vendor"]}
    reviewer_vendors = {e["vendor"] for e in entries
                        if e["role"] == "reviewer" and e["vendor"]}
    return {
        "state_file": str(state_path),
        "agents": entries,
        "vendors": sorted({e["vendor"] for e in entries if e["vendor"]}),
        "cross_vendor_review_possible": bool(coder_vendors)
        and bool(reviewer_vendors - coder_vendors),
    }


def read_current(repo: Path):
    """The contract that already exists in this repo, if any.

    `parses: false` is reported rather than raised: a contract that does not
    parse is silently ignored by the policy loader, so every gate and every
    protected branch in it is dead. A caller that sees this can say so.
    """
    path = repo / ".agents" / "orchestration.yaml"
    if not path.exists():
        return None
    rel = str(path.relative_to(repo))
    try:
        data = yaml.safe_load(path.read_text())
    except Exception as e:
        return {"path": rel, "parses": False, "error": str(e), "contract": None}
    return {"path": rel, "parses": True, "error": None, "contract": data}


def _q_gates(gates):
    return [{"name": n, "run": c, "scope": s} for n, c, s in gates]


def build_questions(gates, names) -> list:
    """The decision schema, mirroring `install.sh --questions`.

    Mirrored: `key`, `type`, `default`, `ask`, `choices`. Dropped from the
    installer: `prompt_ceiling_bytes` and the agent `registry` (installer
    concerns, not repo concerns), `detected_multiplexers` and `multiplexers`
    (a machine concern), and the per-question `current` field (the saved
    state is a top-level `current` here too, because the contract is the state
    this command has).
    """
    detected_model = detect_branching_model(names)
    return [
        {
            "key": "branching_model",
            "type": "choice",
            "choices": list(BRANCHING_MODELS),
            "default": detected_model or ("github-flow" if names & {"main", "master"}
                                          else "gitflow"),
            # `detected: true` means the layout answered it — say so and move
            # on rather than asking a question with a known answer.
            "detected": bool(detected_model),
            "ask": "Which branching model does this repo use? It sets `protected`, "
                   "which is what the merge gate DENYS on. A `dev`/`develop` "
                   "branch means GitFlow; a repo whose only branch is `main` "
                   "means GitHub Flow.",
        },
        {
            "key": "gate_scopes",
            "type": "per_gate",
            "choices": ["worker", "integration"],
            "gates": _q_gates(gates),
            "ask": "For each gate: does it touch a shared database, a fixed port, "
                   "or the network? If it does, it must be integration-only — "
                   "parallel workers running it at the same time corrupt each "
                   "other. This is the question og cannot answer by looking.",
        },
        {
            "key": "gates",
            "type": "gate_list",
            "default": [n for n, _, _ in gates],
            "gates": _q_gates(gates),
            "ask": "Confirm the detected gates. A wrong gate command is worse "
                   "than a missing one, because the orchestrator quotes the "
                   "contract back and runs exactly what it says. Delete any "
                   "that are wrong; og proposes nothing that was not detected.",
        },
    ]


def emit_questions(repo: Path, stacks, branches, gates, high_risk, never_write,
                   notes, names) -> None:
    """Print the decision schema as JSON. Read-only: nothing is written."""
    detected_model = detect_branching_model(names)
    roster = read_roster()
    warnings = []
    if roster is not None and not roster["cross_vendor_review_possible"]:
        warnings.append(
            "the installed roster holds one vendor, so `require_cross_vendor_review: "
            "true` cannot be satisfied — a review will be same-vendor and the PR "
            "must be labelled `degraded-review`")
    payload = {
        "repo": {"path": str(repo), "name": repo.name},
        "detected": {
            "stacks": [{"id": s["id"], "label": s["label"], "dir": s.get("dir", "")}
                       for s in stacks],
            "gates": _q_gates(gates),
            "branches": {
                "integration_base": branches.get("integration_base"),
                "protected": branches.get("protected", []),
                "task_prefix": branches.get("task_prefix"),
                "all": sorted(names),
                "branching_model": detected_model,
            },
            "high_risk_paths": list(dict.fromkeys(high_risk)),
            "never_write": list(dict.fromkeys(never_write)),
            "notes": notes,
        },
        "current": read_current(repo),
        "questions": build_questions(gates, names),
        # The things og decided rather than asked about, with the reason, so
        # a caller driving from this schema does not go and ask them either.
        "not_asked": [
            {"key": "merge_policy.enabled", "value": False,
             "why": "every merge stays a human's decision until the gates have "
                    "been watched on real PRs"},
            {"key": "merge_policy.max_diff_lines", "value": 400,
             "why": "a starting point; recalibrate from real PR history"},
            {"key": "merge_policy.max_diff_files", "value": 15,
             "why": "a starting point; recalibrate from real PR history"},
            {"key": "branches.task_prefix", "value": branches.get("task_prefix"),
             "why": "derived from the branch names already in the repo"},
            {"key": "merge_policy.high_risk_paths",
             "value": "stack files plus .github/workflows",
             "why": "derived from the detected stack and the CI definition"},
        ],
        "roster": roster,
        "warnings": warnings,
        "plan": {
            "branching_model": "one of " + ", ".join(BRANCHING_MODELS),
            "gates": "list of {name, run, scope} — scope null means "
                     "integration-only; omit to keep the detected gates",
            "gate_scopes": "map of gate name -> 'worker' | 'integration', if the "
                           "scopes are answered without replacing the gate list",
            "branches": {"integration_base": "main", "protected": [],
                         "task_prefix": "feature/"},
            "note": "either `gates` or `gate_scopes` (or both) may be given",
        },
        "apply_with": "og init --plan answers.json",
    }
    print(json.dumps(payload, indent=2, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(prog="og init", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".", help="repo to initialise (default: cwd)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing contract")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="print the contract instead of writing it")
    ap.add_argument("--questions", action="store_true",
                    help="emit the decision schema as JSON (for an AI)")
    ap.add_argument("--no-agents-md", action="store_true", help="skip the AGENTS.md stub")
    args = ap.parse_args()

    repo = Path(args.path).resolve()
    top = run(["git", "rev-parse", "--show-toplevel"], repo)
    if not top:
        die(f"{repo} is not a git repository. og init writes a per-repo contract; "
            "run it inside the repo the agents will work in.")
    repo = Path(top)

    stacks = detect_stacks(repo)
    branches = detect_branches(repo)
    gates, high_risk, never_write, notes = build_gates(repo, stacks)
    names = detect_branch_names(repo)

    # `--questions` is a query, exactly like `--print`: it measures and
    # reports, and writes nothing. It runs before the summary lines so its
    # stdout is JSON and nothing else — a caller piping it into a JSON parser
    # must not have to strip prose first.
    if args.questions:
        emit_questions(repo, stacks, branches, gates, high_risk, never_write,
                       notes, names)
        return

    info(f"repo        {repo}")
    info(f"stack       {', '.join(_stack_label(s) for s in stacks) or 'none detected'}")
    info(f"integration {branches['integration_base'] or 'TODO'}"
         f"   protected: {', '.join(branches['protected']) or 'none found'}")
    info(f"gates       {len(gates)} detected "
         f"({sum(1 for g in gates if g[2] == 'worker')} worker-scoped)")
    for n in notes:
        warn(n)

    if args.show:
        body = render(repo, branches, gates, high_risk, never_write, stacks, notes)
        print()
        print(body)
        return

    target = repo / ".agents" / "orchestration.yaml"
    if target.exists() and not args.force:
        die(f"{target.relative_to(repo)} already exists. Re-run with --force to replace it, "
            "or --print to see what would be generated.")

    body = render(repo, branches, gates, high_risk, never_write, stacks, notes)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    ok(f"wrote {target.relative_to(repo)}")

    am = repo / "AGENTS.md"
    if not args.no_agents_md:
        if am.exists():
            info("AGENTS.md exists — left untouched")
        else:
            am.write_text(AGENTS_STUB.format(
                name=repo.name,
                integration_base=branches["integration_base"] or "TODO",
                task_prefix=branches["task_prefix"],
                protected=", ".join(branches["protected"]) or "none detected"))
            ok("wrote AGENTS.md (a stub — fill it in)")

    print()
    print(f"{C['b']}Next{C['x']}")
    print("  1. Read .agents/orchestration.yaml. Fix any gate command that is wrong —")
    print("     the orchestrator runs exactly what it says.")
    print("  2. Write AGENTS.md. It is authoritative and the orchestrator reads it first.")
    print("  3. merge_policy.enabled is false, so every merge is yours. Turn it on")
    print("     once you have watched the gates behave on real PRs.")


if __name__ == "__main__":
    main()
