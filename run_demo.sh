#!/usr/bin/env bash
# One-command demo: runs the fixture and the scraper in a single process
# (scripts/demo.py), does two crawls with a deliberate catalogue change
# in between, prints the change report, and writes CSV/XLSX exports.
# Assumes the venv is already created (see README "Setup"), or falls
# back to the system python3 if not.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
PYTHON="python3"
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
fi

"$PYTHON" scripts/demo.py
