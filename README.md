# WalterChecks — Self-Hosted Code Review

Self-hosted code review on RunPod GPU instances. Combines 11 static analysis tools with an LLM to produce structured markdown reports. Reports are designed to be fed to Claude Code for automated fixes.

**This bot does NOT write code.** It finds issues. Claude Code fixes them.

## Architecture

```
RunPod Pod (GPU)
├── Network Volume (/workspace)
│   ├── models/              ← Model weights (persist across pods)
│   ├── .composer/           ← PHP tools (persist across pods)
│   ├── .npm-global/         ← JS tools (persist across pods)
│   ├── .git-credentials     ← GitHub PAT (persist across pods)
│   ├── .waltercheck-env     ← PATH config (auto-generated)
│   ├── qa-bot/              ← Scripts + prompts
│   └── repos/               ← Cloned repos to review
├── vLLM Server              ← Serves model as OpenAI-compatible API
└── review.py                ← Runs tools, sends to model, generates report
```

## Quick Start

### 1. Create a RunPod Network Volume
- RunPod → Storage → Network Volumes → **50GB**
- Pick the same region you'll launch pods in

### 2. Launch a Pod
- GPU: **RTX 4090** ($0.59/hr, 24GB) — always available, fast with 7B model
- Template: RunPod PyTorch (any CUDA-enabled template)
- Attach your network volume → mounts at `/workspace`

### 3. First-Time Setup (~5-10 min)
```bash
cd /workspace/qa-bot
chmod +x setup.sh setup_tools.sh serve.sh
./setup.sh          # Python deps, PHP 8.4, model download
./setup_tools.sh    # Static analysis tools
```

### 4. Start the Model Server
```bash
./serve.sh
```
Wait for "Application startup complete" — usually 1-2 minutes.

### 5. Run a Review
In a second terminal:
```bash
cd /workspace/repos
git clone https://github.com/your-org/your-repo.git

cd /workspace/qa-bot
python review.py repo /workspace/repos/your-repo
```
If the repo has a `WalterChecks.json`, profile/root/excludes are automatic.
Otherwise specify: `--profile wordpress`

Report saves to `/workspace/qa-bot/reports/`.

## On Subsequent Pods

Network volume keeps model, tools, and credentials. On a new pod:
```bash
cd /workspace/qa-bot
./setup.sh              # Reinstalls Python deps (skips model download)
./setup_tools.sh        # Checks tools, installs any missing
./serve.sh              # Start the LLM server
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
| `root` | No | Subdirectory to scan (e.g. plugin code lives in `plugin/`) |
| `exclude` | No | Additional directories to skip (on top of .gitignore + profile defaults) |
| `phpstan_level` | No | PHPStan level 0-9 (default: 5 for WP, 6 for Laravel) |

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
python review.py repo /workspace/repos/my-site --profile wordpress

# PR review (changed files only)
python review.py pr /workspace/repos/my-site --branch feature/new-header

# Tools only — no GPU needed
python review.py repo /workspace/repos/my-site --profile security --tools-only

# LLM only — skip static analysis
python review.py repo /workspace/repos/my-site --profile wordpress --no-tools

# Follow-up review (after fixes)
python review.py repo /workspace/repos/my-site --prior-report reports/previous.md
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
| **RTX 4090** | 24GB | $0.59 | Ada | ✓ Best pick — fast, cheap, always available |
| A40 | 48GB | $0.40 | Ampere | ✓ Cheapest (low availability) |
| L40S | 48GB | $0.86 | Ada | ✓ Works great |
| RTX 6000 Ada | 48GB | $0.77 | Ada | ✓ Works great |
| A100 SXM | 80GB | $1.39 | Ampere | ✓ Works but AWQ is slow on Ampere |
| RTX 5090 | 32GB | — | Blackwell | ✗ vLLM not compatible |
| RTX PRO 4500 | 32GB | $0.54 | Blackwell | ✗ Despite the name, it's Blackwell |
| RTX PRO 6000 | 96GB | $1.69 | Blackwell | ✗ vLLM not compatible |

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
→ Python packages don't persist. Run `./setup.sh` on each new pod.

**Tools not found after pod restart**
→ Run `source /workspace/.waltercheck-env` or re-run `./setup_tools.sh`.

**PHPStan reports hundreds of "Function X not found"**
→ PHPStan doesn't know about WordPress core functions. Add a `phpstan.neon` to your project (see CLAUDE.md for details).

**Half the review was third-party code**
→ Add `"exclude": ["plugin-update-checker/", "vendor/"]` to WalterChecks.json.

**Model response is slow on A100**
→ A100 has INT8 but not INT4 tensor cores. AWQ (4-bit) runs slowly on Ampere. Use the fp16 7B model on Ada (RTX 4090) instead.
