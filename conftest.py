import sys
from pathlib import Path

# og_install.py is a script, not a package (see AGENTS.md's "AI-driven install"
# section — it's meant to be run via `python3 installer/og_install.py`, not
# imported as `og.installer`), so tests import it directly off sys.path
# instead of relying on a src-layout package install.
sys.path.insert(0, str(Path(__file__).parent / "installer"))
