import sys
from pathlib import Path

import pytest

# og_install.py is a script, not a package (see AGENTS.md's "AI-driven install"
# section — it's meant to be run via `python3 installer/og_install.py`, not
# imported as `og.installer`), so tests import it directly off sys.path
# instead of relying on a src-layout package install.
sys.path.insert(0, str(Path(__file__).parent / "installer"))


@pytest.fixture(autouse=True)
def _hermetic_pythonpath(monkeypatch):
    """Strip PYTHONPATH from every test, because og itself puts it there.

    `bin/og` (see the host-daemon launch, ~line 1046) deliberately exports
    `PYTHONPATH=$OG_POLICY_PATH${PYTHONPATH:+:$PYTHONPATH}` so the daemon can
    import omnigent_local_policies even if the installer's .pth went stale.
    That is intentional and stays. But the daemon spawns everything else --
    including the coding agents it runs -- and they all inherit the value. So
    anyone who runs pytest from inside an og-managed session is executing the
    suite with PYTHONPATH=~/.omnigent/policies set.

    That is not cosmetic. PYTHONPATH entries land on sys.path *before* any
    site-packages .pth gets appended, so the developer's real policies module
    shadows whatever the test just wrote. The concrete symptom, measured on
    this machine: `install_pth`'s verification subprocess (og_install.py,
    ~line 2625) imported the real 22.6 KB ~/.omnigent/policies/
    omnigent_local_policies.py instead of the fixture's stand-in, that file
    does `from omnigent.policies.builtins import orchestration`, the test's
    fake venv has no `omnigent` installed, so the import raised
    ModuleNotFoundError and `die()` turned it into SystemExit(1). Two tests
    failed. CI passed, because CI has no ~/.omnigent and no PYTHONPATH -- so
    this only ever breaks for a developer who has actually installed og,
    i.e. exactly the people most likely to run the suite.

    Worse than red: on such a machine the .pth tests could pass for the wrong
    reason, with the import they are verifying resolving through PYTHONPATH
    rather than through the .pth under test. Deleting this fixture makes the
    suite non-hermetic, not merely flaky.

    Suite-wide rather than scoped to the two known failures: any test that
    spawns a subprocess is exposed to the same contamination whether or not
    it fails today. `monkeypatch` restores the original value at teardown, so
    nothing leaks into a later test or out of the run.
    """
    monkeypatch.delenv("PYTHONPATH", raising=False)
