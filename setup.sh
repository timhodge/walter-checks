#!/bin/bash
# setup.sh — First-time setup on RunPod
# Run ONCE after creating your network volume and launching a pod.
# Installs: Python deps, PHP 8.4, workspace directories
# Model download happens at serve time (./serve.sh)
set -e

echo "========================================="
echo "  WalterChecks — First Time Setup"
echo "========================================="

# ---- Check we're on a RunPod pod with network volume ----
if [ ! -d "/workspace" ]; then
    echo "ERROR: /workspace not found. Make sure you attached a network volume."
    exit 1
fi

# ---- Python dependencies ----
echo ""
echo "[1/4] Installing Python dependencies..."
pip install --break-system-packages -q \
    vllm \
    openai \
    rich \
    gitpython \
    tiktoken \
    hf_transfer 2>&1 | tail -3

echo "  ✓ Python packages installed"

# ---- PHP 8.4 (needed for static analysis tools) ----
echo ""
echo "[2/4] Installing PHP 8.4..."

install_php() {
    echo "  Step 1: Installing prerequisites..."
    apt-get update -qq
    apt-get install -y software-properties-common gnupg2 ca-certificates lsb-release

    echo "  Step 2: Adding ondrej/php PPA..."
    # Add the PPA key and repo manually (more reliable than add-apt-repository in containers)
    if ! add-apt-repository -y ppa:ondrej/php; then
        echo "  add-apt-repository failed, trying manual method..."
        echo "deb http://ppa.launchpad.net/ondrej/php/ubuntu $(lsb_release -cs) main" > /etc/apt/sources.list.d/ondrej-php.list
        apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 4F4EA0AAE5267A6C 2>/dev/null || true
    fi

    echo "  Step 3: Updating package lists..."
    apt-get update -qq

    echo "  Step 4: Installing PHP 8.4 packages..."
    apt-get install -y php8.4-cli php8.4-xml php8.4-mbstring php8.4-curl php8.4-zip php8.4-tokenizer

    # Make sure php points to 8.4
    update-alternatives --set php /usr/bin/php8.4 2>/dev/null || true
}

if command -v php &> /dev/null; then
    PHP_VER=$(php -r 'echo PHP_VERSION;')
    echo "  PHP already installed: $PHP_VER"
    if php -r 'exit(version_compare(PHP_VERSION, "8.3.16", ">=") ? 0 : 1);'; then
        echo "  ✓ PHP version is sufficient"
    else
        echo "  Need PHP 8.3.16+ (Psalm requirement). Upgrading..."
        install_php
        echo "  ✓ PHP upgraded to $(php -r 'echo PHP_VERSION;')"
    fi
else
    echo "  No PHP found, installing fresh..."
    install_php
    echo "  ✓ PHP $(php -r 'echo PHP_VERSION;') installed"
fi

# ---- Directory structure ----
echo ""
echo "[3/4] Setting up workspace..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$SCRIPT_DIR/models"
mkdir -p "$SCRIPT_DIR/repos"
mkdir -p "$SCRIPT_DIR/reports"

echo "  ✓ Workspace ready"

# ---- Git credentials (optional) ----
echo ""
echo "[4/4] Git configuration..."

if [ -f "/workspace/.git-credentials" ]; then
    git config --global credential.helper 'store --file=/workspace/.git-credentials'
    echo "  ✓ Git credentials configured from network volume"
else
    echo "  No git credentials found. To set up (one time):"
    echo ""
    echo "    # Create a GitHub Fine-Grained PAT with read-only Contents access"
    echo "    # Then run:"
    echo "    echo 'https://<your-github-username>:<your-pat>@github.com' > /workspace/.git-credentials"
    echo "    git config --global credential.helper 'store --file=/workspace/.git-credentials'"
    echo ""
fi

echo ""
echo "========================================="
echo "  Setup Complete!"
echo "========================================="
echo ""
echo "  Next steps:"
echo "    1. Install tools:   ./setup_tools.sh"
echo "    2. Start server:    ./serve.sh        (model selection happens here)"
echo "    3. Clone a repo:    ./getrepo.sh <owner/repo>"
echo "    4. Run a review:    python qa-bot/review.py repo repos/<name> -p wordpress"
echo ""
echo "  Or just run ./start.sh to do all of the above."
echo ""
echo "  Your network volume keeps models + credentials between pod restarts."
echo "  On new pods, just re-run: ./start.sh"
echo ""
