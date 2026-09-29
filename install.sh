#!/usr/bin/env sh
# Install aiusage from this checkout. Prefers pipx; falls back to pip --user.
# Does not modify shell rc files.
set -eu
cd "$(dirname "$0")"

if command -v pipx >/dev/null 2>&1; then
    pipx install --force .
elif command -v python3 >/dev/null 2>&1; then
    python3 -m pip install --user .
    echo "Installed with pip --user. Make sure ~/.local/bin is on your PATH."
else
    echo "error: need pipx or python3" >&2
    exit 1
fi

echo
aiusage --version || echo "aiusage installed, but not on PATH yet."
