#!/usr/bin/env bash
# Roster updater for Linux.
#
#     cd Linux && ./update.sh
#
# Nothing you own is touched. The database (data/), attachments, exports,
# backups, mail templates and user_settings.yaml are all outside git, so a
# pull cannot reach them.
#
# config.yaml IS tracked, so local edits to it would block the pull. They
# are stashed and put back around it rather than lost.
#
# There is no database step here on purpose: the app runs its own migration
# at start-up (database/session.py init_db), so a new column appears the
# first time you open it.
set -uo pipefail

# The project root is one level up -- this script lives in Linux/.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

say()  { printf "\n\033[1m%s\033[0m\n" "$1"; }
fail() { printf "\n\033[31m[ERROR] %s\033[0m\n\n" "$1"; exit 1; }
version_now() { sed -n 's/^VERSION = "\(.*\)"/\1/p' "$root/core/constants.py" | head -n 1; }

printf "\n"
cat "$root/assets/pets.txt" 2>/dev/null

printf "\nRoster update\n"
printf "Folder: %s\n" "$root"

# ------------------------------------------------------------ can we update?
#
# Downloading the ZIP gives you the files without the .git folder, and without
# it there is no record of where this came from. Say so plainly instead of
# letting the first git call fail with its own words.
[ -d "$root/.git" ] || fail "This folder did not come from git, so there is
nothing to update from. (You downloaded the ZIP -- it has no .git folder.)

Two ways forward:

  1) Download it once with git, and this script works from then on:

       git clone https://github.com/WayneLY-Chen/Roster.git

     Then copy your data/ folder into the new one.

  2) Or download the new ZIP and replace this folder -- but move these
     out first and put them back afterwards, they are yours and are not
     in the download:

       data/               the database
       attachments/        files attached to companies
       output/             exported lists
       backups/            backups
       user_settings.yaml  your settings
       templates/mail/     your mail templates"

command -v git >/dev/null 2>&1 || fail "git is not installed, so this script
cannot fetch anything. Install it with your package manager, for example:

    sudo apt install git      # Debian / Ubuntu
    sudo dnf install git      # Fedora

Then run this script again."

old_version="$(version_now)"
printf "Installed version: %s\n" "${old_version:-unknown}"

# ------------------------------------------------------------------- 1/3
say "1/3  Checking for a newer version"
git fetch --quiet || fail "Could not reach GitHub. Check your connection and
try again. Nothing was changed."

remote_head="$(git rev-parse '@{u}' 2>/dev/null || true)"

[ -n "$remote_head" ] || fail "This copy is not tracking a branch on GitHub, so
there is nowhere to update from. Nothing was changed.

If you know git, set an upstream with:
    git branch --set-upstream-to=origin/main"

# Ancestor, not equality. Someone who has committed something of their own is
# ahead of GitHub rather than behind it, and has nothing to fetch; a plain
# "are the hashes the same" test would send them into the pull.
if git merge-base --is-ancestor "$remote_head" HEAD; then
    printf "\n\033[32mAlready up to date (%s).\033[0m\n\n" "${old_version:-unknown}"
    exit 0
fi

# Local edits to a tracked file (config.yaml, almost always) would make the
# pull refuse. Put them aside and bring them back afterwards.
stashed=0
if ! git diff --quiet HEAD; then
    printf "   You have edited a tracked file -- most likely config.yaml.\n"
    printf "   Setting your edits aside and restoring them after the update.\n"
    git stash push --quiet -m "roster-update" \
        || fail "Could not set your local edits aside. Nothing was changed."
    stashed=1
fi

# ------------------------------------------------------------------- 2/3
say "2/3  Downloading the new version"
if ! git pull --ff-only --quiet; then
    printf "\n\033[31m[ERROR] The update could not be applied. The messages above\n"
    printf "explain why -- the usual cause is that this copy has its own commits,\n"
    printf "which cannot be fast-forwarded.\033[0m\n"
    [ "$stashed" -eq 1 ] && printf "\nYour local edits are still saved. Get them back with: git stash pop\n"
    printf "\n"
    exit 1
fi

if [ "$stashed" -eq 1 ] && ! git stash pop --quiet; then
    printf "\nThe new version is installed, but your own edits could not be put\n"
    printf "back automatically -- the same lines changed on both sides.\n\n"
    printf "They are not lost. To see them:  git stash show -p\n"
    printf "To apply and fix by hand:        git stash pop\n\n"
    exit 1
fi

# ------------------------------------------------------------------- 3/3
say "3/3  Installing anything newly required"
[ -x "$root/.venv/bin/python" ] || fail "The new version is downloaded, but
there is no .venv here yet. Run ./Linux/install.sh to finish."

# No --upgrade on purpose. requirements.txt uses lower bounds, so a plain
# install adds what is missing and leaves working versions alone; with
# --upgrade every package would jump to its newest release, which is a much
# bigger change than "update Roster".
"$root/.venv/bin/python" -m pip install -r requirements.txt --quiet \
    || fail "The new version is downloaded, but installing its packages failed.
The messages above explain why -- a network problem is the most common cause.
Run this script again."

new_version="$(version_now)"
printf "\n\033[32mUpdated: %s  ->  %s\033[0m\n\n" "${old_version:-unknown}" "${new_version:-unknown}"
printf "Your companies, attachments and settings are untouched.\n"
printf "What changed in this version: see CHANGELOG.md\n\n"
printf "Start the application:  ./Linux/start.sh\n\n"
