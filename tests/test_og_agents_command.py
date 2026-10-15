"""Guards for `og agents`'s bin/og wire-up.

`og agents` is dispatched by bin/og (`cmd_agents`), and that command must point
the viewer at og's OWN server. It used to exec `og_agents.py` with no `--server`,
so the viewer fell back to its hardcoded 6767 — and an install whose og.env moved
the server to 6768 (the installer's move when 6767 is taken) got a 404 from
whatever unrelated listener held 6767. Every sibling command forwards the port;
this pins `cmd_agents` to the same shape.

These run bin/og with a stub `python3` on PATH that records its argv, so the
FORWARDED arguments are observed without starting the viewer or contacting a
server. HOME is redirected to a throwaway directory — the pattern the other
bin/og tests use (`tests/test_og_start_mux.py`, `tests/test_og_install.py`).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# Overridable so the suite can be pointed at an installed og, the same knob the
# other bin/og tests use (the installer copies bin/og out of the checkout).
OG = Path(os.environ.get("OG_BIN") or REPO / "bin" / "og")

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"),
                                reason="bin/og is a bash script")


def _stub_python3(tmp_path):
    """A PATH dir holding a `python3` that records its argv and exits 0."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    out = tmp_path / "argv"
    script = fakebin / "python3"
    script.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > "
                      + shlex.quote(str(out)) + "\nexit 0\n")
    script.chmod(0o755)
    return fakebin, out


def _run_agents(tmp_path, argv, env=None):
    """Run `bin/og agents ...`; return (result, the argv the viewer was given)."""
    fakebin, out = _stub_python3(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        # OG_REPO pins og_repo_dir at THIS checkout so the missing-file guard
        # passes and the viewer path is the real one — only `python3` is stubbed.
        "OG_REPO": str(REPO),
        "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
    }
    if env:
        environment.update(env)
    result = subprocess.run(["bash", str(OG), "agents", *argv],
                            capture_output=True, text=True, timeout=30,
                            env=environment)
    recorded = out.read_text().splitlines() if out.exists() else []
    return result, recorded


def _server_value(argv):
    return argv[argv.index("--server") + 1]


def test_cmd_agents_forwards_the_server_at_the_configured_port(tmp_path):
    """OG_PORT moved the server; `og agents` must follow it, not ask 6767."""
    result, argv = _run_agents(tmp_path, ["--once"], env={"OG_PORT": "6768"})
    assert result.returncode == 0, result.stderr
    assert _server_value(argv) == "http://127.0.0.1:6768"


def test_cmd_agents_defaults_the_server_to_6767(tmp_path):
    """With no og.env override the default port is still forwarded, so the viewer
    never has to fall back to its own hardcoded default."""
    result, argv = _run_agents(tmp_path, ["--once"])
    assert result.returncode == 0, result.stderr
    assert _server_value(argv) == "http://127.0.0.1:6767"


def test_an_explicit_server_on_the_command_line_wins(tmp_path):
    """The operator's own `--server` still wins. bin/og places its own FIRST, and
    argparse keeps the LAST occurrence — so the flag they typed is the one the
    viewer uses. (The parser half of this is pinned in test_og_agents.py.)"""
    result, argv = _run_agents(
        tmp_path, ["--once", "--server", "http://example:9999"],
        env={"OG_PORT": "6768"})
    assert result.returncode == 0, result.stderr
    assert argv.count("--server") == 2, argv
    assert _server_value(argv) == "http://127.0.0.1:6768", "bin/og's is first"
    assert argv[-2:] == ["--server", "http://example:9999"], "the operator's wins"
