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
# `code` defaults to 1 for a plain failure. The DECLINED case gets its own
# code (2), so a caller can tell "the user did not answer" from "the command
# broke" — the two call for different messages, and only one of them is
# worth retrying.
def die(m, code=1):
    # The message goes to stderr, and a stderr that is itself broken (the
    # terminal the user just closed) must not change the exit code: the
    # code is the part a caller acts on.
    try:
        print(f"{C['r']}✗{C['x']} {m}", file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass
    raise SystemExit(code)


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
# `markers` decides whether a stack is present — ANY one of them is enough.
# A single marker per stack was the original shape, and it missed whole repos:
# the Python stack keyed on `pyproject.toml` alone, so a pytest repo whose only
# Python marker is `conftest.py` + `tests/` detected as "no toolchain" and
# `og init` wrote a contract with NO gates while claiming a contract existed.
# `gates` are the commands a worker or the integration branch runs. `scope:
# worker` means every implementer runs it in its own worktree; anything without
# a scope runs ONCE on the integration branch — which is how a suite that shares
# a database or a port avoids being run concurrently by parallel workers.
# --------------------------------------------------------------------------
STACKS = [
    {
        "id": "go", "label": "Go", "markers": ("go.mod",),
        "gates": [("vet", "go vet ./...", "worker"),
                  ("build", "go build ./...", "worker"),
                  ("unit", "go test -count=1 -failfast ./...", "worker")],
        "optional": [("lint", "golangci-lint run ./... --timeout=2m", None, "golangci-lint"),
                     ("security", "gosec -quiet ./...", None, "gosec")],
        "high_risk": ["go.mod", "go.sum"],
        "never_write": ["vendor/**"],
    },
    {
        "id": "node", "label": "Node / TypeScript", "markers": ("package.json",),
        "gates": [("build", "{pm} run build", "worker"),
                  ("unit", "{pm} test", "worker")],
        "optional": [("lint", "{pm} run lint", None, None),
                     ("typecheck", "{pm} run typecheck", None, None)],
        "high_risk": ["package.json", "{lockfile}"],
        "never_write": ["node_modules/**", "dist/**", "build/**"],
    },
    {
        "id": "python", "label": "Python",
        # Packaging markers first, the historical one first of all so an
        # existing repo's detection cannot change. Then the ones that mean
        # "this is a Python project" or "this is a pytest repo" without
        # implying packaging: a bare `conftest.py` is the marker that was
        # missing here, and `tox.ini`/`setup.cfg` are how a test runner gets
        # configured in practice.
        "markers": ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini",
                    "requirements.txt", "conftest.py"),
        "gates": [("unit", "pytest -x -q", "worker")],
        "optional": [("lint", "ruff check .", None, "ruff"),
                     ("typecheck", "mypy .", None, "mypy")],
        "high_risk": ["pyproject.toml", "poetry.lock", "requirements.txt"],
        "never_write": [".venv/**", "**/__pycache__/**"],
    },
    {
        "id": "rust", "label": "Rust", "markers": ("Cargo.toml",),
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
            # The FIRST matching marker is recorded on the item, because a
            # caller (and a reader of `--questions` JSON) wants to know which
            # file made the detection, not just that one of them did.
            hit = next((m for m in s["markers"] if (base / m).exists()), None)
            if hit is not None:
                item = dict(s)
                item["marker"] = hit
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
                notes.append({"text": "package.json has no build/test script — "
                                      "treated as tooling, not a stack; no Node "
                                      "gates emitted",
                              "gate": None})
                continue
        for name, cmd, scope in st["gates"]:
            cmd = cmd.format(**subs) if subs else cmd
            gname = gate_name(name, slug)
            if st["id"] == "node" and name in ("build", "unit"):
                key = "build" if name == "build" else "test"
                if key not in scripts:
                    # A gate that is KNOWN to fail is a gate og is unsure
                    # about, and the gate name rides along with the note so
                    # the question flow can treat it as a gate to remove by
                    # default rather than one to trust.
                    notes.append({"text": f"package.json has no `{key}` script — "
                                         f"`{prefix}{cmd}` will fail",
                                  "gate": gname})
            gates.append((gname, prefix + cmd, scope))
        for name, cmd, scope, binary in st["optional"]:
            cmd = cmd.format(**subs) if subs else cmd
            if binary and not _which(binary):
                notes.append({"text": f"{binary} not installed — `{name}` gate omitted",
                              "gate": None})
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
            # The unit gate above runs the WHOLE tree, shared-state suite
            # included, in every worktree. That is the same failure the
            # scope question is about, so the gate it names is a gate og is
            # unsure about: it is removed by default and shown as such.
            unit_gate = next((g[0] for g in gates
                              if g[0] == "unit" or g[0].startswith("unit-")), None)
            notes.append({
                "text": f"{d}/ looks like a shared-state suite, but the worker-scoped "
                        f"gate above runs the whole tree. Split it: narrow the `unit` "
                        f"gate to the unit packages, and add a separate no-scope gate "
                        f"for {d}/ so it runs once on the integration branch instead "
                        f"of in every worktree.",
                "gate": unit_gate})
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
        # The rendered comment used to claim that an empty list means "nothing
        # here is protected and agents may merge into <base> directly". That was
        # FALSE, and it is worth spelling out because it is a promise about who
        # may merge where: merge_gate unions this list with its own
        # `_ALWAYS_PROTECTED` — {main, master, staging, production, prod,
        # release} at policies/omnigent_local_policies.py:57 — so a merge or
        # force-push that NAMES the base is DENY with or without this key. The
        # contract's list can only ever ADD to that set.
        #
        # Emitting [base] here is still pointless rather than harmful, which is
        # why the list stays empty: it would add the base to a set that already
        # contains it. What actually stops an unattended merge is
        # `merge_policy.enabled: false` below, which returns ASK.
        L.append("  # Empty does NOT mean the integration base is unprotected: merge_gate")
        L.append("  # unions this list with its own always-protected set (main, master,")
        L.append("  # staging, production, prod, release), so a merge or force-push that")
        L.append("  # names the base is denied either way. This list can only ADD to that")
        L.append("  # set — so add release branches here, rather than the integration base.")
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
        # The empty case must still PARSE, exactly like `never_write` below. A
        # key with only comments under it parses as `null`, and the consumer
        # that iterates gates gets a TypeError instead of an empty contract —
        # so the one repo that most needs the TODO read is the one whose
        # contract fails to load. `[]` makes it an empty list.
        L.append("  []")
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
            L.append(f"#   - {_one_line(n['text'] if isinstance(n, dict) else n)}")
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


def branches_for_model(model: str, base: dict, names: set) -> dict:
    """Recompute `protected` and the integration base for a branching model.

    `protected` is what the merge gate DENYS on, so it is derived from the
    model rather than guessed: a gitflow repo protects main and the release
    branches while integrating on develop; github flow protects nothing
    beyond the integration branch itself (which the contract refuses to list
    as protected, because a branch that is both the merge target and
    protected makes the gate forbid the only merge it mandates).
    """
    integ = base.get("integration_base")
    task_prefix = base.get("task_prefix", "feature/")
    if model == "gitflow":
        integ = next((b for b in ("dev", "develop") if b in names), integ)
        protected = sorted({b for b in names
                            if b in ("main", "master", "staging", "production")
                            or b.startswith("release")} - {integ})
    elif model == "github-flow":
        integ = next((b for b in ("main", "master") if b in names), integ)
        protected = []
    elif model == "trunk-release":
        integ = next((b for b in ("main", "master") if b in names), integ)
        protected = sorted({b for b in names if b.startswith("release")} - {integ})
    else:
        return dict(base)
    return {"integration_base": integ, "protected": protected,
            "task_prefix": task_prefix}


# The marked block og is allowed to rewrite inside someone else's AGENTS.md.
# Everything outside these markers belongs to the user, and og never touches
# it — a rerun replaces the block, it does not append a second one.
BLOCK_BEGIN = "<!-- og:orchestration:begin -->"
BLOCK_END = "<!-- og:orchestration:end -->"
# The markers alone do not identify og's block: they are documented, so a
# user may well quote them in their own AGENTS.md. This signature, inside
# the block, is what makes og's own block machine-identifiable — a quoted
# example carries the markers but not the signature.
BLOCK_SIGNATURE = "<!-- og:orchestration:signature:v1 -->"
# The first sentence of og's own block. It is the FINGERPRINT: it is what
# tells a block written by a pre-signature og apart from a section a user
# wrote themselves under the same heading, and it is why the two must not be
# edited apart.
BLOCK_FINGERPRINT = "`.agents/orchestration.yaml` is the machine-readable contract:"


def orchestration_block(branches: dict) -> str:
    """The orchestration directives injected into an existing AGENTS.md.

    Short on purpose: this is appended to a document the user wrote, and a
    long block is a long block of someone else's file. It carries only what
    the orchestrator must know BEFORE deciding to read anything else.
    """
    integ = branches.get("integration_base") or "TODO"
    protected = ", ".join(branches.get("protected") or []) or "none"
    return f"""{BLOCK_BEGIN}
{BLOCK_SIGNATURE}

## Orchestration

{BLOCK_FINGERPRINT} the gates and
their scope, the protected branches, the integration base, and the paths an
implementer must not touch. Read it before planning, and run exactly the
commands it names — a gate that looks wrong is fixed there, not improvised.

AGENTS.md is authoritative: where this file and the contract disagree, this
file wins.

Detected: integration base `{integ}`, protected: {protected}.

Gate scope decides WHO runs a gate. A gate marked `scope: worker` runs in
EVERY implementer's worktree; a gate with no scope runs ONCE on the
integration branch. Anything that touches a shared database, a fixed port, or
the network belongs in the no-scope tier — parallel workers running it at the
same time corrupt each other.
{BLOCK_END}
"""


# --- reading and writing somebody else's document --------------------------
#
# The marker boundary is a PROMISE about another person's file: og rewrites
# one span and leaves every other byte alone. Two ways to break that promise
# are handled here, and both were real bugs:
#
#   1. `Path.read_text()` translates CRLF to LF on the way in, so a
#      CRLF-authored AGENTS.md came back with every line ending rewritten —
#      a whole-file diff in a file the user never asked og to touch. The
#      file is therefore read and written as BYTES.
#   2. A marker pair inside a fenced code block is an EXAMPLE of this
#      feature, not og's block. Replacing it ate the example and left the
#      fence unterminated.


def _split_lines(text: str) -> list:
    """Split text into [content, ending] pairs, preserving the endings.

    `str.splitlines()` normalises CRLF away, which is exactly the loss this
    function exists to prevent: the ending is carried alongside the content
    so it can be written back unchanged. A lone CR is a line ending too —
    classic-Mac files are still classic-Mac files, and a document whose
    endings are not recognised is a document whose markers cannot be found.
    """
    lines = []
    i = 0
    n = len(text)
    while i < n:
        lf = text.find("\n", i)
        cr = text.find("\r", i)
        if lf == -1 and cr == -1:
            lines.append([text[i:], ""])
            break
        if cr != -1 and (lf == -1 or cr < lf):
            if text.startswith("\r\n", cr):
                lines.append([text[i:cr], "\r\n"])
                i = cr + 2
            else:
                lines.append([text[i:cr], "\r"])
                i = cr + 1
        else:
            lines.append([text[i:lf], "\n"])
            i = lf + 1
    return lines


def _join_lines(lines: list) -> str:
    return "".join(c + e for c, e in lines)


def _dominant_newline(text: str) -> str:
    """The line ending this document mostly uses.

    A MIXED file stays mixed: this decides what the INSERTED block looks
    like, never what the user's own lines look like. og does not "fix" a
    user's line endings.
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf:
        return "\r\n"
    if cr > lf:
        return "\r"
    return "\n"


# A markdown fence: three or more backticks or tildes, indented by at most
# three spaces. Anything deeper is an indented code block, not a fence.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _fence_state(lines: list) -> tuple:
    """(the lines inside a fence, the fence still open at the end or None).

    A fence is how a user quotes a marker pair when they document this very
    feature — og's own README does it. The open fence matters too: a
    document that ends inside one is a document og must not write its block
    into.
    """
    inside = set()
    fence = None
    for i, (content, _) in enumerate(lines):
        m = _FENCE_RE.match(content)
        if fence is None:
            if m:
                marker = m.group(1)
                fence = (marker[0], len(marker))
                inside.add(i)
        else:
            inside.add(i)
            if m:
                marker = m.group(1)
                # A closing fence repeats the same character, is at least as
                # long as the opening one, and carries no info string.
                if (marker[0] == fence[0] and len(marker) >= fence[1]
                        and not m.group(2).strip()):
                    fence = None
    return inside, fence


def _is_marker(content: str, marker: str) -> bool:
    """True for a line that is exactly the marker, whatever its indentation.

    Used for the SIGNED path, where the signature has already proved the
    block is og's: the markers around it are then only anchors, and an
    indented one still delimits og's block.
    """
    return content.strip() == marker


def _is_column0_marker(content: str, marker: str) -> bool:
    """True for a marker at the very start of the line.

    Used for the LEGACY path, where the markers are the only evidence: an
    indented marker is somebody's quoted text, not a block to rewrite.
    """
    return content.startswith(marker) and not content[len(marker):].strip()


def _signed_blocks(lines: list) -> list:
    """Every signed block in the document, in file order.

    A signature only counts as a block when a begin marker OF ITS OWN
    precedes it: the begin marker says og wrote this, and the signature
    says WHICH of them is og's. A signature with no begin marker is a
    QUOTATION — somebody pasted the line — and is not a block at all.

    A block is a MARKER SPAN that contains a signature, so several
    signatures inside one span are one block, and a signature quoted
    inside a block's own span does not make a second one.

    Each entry is (begin index, end index, inside a code fence). An end
    index of None means the block was truncated: the end marker is gone,
    and everything from the begin marker on was og's.
    """
    inside, _ = _fence_state(lines)
    blocks = []
    for s, (content, _) in enumerate(lines):
        if content.strip() != BLOCK_SIGNATURE:
            continue
        # The begin marker must belong to the SAME block. A begin marker
        # that sits before an already-closed block's end marker belongs to
        # that block, not to this signature.
        last_end = max((i for i in range(s)
                        if lines[i][0].strip() == BLOCK_END), default=-1)
        begins = [i for i in range(s)
                  if lines[i][0].strip() == BLOCK_BEGIN and i > last_end]
        if not begins:
            continue
        ends = [i for i in range(s + 1, len(lines))
                if lines[i][0].strip() == BLOCK_END]
        begin, end = begins[-1], (ends[0] if ends else None)
        # A signature that falls inside a span already found belongs to
        # that block: it is quoted inside it, not a block of its own.
        if any(b <= s <= (e if e is not None else len(lines))
               for b, e, _ in blocks):
            continue
        blocks.append((begin, end, s in inside))
    return blocks


def _block_candidates(lines: list) -> tuple:
    """(the blocks that COMPETE for selection, the one that wins).

    The rule: the FIRST signed block that is not inside a code fence; if
    every one of them is fenced, the first signed block. Among several
    unfenced blocks, the first is og's: a copy is made FROM a block that
    already existed, so the original comes first in the file.

    The COMPETING blocks are the ones the rule ranks against each other.
    A fenced copy never competes with an unfenced block — it is an
    example somebody wrote, and og leaves it byte-identical — so it is not
    a candidate when an unfenced block exists.

    The warning and the resolution read THIS set, and only this set: a
    warning that fires when nothing is wrong teaches people to ignore
    warnings, and one that says "remove the copy" about a documentation
    example og deliberately preserved is worse than no warning at all.
    """
    blocks = _signed_blocks(lines)
    if not blocks:
        return [], None
    unfenced = [b for b in blocks if not b[2]]
    pool = unfenced or blocks
    return pool, pool[0]


def _find_block(lines: list):
    """Locate og's own block: (begin index, end index) or None."""
    _, chosen = _block_candidates(lines)
    if chosen:
        return chosen[:2]
    return _legacy_block(lines)


def _legacy_block(lines: list):
    """A block written by an og from before the signature existed.

    Narrow on purpose. This path is the ONLY one where a pair of markers
    can be mistaken for og's block, so it demands three things: no signed
    block anywhere in the document, the pair outside any code fence, and
    og's OWN paragraph inside it — not merely a heading called
    "## Orchestration", which a user may well have written themselves.
    """
    inside, _ = _fence_state(lines)
    begins = [i for i, (c, _) in enumerate(lines)
              if i not in inside and _is_column0_marker(c, BLOCK_BEGIN)]
    ends = [i for i, (c, _) in enumerate(lines)
            if i not in inside and _is_column0_marker(c, BLOCK_END)]
    pairs = []
    for b in begins:
        e = next((x for x in ends if x > b), None)
        if e is not None:
            pairs.append((b, e))
    if not pairs:
        return None
    # The LAST pair that carries og's own paragraph is og's: og appends its
    # block at the end of the document, and a user's own section under the
    # same heading does not carry the paragraph.
    for b, e in reversed(pairs):
        body = " ".join(lines[i][0] for i in range(b + 1, e))
        if BLOCK_FINGERPRINT in body:
            return (b, e)
    return None


def upsert_agents_block(text: str, block: str, newline: str = None) -> str:
    """Return `text` with og's block replaced, or the block appended.

    The markers are the entire contract for what og may rewrite, so the
    replacement is a single span: the bytes before it and the bytes after it
    are copied through untouched, ending for ending. This is what makes the
    operation idempotent — the rerunnable rule in AGENTS.md. A second
    `og init` finds og's own block and replaces it in place instead of
    appending another.
    """
    if newline is None:
        newline = _dominant_newline(text)
    lines = _split_lines(text)
    # The block's own text is written with the document's line ending, so a
    # CRLF document does not gain a block with LF endings in the middle of
    # it.
    body = [c for c, _ in _split_lines(block.rstrip("\n"))]
    found = _find_block(lines)

    if found:
        begin, end = found
        if end is None:
            # A truncated block: everything from the begin marker to the end
            # of the file was og's, and is replaced as a unit.
            region_end = len(lines)
            last_ending = (lines[-1][1] or newline) if lines else newline
        else:
            region_end = end + 1
            # The block's last line keeps the ending the replaced one had.
            last_ending = lines[end][1] or newline
        new_lines = [[c, newline] for c in body[:-1]] + [[body[-1], last_ending]]
        return _join_lines(lines[:begin] + new_lines + lines[region_end:])

    # Append. The block goes at the end, and the user's last line keeps its
    # own ending — a line without a terminator gets one, because otherwise
    # the block would be glued onto the user's prose.
    out = [list(l) for l in lines]
    if out and out[-1][1] == "":
        out[-1][1] = newline
    # Never append INSIDE an open fence. A block written into a code block
    # is a block nobody can read — and, worse, one the next run cannot see,
    # because the fence that hides it is never closed. So the fence is
    # closed first: the user's own text stays inside it, exactly as it was,
    # and og's block lands outside.
    _, open_fence = _fence_state(out)
    if open_fence:
        out.append([open_fence[0] * open_fence[1], newline])
    return _join_lines(out + [[c, newline] for c in body])


def block_ambiguity(text: str) -> str:
    """The warning to raise when the document holds more than one og block.

    Two signed blocks are the SAME text, so og cannot tell which one is the
    live one. It updates one of them — deterministically — and says so: a
    stale block that LOOKS current is the exact failure this whole feature
    exists to end, and the user is the only one who can remove a copy.

    The warning fires only when the choice was genuinely ambiguous: when
    more than one block COMPETED for selection. A fenced example never
    competes with an unfenced block, so a document with a real block and a
    documentation example is silent — telling the user to remove their own
    example would be worse than saying nothing.
    """
    lines = _split_lines(text)
    pool, chosen = _block_candidates(lines)
    if len(pool) < 2:
        return ""
    where = f"the first one (line {chosen[0] + 1})"
    others = len(pool) - 1
    stale = ("the other is a copy that is now stale" if others == 1
             else f"the other {others} are copies that are now stale")
    return (f"AGENTS.md holds {len(pool)} og blocks — og's signature appears "
            f"{len(pool)} times, and og cannot tell which one is the live one. "
            f"og updated {where}; {stale}. Remove the "
            f"{'copy' if others == 1 else 'copies'}, or the directives the "
            f"orchestrator reads may be the stale one.")


def write_agents_block(path: Path, block: str) -> str:
    """Update the marked block in an AGENTS.md without touching other bytes.

    The file is read and written as bytes with no newline translation: a
    CRLF document that came back with LF endings is a whole-file diff in
    somebody else's file, and the marker boundary promised that og would
    not touch a single byte outside it.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8", "surrogateescape")
    updated = upsert_agents_block(text, block)
    path.write_bytes(updated.encode("utf-8", "surrogateescape"))
    warning = block_ambiguity(text)
    if warning:
        warn(warning)
    return updated


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
            model_id = e.get("model")
            vendor = _model_vendor(model_id)
            if reg.get("vendor_from_model_only"):
                # Pi's managed ids are provider-prefixed (`deepseek/...`).
                # A bare id names a model, not the provider `omni setup`
                # serves it from, so it cannot prove a vendor family.
                if "/" not in str(model_id or ""):
                    vendor = None
            elif vendor is None:
                vendor = reg.get("vendor")
            entries.append({"role": role, "id": e.get("id"),
                            "model": e.get("model"), "vendor": vendor})
    coder_vendors = {e["vendor"] for e in entries
                     if e["role"] == "coder" and e["vendor"]}
    reviewer_vendors = {e["vendor"] for e in entries
                        if e["role"] == "reviewer" and e["vendor"]}
    # A multi-model row with no family in its pin is UNKNOWN, not a vendor of
    # its own: it may serve the same provider as anyone else, so no pairing can
    # be called cross-vendor while it is in the roster.
    unknown_vendor = any(e["vendor"] is None for e in entries)
    return {
        "state_file": str(state_path),
        "agents": entries,
        "vendors": sorted({e["vendor"] for e in entries if e["vendor"]}),
        "unknown_vendor": unknown_vendor,
        "cross_vendor_review_possible": bool(coder_vendors)
        and bool(reviewer_vendors - coder_vendors) and not unknown_vendor,
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


def cross_vendor_warning(roster: dict) -> str:
    """The one explanation for an unenforceable cross-vendor-review contract."""
    if roster.get("unknown_vendor"):
        return ("the installed roster includes a multi-model agent whose provider "
                "family is unknown, so `require_cross_vendor_review: true` cannot "
                "be satisfied — a review may be same-vendor and the PR must be "
                "labelled `degraded-review`")
    return ("the installed roster holds one vendor, so "
            "`require_cross_vendor_review: true` cannot be satisfied — a review "
            "will be same-vendor and the PR must be labelled `degraded-review`")


def emit_questions(repo: Path, stacks, branches, gates, high_risk, never_write,
                   notes, names) -> None:
    """Print the decision schema as JSON. Read-only: nothing is written."""
    detected_model = detect_branching_model(names)
    roster = read_roster()
    warnings = []
    if roster is not None and not roster["cross_vendor_review_possible"]:
        warnings.append(cross_vendor_warning(roster))
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
            "notes": [n["text"] if isinstance(n, dict) else n for n in notes],
            # The gates og is UNSURE about, and why. These are the ones the
            # question flow removes by default: a gate that is known to
            # fail is worse than no gate at all.
            "unsure_gates": {k: v for k, v in unsure_gates(notes).items()},
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


# --- the interactive flow ---------------------------------------------------


def _prompt(text: str, default: str = "") -> str:
    """Ask one question.

    EOF is NOT a default. Input that ends is the user declining, and a
    contract built from substituted defaults is one nobody chose: it fails
    OPEN, where no contract at all fails closed and warns. The EOFError is
    therefore left for the caller to turn into an abort — nothing is
    written on that path.
    """
    raw = input(text)
    raw = raw.strip()
    return raw or default


def run_flow(gates, branches, names, unsure=None, ask=None) -> dict:
    """Ask the three questions that are genuinely the user's to answer.

    Everything og can measure is decided here and said out loud, not asked:
    the branching model when the branch layout answers it, the task prefix,
    the high-risk paths, the merge policy and the diff thresholds.

    `unsure` maps a gate name to the reason og does not trust it. Those
    gates are the ones removed by default in the confirmation step.
    """
    if ask is None:
        ask = _prompt
    answers = {}

    model = detect_branching_model(names)
    if model:
        info(f"branching    {model} (detected from the branch layout) — not asking")
    else:
        print()
        print("Branching model? This sets `protected`, which is what the merge "
              "gate DENYS on.")
        for i, m in enumerate(BRANCHING_MODELS, 1):
            print(f"  {i}. {BRANCHING_LABELS[m]}")
        default = "github-flow" if names & {"main", "master"} else "gitflow"
        raw = ask(f"  Branching model [{default}]: ", default)
        model = raw if raw in BRANCHING_MODELS else default
        info(f"branching    {model}")
    answers["branching_model"] = model

    print()
    print("Gate scope. A gate that touches a shared database, a fixed port, or the "
          "network")
    print("must be integration-only: parallel workers running it at the same time "
          "corrupt each other.")
    scoped = []
    for name, cmd, scope in gates:
        if scope:
            ask_line = f"  {name}: {cmd}\n    shared database, fixed port, or network? [y/N] "
            default = "n"
        else:
            ask_line = f"  {name}: {cmd}\n    shared database, fixed port, or network? [Y/n] "
            default = "y"
        raw = ask(ask_line, default)
        shared = raw.strip().lower() in ("y", "yes")
        scoped.append((name, cmd, None if shared else "worker"))

    print()
    print("Detected gates. A wrong gate command is worse than a missing one — the "
          "orchestrator")
    print("quotes the contract back and runs exactly what it says.")
    uncertain = unsure or {}
    for i, (name, cmd, scope) in enumerate(scoped, 1):
        line = f"  {i}. {name}: {cmd}  [{scope or 'integration'}]"
        if name in uncertain:
            reasons = uncertain[name]
            if isinstance(reasons, str):
                reasons = [reasons]
            line += (f"\n     likely wrong: {'; '.join(reasons)}"
                     f"\n     -> removed unless you keep it")
        print(line)
    # "Default to fewer" applies where the uncertainty actually is. A gate
    # og detected and can vouch for is kept; a gate og flagged as probably
    # wrong — a command that will fail, or one that runs a shared-state
    # suite in every worktree — is removed unless the user says otherwise.
    # Discarding good detections to look cautious would be its own bug.
    defaults = [i for i, (n, _, _) in enumerate(scoped, 1) if n in uncertain]
    default = ",".join(str(i) for i in defaults) if defaults else "none"
    raw = ask("  Which should be removed? (numbers, comma-separated — blank removes "
              "the ones marked likely-wrong, 'none' keeps everything, an explicit "
              f"list replaces the default) ", default)
    if raw.strip().lower() == "none":
        removed = set()
    else:
        removed = set()
        for part in raw.replace(",", " ").split():
            if part.isdigit():
                removed.add(int(part))
    kept = [g for i, g in enumerate(scoped, 1) if i not in removed]
    answers["gates"] = [{"name": n, "run": c, "scope": s} for n, c, s in kept]
    return answers


def unsure_gates(notes: list) -> dict:
    """The gates a note says are probably wrong, and why.

    A note that names a gate is a note about that gate: a command that will
    fail, or one that runs a shared-state suite in every worktree. Those
    are the gates the question flow removes by default. A gate can carry
    more than one reason, so the reasons are a list.
    """
    out = {}
    for n in notes:
        if not isinstance(n, dict) or not n.get("gate"):
            continue
        out.setdefault(n["gate"], []).append(n["text"])
    return out


def interactive() -> bool:
    """True when a human is there to answer questions."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def load_plan(path: str) -> dict:
    """Read the answers JSON written by `--questions` (or by hand).

    The file may be the plan itself or a wrapper with an `answers` key, so a
    caller that dumped the whole schema to a file can feed it back without
    editing.
    """
    try:
        data = json.loads(Path(path).read_text())
    except Exception as e:
        die(f"could not read the plan {path}: {e}")
    if not isinstance(data, dict):
        die(f"the plan {path} must be a JSON object")
    return data.get("answers") if isinstance(data.get("answers"), dict) else data


def apply_answers(answers: dict, base_branches: dict, base_gates: list,
                  names: set) -> tuple:
    """Turn answers (from the flow or from `--plan`) into branches + gates."""
    branches = dict(base_branches)
    model = answers.get("branching_model")
    if model and model != "custom":
        branches = branches_for_model(model, base_branches, names)
    override = answers.get("branches") or {}
    for key in ("integration_base", "protected", "task_prefix"):
        if override.get(key) is not None:
            branches[key] = override[key]
    gates = base_gates
    gate_list = answers.get("gates")
    if isinstance(gate_list, list):
        kept = []
        for g in gate_list:
            if isinstance(g, str):
                # A bare name keeps the detected gate as it is.
                match = next((b for b in base_gates if b[0] == g), None)
                if match:
                    kept.append(match)
                continue
            scope = g.get("scope")
            if scope in ("integration", "none", "null", True):
                scope = None
            elif scope in ("worker", False):
                scope = "worker"
            run = g.get("run")
            if run is None:
                # A gate named without a command keeps the detected command:
                # a name og detected is worth more than a missing string.
                match = next((b for b in base_gates if b[0] == g.get("name")), None)
                run = match[1] if match else ""
            kept.append((g["name"], run, scope))
        gates = kept
    # The scope question can also be answered on its own, as a map from gate
    # name to scope — the shape the schema's `gate_scopes` question implies.
    scopes = answers.get("gate_scopes")
    if isinstance(scopes, dict):
        rescoped = []
        for n, c, s in gates:
            if n in scopes:
                v = scopes[n]
                s = None if v in ("integration", "none", "null", True) else "worker"
            rescoped.append((n, c, s))
        gates = rescoped
    return branches, gates


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="og init", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes: 0 written, 1 error, 2 declined — the input ended "
               "before the questions were answered, and NOTHING was written.")
    ap.add_argument("path", nargs="?", default=".", help="repo to initialise (default: cwd)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing contract")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="print the contract instead of writing it")
    ap.add_argument("--questions", action="store_true",
                    help="emit the decision schema as JSON (for an AI)")
    ap.add_argument("--plan", metavar="FILE",
                    help="apply answers from a JSON file instead of asking")
    ap.add_argument("--no-prompt", action="store_true",
                    help="use the detected defaults even on a TTY")
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
        warn(n["text"] if isinstance(n, dict) else n)

    if args.show:
        body = render(repo, branches, gates, high_risk, never_write, stacks, notes)
        print()
        print(body)
        return

    target = repo / ".agents" / "orchestration.yaml"
    if target.exists() and not args.force:
        die(f"{target.relative_to(repo)} already exists. Re-run with --force to replace it, "
            "or --print to see what would be generated.")

    # The flow is additive: without a TTY, or with --plan, the detected
    # defaults are used exactly as they were before this existed.
    answers = None
    if args.plan:
        answers = load_plan(args.plan)
    elif not args.no_prompt and interactive():
        # The questions are asked BEFORE anything is written, so an abort
        # here leaves the repo exactly as it was — including an existing
        # contract that `--force` was about to replace.
        try:
            answers = run_flow(gates, branches, names, unsure=unsure_gates(notes))
        except EOFError:
            # Input ended. That is the user DECLINING, not consent to the
            # defaults: a contract built from unanswered questions fails
            # OPEN — the orchestrator quotes it back and runs gates nobody
            # chose — while no contract at all fails closed and warns. So
            # silence aborts, and the exit code says "declined" rather than
            # "failed", because the two call for different messages.
            die("input ended before the questions were answered, so NOTHING was "
                "written. A contract from unanswered questions would fail open: "
                "the orchestrator would run gates nobody chose. Re-run `og init` "
                "and answer the questions, or pass --no-prompt to use the "
                "detected defaults on purpose.", code=2)
        except KeyboardInterrupt:
            die("interrupted, so NOTHING was written. Re-run `og init` when you "
                "are ready to answer the questions.", code=2)
        roster = read_roster()
        if roster and not roster["cross_vendor_review_possible"]:
            warn(cross_vendor_warning(roster))
    if answers:
        branches, gates = apply_answers(answers, branches, gates, names)

    body = render(repo, branches, gates, high_risk, never_write, stacks, notes)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    ok(f"wrote {target.relative_to(repo)}")

    am = repo / "AGENTS.md"
    if not args.no_agents_md:
        if am.exists():
            # A repo that already has a constitution gets the orchestration
            # directives APPENDED inside a marked block, not an overwrite:
            # the orchestrator reads AGENTS.md first, and "left untouched"
            # meant it found nothing about orchestration at all. The markers
            # are the whole contract for what og may rewrite, so the rerun
            # replaces the block in place.
            #
            # The write goes through the byte-preserving path: a CRLF
            # AGENTS.md must come back with its line endings intact, or the
            # user gets a whole-file diff in a file og was only asked to
            # add a block to.
            write_agents_block(am, orchestration_block(branches))
            ok("updated the orchestration block in AGENTS.md")
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
