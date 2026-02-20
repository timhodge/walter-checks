#!/bin/bash
# start.sh — One-command startup for WalterChecks on RunPod
# Runs setup, installs tools, then starts the LLM server.
# Safe to run every time — skips anything already installed.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure all scripts are executable (fresh clone won't preserve this)
chmod +x "$SCRIPT_DIR"/*.sh "$SCRIPT_DIR"/getrepo.sh 2>/dev/null || true

echo ""
echo "========================================="
echo "  WalterChecks — Starting Up"
echo "========================================="
echo ""

# ---- Setup: Python deps, PHP, model ----
"$SCRIPT_DIR/setup.sh"

# ---- Static analysis tools ----
"$SCRIPT_DIR/setup_tools.sh"

# ---- Start LLM server (blocks) ----
echo ""
echo "========================================="
echo "  All set — starting LLM server"
echo "========================================="
echo ""
echo "  Open a second terminal to run reviews:"
echo "    ./getrepo.sh <owner/repo>"
echo "    python qa-bot/review.py repo repos/<name> -p wordpress"
echo ""

"$SCRIPT_DIR/serve.sh" "$@"
