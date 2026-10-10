"""Guards for the access banner (QR + URL) that `og chat` now prints.

A multiplexer launch (`og start herdr`) replaces `og start`'s terminal with
`exec herdr`, so the banner show_access() prints there is never seen: the chat
pane only ever ran `og chat`. These tests drive `og chat` itself, in a sandbox
whose fake `omnigent` answers `server status` and stands in for the REPL, so no
real server is contacted and no real REPL starts. HOME is redirected to a
throwaway directory throughout, the pattern the other bin/og tests use.

The banner is gated on `[[ -t 1 ]]` and the QR on the pane width, so the cases
that must SEE the banner run under a real pty (openpty) with a chosen window
size; a plain `capture_output` run is the non-tty case.

The LAN address is made deterministic by stubbing the tools lan_ip() tries
(route/ipconfig/ip/hostname) rather than trusting the test box's network, and
qrencode is stubbed too so the QR-width gate is proven without depending on the
real encoder being installed.
"""
from __future__ import annotations

import fcntl
import os
import pty
import shutil
import struct
import subprocess
import sys
import termios
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OG = Path(os.environ.get("OG_BIN") or REPO / "bin" / "og")

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"),
                                reason="bin/og is a bash script")

LAN_IP = "10.0.0.99"
TUNNEL_URL = "https://demo.tunnl.gg"
QR_MARKER = "QRMARK"
# The fake qrencode emits a code 31 columns wide (62 single-byte chars per row),
# which is what a typical ngrok/tunnl URL actually renders; see the width
# threshold in print_qr. Wide enough to print in a 200-column pane, too wide for
# the 20-column one.
_QR_ROW = "#" * 62
_QR_MARKED = "#QRMARK" + "#" * 55

FAKE_OMNIGENT = """#!/bin/sh
# Stands in for the omnigent CLI: reports a running server (so cmd_chat's
# precondition passes) and, for `run`, prints a marker and exits — the real REPL
# is never started.
if [ "$1" = server ] && [ "$2" = status ]; then
  echo "running at http://127.0.0.1:6767"
  exit 0
fi
echo "FAKE-OMNIGENT-RUN $*"
exit 0
"""

# lan_ip() resolution, stubbed per tool so a LAN address is deterministic.
FAKE_ROUTE = "#!/bin/sh\ncase \"$*\" in\n  '-n get default') echo '   interface: en9' ;;\n  *) exit 1 ;;\nesac\n"
FAKE_IPCONFIG = "#!/bin/sh\necho " + LAN_IP + "\nexit 0\n"
FAKE_IP = "#!/bin/sh\necho '1.1.1.1 via 10.0.0.1 dev en9 src " + LAN_IP + " uid 0'\nexit 0\n"
FAKE_HOSTNAME = "#!/bin/sh\necho '" + LAN_IP + " 172.17.0.1'\nexit 0\n"

# The same tools, made to resolve nothing — the "neither a tunnel nor a LAN
# address" case.
NO_LAN = {
    "route": "#!/bin/sh\nexit 1\n",
    "ipconfig": "#!/bin/sh\nexit 1\n",
    "ip": "#!/bin/sh\nexit 1\n",
    "hostname": "#!/bin/sh\nexit 0\n",
}


def _write_exec(path, text):
    path.write_text(text)
    path.chmod(0o755)


def _make_bin(tmp_path, *, lan=True, qrencode=True, omnigent=True):
    """A PATH directory holding the fakes this suite needs."""
    b = tmp_path / "bin"
    b.mkdir()
    if omnigent:
        _write_exec(b / "omnigent", FAKE_OMNIGENT)
    if lan:
        _write_exec(b / "route", FAKE_ROUTE)
        _write_exec(b / "ipconfig", FAKE_IPCONFIG)
        _write_exec(b / "ip", FAKE_IP)
        _write_exec(b / "hostname", FAKE_HOSTNAME)
    else:
        for name, text in NO_LAN.items():
            _write_exec(b / name, text)
    if qrencode:
        _write_exec(b / "qrencode",
                    "#!/bin/sh\n"
                    f"printf '%s\\n' '{_QR_ROW}'\n"
                    f"printf '%s\\n' '{_QR_MARKED}'\n"
                    f"printf '%s\\n' '{_QR_ROW}'\n")
    return b


def _path_without(*binaries):
    """The current PATH minus the directories containing `binaries`."""
    drop = {os.path.realpath(os.path.dirname(p))
            for p in (shutil.which(b) for b in binaries) if p}
    kept = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and os.path.realpath(d) not in drop]
    return os.pathsep.join(kept)


def _home(tmp_path, *, tunnel=None):
    """A throwaway HOME with the agent bundle cmd_chat insists on, and
    optionally a cached, still-owned tunnel URL."""
    home = tmp_path / "home"
    omni = home / ".omnigent"
    (omni / "agents" / "dev-lead").mkdir(parents=True)
    if tunnel:
        (omni / "og-tunnel.url").write_text(tunnel + "\n")
        (omni / "og-tunnel.provider").write_text("tunnl\n")
        # os.getpid() is this test process — alive for the whole run, so
        # tunnel_url's liveness check passes without spawning a sleeper.
        (omni / "og-tunnel-tunnl.pid").write_text(str(os.getpid()) + "\n")
    return home


def _env(home, path, *, cols=None):
    env = {
        "HOME": str(home),
        "PATH": path,
        "TERM": "xterm-256color",
    }
    if cols is not None:
        # COLUMNS is the fallback print_qr uses when tput has no terminfo; set
        # it alongside the pty window so the width is deterministic either way.
        env["COLUMNS"] = str(cols)
    return env


def _run_chat_pty(home, path, *, cols, extra_env=None):
    """Drive `og chat` under a real pty of `cols` columns and collect output."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, cols, 0, 0))
    env = _env(home, path, cols=cols)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        ["bash", str(OG), "chat"],
        stdin=slave, stdout=slave, stderr=slave,
        env=env, close_fds=True,
    )
    os.close(slave)
    chunks = []
    while True:
        try:
            data = os.read(master, 4096)
        except OSError:
            break  # Linux raises EIO at EOF
        if not data:
            break
        chunks.append(data)
    os.close(master)
    rc = proc.wait()
    return rc, b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")


def _run_chat_captured(home, path):
    """Drive `og chat` with stdout a pipe — no tty anywhere."""
    return subprocess.run(
        ["bash", str(OG), "chat"], capture_output=True, text=True, timeout=30,
        env={"HOME": str(home), "PATH": path, "TERM": "xterm-256color"},
    )


def test_chat_prints_the_cached_tunnel_url_when_a_tunnel_is_live(tmp_path):
    home = _home(tmp_path, tunnel=TUNNEL_URL)
    path = str(_make_bin(tmp_path)) + os.pathsep + os.environ["PATH"]

    rc, out = _run_chat_pty(home, path, cols=200)

    assert rc == 0, out
    assert f"scan the code above, or open: {TUNNEL_URL}" in out
    assert QR_MARKER in out
    # A public tunnel is not a LAN address, so the same-wifi caveat must not
    # appear; show_access's tunneled branch names the loopback instead.
    assert "same wifi only" not in out
    assert "loopback: http://127.0.0.1:6767" in out
    # And control really reached the exec: the REPL stand-in ran.
    assert "FAKE-OMNIGENT-RUN" in out


def test_chat_prints_the_lan_url_when_there_is_no_tunnel(tmp_path):
    home = _home(tmp_path)
    path = str(_make_bin(tmp_path)) + os.pathsep + os.environ["PATH"]

    rc, out = _run_chat_pty(home, path, cols=200)

    assert rc == 0, out
    assert f"scan the code above, or open: http://{LAN_IP}:6767" in out
    assert "same wifi only" in out
    assert QR_MARKER in out


def test_chat_prints_no_banner_when_no_address_resolves(tmp_path):
    home = _home(tmp_path)
    path = str(_make_bin(tmp_path, lan=False)) + os.pathsep + os.environ["PATH"]

    rc, out = _run_chat_pty(home, path, cols=200)

    assert rc == 0, out
    assert "scan the code above" not in out
    assert QR_MARKER not in out
    # The banner is additive only: the exec path is untouched.
    assert "FAKE-OMNIGENT-RUN" in out


def test_chat_stays_clean_when_stdout_is_not_a_tty(tmp_path):
    home = _home(tmp_path, tunnel=TUNNEL_URL)
    path = str(_make_bin(tmp_path)) + os.pathsep + os.environ["PATH"]

    result = _run_chat_captured(home, path)

    assert result.returncode == 0, result.stderr
    assert "scan the code above" not in result.stdout
    assert QR_MARKER not in result.stdout
    assert "FAKE-OMNIGENT-RUN" in result.stdout


def test_chat_keeps_the_url_but_drops_the_qr_in_a_narrow_pane(tmp_path):
    home = _home(tmp_path, tunnel=TUNNEL_URL)
    path = str(_make_bin(tmp_path)) + os.pathsep + os.environ["PATH"]

    rc, out = _run_chat_pty(home, path, cols=20)

    assert rc == 0, out
    assert f"scan the code above, or open: {TUNNEL_URL}" in out
    assert QR_MARKER not in out


def test_chat_degrades_when_qrencode_is_absent(tmp_path):
    home = _home(tmp_path, tunnel=TUNNEL_URL)
    # Fake bin WITHOUT a qrencode stub, and the real qrencode's directory
    # dropped from PATH — so `command -v qrencode` fails, as on a machine that
    # never installed it.
    path = (str(_make_bin(tmp_path, qrencode=False)) + os.pathsep
            + _path_without("qrencode"))
    assert shutil.which("qrencode", path=path) is None

    rc, out = _run_chat_pty(home, path, cols=200)

    assert rc == 0, out
    assert f"scan the code above, or open: {TUNNEL_URL}" in out
    assert QR_MARKER not in out


def test_chat_banner_is_not_suppressed_by_an_inherited_access_shown_guard(tmp_path):
    """OG_ACCESS_SHOWN is `og start`'s once-only guard, and cmd_chat reuses the
    same show_access. An inherited value must not silently swallow the banner."""
    home = _home(tmp_path, tunnel=TUNNEL_URL)
    path = str(_make_bin(tmp_path)) + os.pathsep + os.environ["PATH"]

    rc, out = _run_chat_pty(home, path, cols=200,
                            extra_env={"OG_ACCESS_SHOWN": "1"})

    assert rc == 0, out
    assert f"scan the code above, or open: {TUNNEL_URL}" in out
