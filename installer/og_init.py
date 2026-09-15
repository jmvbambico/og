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
import os
import re
import subprocess
import sys
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


def detect_stacks(repo: Path) -> list:
    return [s for s in STACKS if (repo / s["marker"]).exists()]


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


def detect_branches(repo: Path) -> dict:
    """Read the real branch layout instead of assuming gitflow."""
    out = {"integration_base": None, "protected": [], "task_prefix": "feature/"}
    head = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo)
    default = head.rsplit("/", 1)[-1] if head else None
    branches = set()
    listing = run(["git", "for-each-ref", "--format=%(refname:short)",
                   "refs/heads", "refs/remotes/origin"], repo) or ""
    for b in listing.splitlines():
        branches.add(b.split("/", 1)[-1] if b.startswith("origin/") else b)
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
    for st in stacks:
        subs = {}
        if st["id"] == "node":
            pm, lock = node_pm(repo)
            subs = {"pm": pm, "lockfile": lock}
            scripts = node_scripts(repo)
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
                    notes.append(f"package.json has no `{key}` script — `{cmd}` will fail")
            gates.append((name, cmd, scope))
        for name, cmd, scope, binary in st["optional"]:
            cmd = cmd.format(**subs) if subs else cmd
            if binary and not _which(binary):
                notes.append(f"{binary} not installed — `{name}` gate omitted")
                continue
            if st["id"] == "node" and name in ("lint", "typecheck") and name not in scripts:
                continue
            gates.append((name, cmd, scope))
        high_risk += [h.format(**subs) if subs else h for h in st["high_risk"]]
        never_write += st["never_write"]
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
    return gates, high_risk, never_write, notes


def _which(b):
    from shutil import which
    return which(b)


def render(repo: Path, branches: dict, gates: list, high_risk: list,
           never_write: list, stacks: list, notes: list) -> str:
    name = repo.name
    stack_label = " + ".join(s["label"] for s in stacks) or "unknown"
    L = []
    L.append(f"# Machine-readable orchestration contract for {name}.")
    L.append("# Consumed by the global og orchestrator agent. AGENTS.md remains the")
    L.append("# human-readable constitution and takes precedence on anything this file")
    L.append("# does not cover.")
    L.append("#")
    L.append(f"# Generated by `og init` from a {stack_label} repo. Every value below was")
    L.append("# detected or left as a TODO — nothing is inherited from another project.")
    L.append("# Review it: a wrong gate command is worse than a missing one, because the")
    L.append("# orchestrator quotes this contract back and runs exactly what it says.")
    L.append("")
    L.append("branches:")
    ib = branches["integration_base"]
    L.append(f"  # Where finished work lands. Workers branch from here and the batched")
    L.append(f"  # integration branch merges back into it.")
    L.append(f"  integration_base: {ib or 'TODO'}")
    L.append("  # Never merged into by an agent; promotions are a human's job.")
    if branches["protected"]:
        L.append(f"  protected: [{', '.join(branches['protected'])}]")
    else:
        # Falling back to [main] here would be actively wrong when main IS the
        # integration base: merge_gate DENYs merges into a protected branch, so
        # the contract would forbid the only merge it also mandates.
        L.append("  # No release branch found alongside the integration base, so nothing")
        L.append(f"  # here is protected and agents may merge into `{ib}` directly.")
        L.append("  # If that is not what you want, add a `dev` branch and make it the")
        L.append(f"  # integration_base, leaving `{ib}` protected below.")
        L.append("  protected: []")
    L.append(f"  task_prefix: {branches['task_prefix']}")
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
        L.append(f"  - name: {nm}")
        L.append(f"    run: {cmd}")
        if scope:
            L.append(f"    scope: {scope}")
    L.append("")
    L.append("never_write:")
    L.append("  # Paths an implementer must never touch.")
    for p in dict.fromkeys(never_write) or ["# - \"vendor/**\""]:
        L.append(f'  - "{p}"')
    L.append("")
    L.append("merge_policy:")
    L.append("  # Enforced by the `merge_gate` policy, not by prompt text. ALLOW only when")
    L.append("  # every condition holds; DENY for protected branches and force-pushes; ASK")
    L.append("  # for everything else INCLUDING anything it cannot determine.")
    L.append("  #")
    L.append("  # enabled: false means every merge is a human decision. That is the safe")
    L.append("  # starting point — turn it on once you trust the gates on real PRs.")
    L.append("  enabled: false")
    L.append(f"  auto_merge_target: {ib or 'TODO'}")
    L.append("  # Thresholds are a starting point; recalibrate from real PR history.")
    L.append("  max_diff_lines: 400")
    L.append("  max_diff_files: 15")
    L.append("  # Any path here forces the human gate regardless of gate results.")
    L.append("  high_risk_paths:")
    for p in dict.fromkeys(high_risk):
        L.append(f'    - "{p}"')
    for p, why in UNIVERSAL_HIGH_RISK:
        L.append(f'    - "{p}"{" " * max(1, 26 - len(p))}# {why}')
    L.append("  require_no_test_count_decrease: true")
    L.append("  # The gate cannot observe review itself (that is session state, not repo")
    L.append("  # state), so it reads the PR body: `cross-vendor-review: passed` permits")
    L.append("  # auto-merge; `degraded-review` always forces the human gate.")
    L.append("  require_cross_vendor_review: true")
    if notes:
        L.append("")
        L.append("# og init could not verify these — check before relying on them:")
        for n in notes:
            L.append(f"#   - {n}")
    L.append("")
    return "\n".join(L)


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


def main() -> None:
    ap = argparse.ArgumentParser(prog="og init", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".", help="repo to initialise (default: cwd)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing contract")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="print the contract instead of writing it")
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

    info(f"repo        {repo}")
    info(f"stack       {', '.join(s['label'] for s in stacks) or 'none detected'}")
    info(f"integration {branches['integration_base'] or 'TODO'}"
         f"   protected: {', '.join(branches['protected']) or 'none found'}")
    info(f"gates       {len(gates)} detected "
         f"({sum(1 for g in gates if g[2] == 'worker')} worker-scoped)")
    for n in notes:
        warn(n)

    body = render(repo, branches, gates, high_risk, never_write, stacks, notes)
    if args.show:
        print()
        print(body)
        return

    target = repo / ".agents" / "orchestration.yaml"
    if target.exists() and not args.force:
        die(f"{target.relative_to(repo)} already exists. Re-run with --force to replace it, "
            "or --print to see what would be generated.")
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
