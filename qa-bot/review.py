#!/usr/bin/env python3
"""
review.py — WalterChecks Code Review Pipeline

Two modes:
  repo  — "Look at this entire damn repo from scratch"
  pr    — "Look at this PR that CC and CodeRabbit feel good about"

Output: Markdown report for Claude Code to ingest. This bot does NOT write code.

Usage:
    # Full repo scan
    python review.py repo /workspace/repos/my-site --profile wordpress

    # PR review (compare branch to main)
    python review.py pr /workspace/repos/my-site --branch feature/new-header

    # PR review (specific commit range)
    python review.py pr /workspace/repos/my-site --range main..feature/new-header

    # Tools only — no GPU needed
    python review.py repo /workspace/repos/my-site --profile security --tools-only

    # LLM only — skip static analysis
    python review.py repo /workspace/repos/my-site --profile wordpress --no-tools
"""

import argparse
import inspect
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.panel import Panel
from rich.table import Table

from prompts import PROFILES
from analyzers import SUITE_RUNNERS

VLLM_BASE_URL = "http://localhost:8000/v1"
MAX_FILE_SIZE = 50_000        # Skip files > 50KB (minified/generated)
MAX_CHARS_PER_BATCH = 12_000  # ~3K tokens per batch
MAX_TOTAL_FILES = 300
CONFIG_FILENAME = "WalterChecks.json"

console = Console()


# ===========================================================================
# PROJECT CONFIG
# ===========================================================================

def load_config(repo: str) -> dict:
    """Load WalterChecks.json from repo root (or parent if scanning a subdirectory).
    Returns config dict or empty dict if not found.

    WalterChecks.json schema:
    {
        "name": "My Plugin",          // Display name for reports
        "profile": "wordpress",       // Default profile (CLI --profile overrides)
        "root": "plugin/",            // Subdirectory to scan (relative to repo root)
        "exclude": [                  // Additional dirs to skip (on top of .gitignore + profile defaults)
            "plugin-update-checker/",
            "lib/legacy/"
        ],
        "phpstan_level": 5            // Override default PHPStan level
    }
    """
    # Check the given directory first, then one level up (in case root is used)
    for search_dir in [repo, os.path.dirname(os.path.abspath(repo))]:
        config_path = os.path.join(search_dir, CONFIG_FILENAME)
        if os.path.isfile(config_path):
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                console.print(f"  Loaded [bold]{CONFIG_FILENAME}[/bold] from {search_dir}")
                return config
            except (json.JSONDecodeError, IOError) as e:
                console.print(f"  [yellow]Warning:[/yellow] Invalid {CONFIG_FILENAME}: {e}")
                return {}
    return {}


def apply_config(config: dict, repo: str, args) -> tuple[str, dict]:
    """Apply WalterChecks.json config. Returns (effective_repo_path, config).
    CLI arguments always override config values."""

    # root: redirect scan to subdirectory
    effective_repo = repo
    if "root" in config:
        candidate = os.path.join(repo, config["root"])
        if os.path.isdir(candidate):
            effective_repo = candidate
            console.print(f"  Scan root: [bold]{config['root']}[/bold]")
        else:
            console.print(f"  [yellow]Warning:[/yellow] root '{config['root']}' not found, scanning full repo")

    # profile: use as default if --profile wasn't explicitly set
    if config.get("profile") and args.profile == "wordpress":
        args.profile = config["profile"]

    # phpstan_level: use as default if not set via CLI
    if config.get("phpstan_level") is not None and args.phpstan_level is None:
        args.phpstan_level = config["phpstan_level"]

    return effective_repo, config


# ===========================================================================
# FILE DISCOVERY
# ===========================================================================

def detect_wp_type(repo: str) -> str:
    """Detect whether a WordPress repo is a theme or plugin.
    Returns 'wp-theme', 'wp-plugin', or 'wp-theme' as default."""
    # Theme indicators: style.css with Theme Name header, template files
    style_css = os.path.join(repo, "style.css")
    if os.path.isfile(style_css):
        try:
            with open(style_css, 'r', encoding='utf-8', errors='ignore') as f:
                head = f.read(2000)
            if "Theme Name" in head:
                return "wp-theme"
        except Exception:
            pass

    # Plugin indicators: main PHP file with Plugin Name header
    for fname in os.listdir(repo):
        if fname.endswith(".php"):
            fp = os.path.join(repo, fname)
            try:
                with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                    head = f.read(3000)
                if "Plugin Name" in head:
                    return "wp-plugin"
            except Exception:
                continue

    # Secondary heuristics
    has_templates = any(os.path.isfile(os.path.join(repo, t)) for t in
                        ["index.php", "single.php", "page.php", "header.php",
                         "footer.php", "archive.php", "functions.php"])
    has_plugin_dirs = any(os.path.isdir(os.path.join(repo, d)) for d in
                          ["admin", "includes", "public", "assets"])

    if has_templates:
        return "wp-theme"
    # If we only see includes/admin structure with no templates, likely plugin
    if has_plugin_dirs and not has_templates:
        return "wp-plugin"

    return "wp-theme"  # Default to theme (more common in your workflow)


def resolve_profile(profile_name: str, repo: str) -> tuple[str, dict]:
    """Resolve auto-detect profiles. Returns (resolved_name, profile_dict)."""
    profile = PROFILES[profile_name]
    if profile.get("auto_detect"):
        detected = detect_wp_type(repo)
        resolved = PROFILES[detected].copy()
        return detected, resolved
    return profile_name, profile

def discover_files(repo: str, profile: dict, file_filter: list[str] = None,
                   extra_excludes: list[str] = None) -> list[dict]:
    """Walk repo and collect reviewable files.

    Uses git ls-files when in a git repo (respects .gitignore automatically).
    Falls back to os.walk for non-git directories.

    file_filter: limit to specific paths (PR mode)
    extra_excludes: additional directory prefixes to skip (from WalterChecks.json)
    """
    files = []
    exts = set(profile["file_extensions"])
    skip_dirs = set(profile["skip_dirs"])
    skip_files = set(profile.get("skip_files", []))
    excludes = [e.strip("/") for e in (extra_excludes or [])]

    def should_skip_path(rel_path: str) -> bool:
        """Check if a path matches skip_dirs or extra excludes."""
        parts = rel_path.split(os.sep)
        # Check directory components against skip_dirs
        for part in parts[:-1]:  # all except filename
            if part in skip_dirs or part.startswith('.'):
                return True
        # Check against extra excludes (prefix match)
        for exc in excludes:
            if rel_path.startswith(exc) or (os.sep + exc) in rel_path:
                return True
        return False

    # Try git ls-files first (respects .gitignore)
    tracked_files = None
    try:
        # Find the git root — might be above our scan directory
        git_root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo, capture_output=True, text=True
        )
        if git_root_result.returncode == 0:
            git_root = git_root_result.stdout.strip()
            r = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=repo, capture_output=True, text=True
            )
            if r.returncode == 0:
                tracked_files = set(r.stdout.strip().split("\n"))
                tracked_files.discard("")
    except Exception:
        pass

    if tracked_files is not None:
        # Git-aware mode: only review tracked/unignored files
        for rp in sorted(tracked_files):
            fn = os.path.basename(rp)
            if fn in skip_files:
                continue
            if not any(fn.endswith(e) for e in exts):
                continue
            if should_skip_path(rp):
                continue
            if file_filter is not None and rp not in file_filter:
                continue
            fp = os.path.join(repo, rp)
            try:
                sz = os.path.getsize(fp)
                if sz > MAX_FILE_SIZE or sz == 0:
                    continue
            except OSError:
                continue
            try:
                with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            except Exception:
                continue
            files.append({"path": rp, "content": content, "size": sz})
    else:
        # Fallback: os.walk (non-git repos)
        for root, dirs, fnames in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]
            rel_root = os.path.relpath(root, repo)
            for fn in fnames:
                if fn in skip_files:
                    continue
                if not any(fn.endswith(e) for e in exts):
                    continue
                fp = os.path.join(root, fn)
                rp = os.path.join(rel_root, fn) if rel_root != '.' else fn
                if should_skip_path(rp):
                    continue
                if file_filter is not None and rp not in file_filter:
                    continue
                try:
                    sz = os.path.getsize(fp)
                    if sz > MAX_FILE_SIZE or sz == 0:
                        continue
                except OSError:
                    continue
                try:
                    with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                except Exception:
                    continue
                files.append({"path": rp, "content": content, "size": sz})

    return files[:MAX_TOTAL_FILES]


# ===========================================================================
# GIT / PR HELPERS
# ===========================================================================

def git_changed_files(repo: str, branch: str = None,
                      commit_range: str = None, base: str = "main") -> list[str]:
    if commit_range:
        cmd = ["git", "diff", "--name-only", commit_range]
    elif branch:
        cmd = ["git", "diff", "--name-only", f"{base}...{branch}"]
    else:
        cmd = ["git", "diff", "--name-only", "HEAD"]
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        return [f.strip() for f in r.stdout.strip().split("\n") if f.strip()]
    except Exception as e:
        console.print(f"[red]Git error:[/red] {e}")
        return []


def git_diff(repo: str, branch: str = None,
             commit_range: str = None, base: str = "main") -> str:
    if commit_range:
        cmd = ["git", "diff", "--unified=5", commit_range]
    elif branch:
        cmd = ["git", "diff", "--unified=5", f"{base}...{branch}"]
    else:
        cmd = ["git", "diff", "--unified=5", "HEAD"]
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        return r.stdout
    except Exception:
        return ""


def git_log(repo: str, branch: str = None,
            commit_range: str = None, base: str = "main") -> str:
    if commit_range:
        cmd = ["git", "log", "--oneline", commit_range]
    elif branch:
        cmd = ["git", "log", "--oneline", f"{base}..{branch}"]
    else:
        cmd = ["git", "log", "--oneline", "-10"]
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        return r.stdout.strip()
    except Exception:
        return ""


# ===========================================================================
# PRIOR REPORT (follow-up review context)
# ===========================================================================

def find_latest_report(repo: str) -> str | None:
    """Auto-find the most recent WalterChecks report for this repo.
    Searches the reports/ directory for files matching the repo name,
    returns the newest one by modification time."""
    rdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    if not os.path.isdir(rdir):
        return None
    name = os.path.basename(os.path.normpath(repo)).lower()
    candidates = []
    for f in os.listdir(rdir):
        if f.lower().startswith(name) and f.endswith(".md"):
            fp = os.path.join(rdir, f)
            candidates.append((os.path.getmtime(fp), fp))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def load_prior_report(path: str, max_chars: int = 15000) -> str:
    """Load a prior report, truncating the middle if too large.
    Keeps the header (finding list/severity) and trims verbose batch output."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return f"(Could not load prior report: {e})"
    if len(content) <= max_chars:
        return content
    # Keep first and last portions — the header has metadata and summary,
    # the end has the summary section. Middle batches get trimmed.
    half = max_chars // 2
    return (content[:half]
            + "\n\n... [PRIOR REPORT TRUNCATED — middle batches omitted] ...\n\n"
            + content[-half:])


# ===========================================================================
# BATCHING
# ===========================================================================

def _batch_groups(groups: dict) -> list[list[dict]]:
    batches = []
    for _, group_files in groups.items():
        if not group_files:
            continue
        batch, sz = [], 0
        for f in group_files:
            fsz = len(f["content"])
            if sz + fsz > MAX_CHARS_PER_BATCH and batch:
                batches.append(batch)
                batch, sz = [], 0
            if fsz > MAX_CHARS_PER_BATCH:
                if batch:
                    batches.append(batch)
                    batch, sz = [], 0
                batches.append([f])
                continue
            batch.append(f)
            sz += fsz
        if batch:
            batches.append(batch)
    return batches


def group_wordpress(files: list[dict]) -> list[list[dict]]:
    """Legacy grouper — used if wordpress auto-detect somehow bypasses resolution."""
    return group_wp_theme(files)


def group_wp_theme(files: list[dict]) -> list[list[dict]]:
    """Group theme files by role: template hierarchy, functions, partials, assets."""
    g = {"functions": [], "template_hierarchy": [], "partials": [],
         "theme_php": [], "javascript": [], "css": [], "config": [], "other": []}
    for f in files:
        p = f["path"].lower()
        # functions.php and its includes
        if "functions.php" in p or (("inc/" in p or "includes/" in p) and p.endswith(".php")):
            g["functions"].append(f)
        # Core template hierarchy files
        elif p.endswith(".php") and any(k in os.path.basename(p) for k in
                ["index.php", "single", "page", "archive", "search", "404",
                 "home", "front-page", "category", "tag", "taxonomy", "author",
                 "date", "attachment", "image", "comments"]):
            g["template_hierarchy"].append(f)
        # Template parts (header, footer, sidebar, get_template_part targets)
        elif p.endswith(".php") and any(k in p for k in
                ["header", "footer", "sidebar", "template-parts/", "parts/",
                 "partials/", "components/"]):
            g["partials"].append(f)
        elif p.endswith((".php", ".twig")):
            g["theme_php"].append(f)
        elif p.endswith((".js", ".jsx", ".ts", ".tsx")):
            g["javascript"].append(f)
        elif p.endswith((".css", ".scss")):
            g["css"].append(f)
        elif p.endswith((".json", ".yml", ".yaml", ".xml", ".conf", ".htaccess")):
            g["config"].append(f)
        else:
            g["other"].append(f)
    return _batch_groups(g)


def group_wp_plugin(files: list[dict]) -> list[list[dict]]:
    """Group plugin files by role: main file, admin, public, includes, assets."""
    g = {"main_plugin": [], "admin": [], "includes": [], "public_facing": [],
         "ajax_rest": [], "database": [], "javascript": [], "css": [],
         "config": [], "other": []}
    for f in files:
        p = f["path"].lower()
        bn = os.path.basename(p)
        # Main plugin file (top-level PHP with Plugin Name header — heuristic: top-level .php)
        depth = p.replace("\\", "/").strip("/").count("/")
        if depth == 0 and p.endswith(".php"):
            g["main_plugin"].append(f)
        # Admin-specific code
        elif "admin/" in p or "admin-" in bn or "settings" in bn:
            g["admin"].append(f)
        # AJAX and REST handlers
        elif any(k in p for k in ["ajax", "rest-api", "rest/", "api/", "endpoints"]):
            g["ajax_rest"].append(f)
        # Database/migration/table code
        elif any(k in p for k in ["database", "migration", "table", "schema", "install"]):
            g["database"].append(f)
        # Public-facing code
        elif "public/" in p or "frontend/" in p:
            g["public_facing"].append(f)
        # Core includes
        elif p.endswith(".php") and any(k in p for k in
                ["includes/", "inc/", "src/", "lib/", "classes/"]):
            g["includes"].append(f)
        elif p.endswith(".php"):
            g["includes"].append(f)
        elif p.endswith((".js", ".jsx", ".ts", ".tsx")):
            g["javascript"].append(f)
        elif p.endswith((".css", ".scss")):
            g["css"].append(f)
        elif p.endswith((".json", ".yml", ".yaml", ".xml")):
            g["config"].append(f)
        else:
            g["other"].append(f)
    return _batch_groups(g)


def group_laravel(files: list[dict]) -> list[list[dict]]:
    """Group Laravel files with Filament and API awareness."""
    g = {"filament_resources": [], "filament_pages_widgets": [],
         "api_controllers": [], "api_resources": [], "controllers": [],
         "form_requests": [], "models": [], "policies": [],
         "middleware": [], "routes": [], "services": [],
         "migrations": [], "views": [], "javascript": [],
         "config": [], "other": []}
    for f in files:
        p = f["path"].lower()
        bn = os.path.basename(p)
        # Filament files — group resources separately from pages/widgets
        if "filament/" in p and ("resource" in p or "relationmanager" in bn
                                  or "relation" in bn):
            g["filament_resources"].append(f)
        elif "filament/" in p:
            g["filament_pages_widgets"].append(f)
        # API layer
        elif "api/" in p and "controller" in p:
            g["api_controllers"].append(f)
        elif "resources/" in p and "resource.php" in bn and p.endswith(".php"):
            g["api_resources"].append(f)
        # Standard Laravel
        elif "controller" in p:
            g["controllers"].append(f)
        elif "request" in p and p.endswith(".php") and "http/" in p:
            g["form_requests"].append(f)
        elif "/models/" in p:
            g["models"].append(f)
        elif "polic" in p:  # policy, policies
            g["policies"].append(f)
        elif "middleware" in p:
            g["middleware"].append(f)
        elif "routes/" in p:
            g["routes"].append(f)
        elif "views/" in p or p.endswith(".blade.php"):
            g["views"].append(f)
        elif "migration" in p:
            g["migrations"].append(f)
        elif any(k in p for k in ["services/", "actions/", "jobs/",
                                    "events/", "listeners/", "notifications/"]):
            g["services"].append(f)
        elif p.endswith((".js", ".jsx", ".ts", ".tsx", ".vue")):
            g["javascript"].append(f)
        elif p.endswith((".json", ".yml", ".yaml")):
            g["config"].append(f)
        else:
            g["other"].append(f)
    return _batch_groups(g)


def group_flat(files: list[dict]) -> list[list[dict]]:
    return _batch_groups({"all": files})


GROUPERS = {
    "wordpress": group_wordpress,
    "wp-theme": group_wp_theme,
    "wp-plugin": group_wp_plugin,
    "laravel": group_laravel,
    "flat": group_flat,
}


# ===========================================================================
# LLM INTERACTION
# ===========================================================================

def review_batch(client: OpenAI, sys_prompt: str, batch: list[dict],
                 model: str, analysis_ctx: str = "", diff_ctx: str = "",
                 prior_report_ctx: str = "") -> str:
    parts = []
    if prior_report_ctx:
        parts.append(
            "PRIOR QA REPORT (your previous findings for this codebase):\n\n"
            "The code changes you are reviewing were made IN RESPONSE to this report.\n"
            "For each of your prior findings, determine whether the changes address it.\n"
            "Flag any prior findings that were NOT addressed.\n"
            "Also flag any NEW issues introduced by the changes.\n\n"
            f"{prior_report_ctx}\n\n---\n"
        )
    if analysis_ctx:
        parts.append(f"STATIC ANALYSIS RESULTS:\n\n{analysis_ctx}\n\n---\n")
    if diff_ctx:
        parts.append(f"GIT DIFF (the changes under review):\n\n```diff\n{diff_ctx[:6000]}\n```\n\n---\n")

    if prior_report_ctx:
        parts.append(
            "Review the code below. This is a FOLLOW-UP review. The developer was given "
            "your prior report and filed a PR to address your findings. Your job:\n"
            "1. For each prior CRITICAL/WARNING finding: was it fixed? Partially fixed? Ignored?\n"
            "2. Are there any NEW issues introduced by these changes?\n"
            "3. Is the fix correct, or did it introduce a different problem?\n"
            "4. Any prior findings that are no longer relevant (code was removed, etc.)?\n\n"
        )
    else:
        parts.append(
            "Review the code below. Reference tool findings where relevant. "
            "Confirm real issues, dismiss false positives, find issues tools missed.\n\n"
        )
    for f in batch:
        parts.append(f"--- FILE: {f['path']} ---")
        parts.append(f["content"])
        parts.append("")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "\n".join(parts)},
            ],
            temperature=0.1,
            max_tokens=4096,
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"**Error reviewing batch:** {e}"


def detect_model(client: OpenAI) -> str:
    try:
        return client.models.list().data[0].id
    except Exception:
        return "unknown"


# ===========================================================================
# REPORT GENERATION
# ===========================================================================

def generate_report(mode: str, results: list[dict], profile_name: str,
                    repo: str, elapsed: float, analysis_report: str = "",
                    pr_info: dict = None, is_followup: bool = False,
                    prior_report_path: str = None,
                    project_name: str = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    name = project_name or os.path.basename(os.path.normpath(repo))
    total_files = sum(r['file_count'] for r in results)

    mode_str = "Follow-up Review" if is_followup else \
               ("Full Repository Scan" if mode == "repo" else "Pull Request Review")

    lines = [
        f"# WalterChecks Report: {name}",
        "",
        f"**Mode:** {mode_str}",
        f"**Profile:** {profile_name}",
        f"**Date:** {now}",
        f"**Files reviewed:** {total_files}",
        f"**Review batches:** {len(results)}",
        f"**Time:** {elapsed:.0f}s",
    ]

    if is_followup and prior_report_path:
        lines.append(f"**Prior report:** `{os.path.basename(prior_report_path)}`")

    if pr_info:
        lines.extend([
            f"**Branch:** {pr_info.get('branch', 'N/A')}",
            f"**Base:** {pr_info.get('base', 'main')}",
            f"**Changed files:** {pr_info.get('changed_count', '?')}",
        ])
        if pr_info.get("commits"):
            lines.extend(["", "**Commits:**", "```", pr_info["commits"], "```"])

    lines.extend(["", "---", ""])

    if is_followup:
        lines.extend([
            "## Follow-up Review", "",
            "This review checked whether a developer's changes properly addressed",
            "the findings from the prior QA report. The review below uses a",
            "finding-by-finding status format:", "",
            "- ✅ **FIXED** — the issue was properly addressed",
            "- ⚠️ **PARTIAL** — the fix is incomplete or introduced a new issue",
            "- ❌ **NOT ADDRESSED** — the finding was not fixed in this PR",
            "- 🆕 **NEW ISSUE** — a problem that wasn't in the prior report",
            "", "---", "",
        ])
    else:
        lines.extend([
            "## How To Use This Report", "",
            "Feed this report to Claude Code:", "",
            "```",
            f"Read the QA bot report at /path/to/this/report.md and fix all",
            "CRITICAL and WARNING issues. For each fix, explain what you changed.",
            "```", "",
            "Then review the fixes with a follow-up scan:", "",
            "```",
            f"python review.py pr <repo> --branch <cc-branch> --latest",
            "```", "",
            "*This bot produces findings only — it does not write code.*",
            "", "---", "",
        ])

    if analysis_report:
        lines.extend(["## Static Analysis Results", "", analysis_report, "", "---", ""])

    if results:
        lines.extend(["## LLM Code Review", ""])
        for i, r in enumerate(results, 1):
            fl = ", ".join(f"`{f}`" for f in r["files"])
            lines.extend([f"### Batch {i}: {fl}", "", r["review"], "", "---", ""])

    lines.extend([
        "## Summary", "",
        f"Reviewed {total_files} files in {len(results)} batches ({elapsed:.0f}s).", "",
    ])
    if is_followup:
        lines.append("*Follow-up review generated by WalterChecks. "
                      "Any NOT ADDRESSED findings should go back to the developer.*")
    else:
        lines.append("*Generated by WalterChecks. Feed to Claude Code for fixes, "
                      "then use --latest for follow-up review.*")
    return "\n".join(lines)


# ===========================================================================
# MAIN
# ===========================================================================

def run_tools(repo: str, profile_name: str, phpstan_level: int, skip: bool):
    if skip:
        console.print("\n[dim]Skipping static analysis (--no-tools)[/dim]")
        return "", ""
    console.print("\n[cyan]Running static analysis tools (parallel)...[/cyan]")
    runner = SUITE_RUNNERS.get(profile_name)
    if not runner:
        console.print("  [yellow]No tool suite for this profile[/yellow]")
        return "", ""
    sig = inspect.signature(runner)
    suite = runner(repo, phpstan_level=phpstan_level) if "phpstan_level" in sig.parameters else runner(repo)

    table = Table(show_header=False, box=None, padding=(0, 2))
    for r in suite.results:
        if r.success:
            if r.findings_count > 0:
                table.add_row("[yellow]![/yellow]", r.tool, f"[yellow]{r.findings_count} findings[/yellow]")
            else:
                table.add_row("[green]✓[/green]", r.tool, "[green]Clean[/green]")
        else:
            table.add_row("[dim]—[/dim]", r.tool, f"[dim]{r.error}[/dim]")
    console.print(table)
    return suite.to_prompt_context(), suite.to_report_section()


def save_report(report: str, repo: str, profile: str, mode: str, out: str = None) -> str:
    if out:
        path = out
    else:
        rdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
        os.makedirs(rdir, exist_ok=True)
        name = os.path.basename(os.path.normpath(repo))
        ts = datetime.now().strftime("%Y%m%d-%H%M")
        path = os.path.join(rdir, f"{name}-{mode}-{profile}-{ts}.md")
    with open(path, 'w') as f:
        f.write(report)
    return path


def main():
    parser = argparse.ArgumentParser(
        description="WalterChecks — Code review pipeline (report only, no code changes)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Initial scan
  python review.py repo /workspace/repos/my-site --profile wordpress

  # Review a PR
  python review.py pr /workspace/repos/my-site --branch feature/new-header

  # Follow-up: CC filed a PR to fix your findings
  python review.py pr /workspace/repos/my-site --branch cc-fixes --latest

  # Follow-up: specify exact prior report
  python review.py pr /workspace/repos/my-site --branch cc-fixes \
    --prior-report reports/my-site-repo-wordpress-20260207-1430.md

  # Tools only (no GPU)
  python review.py repo /workspace/repos/my-site --profile security --tools-only
        """)

    sub = parser.add_subparsers(dest="mode", required=True)

    # -- repo mode --
    rp = sub.add_parser("repo", help="Full repository scan")
    rp.add_argument("repo_path")
    rp.add_argument("--profile", "-p", default="general", choices=list(PROFILES.keys()))
    rp.add_argument("--output", "-o", default=None)
    rp.add_argument("--max-files", type=int, default=MAX_TOTAL_FILES)
    rp.add_argument("--no-tools", action="store_true", help="Skip static analysis")
    rp.add_argument("--tools-only", action="store_true", help="Run tools only, no LLM")
    rp.add_argument("--phpstan-level", type=int, default=None)
    rp.add_argument("--prior-report", default=None,
                    help="Path to a previous QA report for follow-up review")
    rp.add_argument("--latest", action="store_true",
                    help="Auto-load the most recent report for this repo as context")
    rp.add_argument("--url", default=VLLM_BASE_URL)

    # -- pr mode --
    pp = sub.add_parser("pr", help="Review a PR / branch diff")
    pp.add_argument("repo_path")
    pp.add_argument("--branch", "-b", default=None, help="Branch to review")
    pp.add_argument("--base", default="main", help="Base branch (default: main)")
    pp.add_argument("--range", default=None, help="Commit range (e.g. main..feature/x)")
    pp.add_argument("--profile", "-p", default="general", choices=list(PROFILES.keys()))
    pp.add_argument("--output", "-o", default=None)
    pp.add_argument("--no-tools", action="store_true")
    pp.add_argument("--tools-only", action="store_true")
    pp.add_argument("--phpstan-level", type=int, default=None)
    pp.add_argument("--prior-report", default=None,
                    help="Path to a previous QA report for follow-up review")
    pp.add_argument("--latest", action="store_true",
                    help="Auto-load the most recent report for this repo as context")
    pp.add_argument("--url", default=VLLM_BASE_URL)

    args = parser.parse_args()
    repo = os.path.abspath(args.repo_path)
    if not os.path.isdir(repo):
        console.print(f"[red]Error:[/red] {repo} is not a directory")
        sys.exit(1)

    # ---- Load project config ----
    console.print("\n[cyan]Loading project config...[/cyan]")
    config = load_config(repo)
    repo, config = apply_config(config, repo, args)
    extra_excludes = config.get("exclude", [])
    project_name = config.get("name", os.path.basename(os.path.normpath(repo)))

    if extra_excludes:
        console.print(f"  Excluding: {', '.join(extra_excludes)}")
    if not config:
        console.print(f"  No {CONFIG_FILENAME} found (using defaults)")
        console.print(f"  [dim]Tip: Add a {CONFIG_FILENAME} to your repo root to configure reviews[/dim]")

    # Auto-detect theme vs plugin for generic "wordpress" profile
    resolved_profile_name, profile = resolve_profile(args.profile, repo)
    phpstan_defaults = {"wordpress": 5, "wp-theme": 5, "wp-plugin": 5, "laravel": 6}
    phpstan_level = args.phpstan_level if args.phpstan_level is not None \
        else phpstan_defaults.get(resolved_profile_name, 5)

    mode_label = "Full Repository Scan" if args.mode == "repo" else "Pull Request Review"
    detected_note = ""
    if args.profile == "wordpress" and resolved_profile_name != "wordpress":
        detected_note = f"\nAuto-detected: [bold green]{resolved_profile_name}[/bold green]"
    console.print(Panel(
        f"[bold]{mode_label}[/bold] — {profile['name']}\n"
        f"Project: {project_name}\nRepo: {repo}\nPHPStan Level: {phpstan_level}{detected_note}",
        title="🔍 WalterChecks", border_style="cyan"))

    start = time.time()

    # ---- Load prior report for follow-up context ----
    prior_report_ctx = ""
    prior_report_path = None
    if hasattr(args, 'prior_report') and args.prior_report:
        prior_report_path = os.path.abspath(args.prior_report)
    elif hasattr(args, 'latest') and args.latest:
        prior_report_path = find_latest_report(repo)

    if prior_report_path:
        if os.path.isfile(prior_report_path):
            prior_report_ctx = load_prior_report(prior_report_path)
            console.print(f"\n[magenta]Follow-up mode:[/magenta] loaded prior report")
            console.print(f"  [dim]{prior_report_path}[/dim]")
            console.print(f"  [dim]{len(prior_report_ctx):,} chars of context[/dim]")
        else:
            console.print(f"\n[yellow]Warning:[/yellow] Prior report not found: {prior_report_path}")
    elif hasattr(args, 'latest') and args.latest:
        console.print(f"\n[yellow]Warning:[/yellow] No previous reports found for this repo")

    # ---- Static analysis ----
    analysis_ctx, analysis_rpt = run_tools(repo, resolved_profile_name, phpstan_level, args.no_tools)

    if args.tools_only:
        report = (
            f"# WalterChecks Report: {project_name}\n\n"
            f"**Mode:** Tools Only\n**Profile:** {profile['name']}\n"
            f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n"
            f"## Static Analysis Results\n\n{analysis_rpt}\n")
        path = save_report(report, repo, resolved_profile_name, "tools", args.output)
        console.print(f"\n[green]Report saved:[/green] {path}")
        sys.exit(0)

    # ---- Discover files ----
    file_filter = None
    diff_ctx = ""
    pr_info = None

    if args.mode == "pr":
        console.print("\n[cyan]Analyzing PR...[/cyan]")
        changed = git_changed_files(repo, branch=args.branch,
                                    commit_range=args.range, base=args.base)
        if not changed:
            console.print("[yellow]No changed files found.[/yellow]")
            sys.exit(0)
        console.print(f"  Changed files: [bold]{len(changed)}[/bold]")
        for f in changed[:20]:
            console.print(f"    {f}")
        if len(changed) > 20:
            console.print(f"    ... and {len(changed) - 20} more")
        file_filter = changed
        diff_ctx = git_diff(repo, branch=args.branch,
                            commit_range=args.range, base=args.base)
        commits = git_log(repo, branch=args.branch,
                          commit_range=args.range, base=args.base)
        pr_info = {"branch": args.branch or args.range or "HEAD",
                   "base": args.base, "changed_count": len(changed),
                   "commits": commits}

    console.print("\n[cyan]Scanning files...[/cyan]")
    files = discover_files(repo, profile, file_filter=file_filter,
                           extra_excludes=extra_excludes)
    console.print(f"  Found [bold]{len(files)}[/bold] reviewable files")
    if not files:
        console.print("[yellow]No matching files found.[/yellow]")
        sys.exit(0)

    # ---- Batch ----
    grouper = GROUPERS.get(profile.get("group_strategy", "flat"), group_flat)
    batches = grouper(files)
    console.print(f"  Organized into [bold]{len(batches)}[/bold] review batches")

    # ---- Connect LLM ----
    console.print(f"\n[cyan]Connecting to LLM...[/cyan]")
    client = OpenAI(base_url=args.url, api_key="not-needed")
    model_name = detect_model(client)
    if model_name == "unknown":
        console.print("[red]Cannot connect to vLLM. Is serve.sh running?[/red]")
        sys.exit(1)
    console.print(f"  Model: [green]{model_name}[/green]")

    # ---- Review ----
    console.print(f"\n[cyan]Running LLM review...[/cyan]")
    results = []
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"),
                  console=console) as prog:
        task = prog.add_task("Reviewing...", total=len(batches))
        for i, batch in enumerate(batches):
            fnames = [f["path"] for f in batch]
            prog.update(task, description=f"Batch {i+1}: {fnames[0]}...")
            review = review_batch(
                client, profile["system_prompt"], batch, model_name,
                analysis_ctx if i == 0 else "",  # Tools context on first batch only
                diff_ctx if i == 0 else "",
                prior_report_ctx if i == 0 else "",  # Prior report on first batch
            )
            results.append({"files": fnames, "file_count": len(batch), "review": review})
            prog.advance(task)

    elapsed = time.time() - start

    is_followup = bool(prior_report_ctx)
    report = generate_report(args.mode, results, profile["name"], repo,
                             elapsed, analysis_rpt, pr_info,
                             is_followup=is_followup,
                             prior_report_path=prior_report_path,
                             project_name=project_name)
    mode_suffix = "followup" if is_followup else args.mode
    path = save_report(report, repo, resolved_profile_name, mode_suffix, args.output)

    console.print(f"\n[green]Review complete![/green]")
    console.print(f"  Report: [bold]{path}[/bold]")
    console.print(f"  Time: {elapsed:.0f}s | Files: {sum(r['file_count'] for r in results)}")
    console.print(f"\n  [dim]Feed to Claude Code:[/dim]")
    console.print(f'  [dim]  "Read {path} and fix all CRITICAL and WARNING issues."[/dim]')


if __name__ == "__main__":
    main()
