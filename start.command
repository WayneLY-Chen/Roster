#!/usr/bin/env bash
# Roster launcher for macOS Finder.
#
# Why this exists next to start.sh: macOS Finder does NOT run .sh files when
# you double-click them -- it opens them in a text editor. The .command
# extension is the one Finder recognises as "run this in Terminal", so this
# file is what a Mac user actually double-clicks.
#
# It is a two-line wrapper on purpose. All the real logic lives in start.sh,
# so there is only one place to change.
#
# First time only: right-click -> Open (Gatekeeper blocks double-clicking a
# script downloaded from the internet until you approve it once).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/start.sh"
