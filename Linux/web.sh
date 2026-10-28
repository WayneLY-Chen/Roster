#!/usr/bin/env bash
# Roster in the browser. Same database as start.sh, only the interface
# differs. Only this computer can reach the page; see web/app.py.
#
# This terminal is the web server: close it (or press Ctrl+C) to stop.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

python_bin="$here/.venv/bin/python"

if [ ! -x "$python_bin" ]; then
    cat <<EOF

[ERROR] Not installed yet.

Run the setup script in this same folder first:
    ./install.sh

EOF
    exit 1
fi

printf "
"
cat "$here/assets/pets.txt" 2>/dev/null

exec "$python_bin" main.py web
