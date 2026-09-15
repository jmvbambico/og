"""Local Omnigent policies: a mechanism-layer merge gate.

Why this exists
---------------
The bundled orchestrator examples enforce "never merge" in their *system
prompt* only. A prompt rule is a strong default, not a guarantee — a model
under pressure can reason past it. This module moves the merge decision into
the policy layer, where a confused or overconfident agent physically cannot
proceed.

The gate reads its rules from the repository's ``.agents/orchestration.yaml``
so that one global policy serves every project, and the rules live with the
code they govern.

Decision model
--------------
Fail-safe by construction. The gate returns:

* ``DENY``  — the action is never acceptable (merging into a protected
  branch, force-pushing a protected ref).
* ``ASK``   — the merge may be fine but carries risk the machine cannot
  clear on its own: a high-risk path, an oversized diff, fewer tests than
  before, a review that was flagged degraded, or *anything the gate could
  not determine*.
* ``ALLOW`` — every verifiable precondition is clean.

Anything unrecognised, unparseable, or unknown resolves to ``ASK``. A new
risk surface therefore protects itself until somebody classifies it.

What this gate can and cannot verify
------------------------------------
It verifies mechanically: the target branch, the diff's size and paths, the
test-count delta, and any ``degraded-review`` marker in the PR body. It
cannot itself observe whether a cross-vendor review actually happened — that
is session state, not repository state — so when the contract requires
cross-vendor review the gate looks for the orchestrator's marker in the PR
body and asks when it is absent rather than assuming success.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["merge_gate", "POLICY_REGISTRY"]

_CONTRACT_NAME = ".agents/orchestration.yaml"

# Branch names treated as protected even when no contract can be found.
# The catastrophic denials must not depend on locating a config file: an agent
# running from an unexpected directory would otherwise downgrade "force-push to
# main" from DENY to ASK. The contract's own `protected` list is unioned on top.
_ALWAYS_PROTECTED = frozenset(
    {"main", "master", "staging", "production", "prod", "release"}
)
_SHELL_TOOLS = frozenset({"sys_os_shell", "shell", "bash", "run_command"})

# Merge-ish commands we care about.
_GH_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")
_GIT_MERGE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?merge\b")
_GIT_PUSH = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b")
_FORCE = re.compile(r"(?:^|\s)(?:--force\b|--force-with-lease\b|-f\b)")


def _run(args: list[str], cwd: Path | None = None) -> str | None:
    """Run a command and return stdout, or ``None`` on any failure."""
    try:
        out = subprocess.run(
            args, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _load_contract(cwd: Path) -> dict[str, Any] | None:
    """Load ``.agents/orchestration.yaml`` walking up from *cwd*."""
    try:
        import yaml  # provided by the omnigent environment
    except ImportError:
        return None
    here = cwd.resolve()
    for d in (here, *here.parents):
        p = d / _CONTRACT_NAME
        if p.is_file():
            try:
                data = yaml.safe_load(p.read_text())
            except Exception:
                return None
            return data if isinstance(data, dict) else None
    return None


def _glob_match(path: str, pattern: str) -> bool:
    """Match a repo-relative path against a ``**``-aware glob."""
    rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(
        r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(rx, path) is not None


def _changed_files(cwd: Path, base: str) -> list[str] | None:
    out = _run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd)
    if out is None:
        return None
    return [l.strip() for l in out.splitlines() if l.strip()]


def _diff_stats(cwd: Path, base: str) -> tuple[int, int] | None:
    """Return ``(files_changed, lines_changed)`` or ``None``."""
    out = _run(["git", "diff", "--numstat", f"{base}...HEAD"], cwd)
    if out is None:
        return None
    files = 0
    lines = 0
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) != 3:
            continue
        files += 1
        for n in parts[:2]:
            if n.isdigit():
                lines += int(n)
    return files, lines


def _test_count_decreased(cwd: Path, base: str) -> bool | None:
    """True when the diff removes more test functions than it adds."""
    out = _run(["git", "diff", "-U0", f"{base}...HEAD"], cwd)
    if out is None:
        return None
    added = removed = 0
    pat = re.compile(r"^[+-]\s*(?:func\s+Test|def\s+test_|it\(|test\()")
    for line in out.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if pat.match(line):
            if line[0] == "+":
                added += 1
            else:
                removed += 1
    return removed > added


def _pr_body(cwd: Path) -> str | None:
    out = _run(["gh", "pr", "view", "--json", "body,baseRefName"], cwd)
    if out is None:
        return None
    try:
        return json.dumps(json.loads(out))
    except ValueError:
        return None


def merge_gate(
    *,
    require_contract: bool = True,
    degraded_marker: str = "degraded-review",
    review_marker: str = "cross-vendor-review: passed",
):
    """Build the merge-gate evaluator.

    :param require_contract: When ``True`` (default) a merge attempted in a
        repo with no ``.agents/orchestration.yaml`` is ASKed rather than
        allowed — an unconfigured project should not inherit auto-merge.
    :param degraded_marker: Substring in the PR body meaning review was not
        cross-vendor. Its presence always forces ASK.
    :param review_marker: Substring the orchestrator writes once an
        independent cross-vendor review has passed.
    :returns: A policy evaluator callable.
    """

    def evaluate(event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("type") != "tool_call":
            return None
        data = event.get("data") or {}
        if data.get("name") not in _SHELL_TOOLS:
            return None
        args = data.get("arguments") or {}
        cmd = args.get("command") or args.get("cmd") or ""
        if not isinstance(cmd, str) or not cmd.strip():
            return None

        is_merge = bool(_GH_MERGE.search(cmd) or _GIT_MERGE.search(cmd))
        is_push = bool(_GIT_PUSH.search(cmd))
        if not (is_merge or is_push):
            return None

        cwd = Path(args.get("cwd") or ".").expanduser()
        if not cwd.is_absolute():
            cwd = Path.cwd() / cwd
        contract = _load_contract(cwd)

        branches = (contract or {}).get("branches") or {}
        protected = set(_ALWAYS_PROTECTED) | {
            str(b) for b in (branches.get("protected") or [])
        }
        base = str(branches.get("integration_base") or "")
        policy = (contract or {}).get("merge_policy") or {}

        # --- absolute denials, contract or not -------------------------
        if is_push and _FORCE.search(cmd):
            for ref in protected | ({base} if base else set()):
                if ref and re.search(rf"(?:^|[\s:/]){re.escape(ref)}(?:\s|$)", cmd):
                    return {
                        "result": "DENY",
                        "reason": (
                            f"Force-push targeting protected ref '{ref}'. "
                            "Open a PR instead."
                        ),
                    }
        if is_merge:
            for ref in protected:
                if re.search(rf"(?:^|[\s:/]){re.escape(ref)}(?:\s|$)", cmd):
                    return {
                        "result": "DENY",
                        "reason": (
                            f"'{ref}' is a protected branch. Promotions to it are "
                            "performed by a human, with a merge commit."
                        ),
                    }

        if is_push and not is_merge:
            return None  # ordinary branch pushes are not this gate's business

        # --- from here on: a merge ------------------------------------
        if contract is None:
            if require_contract:
                return {
                    "result": "ASK",
                    "reason": (
                        f"No {_CONTRACT_NAME} found for this repository, so the "
                        "merge rules are unknown. Confirm this merge manually."
                    ),
                }
            return {"result": "ASK", "reason": "No orchestration contract found."}

        if not policy.get("enabled", False):
            return {
                "result": "ASK",
                "reason": (
                    "Auto-merge is disabled for this repository "
                    "(merge_policy.enabled: false). A human performs the merge."
                ),
            }

        target = str(policy.get("auto_merge_target") or base or "")
        if target and not re.search(
            rf"(?:^|[\s:/]){re.escape(target)}(?:\s|$)", cmd
        ):
            # Merging somewhere other than the single sanctioned target.
            return {
                "result": "ASK",
                "reason": (
                    f"Auto-merge is sanctioned only into '{target}'. This command "
                    "targets something else; confirm manually."
                ),
            }

        reasons: list[str] = []

        # Diff-derived checks. Any indeterminate result becomes an ASK.
        files = _changed_files(cwd, target) if target else None
        if files is None:
            reasons.append("the diff could not be read")
        else:
            risky = sorted(
                {
                    f
                    for f in files
                    for pat in (policy.get("high_risk_paths") or [])
                    if _glob_match(f, str(pat))
                }
            )
            if risky:
                shown = ", ".join(risky[:5])
                more = f" (+{len(risky) - 5} more)" if len(risky) > 5 else ""
                reasons.append(f"high-risk paths touched: {shown}{more}")

        stats = _diff_stats(cwd, target) if target else None
        if stats is None:
            reasons.append("diff size could not be measured")
        else:
            n_files, n_lines = stats
            max_files = policy.get("max_diff_files")
            max_lines = policy.get("max_diff_lines")
            if isinstance(max_files, int) and n_files > max_files:
                reasons.append(f"{n_files} files changed (limit {max_files})")
            if isinstance(max_lines, int) and n_lines > max_lines:
                reasons.append(f"{n_lines} lines changed (limit {max_lines})")

        if policy.get("require_no_test_count_decrease", True):
            dec = _test_count_decreased(cwd, target) if target else None
            if dec is None:
                reasons.append("test-count delta could not be determined")
            elif dec:
                reasons.append("the diff removes more tests than it adds")

        body = _pr_body(cwd)
        if body is None:
            reasons.append("the PR body could not be read")
        else:
            low = body.lower()
            if degraded_marker.lower() in low:
                reasons.append("review was flagged degraded (not cross-vendor)")
            elif policy.get("require_cross_vendor_review", True) and (
                review_marker.lower() not in low
            ):
                reasons.append("no recorded cross-vendor review")

        if reasons:
            return {
                "result": "ASK",
                "reason": "Human approval required — " + "; ".join(reasons) + ".",
            }

        return {
            "result": "ALLOW",
            "reason": (
                f"Auto-merge into '{target}': gates green, cross-vendor review "
                "recorded, no high-risk paths, diff within limits."
            ),
        }

    return evaluate


POLICY_REGISTRY = [
    {
        "handler": "omnigent_local_policies.merge_gate",
        "kind": "factory",
        "name": "Merge Gate",
        "description": (
            "Risk-tiered merge gate. Denies merges into protected branches, "
            "allows auto-merge into the sanctioned target only when the diff, "
            "paths, tests and recorded review all check out, and asks in every "
            "other case including anything it cannot determine."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "require_contract": {"type": "boolean", "default": True},
                "degraded_marker": {"type": "string", "default": "degraded-review"},
                "review_marker": {
                    "type": "string",
                    "default": "cross-vendor-review: passed",
                },
            },
        },
    }
]


# ---------------------------------------------------------------------------
# Branch-cleanup-aware blast radius
# ---------------------------------------------------------------------------
#
# Why this exists
# ---------------
# ``omnigent.policies.builtins.orchestration.blast_radius`` classifies EVERY
# remote-branch deletion (``git push --delete`` or a ``:ref`` refspec) as
# irreversible and returns DENY. That tier is unconditional: it applies even
# with ``gate_pushes: false``, and DENY is not approvable, so the human is
# never prompted either.
#
# The observed consequence: after the orchestrator's PR merges, post-merge
# cleanup (``git push origin --delete feature/…`` / ``integration/…``) is
# hard-blocked, the orchestrator correctly refuses to route around a
# guardrail, and every run strands its short-lived branches on the remote.
# Bundling the deletion with benign cleanup (``git branch -D``,
# ``git fetch --prune``) makes it worse: the whole compound command dies on
# the one offending statement.
#
# Deleting a merged ``feature/<slug>`` is not irreversible in any meaningful
# sense — its commits are already reachable from the integration base and the
# ref is recreatable from the PR. Deleting ``main`` is a different act. This
# policy draws that line; everything else stays with the builtin.
#
# Decision model
# --------------
# Delegate to the builtin evaluator, then override its verdict to ALLOW only
# when the *sole* reason it objected is one or more remote-branch deletions
# that are all provably safe:
#
#   * the statement is a ``git push`` carrying ``--delete`` / ``-d`` / a
#     ``:ref`` refspec — never ``--force*``, ``-f``, ``--mirror`` or
#     ``--prune`` (a mass prune is not a named deletion and stays DENY);
#   * every ref it names starts with one of *allow_prefixes*; and
#   * no ref is protected — ``_ALWAYS_PROTECTED`` unioned with the repo
#     contract's ``branches.protected`` and ``branches.integration_base``.
#
# Anything else keeps the builtin's verdict: an unrecognised ref, a deletion
# mixed with an ``rm -rf``, an unreadable contract, a force-push. The override
# is permissive on one narrow shape only — it can never turn a builtin ALLOW
# into a denial, and it never widens the force-push or ``rm -rf`` tiers.

from omnigent.policies.builtins import orchestration as _blast  # noqa: E402

# Branch namespaces an orchestrator creates and is therefore expected to clean up.
_CLEANUP_PREFIXES: tuple[str, ...] = ("feature/", "bugfix/", "fix/", "hotfix/", "integration/")


def _push_delete_refs(argv: list[str]) -> list[str] | None:
    """Return the branch names a ``git push`` statement deletes.

    :param argv: One statement's tokens, e.g.
        ``["git", "push", "origin", "--delete", "feature/x"]``.
    :returns: The deleted ref names, or ``None`` when the statement is not a
        pure named-deletion push — not a push at all, a force-push, a
        ``--mirror``/``--prune``, or a push that deletes nothing.
    """
    i = _blast._command_index_after_shell_prefixes(argv)
    if i >= len(argv) or argv[i] != "git":
        return None
    j = i + 1
    while j < len(argv) and argv[j].startswith("-"):
        j += 2 if argv[j] in _blast._GIT_GLOBAL_VALUE_OPTS and j + 1 < len(argv) else 1
    if j >= len(argv) or argv[j] != "push":
        return None

    deletes = False
    refs: list[str] = []
    remote_seen = False
    skip_next = False
    for tok in argv[j + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("--force") or tok in ("--mirror", "--prune"):
            return None  # force / mass-prune is never "just cleanup"
        if tok == "--delete":
            deletes = True
            continue
        if tok in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            skip_next = True
            continue
        if tok.startswith("--"):
            continue  # --quiet, --porcelain, --no-verify, …
        if tok.startswith("-") and len(tok) > 1:
            if "f" in tok[1:]:
                return None
            if "d" in tok[1:]:
                deletes = True
            continue
        if tok.startswith("+"):
            return None  # +refspec is a force-push
        if tok.startswith(":") and len(tok) > 1:
            deletes = True
            refs.append(tok[1:])
            continue
        if not remote_seen:
            remote_seen = True  # the remote name (origin, a URL, …)
            continue
        refs.append(tok)

    if not deletes or not refs:
        return None
    return refs


def _refs_are_safe_to_delete(
    refs: list[str], *, protected: set[str], allow_prefixes: tuple[str, ...]
) -> bool:
    """Whether every ref is a short-lived branch the orchestrator may delete."""
    for ref in refs:
        name = ref.split(":")[-1].strip()
        if name.startswith("refs/heads/"):
            name = name[len("refs/heads/") :]
        if not name or name in protected:
            return False
        if not any(name.startswith(p) for p in allow_prefixes):
            return False
    return True


def blast_radius_with_branch_cleanup(
    *,
    gate_pushes: bool = True,
    risky_action: str = "ASK",
    deny_reason: str = "Blocked by the blast-radius policy.",
    allow_prefixes: tuple[str, ...] = _CLEANUP_PREFIXES,
):
    """Build the builtin blast-radius gate, minus the merged-branch-cleanup blind spot.

    Identical to ``omnigent.policies.builtins.orchestration.blast_radius`` in
    every respect but one: a ``git push`` deleting only non-protected branches
    under *allow_prefixes* is ALLOWed instead of DENYed, so an orchestrator can
    tidy up the branches it created.

    :param gate_pushes: Passed through. ``False`` enforces only the
        catastrophic DENY tier (trusted unattended runs).
    :param risky_action: Passed through (``"ASK"`` or ``"DENY"``).
    :param deny_reason: Passed through.
    :param allow_prefixes: Branch-name prefixes eligible for remote deletion,
        e.g. ``("feature/", "integration/")``. A YAML list is coerced.
    :returns: A policy evaluator ``fn(event, config)``.
    """
    inner = _blast.blast_radius(
        gate_pushes=gate_pushes, risky_action=risky_action, deny_reason=deny_reason
    )
    prefixes = tuple(str(p) for p in allow_prefixes)

    def evaluate(event: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        verdict = inner(event, config or {})
        if verdict.get("result") == "ALLOW":
            return verdict

        args = _blast._tool_call(event, _blast._SHELL_TOOLS)
        if args is None:
            return verdict
        command = args.get("command")
        if not isinstance(command, str):
            return verdict

        # The builtin's regex tier (git reset --hard <remote>/, gh pr merge,
        # infra apply/destroy) is out of scope for this override.
        if any(p.search(command) for p in _blast._DENY_PATTERNS):
            return verdict
        if gate_pushes and any(p.search(command) for p in _blast._ASK_PATTERNS):
            return verdict

        cwd = Path(args.get("cwd") or ".").expanduser()
        if not cwd.is_absolute():
            cwd = Path.cwd() / cwd
        contract = _load_contract(cwd)
        branches = (contract or {}).get("branches") or {}
        protected = set(_ALWAYS_PROTECTED) | {
            str(b) for b in (branches.get("protected") or [])
        }
        if branches.get("integration_base"):
            protected.add(str(branches["integration_base"]))

        # Every statement the builtin could have objected to must be a safe
        # deletion. One `rm -rf`, one plain push under `gate_pushes`, one
        # unrecognised ref — and the builtin's verdict stands.
        cleaned: list[str] = []
        for stmt in _blast._shell_statements(command):
            if _blast._rm_severity(stmt) is not None:
                return verdict
            if _blast._push_severity(stmt) is None:
                continue
            refs = _push_delete_refs(stmt)
            if refs is None or not _refs_are_safe_to_delete(
                refs, protected=protected, allow_prefixes=prefixes
            ):
                return verdict
            cleaned.extend(refs)

        if not cleaned:
            return verdict
        return {
            "result": "ALLOW",
            "reason": (
                "Remote deletion of merged short-lived branch(es) "
                + ", ".join(sorted(set(cleaned)))
                + " — recreatable from the PR, none protected."
            ),
        }

    return evaluate


POLICY_REGISTRY.append(
    {
        "handler": "omnigent_local_policies.blast_radius_with_branch_cleanup",
        "kind": "factory",
        "name": "Blast Radius (branch-cleanup aware)",
        "description": (
            "The builtin blast-radius gate, except that deleting a remote "
            "non-protected branch under a cleanup prefix (feature/, "
            "integration/, …) is ALLOWed instead of DENYed, so an orchestrator "
            "can remove the branches it created once its PR has merged."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "gate_pushes": {"type": "boolean", "default": True},
                "risky_action": {"type": "string", "default": "ASK"},
                "deny_reason": {"type": "string"},
                "allow_prefixes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": list(_CLEANUP_PREFIXES),
                },
            },
        },
    }
)

__all__.append("blast_radius_with_branch_cleanup")
