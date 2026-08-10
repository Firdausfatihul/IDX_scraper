#!/usr/bin/env bash
set -euo pipefail

python - <<'PY2'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"Python 3.11+ required, found {sys.version.split()[0]}")
print("Python", sys.version.split()[0])
PY2

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
python verify_install.py

echo "v0.15.5 installed successfully"
