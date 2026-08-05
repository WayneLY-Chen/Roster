#!/usr/bin/env bash
# Roster launcher for macOS and Linux.
#
# Everything is resolved relative to this script's own location, so the
# project folder can be renamed or moved anywhere without breaking.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

python_bin="$here/.venv/bin/python"

if [ ! -x "$python_bin" ]; then
    cat <<EOF

[ERROR] Virtual environment not found: .venv/bin/python

Run these two commands in this folder first:
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt

EOF
    exit 1
fi

echo "Starting Roster..."
echo "(Close the application window to quit.)"
echo

exec "$python_bin" main.py gui
