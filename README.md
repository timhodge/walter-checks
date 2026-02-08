# WalterChecks — Self-Hosted Code Review

**This bot does NOT write code.** It finds issues. Your coding agent fixes them.

WalterChecks is a fully self-hosted code review pipeline that combines 11 static analysis tools with an LLM to produce structured markdown reports. The reports are designed to be fed directly to Claude Code (or any coding agent) for automated fixes.

## Why Self-Hosted?

Every SaaS code review tool is one API change, pricing update, or sunset notice away from breaking your workflow. WalterChecks runs entirely on infrastructure you control — no API keys to manage, no rate limits to hit, no per-seat pricing, and no sending your private code to third-party services.

The model, the GPU, the static analysis tools, and the review logic all live on a RunPod pod you spin up on demand. When you're not reviewing code, the pod is off and you're not paying for it. Your network volume keeps everything persistent between sessions for a few dollars a month.

### Built to Evolve

The current default setup uses **Qwen2.5-Coder-7B** on an **RTX 4090** — a fast, cheap combination that handles most codebases well. But nothing about WalterChecks is locked to that choice:

- **Swap the model** — Point `serve.sh` at any vLLM-compatible model. A 14B or 32B model on a larger GPU will catch subtler issues. A smaller model on a cheaper card still runs the 11 static analysis tools perfectly.
- **Swap the GPU** — Any CUDA-compatible card with enough VRAM works. The scripts auto-detect VRAM and configure accordingly. As new GPU generations become available (and vLLM adds support), just pick a different card on your next pod.
- **Swap the provider** — While the setup scripts target RunPod, vLLM runs anywhere with a GPU. A local workstation, a cloud VM, a Kubernetes cluster — the review pipeline doesn't care where the model is served.

The static analysis tools (PHPStan, Psalm, PHPCS, ESLint, etc.) are industry-standard open source tools installed via Composer and npm. They'll keep getting updates from their respective communities regardless of what happens in the AI space.

### How It Fits Together

WalterChecks is the **reviewer**. It reads code, runs tools, and writes a findings report. It never touches your codebase. A separate coding agent (we use Claude Code) reads the report, makes fixes on a branch, and you run WalterChecks again to verify. This separation means you can upgrade either side independently — a better model improves the reviews, a better coding agent improves the fixes.

## Architecture

```
RunPod Pod (GPU)
└── Network Volume (/workspace)
    ├── start.sh                ← Single entry point
    ├── getrepo.sh              ← Clone repos for review
    ├── setup.sh                ← Python deps, PHP, model download
    ├── setup_tools.sh          ← Static analysis tool installation
    ├── serve.sh                ← Starts vLLM server
    ├── qa-bot/
    │   ├── review.py           ← Runs tools, sends to model, generates report
    │   ├── analyzers.py        ← Static analysis tool runners
    │   ├── prompts.py          ← LLM review profiles and system prompts
    │   └── test_connection.py
    ├── models/                 ← Model weights (persist across pods)
    ├── repos/                  ← Cloned repos to review
    ├── reports/                ← Generated review reports
    ├── .composer/              ← PHP tools (persist across pods)
    ├── .npm-global/            ← JS tools (persist across pods)
    └── .git-credentials        ← GitHub PAT (persist across pods)
```

## Quick Start

### 1. Create a RunPod Network Volume
- RunPod → Storage → Network Volumes → **50GB**
- Pick the same region you'll launch pods in

### 2. Launch a Pod
- GPU: **RTX 4090** ($0.59/hr, 24GB) — always available, fast with 7B model
- Template: RunPod PyTorch (any CUDA-enabled template)
- Attach your network volume → mounts at `/workspace`

### 3. Clone and Start
```bash
cd /workspace
git clone https://github.com/timhodge/walter-checks.git .
bash start.sh
```

`start.sh` runs setup, installs tools, and starts the LLM server. Safe to run every time — skips anything already installed.

Wait for "Application startup complete" — usually 1-2 minutes.

### 4. Run a Review
In a second terminal:
```bash
./getrepo.sh your-org/your-repo
python qa-bot/review.py repo repos/your-repo
```
If the repo has a `WalterChecks.json`, profile/root/excludes are automatic.
Otherwise specify: `--profile wordpress`

Report saves to `reports/`.

### 5. Use the Report

Copy the report into your project's `wc-reports/` directory:

```bash
# From your local machine
scp runpod:/workspace/reports/my-plugin-repo-wordpress-20260208.md \
    ~/projects/my-plugin/wc-reports/
```

The `wc-reports/` folder is the convention for storing WalterChecks reports inside each project. Your coding agent (Claude Code, etc.) reads from this directory to find actionable findings. Add `wc-reports/` to `.gitignore` — reports are working artifacts, not source code.

## On Subsequent Pods

Network volume keeps the repo, model, tools, and credentials. On a new pod:
```bash
cd /workspace
./start.sh    # Reinstalls Python deps, checks tools, starts server
```

## WalterChecks.json

Drop this in your repo root to configure reviews. Checked into git so the whole team shares the config.

```json
{
  "name": "My Plugin",
  "profile": "wordpress",
  "root": "plugin/",
  "exclude": [
    "plugin-update-checker/",
    "lib/legacy/"
  ],
  "phpstan_level": 5
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | No | Display name for reports (defaults to folder name) |
| `profile` | No | Default review profile. CLI `--profile` overrides |
| `root` | No | Subdirectory containing source code (tools still run from repo root where configs live) |
| `exclude` | No | Additional directories to skip (on top of .gitignore + profile defaults) |
| `phpstan_level` | No | PHPStan level 0-9 (default: 5 for WP, 6 for Laravel) |

## Preparing Your Project

WalterChecks runs standard tools (PHPStan, PHPCS, ESLint, etc.) against your code. These tools look for their config files in the repo root. For the cleanest results, set up your project so tools find the right configs automatically.

### Config File Placement

**All config files live at the repo root.** If your code lives in a subdirectory (e.g. `plugin/`), the config files still go at root — each one points into the code directory via its own path settings.

```
my-project/
├── WalterChecks.json          ← Review config
├── phpstan.neon               ← PHPStan config (points at plugin/)
├── .phpcs.xml.dist            ← PHPCS config (points at plugin/)
├── composer.json               ← Dependencies (WP stubs, etc.)
├── plugin/                    ← Actual code
│   ├── my-plugin.php
│   ├── includes/
│   └── vendor/                ← Composer deps for the plugin
└── dev-tools/                 ← Optional local dev scripts
```

### WordPress Plugin Example

**WalterChecks.json:**
```json
{
  "name": "My Plugin",
  "profile": "wordpress",
  "root": "plugin/",
  "exclude": ["vendor/"],
  "phpstan_level": 6
}
```

**phpstan.neon** (at repo root, scans into `plugin/`):
```yaml
includes:
    - plugin/vendor/szepeviktor/phpstan-wordpress/extension.neon

parameters:
    level: 6
    paths:
        - plugin
    excludePaths:
        - plugin/vendor
    scanFiles:
        - plugin/my-plugin.php
```

The WordPress extension (`szepeviktor/phpstan-wordpress`) teaches PHPStan about WordPress core functions, hooks, and globals. Without it, PHPStan will report hundreds of "Function not found" errors for things like `add_action`, `wp_enqueue_script`, etc.

Install it in your plugin's Composer dev dependencies:
```bash
cd plugin && composer require --dev szepeviktor/phpstan-wordpress
```

**.phpcs.xml.dist** (at repo root, scans into `plugin/`):
```xml
<?xml version="1.0"?>
<ruleset name="My Plugin Standards">
    <description>WordPress coding standards</description>
    <rule ref="WordPress"/>
    <arg name="extensions" value="php"/>
    <file>plugin</file>
    <exclude-pattern>plugin/vendor/*</exclude-pattern>
</ruleset>
```

### Why Configs at Root?

The `root` field in WalterChecks.json controls where the **LLM scans for source files** to review. But static analysis tools always run from the repo root, where they expect to find their config files. This separation means:

- One set of configs, shared between local dev, CI, and WalterChecks
- Tools pick up project-specific settings (ignore patterns, WP stubs, custom rules)
- No duplicate configs in subdirectories

### Simple Projects (No Subdirectory)

If your code is at the repo root (no `root` field needed), the setup is simpler:

```
my-theme/
├── WalterChecks.json
├── phpstan.neon
├── .phpcs.xml.dist
├── style.css
├── functions.php
└── ...
```

Just point the tools at `.` (current directory) in their configs, or let them auto-detect.

## GitHub Access

Set up once, persists on your network volume:
```bash
# Create a GitHub Fine-Grained PAT:
#   Scope: Your org's repos
#   Permission: Contents (read-only)
echo 'https://YOUR_USERNAME:YOUR_PAT@github.com' > /workspace/.git-credentials
git config --global credential.helper 'store --file=/workspace/.git-credentials'
```

## Review Profiles

| Profile | Use For |
|---------|---------|
| `wordpress` | WP themes + plugins — auto-detects which |
| `laravel` | Laravel apps with Filament/API awareness |
| `react` | React components, hooks, state management |
| `security` | Security-focused scan across any PHP/JS |
| `performance` | Performance bottlenecks, N+1 queries, caching |
| `general` | General code quality, any language |

## Review Modes

```bash
# Full repository scan
python qa-bot/review.py repo repos/my-site --profile wordpress

# PR review (changed files only)
python qa-bot/review.py pr repos/my-site --branch feature/new-header

# Tools only — no GPU needed
python qa-bot/review.py repo repos/my-site --profile security --tools-only

# LLM only — skip static analysis
python qa-bot/review.py repo repos/my-site --profile wordpress --no-tools

# Follow-up review (after fixes)
python qa-bot/review.py repo repos/my-site --prior-report reports/previous.md
```

## Static Analysis Tools (11)

| Tool | What It Does |
|------|-------------|
| PHPStan | Type analysis (levels 0-9, WordPress extension included) |
| Psalm | Taint analysis, security flow tracking |
| PHPCS | Coding standards (WordPress + PSR-12) |
| PHPMD | Mess detector (complexity, design, naming) |
| PHPCPD | Copy/paste detection |
| PHP Parallel Lint | Fast syntax checking |
| Rector | Deprecation detection (dry run) |
| Composer Audit | Known vulnerability check |
| ESLint | JavaScript linting |
| Stylelint | CSS linting |
| npm Audit | JS dependency vulnerability check |

## Compatible GPUs

| GPU | VRAM | $/hr | Architecture | Status |
|-----|------|------|--------------|--------|
| **RTX 4090** | 24GB | $0.59 | Ada | Best pick — fast, cheap, always available |
| A40 | 48GB | $0.40 | Ampere | Cheapest (low availability) |
| L40S | 48GB | $0.86 | Ada | Works great |
| RTX 6000 Ada | 48GB | $0.77 | Ada | Works great |
| A100 SXM | 80GB | $1.39 | Ampere | Works but AWQ is slow on Ampere |
| RTX 5090 | 32GB | — | Blackwell | vLLM not compatible |
| RTX PRO 4500 | 32GB | $0.54 | Blackwell | Despite the name, it's Blackwell |
| RTX PRO 6000 | 96GB | $1.69 | Blackwell | vLLM not compatible |

**Rule of thumb:** If it says "Blackwell", "RTX 50xx", or "RTX PRO" — don't use it until vLLM ships Blackwell support.

## Cost Estimates

| Scenario | GPU | Time | Cost |
|----------|-----|------|------|
| Small plugin (20 files) | RTX 4090 | ~5 min | ~$0.05 |
| Medium plugin (60 files) | RTX 4090 | ~15 min | ~$0.15 |
| Large theme (150 files) | RTX 4090 | ~30 min | ~$0.30 |
| Tools only (no GPU) | Any | ~2 min | $0.02 |

Plus ~$3.50/mo for 50GB network volume storage.

## Troubleshooting

**"CUDA error: unsupported toolchain"**
→ You're on a Blackwell GPU. Switch to Ada/Ampere (see table above).

**"CUDA out of memory"**
→ The 7B fp16 model needs ~14GB. If OOM, check that nothing else is using the GPU.

**vLLM not found on new pod**
→ Python packages don't persist. Run `./start.sh` — it reinstalls them automatically.

**Tools not found after pod restart**
→ Run `source /workspace/.waltercheck-env` or re-run `./start.sh`.

**PHPStan reports hundreds of "Function X not found"**
→ PHPStan doesn't know about WordPress core functions. Add a `phpstan.neon` with the WordPress extension — see "Preparing Your Project" above.

**Half the review was third-party code**
→ Add `"exclude": ["plugin-update-checker/", "vendor/"]` to WalterChecks.json.

**Model response is slow on A100**
→ A100 has INT8 but not INT4 tensor cores. AWQ (4-bit) runs slowly on Ampere. Use the fp16 7B model on Ada (RTX 4090) instead.
