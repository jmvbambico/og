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
import shlex
import shutil
import signal
import socket
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
# the launcher's OWN flags — `og start herdr --once`
# --------------------------------------------------------------------------
# `--once` is the launcher's documented diagnostic (it does the work, prints it,
# and returns instead of exec'ing the multiplexer), and until now it could not be
# TYPED: cmd_start exec'd the launcher with a fixed argv and parse_start_args
# refused every option but --use, so `og start herdr --once` died here as an
# unknown option on its way to a parser that has it. It is the one mode in which
# the launcher's warnings outlive the exec — which is exactly why the failure it
# was meant to explain could not be reproduced with it.
#
# The flags collected here are the launcher's own (og_herdr_launch
# .build_parser) MINUS the two cmd_start derives itself; see the parse loop for
# why --mux/--server are excluded. Everything else still dies in this function:
# a flag that is accepted and quietly discarded is the defect being fixed, one
# layer up.

_FORWARD_DRIVER = r'''
src="$0"; args=("$@")
set --
source "$src" >/dev/null 2>&1
parse_start_args ${args[@]+"${args[@]}"} || exit 1
printf 'mux=%s\nforwarded=%s\n' "${OG_START_MUX-}" "${OG_START_MUX_ARGS[*]-}"
'''


def _forward(result):
    got = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("mux", "forwarded"):
            got[key] = value
    return got


# argv -> (mux, what the launcher is handed). The empty rows are the regression
# guard: an `og start` with no launcher flag must forward NOTHING, or the exec
# grows a stray argument on every start on the planet.
FORWARD_CASES = [
    (["herdr", "--once"], "herdr", ["--once"]),
    (["--once", "herdr"], "herdr", ["--once"]),          # mux stripped anywhere
    (["herdr", "tunneled", "--once"], "herdr", ["--once"]),
    (["herdr", "--socket", "/tmp/h.sock"], "herdr",
     ["--socket", "/tmp/h.sock"]),
    (["herdr", "--socket=/tmp/h.sock"], "herdr", ["--socket", "/tmp/h.sock"]),
    (["herdr", "--bin", "/opt/herdr"], "herdr", ["--bin", "/opt/herdr"]),
    (["herdr"], "herdr", []),
    ([], "", []),
    (["local"], "", []),
    (["tunneled", "mydomain"], "", []),
]


@pytest.mark.parametrize("argv,mux,forwarded", FORWARD_CASES,
                         ids=[" ".join(c[0]) or "(empty)" for c in FORWARD_CASES])
def test_a_launcher_flag_is_forwarded_and_everything_else_is_not(
        tmp_path, argv, mux, forwarded):
    result = _source_and_forward(tmp_path, argv)
    assert result.returncode == 0, result.stderr
    assert _forward(result) == {"mux": mux,
                                "forwarded": " ".join(forwarded)}


def _source_and_forward(home, argv):
    return subprocess.run(
        ["bash", "-c", _FORWARD_DRIVER, str(OG), *argv],
        capture_output=True, text=True, timeout=30,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin",
             "OG_REPO": str(REPO)},
    )


def test_a_launcher_flag_with_no_multiplexer_is_refused_not_swallowed(tmp_path):
    """There is nothing to forward it to, and accepting it anyway would leave the
    operator believing `--once` did something. A multiplexer whose BINARY is
    missing is a different question — that one is cmd_start's, and it is never
    fatal (see `test_og_start_herdr_warns_and_degrades_without_herdr`)."""
    result = _source_and_forward(tmp_path, ["--once"])
    assert result.returncode != 0
    assert "--once is a flag of 'og start <mux>'" in result.stderr
    assert "og start herdr --once" in result.stderr


def test_an_option_the_launcher_does_not_have_is_still_an_unknown_option(tmp_path):
    """The refusal is not a hole everything falls through. `--mux` and `--server`
    land here too, on purpose: cmd_start derives both, and a user override would
    point the launcher at another space or another server with nothing to show
    for it."""
    for flag in ("--nope", "--mux", "--server"):
        result = _source_and_forward(tmp_path, [flag])
        assert result.returncode != 0, flag
        assert "unknown option: {0}".format(flag) in result.stderr


@pytest.mark.parametrize("typed", ["--socket", "--socket=", "--bin", "--bin="],
                         ids=["socket", "socket=", "bin", "bin="])
def test_a_forwarded_flag_missing_its_value_is_refused(tmp_path, typed):
    """Both spellings, and the SAME sentence for both. `--socket=` used to
    normalize into `--socket` plus an empty argument and sail through: an empty
    socket path is not a socket path, and two spellings of one flag disagreeing
    about emptiness is exactly how a real path stops being checkable."""
    result = _source_and_forward(tmp_path, ["herdr", typed])
    assert result.returncode != 0, typed
    assert f"{typed.rstrip('=')} needs a value" in result.stderr


# --------------------------------------------------------------------------
# the exec itself -- the flags have to ARRIVE, not merely be collected
# --------------------------------------------------------------------------
# The same seam _run_sourced uses (source bin/og, call a function), driven to
# the one statement that cannot be called on its own: cmd_start's exec. The
# program it exec's is replaced by a recorder, so the argv is observed rather
# than inferred from the source.
_EXEC_DRIVER = r'''
src="$0"; args=("$@")
set --
source "$src" >/dev/null 2>&1

# Seams, all applied AFTER sourcing so the real ones are the ones replaced.
# Each of these either spawns a process or speaks HTTP to a server this test
# does not run; cmd_start and parse_start_args are left alone.
server_running() { return 1; }
lan_ip() { printf '127.0.0.1'; }
mint_token() { echo 3600; }
start_host_daemon() { ok "host daemon stubbed"; return 0; }
host_online() { return 0; }
ensure_project() { printf '%s' "$AGENT_NAME"; }
show_access() { :; }
start_herdr_bridge() { ok "herdr bridge stubbed"; return 0; }

cmd_start ${args[@]+"${args[@]}"}
'''


def _write_stub(path, script):
    path.write_text(script)
    path.chmod(0o755)


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _sandbox_port(sandbox):
    return (sandbox / "port").read_text().strip()


def _recorded_exec(sandbox):
    record = sandbox / "exec-argv"
    assert record.exists(), "cmd_start never reached the launcher exec"
    return record.read_text().splitlines()


def _run_cmd_start_to_the_exec(tmp_path, argv):
    """Drive the real cmd_start until it exec's the launcher. Returns the
    sandbox, which holds the recorded argv and the port the run was given."""
    sandbox = tmp_path / "sandbox"
    home = sandbox / "home"
    (home / ".omnigent" / "agents" / "dev-lead").mkdir(parents=True)
    # The cookie secret is normally generated with `python3 -c`; the interpreter
    # on PATH is a recorder, so a well-formed one is planted to skip that.
    (home / ".omnigent" / ".cookie-secret").write_text("0" * 64)

    fakebin = sandbox / "fakebin"
    fakebin.mkdir()
    # The exec under test. `python3 <script>` is recorded and stops there; `-c`
    # and the heredoc form are og's own inline helpers and must run for real,
    # or mux_rows cannot read the registry and no mux keyword is recognised.
    _write_stub(fakebin / "python3", (
        "#!/bin/sh\n"
        'case "$1" in -c|-) exec "$OG_REAL_PYTHON" "$@" ;; esac\n'
        'for a in "$@"; do printf \'%s\\n\' "$a" >> "$OG_EXEC_RECORD"; done\n'
        "exit 0\n"))
    # The server. Only the registration line matters: cmd_start greps its own
    # server log for it, and omnigent writes it to stdout, which the launch's
    # redirect is what captures.
    _write_stub(fakebin / "omnigent", (
        "#!/bin/sh\n"
        'case "$1:$2" in\n'
        '  server:--agent) printf \'  agent: dev-lead (from %s)\\n\' '
        '"$HOME/.omnigent/agents/dev-lead" ;;\n'
        "esac\n"
        "exit 0\n"))
    # The /health poll. Answers only once the registration line is in the log,
    # so the grep that follows cannot race the backgrounded spawn.
    _write_stub(fakebin / "curl", (
        "#!/bin/sh\n"
        "i=0\n"
        'while [ "$i" -lt 50 ]; do\n'
        '  grep -qs -e \'^  agent: \' "$OG_SERVER_LOGS"/server/*-og.log '
        '2>/dev/null && exit 0\n'
        '  i=$((i + 1)); sleep 0.1\n'
        "done\n"
        "exit 0\n"))
    # cmd_start drops the mux keyword if its binary is missing, so the test
    # cannot depend on herdr being installed on the machine running it.
    _write_stub(fakebin / "herdr", "#!/bin/sh\nexit 0\n")

    port = _free_port()
    (sandbox / "port").write_text(str(port))
    result = subprocess.run(
        ["bash", "-c", _EXEC_DRIVER, str(OG), *argv],
        capture_output=True, text=True, timeout=120,
        env={
            "HOME": str(home),
            "OG_REPO": str(REPO),
            "OG_SKIP_UPDATE": "1",
            # A port of its own, bound by nothing: the live og server is not
            # this test's to probe, collide with, or stop.
            "OG_PORT": str(port),
            "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
            "OG_REAL_PYTHON": sys.executable,
            "OG_EXEC_RECORD": str(sandbox / "exec-argv"),
            "OG_SERVER_LOGS": str(home / ".omnigent" / "logs"),
        })
    assert result.returncode == 0, (
        "cmd_start did not reach the exec:\n" + result.stdout + result.stderr)
    return sandbox


def test_cmd_start_hands_the_forwarded_flags_to_the_launcher(tmp_path):
    """Collection is not delivery. parse_start_args only ever proved that the
    flags are gathered; the exec at the end of cmd_start is the only place they
    reach the launcher, and a launcher exec'd without them is exactly the bug
    being fixed. So this drives the REAL cmd_start all the way to that exec,
    with the program it exec's replaced by a recorder — a fake `python3` first
    on PATH that writes its argv out and exits — and asserts on the argv that
    was actually handed over.

    The previous version of this test read bin/og's SOURCE and asserted the
    forwarding text appeared after `--server`. That passed whether or not the
    arguments arrived: it pinned the shape of a line, not a behaviour, and a
    rewrite that dropped the flags one line above it would still have gone
    green. Only a recorded argv can fail for that reason.
    """
    sandbox = _run_cmd_start_to_the_exec(tmp_path, ["herdr", "--once"])
    assert _recorded_exec(sandbox) == [
        str(REPO / "installer" / "og_herdr_launch.py"),
        "--mux", "herdr",
        "--server", f"http://127.0.0.1:{_sandbox_port(sandbox)}",
        "--once",
    ]


def test_the_launcher_gets_nothing_but_the_derived_flags_when_none_were_typed(
        tmp_path):
    """The other half of "the flags arrive": the exec must not have GROWN. Every
    plain `og start <mux>` on the planet runs through this line, so a stray
    argument here is a regression nobody would notice from the terminal."""
    sandbox = _run_cmd_start_to_the_exec(tmp_path, ["herdr"])
    assert _recorded_exec(sandbox) == [
        str(REPO / "installer" / "og_herdr_launch.py"),
        "--mux", "herdr",
        "--server", f"http://127.0.0.1:{_sandbox_port(sandbox)}",
    ]


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


# --------------------------------------------------------------------------
# og's own openers, and the guarantee that none touches omnigent's config
# --------------------------------------------------------------------------
# Silencing the server is not enough (see suppress_accounts_autopen): the browser
# the operator kept getting came from og's own openers. These pin that the mux
# suppression (a) sets the flag og's own needs_setup path consults, and (b)
# writes NOTHING to omnigent's config — the chat pane's browser is shut with
# BROWSER in herdr's pane env instead (og_herdr.pane_env_for), so omnigent
# must not be invoked at all. The `omnigent` CLI is STUBBED so that an invocation
# would be observed.

def _stub_omnigent(tmp_path):
    """A fake `omnigent` that records every invocation.

    PATH is prepended with its dir so bin/og resolves THIS one. The calls file
    is written only when something actually runs it, so its absence is proof
    omnigent was never invoked.
    """
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    calls = tmp_path / "omnigent-calls"
    script = fakebin / "omnigent"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        "exit 0\n")
    script.chmod(0o755)
    return fakebin, calls


def _path_with(fakebin):
    return str(fakebin) + os.pathsep + os.environ.get("PATH", "")


def test_suppress_accounts_autopen_sets_og_own_no_browser_flag(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fakebin, _calls = _stub_omnigent(tmp_path)
    body = "\n".join([
        'suppress_accounts_autopen "herdr"',
        'og_browser_suppressed && echo "browser=suppressed" || echo "browser=allowed"',
    ])
    result = _run_sourced(home, body, env={"PATH": _path_with(fakebin)})
    assert result.returncode == 0, result.stderr
    assert "browser=suppressed" in result.stdout


def test_a_plain_start_leaves_og_own_browser_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fakebin, _calls = _stub_omnigent(tmp_path)
    body = ('suppress_accounts_autopen ""\n'
            'og_browser_suppressed && echo "browser=suppressed" '
            '|| echo "browser=allowed"')
    result = _run_sourced(home, body, env={"PATH": _path_with(fakebin)})
    assert result.returncode == 0, result.stderr
    assert "browser=allowed" in result.stdout


def test_a_mux_launch_never_writes_global_omnigent_config(tmp_path):
    """The browser is suppressed PER PANE now (BROWSER in herdr's pane env, see
    og_herdr.pane_env_for), not by mutating the operator's omnigent
    config. An earlier fix ran `omnigent config set --global
    auto_open_conversation=false`; that leaked one `og start herdr` into a
    persistent, machine-wide change. This pins the behaviour (the suppression
    invokes omnigent not at all) AND the spelling (so it cannot creep back)."""
    home = tmp_path / "home"
    home.mkdir()
    fakebin, calls = _stub_omnigent(tmp_path)
    body = 'suppress_accounts_autopen "herdr"'
    result = _run_sourced(home, body, env={"PATH": _path_with(fakebin)})
    assert result.returncode == 0, result.stderr

    # No invocation of omnigent at all — no config read, no config write.
    assert not calls.exists(), calls.read_text()

    text = OG.read_text()
    assert "suppress_conversation_autopen" not in text
    assert "config set --global auto_open_conversation" not in text


def test_the_needs_setup_open_browser_is_gated_by_the_suppression():
    """The one opener og owns itself (the create-admin path) must ask the flag
    rather than call `open_in_browser` unconditionally."""
    text = OG.read_text()
    assert re.search(r'^\s*if og_browser_suppressed; then$', text, re.M)
    assert re.search(r'^\s*open_in_browser "\$OG_PUBLIC_URL"$', text, re.M)
