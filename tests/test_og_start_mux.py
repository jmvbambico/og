"""Guards for `og start`'s multiplexer keyword and the `og help` block.

None of these tests may start a server: they would collide with a live
Omnigent, and `og start` is exactly the thing under test. So the parser is
driven by SOURCING bin/og in a subshell (which only defines functions; the
top-level `case` runs the no-argument help arm and is discarded) and calling
`parse_start_args` directly. That function exists precisely because the
parsing was split out of cmd_start to be callable this way.

HOME is always redirected to a throwaway directory, the pattern tests/
test_og_install.py already uses, so nothing here reads or writes ~/.omnigent.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# Overridable so the mux-strip trap can be proven against a mutated COPY of
# bin/og (see the test report), and so the suite can be pointed at an installed
# og — the installer copies bin/og out of the checkout, so the shipped file is
# the one that matters.
OG = Path(os.environ.get("OG_BIN") or REPO / "bin" / "og")

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"),
                                reason="bin/og is a bash script")


# --------------------------------------------------------------------------
# parse_start_args -- the mux keyword vs. the bare-argument ngrok domain
# --------------------------------------------------------------------------
# THE PARSING COLLISION these tests pin: before the multiplexer keyword, a bare
# argument to `og start` meant an ngrok reserved DOMAIN, so `og start herdr`
# already parsed as "tunneled on ngrok domain herdr". A multiplexer keyword has
# to be recognised and removed BEFORE that bare-argument arm sees it, or the new
# command quietly does something else. The mux-strip loop scans every positional
# (so `og start tunneled herdr` also works), and the bare-domain rows below are
# the regression: a reserved ngrok domain must behave exactly as before.
#
# The driver takes the script path as $0 and the argv to parse as the remaining
# positional parameters. `source "$0"` with NO extra args inherits the caller's
# positionals, so $1 must be the script path (not an og argument) — otherwise
# the top-level `case "${1:-}"` matches the path and calls die. `set --` clears
# them so the sourced help arm is the one that runs and is thrown away.
_PARSE_DRIVER = r'''
src="$0"; args=("$@")
set --
source "$src" >/dev/null 2>&1
parse_start_args ${args[@]+"${args[@]}"}
printf 'mode=%s\ndomain=%s\nprovider=%s\nmux=%s\n' \
  "${OG_START_MODE-}" "${OG_START_DOMAIN-}" "${OG_START_PROVIDER-}" "${OG_START_MUX-}"
'''


def _source_and_parse(home, argv):
    return subprocess.run(
        ["bash", "-c", _PARSE_DRIVER, str(OG), *argv],
        capture_output=True, text=True, timeout=30,
        # /usr/bin:/bin resolves bash to bash 3.2 on macOS — the oldest shell
        # bin/og claims to support (its empty-array idioms exist for exactly
        # that), so the parser is exercised under the strictest interpreter.
        env={"HOME": str(home), "PATH": "/usr/bin:/bin",
             "OG_REPO": str(REPO)},
    )


def _vars(result):
    got = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("mode", "domain", "provider", "mux"):
            got[key] = value
    return got


# argv -> (mux, mode, domain, provider). Verified against the implementation,
# not just the spec: a mux keyword is stripped from ANY position, mode/domain
# come from the SURVIVING positionals, and provider is ngrok unless --use says
# otherwise.
PARSE_CASES = [
    (["herdr"], "herdr", "local", "", "ngrok"),
    (["herdr", "tunneled"], "herdr", "tunneled", "", "ngrok"),
    (["tunneled", "herdr"], "herdr", "tunneled", "", "ngrok"),
    (["herdr", "tunneled", "--use", "tunnl"], "herdr", "tunneled", "", "tunnl"),
    (["tunneled", "mydomain"], "", "tunneled", "mydomain", "ngrok"),
    (["mydomain"], "", "tunneled", "mydomain", "ngrok"),
    ([], "", "local", "", "ngrok"),
    (["local"], "", "local", "", "ngrok"),
]


@pytest.mark.parametrize("argv,mux,mode,domain,provider", PARSE_CASES,
                         ids=[" ".join(c[0]) or "(empty)" for c in PARSE_CASES])
def test_parse_start_args(tmp_path, argv, mux, mode, domain, provider):
    result = _source_and_parse(tmp_path, argv)
    assert result.returncode == 0, result.stderr
    assert _vars(result) == {"mux": mux, "mode": mode,
                             "domain": domain, "provider": provider}


def test_bare_ngrok_domain_still_reads_as_a_tunneled_domain(tmp_path):
    """The exact behaviour the mux keyword could have broken: `og start
    mydomain` is still an ngrok reserved domain, and still says so. The
    explicit `tunneled mydomain` form parses identically but is a different
    case arm, so it must NOT print the bare-argument disclaimer."""
    bare = _source_and_parse(tmp_path, ["mydomain"])
    assert bare.returncode == 0, bare.stderr
    assert _vars(bare) == {"mux": "", "mode": "tunneled",
                           "domain": "mydomain", "provider": "ngrok"}
    assert "a bare argument is read as an ngrok domain" in bare.stdout
    assert "og start tunneled mydomain" in bare.stdout

    explicit = _source_and_parse(tmp_path, ["tunneled", "mydomain"])
    assert explicit.returncode == 0, explicit.stderr
    assert _vars(explicit) == _vars(bare)
    assert "bare argument" not in explicit.stdout


def test_a_non_mux_bare_argument_is_still_a_domain(tmp_path):
    """The mux-strip loop must remove ONLY registry ids: an arbitrary word is
    left for the bare-argument arm rather than swallowed."""
    result = _source_and_parse(tmp_path, ["some-other-name"])
    assert result.returncode == 0, result.stderr
    assert _vars(result) == {"mux": "", "mode": "tunneled",
                             "domain": "some-other-name", "provider": "ngrok"}


# --------------------------------------------------------------------------
# `og help` -- the usage block must list every command the dispatch handles
# --------------------------------------------------------------------------
# bin/og prints usage with `sed -n '2,26p'` over its own header comment. The
# range is a number, and a number rots: adding a command without widening the
# range silently drops it off the end — which is how `og version` went missing
# once already. This is the guard the comment beside that sed line names.
_DISPATCH_CASE = re.compile(r'\ncase "\$\{1:-\}" in\n(.*?)\nesac\n', re.S)


def _dispatched_commands():
    """The canonical command names in the top-level dispatch case.

    Arms sit at exactly two spaces; the explanatory comment above the sed
    range sits at four and must not be read as an arm, so the pattern is
    anchored to `\\S` at column three.
    """
    match = _DISPATCH_CASE.search(OG.read_text())
    assert match, "top-level dispatch `case` not found in bin/og"
    arms = re.findall(r'^  (\S[^)\n]*)\)', match.group(1), re.M)
    commands = []
    for arm in arms:
        first = arm.split("|")[0].strip()
        # Skip the wildcard arm and the help arm (whose first alternative is
        # the empty string); `version|--version|-V` contributes `version`.
        if first and first != "*" and not first.startswith('"'):
            commands.append(first)
    return commands


def test_help_block_lists_every_dispatched_command(tmp_path):
    result = subprocess.run(
        ["bash", str(OG), "help"], capture_output=True, text=True, timeout=30,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    help_block = result.stdout

    commands = _dispatched_commands()
    assert commands, "parsed no dispatch arms — did the case statement move?"
    # Rendered exactly as `og help` renders it (the same `sed -n '2,26p'`), so
    # a command documented outside the range fails here rather than staying
    # invisible. The seam is the RANGE, not just the command name.
    missing = [c for c in commands
               if not re.search(rf"^\s*og {re.escape(c)}\b", help_block, re.M)]
    assert not missing, f"dispatched but absent from `og help`: {missing}"


# --------------------------------------------------------------------------
# graceful degradation -- a missing multiplexer is a warning, never fatal
# --------------------------------------------------------------------------
def _path_without(*binaries):
    """The current PATH minus the directories containing `binaries`.

    Removing the binary's own directory (rather than prepending a stub dir)
    keeps every other real tool available, so `og` can run far enough to show
    what it does when the multiplexer is absent.
    """
    drop = {os.path.realpath(os.path.dirname(p))
            for p in (shutil.which(b) for b in binaries) if p}
    kept = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and os.path.realpath(d) not in drop]
    return os.pathsep.join(kept)


def test_og_start_herdr_warns_and_degrades_without_herdr(tmp_path):
    """`og start herdr` on a machine with no herdr must warn and start
    normally, not fail. The warn-and-continue lives inline in cmd_start (the
    mux check at the top of it), which is not separable from starting a server,
    so this drives the real command exactly far enough to prove the degrade and
    no further: it stops at the NEXT precondition — the sandbox has no agent
    bundle — without ever reaching a server or a multiplexer."""
    home = tmp_path / "home"
    home.mkdir()
    path = _path_without("herdr", "omnigent")
    assert shutil.which("herdr", path=path) is None, "PATH still exposes herdr"

    result = subprocess.run(
        ["bash", str(OG), "start", "herdr"], capture_output=True, text=True,
        timeout=60,
        # OG_SKIP_UPDATE stops `og start`'s version check from going to the
        # network before it reaches the mux guard.
        env={"HOME": str(home), "OG_REPO": str(REPO), "OG_SKIP_UPDATE": "1",
             "PATH": path},
    )

    # The warning names the missing multiplexer and where to get it.
    assert "herdr not found on PATH" in result.stdout
    assert "starting without it" in result.stdout
    assert "https://herdr.dev/" in result.stdout

    # Non-fatal, PROVEN: control ran PAST the mux guard and died at the next,
    # unrelated precondition instead. A fatal mux path would never get here.
    assert "error:" in result.stderr and "agent bundle not found" in result.stderr
    # The warning goes to stdout; nothing mux-related reaches stderr.
    assert "herdr not found" not in result.stderr
    assert "multiplexer" not in result.stderr

    # And nothing was started, in the sandbox or (by construction) anywhere.
    assert result.returncode != 0
    assert not (home / ".omnigent" / "og-server.pid").exists()
    assert not (home / ".omnigent" / "logs" / "server").exists()


# --------------------------------------------------------------------------
# the herdr bridge is started for a mux launch, and stopped by `og stop`
# --------------------------------------------------------------------------
# The daemon helpers are driven by SOURCING bin/og (functions only; the
# top-level help arm prints usage and is discarded) and calling them directly,
# the same seam parse_start_args uses. Driving `og start` to the bridge would
# start a real server, and the real `og stop` body would reap the operator's
# live tmux terminals — so the helpers run in isolation against a STUB bridge.
# OG_REPO is pointed at the stub's fake checkout, so start_herdr_bridge spawns a
# sleeper and never the real bridge, which would connect to a herdr socket.

def _run_sourced(home, body, *, env=None, timeout=30):
    """Source bin/og in a throwaway HOME and run the bash snippet `body`."""
    environment = {
        "HOME": str(home),
        "OG_REPO": str(REPO),
        # The real PATH (plus the POSIX dirs) so the stub's python3 resolves;
        # OG_REPO is what keeps the REAL bridge — and the herdr socket — out.
        "PATH": os.environ.get("PATH", "") + os.pathsep + "/usr/bin:/bin",
    }
    if env:
        environment.update(env)
    return subprocess.run(
        ["bash", "-c",
         'src="$0"; set --; source "$src" >/dev/null 2>&1; ' + body, str(OG)],
        capture_output=True, text=True, timeout=timeout, env=environment)


def _stub_checkout(tmp_path):
    """A fake og checkout whose installer/og_herdr.py just sleeps."""
    repo = tmp_path / "stub-repo"
    (repo / "installer").mkdir(parents=True)
    (repo / "installer" / "og_herdr.py").write_text(
        "import time\ntime.sleep(300)\n")
    return repo


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_a_mux_launch_starts_the_bridge_and_starts_it_only_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stub = _stub_checkout(tmp_path)
    body = "\n".join([
        "start_herdr_bridge",
        'printf "first=%s\\n" "$(cat "$OG_HERDR_PIDFILE")"',
        "start_herdr_bridge",
        'printf "second=%s\\n" "$(cat "$OG_HERDR_PIDFILE")"',
    ])
    result = _run_sourced(home, body, env={"OG_REPO": str(stub)})
    assert result.returncode == 0, result.stderr

    first = re.search(r"^first=(\d+)$", result.stdout, re.M)
    second = re.search(r"^second=(\d+)$", result.stdout, re.M)
    assert first and second, result.stdout
    # The second call saw a live pid and did NOT start a second bridge.
    assert "already running" in result.stdout
    assert first.group(1) == second.group(1)

    pid = int(first.group(1))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pytest.fail("the stub bridge is not running")
    finally:
        _kill(pid)
    # The pidfile lives beside the server/host pidfiles, and the log where the
    # other daemons log, so `og stop` and `og logs` can both find the bridge.
    assert (home / ".omnigent" / "og-herdr.pid").exists()
    assert list((home / ".omnigent" / "logs" / "herdr").glob("herdr-*.log"))


def test_og_stop_stops_the_bridge(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stub = _stub_checkout(tmp_path)
    started = _run_sourced(
        home, 'start_herdr_bridge\ncat "$OG_HERDR_PIDFILE"\n',
        env={"OG_REPO": str(stub)})
    assert started.returncode == 0, started.stderr
    pid = int(started.stdout.strip().splitlines()[-1])
    os.kill(pid, 0)  # alive before the stop
    try:
        # A stop sandbox that CANNOT reach the operator's live server or live
        # tmux terminals: a fake omnigent reports "not running" (so `omnigent
        # stop` is never invoked), and TMPDIR points at an empty dir so
        # orphan_terminal_dirs sweeps nothing.
        fakebin = tmp_path / "bin"
        fakebin.mkdir()
        omni = fakebin / "omnigent"
        omni.write_text('#!/bin/sh\n[ "$*" = "server status" ] && echo "not running"\nexit 1\n')
        omni.chmod(0o755)
        (fakebin / "python3").symlink_to(sys.executable)
        stop_home = tmp_path / "stop-home"
        (stop_home / ".omnigent").mkdir(parents=True)
        (stop_home / ".omnigent" / "og-herdr.pid").write_text(str(pid))
        empty_tmp = tmp_path / "empty-tmp"
        empty_tmp.mkdir()

        result = subprocess.run(
            ["bash", str(OG), "stop"], capture_output=True, text=True,
            timeout=30,
            env={"HOME": str(stop_home), "TMPDIR": str(empty_tmp),
                 "PATH": str(fakebin) + ":/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert "stopping the herdr bridge" in result.stdout

        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not (stop_home / ".omnigent" / "og-herdr.pid").exists()
    finally:
        _kill(pid)


# --------------------------------------------------------------------------
# a mux launch must not auto-open a browser
# --------------------------------------------------------------------------
# The suppression must be observable, but cmd_start cannot be driven to the
# export without starting a server — so it lives in its own function (the same
# reason parse_start_args was split out) and cmd_start calls it.

def _autopen_after_call(home, *, mux, preset=None):
    env = {} if preset is None else {"OMNIGENT_ACCOUNTS_AUTO_OPEN": preset}
    body = "\n".join([
        'suppress_accounts_autopen "%s"' % mux,
        'printf "autopen=%s\\n" "${OMNIGENT_ACCOUNTS_AUTO_OPEN-unset}"',
    ])
    result = _run_sourced(home, body, env=env)
    assert result.returncode == 0, result.stderr
    match = re.search(r"^autopen=(.*)$", result.stdout, re.M)
    assert match, result.stdout
    return match.group(1)


def test_a_mux_launch_suppresses_the_browser_autopen(tmp_path):
    assert _autopen_after_call(tmp_path / "home", mux="herdr") == "0"


def test_an_explicit_autopen_setting_is_not_overridden(tmp_path):
    # The operator said what they want — either value — so leave it alone.
    home = tmp_path / "home"
    assert _autopen_after_call(home, mux="herdr", preset="1") == "1"
    assert _autopen_after_call(home, mux="herdr", preset="0") == "0"


def test_a_plain_start_is_not_suppressed(tmp_path):
    # No multiplexer: existing `og start` behaviour, unchanged.
    assert _autopen_after_call(tmp_path / "home", mux="") == "unset"


def test_cmd_start_calls_the_helpers_it_is_meant_to():
    """The helpers above are inert unless cmd_start calls them. Pin both call
    sites so a refactor that drops one fails here rather than at the operator."""
    text = OG.read_text()
    assert re.search(r'^\s*suppress_accounts_autopen "\$mux"$', text, re.M)
    assert re.search(r'^\s*start_herdr_bridge$', text, re.M)
