#!/bin/bash
# getrepo.sh — Clone a GitHub repo into repos/
# If already cloned, pulls latest instead.
#
# Usage:
#   ./getrepo.sh timhodge/walter-checks
#   ./getrepo.sh bwgdev/bwg-events-wp
#   ./getrepo.sh timhodge/fun-bobby main
set -e

if [ -z "$1" ]; then
    echo "Usage: ./getrepo.sh <owner/repo> [branch]"
    echo ""
    echo "Examples:"
    echo "  ./getrepo.sh timhodge/walter-checks"
    echo "  ./getrepo.sh bwgdev/bwg-events-wp"
    echo "  ./getrepo.sh timhodge/fun-bobby main"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$1"
BRANCH="${2:-}"
REPO_NAME="$(basename "$REPO")"
DEST="$SCRIPT_DIR/repos/$REPO_NAME"

if [ -d "$DEST/.git" ]; then
    echo "Already cloned: $DEST"
    echo "Pulling latest..."
    git -C "$DEST" pull
else
    echo "Cloning $REPO → $DEST"
    mkdir -p "$SCRIPT_DIR/repos"
    if [ -n "$BRANCH" ]; then
        git clone --branch "$BRANCH" "https://github.com/$REPO.git" "$DEST"
    else
        git clone "https://github.com/$REPO.git" "$DEST"
    fi
fi

echo ""
if [ -f "$DEST/WalterChecks.json" ]; then
    echo "✓ WalterChecks.json found"
    cat "$DEST/WalterChecks.json"
else
    echo "⚠ No WalterChecks.json — review will use defaults"
    echo "  Tip: Add one to configure profile, root, and excludes"
fi

echo ""
echo "Ready to review:"
echo "  python qa-bot/review.py repo $DEST"
