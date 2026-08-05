#!/usr/bin/env bash
# Roster setup for Linux.
#
#     cd Linux && ./install.sh
#
# Creates the virtual environment and installs the packages. Safe to run
# again: an existing .venv is reused and only missing pieces are installed.
set -uo pipefail

# The project root is one level up -- this script lives in Linux/.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

say()  { printf "\n\033[1m%s\033[0m\n" "$1"; }
fail() { printf "\n\033[31m[ERROR] %s\033[0m\n\n" "$1"; exit 1; }

printf "\nRoster setup\n"
printf "Install location: %s\n" "$root"

# ---------------------------------------------------------------- Python
say "1/3  Checking Python"

python_bin=""
for candidate in python3.13 python3.12 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
    if [ "${version%%.*}" -eq 3 ] && [ "${version##*.}" -ge 12 ]; then
        python_bin="$candidate"
        break
    fi
done

[ -n "$python_bin" ] || fail "Python 3.12 or newer was not found.

Install it with your distribution's package manager, for example:
    sudo apt install python3.13 python3.13-venv     # Debian / Ubuntu
    sudo dnf install python3.13                     # Fedora

Then run this script again."

printf "   Using %s (%s)\n" "$python_bin" "$("$python_bin" --version)"

# ------------------------------------------------------------------ venv
say "2/3  Creating the virtual environment"
if [ -x "$root/.venv/bin/python" ]; then
    printf "   .venv already exists, skipping\n"
else
    # Debian and Ubuntu ship venv separately; say so instead of leaving the
    # user with ensurepip's own error message.
    "$python_bin" -m venv .venv || fail "Could not create the virtual environment.

On Debian/Ubuntu this usually means the venv module is missing:
    sudo apt install python3-venv"
    printf "   Created .venv\n"
fi

# -------------------------------------------------------------- packages
say "3/3  Installing packages (about 150 MB on the first run)"
"$root/.venv/bin/python" -m pip install --upgrade pip --quiet
"$root/.venv/bin/python" -m pip install -r requirements.txt \
    || fail "Package installation failed. The messages above explain why."

chmod +x "$root/Linux/start.sh" "$root/Linux/console.sh" 2>/dev/null || true

printf "\n\033[32mSetup complete.\033[0m\n\n"
printf "Start the application:  ./Linux/start.sh\n"
printf "Command line:           ./Linux/console.sh\n\n"
