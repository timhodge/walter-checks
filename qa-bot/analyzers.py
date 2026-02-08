#!/usr/bin/env python3
"""
analyzers.py -- Static analysis tool runners for QA Bot

Runs every available tool and collects structured output.
The LLM receives tool results as context so it can:
  - Confirm or dismiss findings (reduce false positives)
  - Explain WHY something matters
  - Spot patterns across multiple findings
  - Catch logic/architecture issues tools miss

All tools run in parallel. Missing tools are skipped gracefully.
"""

import json
import os
import re
import subprocess
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


@dataclass
class AnalyzerResult:
    """Result from a single static analysis tool."""
    tool: str
    success: bool
    findings_count: int = 0
    output: str = ""
    error: str = ""
    summary: str = ""


@dataclass
class AnalysisSuite:
    """Collection of all results for a repo."""
    repo_path: str
    results: list[AnalyzerResult] = field(default_factory=list)

    def has_findings(self) -> bool:
        return any(r.findings_count > 0 for r in self.results)

    def to_prompt_context(self) -> str:
        """Format all results as context for LLM prompt."""
        if not self.results:
            return ""
        sections = [
            "# Static Analysis Results",
            "",
            "The following tools were run against this codebase before your review.",
            "Use these results to inform your review: confirm real issues, dismiss",
            "false positives, explain patterns, and find issues the tools missed.",
            "",
        ]
        for r in self.results:
            if not r.success and not r.output:
                sections.append(f"## {r.tool}: SKIPPED ({r.error})")
                sections.append("")
                continue
            sections.append(f"## {r.tool}: {r.findings_count} findings")
            if r.summary:
                sections.append(r.summary)
            sections.append("")
            if r.output:
                output = r.output
                if len(output) > 8000:
                    output = output[:8000] + "\n\n... (truncated, first 8000 chars shown)"
                sections.append("```")
                sections.append(output)
                sections.append("```")
            sections.append("")
        return "\n".join(sections)

    def to_report_section(self) -> str:
        """Format as standalone report section (for --tools-only mode)."""
        if not self.results:
            return "No tools were run."
        sections = []
        total = 0
        for r in self.results:
            if not r.success and not r.output:
                sections.append(f"### ⏭ {r.tool}")
                sections.append(f"*Skipped: {r.error}*\n")
                continue
            total += r.findings_count
            icon = "🔴" if r.findings_count > 10 else "🟡" if r.findings_count > 0 else "🟢"
            sections.append(f"### {icon} {r.tool} — {r.findings_count} findings")
            if r.summary:
                sections.append(f"*{r.summary}*")
            sections.append("")
            if r.output and r.output != "No issues found.":
                sections.append("```")
                sections.append(r.output)
                sections.append("```")
            elif r.findings_count == 0:
                sections.append("Clean — no issues found.")
            sections.append("")
        return f"**Total findings across all tools: {total}**\n\n" + "\n".join(sections)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: str, timeout: int = 180) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", f"Not found: {cmd[0]}"


def _find_bin(tool: str, repo: str) -> str | None:
    """Find the best binary for a tool. Prefers project vendor/bin
    (picks up project-specific extensions like Larastan, Laravel IDE Helper, etc.)
    then falls back to global install."""
    vendor_bin = os.path.join(repo, "vendor", "bin", tool)
    if os.path.isfile(vendor_bin):
        return vendor_bin
    global_bin = shutil.which(tool)
    if global_bin:
        return global_bin
    return None


def _php_dirs(repo: str) -> list[str]:
    """Auto-detect directories with PHP files."""
    for d in ["app", "src", "wp-content/themes", "wp-content/plugins",
              "public", "lib", "includes", "inc"]:
        if os.path.isdir(os.path.join(repo, d)):
            return [d for d in ["app", "src", "wp-content/themes", "wp-content/plugins",
                                "public", "lib", "includes", "inc"]
                    if os.path.isdir(os.path.join(repo, d))]
    return ["."]


def _has_ext(repo: str, exts: list[str]) -> bool:
    for root, _, files in os.walk(repo):
        if any(x in root for x in ["node_modules", "vendor", ".git"]):
            continue
        for f in files:
            if any(f.endswith(e) for e in exts):
                return True
    return False


# ===========================================================================
# PHP TOOLS
# ===========================================================================

def run_parallel_lint(repo: str) -> AnalyzerResult:
    """PHP Parallel Lint — fast syntax error checker across entire codebase."""
    name = "PHP Parallel Lint"
    if not shutil.which("parallel-lint"):
        return AnalyzerResult(tool=name, success=False,
                              error="Not installed (composer global require php-parallel-lint/php-parallel-lint)")
    dirs = _php_dirs(repo)
    cmd = ["parallel-lint", "--no-progress", "--json",
           "--exclude", "vendor", "--exclude", "node_modules"] + dirs
    code, stdout, stderr = _run(cmd, repo, timeout=60)
    try:
        data = json.loads(stdout) if stdout else {}
        errors = data.get("results", {}).get("errors", [])
        n = len(errors)
        lines = []
        for e in errors:
            fp = os.path.relpath(e.get("file", "?"), repo)
            lines.append(f"  {fp}:{e.get('line', '?')} — {e.get('message', 'Syntax error')}")
        return AnalyzerResult(tool=name, success=True, findings_count=n,
                              output="\n".join(lines) or "No syntax errors.",
                              summary=f"{n} PHP syntax error(s).")
    except (json.JSONDecodeError, KeyError):
        ec = stderr.count("Parse error") + stderr.count("syntax error")
        return AnalyzerResult(tool=name, success=True, findings_count=ec,
                              output=stderr or stdout or "No syntax errors.")


def run_phpstan(repo: str, level: int = 5) -> AnalyzerResult:
    """PHPStan — static type analysis. Finds type errors, undefined vars, dead code.
    Prefers project's vendor/bin/phpstan (picks up Larastan and project config)."""
    name = f"PHPStan (Level {level})"
    phpstan_bin = _find_bin("phpstan", repo)
    if not phpstan_bin:
        return AnalyzerResult(tool=name, success=False, error="Not installed")
    cfg = None
    for c in ["phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon"]:
        if os.path.exists(os.path.join(repo, c)):
            cfg = c
            break
    cmd = [phpstan_bin, "analyse", "--no-progress", "--error-format=json", f"--level={level}"]
    if cfg:
        cmd.append(f"--configuration={cfg}")
    else:
        cmd.extend(_php_dirs(repo))
    code, stdout, stderr = _run(cmd, repo)

    # PHPStan exits non-zero when it has findings (code 1) — that's normal.
    # But if there's no JSON output, something went wrong (config error, missing extension, etc.)
    if not stdout or not stdout.strip().startswith("{"):
        # No JSON output — PHPStan itself errored
        error_msg = stderr or stdout or "Unknown error"
        # Truncate but show enough to diagnose
        error_msg = error_msg.strip()[:500]
        return AnalyzerResult(tool=name, success=False, findings_count=0,
                              output=error_msg,
                              error=f"PHPStan error (check config): {error_msg[:100]}")

    try:
        data = json.loads(stdout)
        totals = data.get("totals", {})
        n = totals.get("file_errors", 0) + totals.get("errors", 0)

        # Collect all findings
        all_lines = []
        for fp, fd in data.get("files", {}).items():
            rp = os.path.relpath(fp, repo)
            for m in fd.get("messages", []):
                all_lines.append(f"  {rp}:{m.get('line', '?')} — {m.get('message', '')}")
        for e in data.get("errors", []):
            all_lines.append(f"  [General] {e}")

        # Deduplicate for LLM context: group by error message pattern
        # Errors appearing ≤10 times keep their individual file:line locations.
        # Errors appearing >10 times get collapsed to "[Nx] message" to save tokens.
        from collections import Counter, defaultdict
        DEDUP_THRESHOLD = 10

        error_types = Counter()
        error_examples = defaultdict(list)  # keep first few file:line examples
        for line in all_lines:
            parts = line.split(" — ", 1)
            msg = parts[1] if len(parts) > 1 else line.strip()
            loc = parts[0].strip() if len(parts) > 1 else ""
            error_types[msg] += 1
            if len(error_examples[msg]) < 3:  # keep up to 3 example locations
                error_examples[msg].append(loc)

        # Build compact output: keep individuals for low counts, collapse for high
        compact_lines = []
        for msg, count in error_types.most_common():
            if count > DEDUP_THRESHOLD:
                examples = ", ".join(error_examples[msg])
                compact_lines.append(f"  [{count}x] {msg}")
                compact_lines.append(f"         e.g. {examples}")
            else:
                # Keep individual lines with locations
                for line in all_lines:
                    if f" — {msg}" in line or (msg in line and " — " not in line):
                        compact_lines.append(line)

        output = "\n".join(compact_lines) if compact_lines else "No issues found."

        return AnalyzerResult(tool=name, success=True, findings_count=n,
                              output=output or "No issues found.",
                              summary=f"{n} issue(s) at level {level}/9 ({len(error_types)} unique error types).")
    except (json.JSONDecodeError, KeyError):
        return AnalyzerResult(tool=name, success=False, findings_count=0,
                              output=stdout or stderr,
                              error="PHPStan returned invalid JSON")


def run_psalm(repo: str) -> AnalyzerResult:
    """Psalm — PHP static analysis with taint/security tracking.
    Traces user input through code to dangerous outputs (SQLi, XSS paths).
    Prefers project's vendor/bin/psalm (picks up Laravel plugin and project config)."""
    name = "Psalm (Taint Analysis)"
    psalm_bin = _find_bin("psalm", repo)
    if not psalm_bin:
        return AnalyzerResult(tool=name, success=False, error="Not installed")
    has_cfg = any(os.path.exists(os.path.join(repo, f))
                  for f in ["psalm.xml", "psalm.xml.dist"])
    if not has_cfg:
        _run([psalm_bin, "--init", ".", "3"], repo, timeout=30)
    # Try taint analysis first
    cmd = [psalm_bin, "--output-format=json", "--no-progress", "--taint-analysis"]
    code, stdout, stderr = _run(cmd, repo, timeout=300)
    if code != 0 and "taint" in stderr.lower():
        cmd = [psalm_bin, "--output-format=json", "--no-progress"]
        code, stdout, stderr = _run(cmd, repo, timeout=300)
    try:
        data = json.loads(stdout) if stdout else []
        if isinstance(data, list):
            n = len(data)
            lines = []
            for item in data:
                sev = item.get("severity", "error")
                fp = item.get("file_path", "?")
                ln = item.get("line_from", "?")
                msg = item.get("message", "")
                lines.append(f"  [{sev.upper()}] {fp}:{ln} — {msg}")
        else:
            n, lines = 0, []
        return AnalyzerResult(tool=name, success=True, findings_count=n,
                              output="\n".join(lines) or "No issues found.",
                              summary=f"{n} issue(s) including taint/security analysis.")
    except (json.JSONDecodeError, KeyError):
        return AnalyzerResult(tool=name, success=True, findings_count=0,
                              output=stdout or stderr, error="Parse error")


def run_phpcs(repo: str, standard: str = "WordPress") -> AnalyzerResult:
    """PHPCS — coding standards enforcement (WordPress/PSR-12)."""
    name = f"PHPCS ({standard})"
    phpcs_bin = _find_bin("phpcs", repo)
    if not phpcs_bin:
        return AnalyzerResult(tool=name, success=False, error="Not installed")
    cfg = None
    for c in [".phpcs.xml", ".phpcs.xml.dist", "phpcs.xml", "phpcs.xml.dist"]:
        if os.path.exists(os.path.join(repo, c)):
            cfg = c
            break
    cmd = [phpcs_bin, "--report=json", "-q"]
    if cfg:
        cmd.append(f"--standard={cfg}")
    else:
        cmd.append(f"--standard={standard}")
        cmd.extend(_php_dirs(repo))
    cmd.extend(["--extensions=php", "--ignore=vendor/*,node_modules/*,*.min.js,*.min.css"])
    code, stdout, stderr = _run(cmd, repo)
    try:
        data = json.loads(stdout) if stdout else {}
        t = data.get("totals", {})
        errs, warns = t.get("errors", 0), t.get("warnings", 0)
        n = errs + warns
        lines = []
        for fp, fd in data.get("files", {}).items():
            rp = os.path.relpath(fp, repo)
            for m in fd.get("messages", []):
                lvl = "ERROR" if m.get("type") == "ERROR" else "WARN"
                lines.append(f"  [{lvl}] {rp}:{m.get('line', '?')} — {m.get('message', '')}")
                src = m.get("source", "")
                if src:
                    lines.append(f"          Rule: {src}")
        return AnalyzerResult(tool=name, success=True, findings_count=n,
                              output="\n".join(lines) or "No issues found.",
                              summary=f"{errs} error(s), {warns} warning(s).")
    except (json.JSONDecodeError, KeyError):
        return AnalyzerResult(tool=name, success=True, findings_count=0,
                              output=stdout or stderr, error="Parse error")


def run_phpmd(repo: str) -> AnalyzerResult:
    """PHPMD — code complexity, maintainability, tech debt assessment.
    Finds overly complex functions, long methods, too many params, unused code."""
    name = "PHPMD (Mess Detector)"
    if not shutil.which("phpmd"):
        return AnalyzerResult(tool=name, success=False, error="Not installed")
    dirs = ",".join(_php_dirs(repo))
    cmd = ["phpmd", dirs, "json",
           "cleancode,codesize,controversial,design,naming,unusedcode",
           "--exclude", "vendor,node_modules"]
    code, stdout, stderr = _run(cmd, repo)
    try:
        data = json.loads(stdout) if stdout else {}
        violations = data.get("files", [])
        n = sum(len(f.get("violations", [])) for f in violations)
        lines = []
        for fe in violations:
            rp = os.path.relpath(fe.get("file", "?"), repo)
            for v in fe.get("violations", []):
                rule = v.get("rule", "?")
                bl = v.get("beginLine", "?")
                desc = v.get("description", "").strip()
                pri = v.get("priority", 3)
                lvl = "HIGH" if pri <= 2 else "MED" if pri == 3 else "LOW"
                lines.append(f"  [{lvl}] {rp}:{bl} — [{rule}] {desc}")
        return AnalyzerResult(tool=name, success=True, findings_count=n,
                              output="\n".join(lines) or "No issues found.",
                              summary=f"{n} complexity/design/naming issue(s).")
    except (json.JSONDecodeError, KeyError):
        return AnalyzerResult(tool=name, success=True, findings_count=0,
                              output=stdout or stderr, error="Parse error")


def run_phpcpd(repo: str) -> AnalyzerResult:
    """PHPCPD — copy/paste detector. Finds duplicated code blocks."""
    name = "PHPCPD (Copy/Paste Detector)"
    if not shutil.which("phpcpd"):
        return AnalyzerResult(tool=name, success=False, error="Not installed")
    dirs = _php_dirs(repo)
    cmd = ["phpcpd", "--min-lines=5", "--min-tokens=70",
           "--exclude=vendor", "--exclude=node_modules"] + dirs
    code, stdout, stderr = _run(cmd, repo)
    output = (stdout + "\n" + stderr).strip()
    match = re.search(r"Found (\d+) clones", output)
    n = int(match.group(1)) if match else 0
    return AnalyzerResult(tool=name, success=True, findings_count=n,
                          output=output or "No duplicated code found.",
                          summary=f"{n} duplicated code block(s).")


def run_rector_dry(repo: str) -> AnalyzerResult:
    """Rector (dry run) — finds deprecated PHP/WP patterns, shows what would change."""
    name = "Rector (Deprecation Check)"
    rector_bin = _find_bin("rector", repo)
    if not rector_bin:
        return AnalyzerResult(tool=name, success=False, error="Not installed")
    has_cfg = any(os.path.exists(os.path.join(repo, f))
                  for f in ["rector.php", "rector.php.dist"])
    cmd = [rector_bin, "process", "--dry-run", "--no-progress-bar"]
    if not has_cfg:
        cmd.append("--no-diffs")
    code, stdout, stderr = _run(cmd, repo, timeout=300)
    output = stdout or stderr
    match = re.search(r"(\d+) file", output)
    n = int(match.group(1)) if match else 0
    return AnalyzerResult(tool=name, success=True, findings_count=n,
                          output=(output[:8000] if output else "No deprecated patterns."),
                          summary=f"{n} file(s) with deprecated/improvable patterns.")


def run_composer_audit(repo: str) -> AnalyzerResult:
    """Composer Audit — checks dependencies for known CVEs."""
    name = "Composer Security Audit"
    if not os.path.exists(os.path.join(repo, "composer.lock")):
        return AnalyzerResult(tool=name, success=False, error="No composer.lock")
    if not shutil.which("composer"):
        return AnalyzerResult(tool=name, success=False, error="composer not found")
    cmd = ["composer", "audit", "--format=json", "--no-interaction"]
    code, stdout, stderr = _run(cmd, repo, timeout=60)
    try:
        data = json.loads(stdout) if stdout else {}
        advs = data.get("advisories", {})
        n = sum(len(v) for v in advs.values())
        lines = []
        for pkg, vulns in advs.items():
            for v in vulns:
                sev = v.get("severity", "unknown")
                lines.append(f"  [{sev.upper()}] {pkg}: {v.get('title', '?')} (CVE: {v.get('cve', 'N/A')})")
        return AnalyzerResult(tool=name, success=True, findings_count=n,
                              output="\n".join(lines) or "No known vulnerabilities.",
                              summary=f"{n} known vulnerability/ies.")
    except (json.JSONDecodeError, KeyError):
        return AnalyzerResult(tool=name, success=True, findings_count=0,
                              output=stdout or stderr)


# ===========================================================================
# JAVASCRIPT / CSS TOOLS
# ===========================================================================

def run_eslint(repo: str) -> AnalyzerResult:
    """ESLint — JavaScript/TypeScript linting."""
    name = "ESLint"
    if not shutil.which("npx"):
        return AnalyzerResult(tool=name, success=False, error="npx not found")
    has_cfg = any(os.path.exists(os.path.join(repo, f))
                  for f in [".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
                            "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs"])
    if not has_cfg:
        return AnalyzerResult(tool=name, success=False, error="No ESLint config in repo")
    cmd = ["npx", "eslint", ".", "--format=json",
           "--ignore-pattern=node_modules", "--ignore-pattern=vendor",
           "--ignore-pattern=build", "--ignore-pattern=dist",
           "--ignore-pattern=*.min.js"]
    code, stdout, stderr = _run(cmd, repo, timeout=120)
    try:
        data = json.loads(stdout) if stdout else []
        te = sum(f.get("errorCount", 0) for f in data)
        tw = sum(f.get("warningCount", 0) for f in data)
        n = te + tw
        lines = []
        for fr in data:
            rp = os.path.relpath(fr.get("filePath", ""), repo)
            for m in fr.get("messages", []):
                lvl = "ERROR" if m.get("severity", 0) == 2 else "WARN"
                lines.append(f"  [{lvl}] {rp}:{m.get('line', '?')} — {m.get('message', '')} ({m.get('ruleId', '')})")
        return AnalyzerResult(tool=name, success=True, findings_count=n,
                              output="\n".join(lines) or "No issues found.",
                              summary=f"{te} error(s), {tw} warning(s).")
    except (json.JSONDecodeError, TypeError):
        return AnalyzerResult(tool=name, success=True, findings_count=0,
                              output=stderr or stdout or "Parse error")


def run_stylelint(repo: str) -> AnalyzerResult:
    """Stylelint — CSS/SCSS linting."""
    name = "Stylelint"
    if not shutil.which("npx"):
        return AnalyzerResult(tool=name, success=False, error="npx not found")
    has_cfg = any(os.path.exists(os.path.join(repo, f))
                  for f in [".stylelintrc", ".stylelintrc.json", ".stylelintrc.js",
                            ".stylelintrc.yml", "stylelint.config.js", "stylelint.config.mjs"])
    if not has_cfg:
        return AnalyzerResult(tool=name, success=False, error="No Stylelint config in repo")
    cmd = ["npx", "stylelint", "**/*.{css,scss}", "--formatter=json",
           "--ignore-pattern=node_modules", "--ignore-pattern=vendor",
           "--ignore-pattern=*.min.css"]
    code, stdout, stderr = _run(cmd, repo, timeout=120)
    try:
        data = json.loads(stdout) if stdout else []
        n = sum(len(f.get("warnings", [])) for f in data)
        lines = []
        for fr in data:
            rp = os.path.relpath(fr.get("source", ""), repo) if fr.get("source") else "?"
            for w in fr.get("warnings", []):
                sev = w.get("severity", "warning")
                lines.append(f"  [{sev.upper()}] {rp}:{w.get('line', '?')} — {w.get('text', '')} ({w.get('rule', '')})")
        return AnalyzerResult(tool=name, success=True, findings_count=n,
                              output="\n".join(lines) or "No issues found.",
                              summary=f"{n} CSS issue(s).")
    except (json.JSONDecodeError, TypeError):
        return AnalyzerResult(tool=name, success=True, findings_count=0,
                              output=stderr or stdout or "Parse error")


def run_npm_audit(repo: str) -> AnalyzerResult:
    """npm Audit — checks npm dependencies for known vulnerabilities."""
    name = "npm Security Audit"
    if not os.path.exists(os.path.join(repo, "package-lock.json")):
        return AnalyzerResult(tool=name, success=False, error="No package-lock.json")
    cmd = ["npm", "audit", "--json"]
    code, stdout, stderr = _run(cmd, repo, timeout=60)
    try:
        data = json.loads(stdout) if stdout else {}
        vulns = data.get("vulnerabilities", {})
        n = len(vulns)
        lines = []
        for pkg, info in vulns.items():
            sev = info.get("severity", "unknown")
            via = info.get("via", [])
            title = via[0].get("title", "?") if via and isinstance(via[0], dict) else str(via)
            lines.append(f"  [{sev.upper()}] {pkg}: {title}")
        return AnalyzerResult(tool=name, success=True, findings_count=n,
                              output="\n".join(lines) or "No known vulnerabilities.",
                              summary=f"{n} vulnerable package(s).")
    except (json.JSONDecodeError, KeyError):
        return AnalyzerResult(tool=name, success=True, findings_count=0,
                              output=stdout or stderr)


# ===========================================================================
# SUITE RUNNERS — what tools run for each profile
# ===========================================================================

def _run_parallel(tasks: list) -> list[AnalyzerResult]:
    import time, threading

    results = []
    total = len(tasks)
    start = time.time()
    completed_labels = set()
    done_event = threading.Event()

    # Background thread prints a heartbeat so you know it's alive
    def heartbeat():
        while not done_event.wait(timeout=30):
            pending = [label for label, _, _ in tasks if label not in completed_labels]
            if pending:
                elapsed = time.time() - start
                print(f"  ... {elapsed:.0f}s elapsed, waiting on: {', '.join(pending)}")

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fn, *args): (label, time.time()) for label, fn, args in tasks}
        for i, f in enumerate(as_completed(futs), 1):
            label, t0 = futs[f]
            secs = time.time() - t0
            try:
                r = f.result()
                if r.success and r.findings_count > 0:
                    status = f"⚠ {r.findings_count} findings"
                elif r.success:
                    status = "✓ clean"
                else:
                    status = f"— skipped: {r.error}"
                print(f"  [{i}/{total}] {label}: {status} ({secs:.1f}s)")
                completed_labels.add(label)
                results.append(r)
            except Exception as e:
                print(f"  [{i}/{total}] {label}: ✗ error: {e} ({secs:.1f}s)")
                completed_labels.add(label)
                results.append(AnalyzerResult(tool=label, success=False, error=str(e)))

    done_event.set()
    results.sort(key=lambda r: r.tool)
    return results


def run_wordpress_suite(repo: str, phpstan_level: int = 5) -> AnalysisSuite:
    suite = AnalysisSuite(repo_path=repo)
    tasks = [
        ("Parallel Lint", run_parallel_lint, (repo,)),
        ("PHPStan", run_phpstan, (repo, phpstan_level)),
        ("Psalm", run_psalm, (repo,)),
        ("PHPCS", run_phpcs, (repo, "WordPress")),
        ("PHPMD", run_phpmd, (repo,)),
        ("PHPCPD", run_phpcpd, (repo,)),
        ("Rector", run_rector_dry, (repo,)),
        ("Composer Audit", run_composer_audit, (repo,)),
        ("ESLint", run_eslint, (repo,)),
        ("Stylelint", run_stylelint, (repo,)),
        ("npm Audit", run_npm_audit, (repo,)),
    ]
    suite.results = _run_parallel(tasks)
    return suite


def run_laravel_suite(repo: str, phpstan_level: int = 6) -> AnalysisSuite:
    suite = AnalysisSuite(repo_path=repo)
    tasks = [
        ("Parallel Lint", run_parallel_lint, (repo,)),
        ("PHPStan", run_phpstan, (repo, phpstan_level)),
        ("Psalm", run_psalm, (repo,)),
        ("PHPCS", run_phpcs, (repo, "PSR12")),
        ("PHPMD", run_phpmd, (repo,)),
        ("PHPCPD", run_phpcpd, (repo,)),
        ("Rector", run_rector_dry, (repo,)),
        ("Composer Audit", run_composer_audit, (repo,)),
        ("ESLint", run_eslint, (repo,)),
        ("Stylelint", run_stylelint, (repo,)),
        ("npm Audit", run_npm_audit, (repo,)),
    ]
    suite.results = _run_parallel(tasks)
    return suite


def run_react_suite(repo: str) -> AnalysisSuite:
    suite = AnalysisSuite(repo_path=repo)
    tasks = [
        ("ESLint", run_eslint, (repo,)),
        ("Stylelint", run_stylelint, (repo,)),
        ("npm Audit", run_npm_audit, (repo,)),
    ]
    suite.results = _run_parallel(tasks)
    return suite


def run_full_suite(repo: str, phpstan_level: int = 5) -> AnalysisSuite:
    """Auto-detect stack and run everything available."""
    suite = AnalysisSuite(repo_path=repo)
    tasks = []
    has_php = _has_ext(repo, [".php"])
    has_js = os.path.exists(os.path.join(repo, "package.json"))
    has_css = _has_ext(repo, [".css", ".scss"])
    if has_php:
        is_wp = os.path.exists(os.path.join(repo, "wp-content")) or \
                os.path.exists(os.path.join(repo, "wp-config.php"))
        std = "WordPress-Security" if is_wp else "PSR12"
        tasks.extend([
            ("Parallel Lint", run_parallel_lint, (repo,)),
            ("PHPStan", run_phpstan, (repo, phpstan_level)),
            ("Psalm", run_psalm, (repo,)),
            ("PHPCS", run_phpcs, (repo, std)),
            ("PHPMD", run_phpmd, (repo,)),
            ("PHPCPD", run_phpcpd, (repo,)),
            ("Rector", run_rector_dry, (repo,)),
            ("Composer Audit", run_composer_audit, (repo,)),
        ])
    if has_js:
        tasks.extend([
            ("ESLint", run_eslint, (repo,)),
            ("npm Audit", run_npm_audit, (repo,)),
        ])
    if has_css:
        tasks.append(("Stylelint", run_stylelint, (repo,)))
    suite.results = _run_parallel(tasks)
    return suite


SUITE_RUNNERS = {
    "wordpress": run_wordpress_suite,
    "wp-theme": run_wordpress_suite,
    "wp-plugin": run_wordpress_suite,
    "laravel": run_laravel_suite,
    "react": run_react_suite,
    "security": run_full_suite,
    "performance": run_full_suite,
    "general": run_full_suite,
}
