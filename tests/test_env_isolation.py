"""Guards the PYTHONPATH hermeticity fixture in the repo-root conftest.py.

og exports PYTHONPATH on purpose when it launches the host daemon (see
bin/og's `start_host_daemon`), so every coding agent it runs inherits a live
`~/.omnigent/policies` on sys.path -- and PYTHONPATH is applied *before* any
site-packages `.pth`, so it shadows whatever a test just wrote.

The first two tests assert the ordinary invariants and are what fail loudly
if someone narrows the fixture. They are vacuous on a clean machine, though:
pass or fail depends entirely on what the shell exported, which is exactly
the variable under test. `test_fixture_strips_pythonpath_when_it_is_set` is
the one that actually proves the fixture works: it hands a nested pytest a
deliberately poisoned PYTHONPATH and asserts the inner run still sees none,
so the guarantee holds whether or not the developer's shell exports anything.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A path that exists nowhere. If it ever shows up in a child process's
# environment or sys.path, the isolation regressed -- and an empty string
# would not catch it, so the assertion is on the value, not mere presence.
POISON = "/nonexistent/og-pythonpath-hermeticity-probe"


def test_pythonpath_is_absent_from_the_test_environment():
    """In-process: the fixture removed PYTHONPATH before the test body ran."""
    assert "PYTHONPATH" not in os.environ


def test_subprocess_spawned_from_a_test_does_not_see_pythonpath():
    """The part that matters.

    `install_pth` verifies a `.pth` by running a fresh interpreter, so the
    failure mode this guards is not about this process's os.environ at all --
    it is about what a child process resolves. PYTHONPATH entries land on
    sys.path ahead of anything a `.pth` appends, so a leaked value makes the
    child import the developer's real policies module instead of the test's
    stand-in. Check both the child's environment and its sys.path.
    """
    code = textwrap.dedent("""
        import json, os, sys
        print(json.dumps({
            "env": os.environ.get("PYTHONPATH"),
            "on_path": any("og-pythonpath-hermeticity-probe" in p for p in sys.path),
        }))
    """)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, check=True,
    )
    seen = json.loads(out.stdout)
    assert seen["env"] is None, f"subprocess inherited PYTHONPATH={seen['env']!r}"
    assert seen["on_path"] is False, "PYTHONPATH leaked onto the child's sys.path"


def test_fixture_strips_pythonpath_when_it_is_set(tmp_path):
    """Prove the fixture bites even when the ambient env has nothing to strip.

    On CI -- and on any machine where the developer never ran `og` -- PYTHONPATH
    is already unset, so the two tests above pass whether the fixture exists or
    not. Deleting the fixture would therefore not turn the suite red in the one
    place a reviewer is most likely to run it, and would go unnoticed until it
    hit an og-installed machine.

    So drive it: re-exec pytest on a throwaway test file with PYTHONPATH
    deliberately poisoned, pointed at this repo's root conftest via `-p
    conftest` (the outer fixture has already stripped ours, so the inner run
    has to do the work itself). If the fixture stops stripping, the inner
    assertion fails and so does this.
    """
    probe = tmp_path / "test_probe_env.py"
    probe.write_text(textwrap.dedent("""
        import json, os, subprocess, sys

        def test_pythonpath_was_stripped_by_the_fixture():
            assert "PYTHONPATH" not in os.environ, (
                "conftest fixture did not strip the inherited PYTHONPATH"
            )
            # Deliberately NOT asserting on this interpreter's own sys.path.
            # Python applies PYTHONPATH at interpreter boot, so an entry the
            # process started with is already baked into sys.path and
            # monkeypatch.delenv cannot un-apply it -- retroactively purging
            # it would mean mutating a global that every other test shares.
            # What the fixture must guarantee is the environment, because
            # that is what a subprocess is launched from, and that is the
            # direction the install_pth failure actually travelled.
            child = subprocess.run(
                [sys.executable, "-c", "import json, os, sys; print(json.dumps("
                 "{'env': os.environ.get('PYTHONPATH'), 'on_path': any("
                 "'og-pythonpath-hermeticity-probe' in p for p in sys.path)}))"],
                capture_output=True, text=True, check=True,
            )
            seen = json.loads(child.stdout)
            assert seen["env"] is None, (
                f"child inherited PYTHONPATH={seen['env']!r}"
            )
            assert seen["on_path"] is False, (
                "poisoned PYTHONPATH entry reached the child's sys.path"
            )
    """))

    env = dict(os.environ)
    env["PYTHONPATH"] = POISON  # what bin/og would have handed the daemon
    env.pop("PYTEST_CURRENT_TEST", None)

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
         "-p", "conftest", "-q", str(probe)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert out.returncode == 0, (
        f"nested pytest failed with PYTHONPATH={POISON} set, so the conftest "
        f"fixture is not doing its job.\nstdout:\n{out.stdout}\nstderr:\n{out.stderr}"
    )