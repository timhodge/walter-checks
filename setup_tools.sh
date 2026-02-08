#!/bin/bash
# setup_tools.sh — Install static analysis tools
# Run on each new pod. Tools persist on network volume now!
# First run: ~3-5 min. Subsequent pods: ~30 seconds (skips already installed).
#
# DO NOT use set -e here — individual tool install failures are non-fatal.

echo "========================================="
echo "  WalterChecks — Static Analysis Tools"
echo "========================================="
echo ""

# ---- Verify PHP is available ----
if ! command -v php &> /dev/null; then
    echo "ERROR: PHP not found. Run ./setup.sh first."
    exit 1
fi
echo "PHP: $(php -r 'echo PHP_VERSION;')"
echo ""

# ---- Persistent paths on network volume ----
# Composer and npm install to /workspace so tools survive pod restarts
export COMPOSER_HOME="/workspace/.composer"
export PATH="$COMPOSER_HOME/vendor/bin:$PATH"

NPM_PREFIX="/workspace/.npm-global"
mkdir -p "$NPM_PREFIX"

# ---- System dependencies ----
echo "[1/5] System dependencies..."
echo ""

# unzip is needed by Composer — without it, archives extract via PHP zip extension
# which can corrupt packages and lose file permissions
if ! command -v unzip &> /dev/null; then
    echo "  Installing unzip..."
    apt-get update -qq
    apt-get install -y -qq unzip
fi
echo "  ✓ unzip available"

# ---- Install Composer if missing ----
echo ""
echo "[2/5] PHP tools via Composer..."
echo ""

if ! command -v composer &> /dev/null; then
    echo "  Installing Composer..."
    curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer 2>/dev/null
    if [ $? -ne 0 ]; then
        echo "  ERROR: Composer install failed. Check network/PHP."
        exit 1
    fi
fi

# Pre-allow the PHPCS installer plugin so it doesn't prompt interactively
mkdir -p "$COMPOSER_HOME"
composer global config allow-plugins.dealerdirect/phpcodesniffer-composer-installer true 2>/dev/null

# Install each tool only if its binary isn't already on the volume
install_if_missing() {
    local bin_name="$1"
    local package="$2"
    local label="$3"
    if [ -f "$COMPOSER_HOME/vendor/bin/$bin_name" ]; then
        echo "  ✓ $label (already installed)"
    else
        echo "  $label..."
        composer global require --quiet "$package" || echo "    ⚠ $label install failed"
    fi
}

install_if_missing "phpstan"       "phpstan/phpstan"                        "PHPStan"
install_if_missing "phpstan"       "szepeviktor/phpstan-wordpress"          "PHPStan WordPress ext"
install_if_missing "psalm"         "vimeo/psalm"                            "Psalm"
install_if_missing "phpcs"         "squizlabs/php_codesniffer"              "PHPCS"

# PHPCS standards (no binary to check — just install if phpcs is present)
if [ -f "$COMPOSER_HOME/vendor/bin/phpcs" ]; then
    composer global require --quiet wp-coding-standards/wpcs 2>/dev/null || true
    composer global require --quiet phpcompatibility/phpcompatibility-wp 2>/dev/null || true
    composer global require --quiet dealerdirect/phpcodesniffer-composer-installer 2>/dev/null || true
fi

install_if_missing "phpmd"         "phpmd/phpmd"                            "PHPMD"
install_if_missing "phpcpd"        "sebastian/phpcpd"                       "PHPCPD"
install_if_missing "parallel-lint" "php-parallel-lint/php-parallel-lint"    "Parallel Lint"
install_if_missing "rector"        "rector/rector"                          "Rector"

echo ""
echo "  ✓ PHP tools ready"
echo ""

# ---- Node.js / JavaScript Tools ----
echo "[3/5] JavaScript tools..."
echo ""

if ! command -v node &> /dev/null; then
    echo "  Installing Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_20.x 2>/dev/null | bash - 2>/dev/null
    apt-get install -y -qq nodejs 2>/dev/null || echo "  ⚠ Node.js install failed"
fi

# Check if JS tools already on volume
if [ -f "$NPM_PREFIX/bin/eslint" ]; then
    echo "  ✓ JS tools already on network volume — skipping install"
else
    echo "  Installing ESLint + Stylelint to $NPM_PREFIX..."
    npm install --prefix "$NPM_PREFIX" -g eslint stylelint 2>/dev/null || echo "  ⚠ npm install failed"
fi

# Add npm global to PATH
export PATH="$NPM_PREFIX/bin:$PATH"

echo ""
echo "  ✓ JavaScript tools ready"
echo ""

# ---- Configure PATH persistence ----
echo "[4/5] Configuring PATH..."

# Write a sourceable env file to the network volume
cat > /workspace/.waltercheck-env << 'EOF'
# WalterChecks environment — source this on new pods
export COMPOSER_HOME="/workspace/.composer"
export PATH="$COMPOSER_HOME/vendor/bin:/workspace/.npm-global/bin:$PATH"
EOF

# Source it in bashrc if not already
if ! grep -q 'waltercheck-env' ~/.bashrc 2>/dev/null; then
    echo '' >> ~/.bashrc
    echo '# WalterChecks tools' >> ~/.bashrc
    echo '[ -f /workspace/.waltercheck-env ] && source /workspace/.waltercheck-env' >> ~/.bashrc
fi

# Source it now for this session
source /workspace/.waltercheck-env

echo "  ✓ PATH configured (persists via /workspace/.waltercheck-env)"
echo ""

# ---- Verify installations ----
echo "[5/5] Verifying tools..."
echo ""

installed=0
missing=0

check_tool() {
    local cmd="$1"
    local label="$2"
    if command -v "$cmd" &> /dev/null; then
        echo "  ✓ $label"
        installed=$((installed + 1))
    else
        echo "  ✗ $label (not found)"
        missing=$((missing + 1))
    fi
}

check_tool "phpstan"       "PHPStan (type analysis)"
check_tool "psalm"         "Psalm (taint/security)"
check_tool "phpcs"         "PHPCS (coding standards)"
check_tool "phpmd"         "PHPMD (mess detector)"
check_tool "phpcpd"        "PHPCPD (copy/paste)"
check_tool "parallel-lint" "Parallel Lint (syntax)"
check_tool "rector"        "Rector (deprecations)"
check_tool "composer"      "Composer (audit)"
check_tool "eslint"        "ESLint (JS linting)"
check_tool "stylelint"     "Stylelint (CSS linting)"
check_tool "npm"           "npm (audit)"

echo ""
echo "========================================="
echo "  Tools Ready: $installed installed, $missing missing"
echo "========================================="
echo ""
echo "  Tools persist on network volume — no reinstall needed on next pod!"
echo "  Just source the env: source /workspace/.waltercheck-env"
echo ""
echo "  You can now run reviews:"
echo "    ./serve.sh                                 # Start LLM server"
echo "    python review.py repo <path> -p wordpress  # Full review"
echo "    python review.py repo <path> --tools-only  # Tools only (no GPU)"
echo ""
echo "  TIP: For Laravel projects, install Larastan in the project itself:"
echo "    cd /workspace/repos/your-laravel-app"
echo "    composer require --dev nunomaduro/larastan"
echo "  The QA bot will automatically use vendor/bin/phpstan (with Larastan)"
echo "  over the global install for smarter Laravel-aware analysis."
echo ""
