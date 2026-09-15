#!/usr/bin/env bash
# install.sh — bootstrap og on this machine.
#
#   ./install.sh              interactive setup
#   ./install.sh --check      report prerequisites, change nothing
#   ./install.sh --show       print the current config
#   ./install.sh --plan f.json  apply a plan non-interactively
#
# All this does itself is find a Python that can import yaml, then hand over to
# installer/og_install.py. Omnigent ships PyYAML in its own venv, so that
# interpreter is the reliable fallback when the system python3 lacks it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf '\033[0;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[0;36m→\033[0m %s\n' "$*"; }

# Candidate interpreters, best first. The omnigent venv is last but surest:
# if omnigent is installed at all, its python has yaml.
candidates=()
command -v python3 >/dev/null 2>&1 && candidates+=("$(command -v python3)")
for p in "$HOME"/.local/share/uv/tools/omnigent/bin/python3 \
         "$HOME"/.local/share/uv/tools/omnigent/bin/python; do
  [[ -x "$p" ]] && candidates+=("$p")
done
# Wherever `omnigent` itself resolves from, its sibling python is a candidate too.
if command -v omnigent >/dev/null 2>&1; then
  ogbin="$(command -v omnigent)"
  real="$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$ogbin" 2>/dev/null || echo "")"
  [[ -n "$real" && -x "$(dirname "$real")/python3" ]] && candidates+=("$(dirname "$real")/python3")
fi

PY=""
for c in "${candidates[@]}"; do
  if "$c" -c 'import yaml' >/dev/null 2>&1; then PY="$c"; break; fi
done

if [[ -z "$PY" ]]; then
  info "no interpreter with PyYAML found; trying: pip3 install --user pyyaml"
  if command -v pip3 >/dev/null 2>&1 && pip3 install --user --quiet pyyaml >/dev/null 2>&1 \
     && python3 -c 'import yaml' >/dev/null 2>&1; then
    PY="$(command -v python3)"
  else
    die "og-install needs Python with PyYAML.
    Install Omnigent first (it ships PyYAML):  uv tool install omnigent
    or install PyYAML yourself:                pip3 install --user pyyaml"
  fi
fi

info "using $PY"
exec "$PY" "$HERE/installer/og_install.py" "$@"
