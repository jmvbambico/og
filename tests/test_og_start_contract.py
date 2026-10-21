"""Guards for `og start`'s orchestration-contract warning.

The bug these pin is invisibility, not failure: with no contract `og start`
serves fine and the merge policy fails closed, so the start looks healthy while
the orchestrator plans without gates, protected branches or an integration
base. The check therefore WARNS and always continues — none of these tests may
start a server, and none may expect a refusal.

They drive `check_orchestration_contract` by SOURCING bin/og in a subshell,
the same seam tests/test_og_start_mux.py uses for parse_start_args: sourcing
only defines functions (its top-level help arm is discarded). cwd is the
directory under test because bin/og captures OG_LAUNCH_DIR from $PWD at source
time — which is exactly the value it pins as the project's workspace, and so
the directory the policy is handed.

Kept in its own file rather than appended to test_og_start_mux.py: that file
already carries the mux parser, the launcher exec and the bridge, and this
check has its own four-state machine to explain.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OG = Path(os.environ.get("OG_BIN") or REPO / "bin" / "og")

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"),
                                reason="bin/og is a bash script")

VALID = """\
branches:
  integration_base: dev
  protected: [main]
gates:
  - name: unit
    run: make test
    scope: worker
"""

_CHECK_DRIVER = r'''
src="$0"; set --
source "$src" >/dev/null 2>&1
check_orchestration_contract
printf 'rc=%s\n' "$?"
'''

_NOCHECK_DRIVER = r'''
src="$0"; set --
source "$src" >/dev/null 2>&1
parse_start_args --no-contract-check
check_orchestration_contract
printf 'rc=%s\ncheck=%s\n' "$?" "${OG_START_CONTRACT_CHECK-}"
'''

# cmd_start must actually CALL the check, early — a perfect checker nobody
# invokes is the same silence this feature is about. Everything after the call
# is stubbed by the sandbox: no agent bundle, so it dies at that precondition
# and never reaches a server or a multiplexer.
_WIRING_DRIVER = r'''
src="$0"; set --
source "$src" >/dev/null 2>&1
check_orchestration_contract() { echo "CONTRACT-CHECK-RAN"; }
cmd_start local
'''


def _fake_python(tmp_path, *, no_yaml=False):
    """A PATH-only python3 that is the test venv's interpreter.

    A shell wrapper rather than a symlink: a symlink loses the venv's
    pyvenv.cfg lookup (and with it PyYAML) because sys.executable's directory
    is the wrapper's.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = "#!/bin/sh\n"
    if no_yaml:
        script += 'case "$1" in -) printf "noyaml\\t\\n"; exit 0 ;; esac\n'
    script += 'exec "$OG_TEST_PYTHON" "$@"\n'
    py = bindir / "python3"
    py.write_text(script)
    py.chmod(0o755)
    return bindir


def _env(tmp_path, *, no_yaml=False, extra=None):
    bindir = _fake_python(tmp_path, no_yaml=no_yaml)
    environment = {
        "HOME": str(tmp_path / "home"),
        "OG_REPO": str(REPO),
        # No `omnigent` here, so omnigent_python() fails and the check falls
        # back to this python3 — the same fallback a machine without the
        # launcher would take. /usr/bin:/bin keeps dirname etc. available and
        # resolves bash to bash 3.2 on macOS, the oldest shell bin/og claims.
        "PATH": str(bindir) + os.pathsep + "/usr/bin:/bin",
        "OG_TEST_PYTHON": sys.executable,
    }
    if extra:
        environment.update(extra)
    return environment


def _run_contract(workdir, tmp_path, *, driver=_CHECK_DRIVER, no_yaml=False,
                  extra=None):
    return subprocess.run(
        ["bash", "-c", driver, str(OG)],
        cwd=str(workdir), capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL,
        env=_env(tmp_path, no_yaml=no_yaml, extra=extra),
    )


def _write_contract(directory, text=VALID):
    agents = directory / ".agents"
    agents.mkdir(parents=True, exist_ok=True)
    contract = agents / "orchestration.yaml"
    contract.write_text(text)
    return contract


# --------------------------------------------------------------------------
# the offer -- only reachable on a terminal, so these need a pty
# --------------------------------------------------------------------------
_OFFER_DRIVER = r'''
src="$0"; set --
source "$src" >/dev/null 2>&1
check_orchestration_contract
echo "START-CONTINUED rc=$?"
'''

# The interview `og init` now runs is another process inside the subshell.
# Stub it with a sleeper so a SIGINT lands there without touching the real
# installer (which the other worker owns).
_INTERVIEW_DRIVER = r'''
src="$0"; set --
source "$src" >/dev/null 2>&1
cmd_init() { echo "INIT-INTERVIEW $*"; sleep 30; }
check_orchestration_contract
echo "START-CONTINUED rc=$?"
'''


# The pty work runs in a FRESH Python interpreter, not in the pytest process.
# `pty.fork()` from a multi-threaded process is explicitly deprecated (Python
# warns about deadlocks in the child), and earlier tests in the suite leave
# daemon threads behind: run in-process, the Ctrl-C tests passed alone and
# hung only in the full suite. A single-threaded child makes the behaviour
# depend on bash's signal handling rather than on unrelated test residue.
_PTY_HARNESS = r'''
import json, os, pty, select, signal, sys, time

workdir, driver, og, spec_json, timeout_s = sys.argv[1:6]

# Do not inherit the runner's attitude to SIGINT. A blocked or ignored SIGINT
# in this fresh process would survive exec into bash (the kernel keeps blocked
# masks and ignored dispositions), and the Ctrl-C tests would hang while the
# same tests pass outside a full-suite run. The operator's shell has INT at its
# default disposition; this makes the harness match it.
signal.signal(signal.SIGINT, signal.SIG_DFL)
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})

pending = list(json.loads(spec_json))
captured = b""
status = None
deadline = time.monotonic() + float(timeout_s)

pid, fd = pty.fork()
if pid == 0:  # pragma: no cover - child process
    try:
        os.chdir(workdir)
        os.execvpe("bash", ["bash", "-c", driver, og], os.environ)
    finally:
        os._exit(127)

while True:
    if time.monotonic() > deadline:
        os.killpg(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        sys.stdout.write(captured.decode(errors="replace"))
        sys.stdout.write("\n__EXIT__ timeout\n")
        raise SystemExit(3)
    ready, _, _ = select.select([fd], [], [], 0.2)
    if ready:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            chunk = b""
        captured += chunk
    while pending and pending[0]["marker"].encode() in captured:
        action = pending.pop(0)
        if "write" in action:
            os.write(fd, action["write"].encode())
        if "signal" in action:
            # A beat first: a human reads the prompt (or the interview) before
            # typing Ctrl-C, so the signal lands on a wait that is already in
            # progress rather than on a fork still setting itself up.
            time.sleep(0.2)
            os.killpg(pid, getattr(signal, action["signal"]))
    done, wstatus = os.waitpid(pid, os.WNOHANG)
    if done:
        status = wstatus
        while True:  # drain anything written between the last read and exit
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                break
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            captured += chunk
        break

try:
    os.close(fd)
except OSError:
    pass
code = (os.WEXITSTATUS(status) if os.WIFEXITED(status)
        else 128 + os.WTERMSIG(status))
sys.stdout.write(captured.decode(errors="replace"))
sys.stdout.write("\n__EXIT__ %d\n" % code)
raise SystemExit(0 if code == 0 else 1)
'''


def _pty_run(workdir, tmp_path, driver, actions, timeout=20):
    """Run `driver` on a real pty and fire marker-triggered actions.

    A pty is the only way to reach `[[ -t 0 ]]`; faking a terminal for bash's
    `test -t` is not portable. Returns the harness's CompletedProcess: exit 0
    means the bash under test exited 0, and `stdout` is everything it wrote.
    """
    result = subprocess.run(
        [sys.executable, "-c", _PTY_HARNESS, str(workdir), driver,
         str(OG), json.dumps(actions), str(timeout)],
        capture_output=True, text=True, timeout=timeout + 10,
        env=_env(tmp_path),
    )
    if result.returncode != 0 and "__EXIT__ timeout" in result.stdout:
        pytest.fail("pty run timed out:\n" + result.stdout)
    return result


def test_the_broken_contract_offer_says_force_overwrites_and_names_the_file(
        tmp_path):
    """A prompt that hides a destructive flag is worse than no prompt. This is
    the branch where the operator may be mid-edit on the very file that broke,
    so the wording must name it and say it is replaced."""
    repo = tmp_path / "repo"
    repo.mkdir()
    contract = _write_contract(repo, "gates: [unclosed\n")
    result = _pty_run(
        repo, tmp_path, _OFFER_DRIVER,
        [{"marker": "[y/N]", "write": "n\n"}])
    text = result.stdout

    assert result.returncode == 0, text
    assert "Replace " in text
    assert str(contract) in text
    assert "overwrite" in text
    assert "ask a few questions" in text
    assert "START-CONTINUED rc=0" in text


def test_the_no_contract_offer_does_not_borrow_the_overwrite_warning(tmp_path):
    """Nothing is destroyed when no contract exists, so this prompt must not
    inherit scary wording it has not earned — but it should still set the
    expectation that og init asks questions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _pty_run(
        repo, tmp_path, _OFFER_DRIVER,
        [{"marker": "[y/N]", "write": "n\n"}])
    text = result.stdout

    assert result.returncode == 0, text
    assert "Run 'og init'" in text
    assert "ask a few questions" in text
    assert "overwrite" not in text
    assert "Replace " not in text
    assert "START-CONTINUED rc=0" in text


def test_a_broken_parent_contract_is_replaced_in_place_not_shadowed(tmp_path):
    """The policy walks up, so a broken contract may live above the launch
    directory. `og init --force` then has to run against THAT directory:
    scaffolding one below it would only shadow the broken file, leaving it to
    resurface for anything started from the parent."""
    repo = tmp_path / "repo"
    child = repo / "packages" / "worker"
    child.mkdir(parents=True)
    contract = _write_contract(repo, "gates: [unclosed\n")
    result = _pty_run(
        child, tmp_path, _INTERVIEW_DRIVER,
        [{"marker": "[y/N]", "write": "y\n"},
         {"marker": "INIT-INTERVIEW", "signal": "SIGINT"}])
    text = result.stdout.replace("\r\n", "\n")

    assert result.returncode == 0, text
    assert f"Replace {contract}" in text
    assert f"INIT-INTERVIEW --force {repo}\n" in text
    assert f"--force {child}" not in text


def test_ctrl_c_at_the_offer_does_not_take_the_start_down(tmp_path):
    """SIGINT goes to the whole foreground process group, og included. The
    offer is a kindness; it must not be able to kill the start."""
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _pty_run(
        repo, tmp_path, _OFFER_DRIVER,
        [{"marker": "[y/N]", "signal": "SIGINT"}])
    text = result.stdout

    assert result.returncode == 0, text
    assert "START-CONTINUED rc=0" in text
    assert "interrupted" in text


def test_eof_at_the_offer_declines_and_continues(tmp_path):
    """Ctrl-D is not consent, and must not be read as one. Our own read sees
    EOF, treats it as "no", and the start continues."""
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _pty_run(
        repo, tmp_path, _OFFER_DRIVER,
        [{"marker": "[y/N]", "write": "\x04"}])
    text = result.stdout

    assert result.returncode == 0, text
    assert "START-CONTINUED rc=0" in text
    assert "running og init" not in text


def test_ctrl_c_during_the_init_interview_does_not_take_the_start_down(
        tmp_path):
    """Accepting the offer now leads into an interview. A Ctrl-C there hits og
    too; the scaffold is abandoned, not the start."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_contract(repo, "gates: [unclosed\n")
    result = _pty_run(
        repo, tmp_path, _INTERVIEW_DRIVER,
        [{"marker": "[y/N]", "write": "y\n"},
         {"marker": "INIT-INTERVIEW", "signal": "SIGINT"}])
    text = result.stdout

    assert result.returncode == 0, text
    assert "INIT-INTERVIEW" in text
    assert "START-CONTINUED rc=0" in text
    assert "did not finish" in text or "interrupted" in text


# --------------------------------------------------------------------------
# no contract anywhere: name what is lost, do not just say "not found"
# --------------------------------------------------------------------------
def test_no_contract_anywhere_names_the_consequences_and_continues(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _run_contract(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout
    # The consequence, not just the absence: an operator who reads
    # "no contract found" learns nothing about why that matters.
    assert "no orchestration contract" in result.stderr
    assert "no gates" in result.stderr
    assert "no protected branches" in result.stderr
    assert "no integration base" in result.stderr
    assert "fails closed" in result.stderr
    # Non-interactive: warned, and did not ask.
    assert "[y/N]" not in result.stderr
    assert "rc=0" in result.stdout


def test_non_interactive_never_prompts_and_returns_success(tmp_path):
    """CI and scripted starts must not sit on a read. stdin is not a terminal,
    so the warning is the whole interaction and the function reports success."""
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _run_contract(repo, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout
    assert "Run 'og init" not in result.stderr
    assert "[y/N]" not in result.stderr


# --------------------------------------------------------------------------
# THE regression that matters: the policy walks UP, so this must too
# --------------------------------------------------------------------------
def test_contract_in_a_parent_directory_is_found(tmp_path):
    """A checker that only looked in the launch directory would warn while the
    policy happily loaded a parent's contract — teaching the operator to
    distrust the warning. This is the same stop-at-first-match walk
    _load_contract does through the filesystem root."""
    repo = tmp_path / "repo"
    nested = repo / "packages" / "worker"
    nested.mkdir(parents=True)
    _write_contract(repo)
    result = _run_contract(nested, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "rc=0" in result.stdout


def test_the_nearest_contract_wins_and_a_broken_one_stops_the_walk(tmp_path):
    """_load_contract returns None IMMEDIATELY when the first file it finds
    does not parse — it does not keep looking upward. A checker that fell back
    to a healthy grandparent would report success while the policy had none."""
    repo = tmp_path / "repo"
    child = repo / "child"
    child.mkdir(parents=True)
    _write_contract(repo)  # valid, in the grandparent
    broken = _write_contract(child, "gates: [unclosed\n")
    result = _run_contract(child, tmp_path)

    assert result.returncode == 0, result.stderr
    assert str(broken) in result.stderr
    assert "does not parse" in result.stderr
    assert "rc=0" in result.stdout


# --------------------------------------------------------------------------
# found but broken: the state that currently looks identical to working
# --------------------------------------------------------------------------
def test_unparseable_contract_warns_and_the_start_continues(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    contract = _write_contract(repo, "gates: [unclosed\n")
    result = _run_contract(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    assert str(contract) in result.stderr
    assert "does not parse" in result.stderr
    assert "no gates" in result.stderr
    assert "rc=0" in result.stdout


def test_non_mapping_top_level_warns(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    contract = _write_contract(repo, "- one\n- two\n")
    result = _run_contract(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    assert str(contract) in result.stderr
    assert "not a mapping" in result.stderr
    assert "rc=0" in result.stdout


def test_missing_pyyaml_is_its_own_machine_problem(tmp_path):
    """The policy's `import yaml` failing means NO contract loads, whatever is
    on disk. That is a machine/install problem and must not be reported as a
    broken repo file (which is what a bare 'does not parse' would imply)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    contract = _write_contract(repo)
    result = _run_contract(repo, tmp_path, no_yaml=True)

    assert result.returncode == 0, result.stderr
    assert "PyYAML" in result.stderr
    assert "machine/install problem" in result.stderr
    assert str(contract) in result.stderr
    assert "does not parse" not in result.stderr
    assert "rc=0" in result.stdout


# --------------------------------------------------------------------------
# the common case is quiet, and the escape hatch silences everything
# --------------------------------------------------------------------------
def test_a_valid_contract_produces_no_warning(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_contract(repo)
    result = _run_contract(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "rc=0" in result.stdout


def test_no_contract_check_silences_every_state(tmp_path):
    """The flag is parsed by parse_start_args and silences the whole check —
    including the missing-contract state, which is the noisy one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _run_contract(repo, tmp_path, driver=_NOCHECK_DRIVER)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "check=0" in result.stdout
    assert "rc=0" in result.stdout


# --------------------------------------------------------------------------
# wiring: cmd_start runs the check early, and never lets it block
# --------------------------------------------------------------------------
def test_cmd_start_calls_the_contract_check_before_any_side_effect(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["bash", "-c", _WIRING_DRIVER, str(OG)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL,
        env={
            "HOME": str(home),
            "OG_REPO": str(REPO),
            "OG_SKIP_UPDATE": "1",
            "PATH": os.environ.get("PATH", "") + os.pathsep + "/usr/bin:/bin",
        },
    )

    combined = result.stdout + result.stderr
    assert "CONTRACT-CHECK-RAN" in combined
    # It ran, and control went PAST it: the sandbox has no agent bundle, which
    # is the next precondition. A warning that returned non-zero under set -e
    # would kill cmd_start right here instead.
    assert "agent bundle not found" in result.stderr
    assert combined.index("CONTRACT-CHECK-RAN") < combined.index(
        "agent bundle not found")
