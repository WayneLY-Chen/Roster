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

[ERROR] Not installed yet.

Run the setup script in this same folder first:
    ./install.sh

EOF
    exit 1
fi

# The mascots -- the same drawing the installer prints, from a single file
# shared by every launcher on every platform.
printf "\n"
cat "$here/assets/pets.txt" 2>/dev/null

echo
echo "Starting Roster..."
echo "(Close the application window to quit.)"
echo

exec "$python_bin" main.py gui
