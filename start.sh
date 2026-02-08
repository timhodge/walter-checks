#!/bin/bash
# start.sh — One-command startup for WalterChecks on RunPod
# Runs setup, installs tools, then starts the LLM server.
# Safe to run every time — skips anything already installed.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
echo "    python review.py repo /workspace/repos/<name> -p wordpress"
echo ""

"$SCRIPT_DIR/serve.sh"
