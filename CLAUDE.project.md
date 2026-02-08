# CLAUDE.md — WalterChecks Reference for Claude Code

This document describes WalterChecks, a self-hosted code review system. When working on projects that will be reviewed by WalterChecks, use this reference to write code that passes review cleanly and to set up projects correctly.

## What WalterChecks Does

WalterChecks runs 11 static analysis tools + an LLM review against a codebase and produces a markdown report. The report is then fed back to Claude Code for fixes. The goal is to write code that minimizes findings on the first pass.

## WalterChecks.json

Every project should have a `WalterChecks.json` in its repo root. Create one when starting a new project or cleaning up an existing one.

```json
{
  "name": "Human-Readable Project Name",
  "profile": "wordpress",
  "root": "plugin/",
  "exclude": [
    "plugin-update-checker/",
    "lib/third-party/"
  ],
  "phpstan_level": 5
}
```

### Fields

- **name**: Display name for reports.
- **profile**: One of `wordpress`, `laravel`, `react`, `security`, `performance`, `general`. Use `wordpress` for themes and plugins (auto-detects which).
- **root**: If the reviewable code lives in a subdirectory (e.g. the actual plugin files are in `plugin/` but the repo root has dev tools, specs, CI configs), set this so WalterChecks only scans the right directory.
- **exclude**: Directories to skip. Always exclude vendored/third-party code that the project doesn't maintain. Common examples:
  - `plugin-update-checker/` — third-party updater library
  - `vendor/` — Composer dependencies (already excluded by default)
  - `node_modules/` — npm dependencies (already excluded by default)
  - `lib/legacy/` — old code not being maintained
  - `assets/vendor/` — third-party JS/CSS libraries
- **phpstan_level**: 0 (loosest) to 9 (strictest). Default is 5 for WordPress, 6 for Laravel. Start at 5 and increase as codebase matures.

### Directories Excluded by Default

These are always skipped regardless of config: `node_modules`, `vendor`, `.git`, `wp-admin`, `wp-includes`, `uploads`, `cache`, `.svn`, `backups`, `build`, `dist`.

Files in `.gitignore` are also automatically excluded.

## Tools and How to Satisfy Them

### 1. PHPStan (Type Analysis)

PHPStan checks for type errors, undefined functions, and dead code. The most common issue with WordPress projects is PHPStan reporting "Function X not found" for WordPress core functions like `sanitize_text_field`, `esc_html`, `add_action`, etc.

**Fix: Add a `phpstan.neon` to the project root (or the `root` directory specified in WalterChecks.json):**

```neon
includes:
    - vendor/szepeviktor/phpstan-wordpress/extension.neon

parameters:
    level: 5
    paths:
        - .
    excludePaths:
        - vendor
        - node_modules
        - plugin-update-checker
    bootstrapFiles:
        - vendor/php-stubs/wordpress-stubs/wordpress-stubs.php
```

If the project doesn't use Composer, create a minimal `composer.json` with WordPress stubs:

```json
{
    "require-dev": {
        "szepeviktor/phpstan-wordpress": "^2.0",
        "php-stubs/wordpress-stubs": "^6.0"
    }
}
```

Then `composer install` so the stubs are available.

**Writing PHPStan-friendly code:**
- Always type-hint function parameters and return types where practical
- Check for null before accessing object properties: `if ($result) { echo $result->name; }`
- Use `@var` annotations when PHPStan can't infer types from WordPress functions
- Use `@phpstan-ignore-next-line` sparingly and only with a comment explaining why

### 2. Psalm (Taint Analysis)

Psalm traces user input through code to detect SQL injection and XSS paths. It will flag any path where `$_GET`, `$_POST`, `$_REQUEST`, or `$_SERVER` data reaches a database query or HTML output without sanitization.

**Writing Psalm-friendly code:**
- Always sanitize input: `sanitize_text_field()`, `absint()`, `sanitize_email()`, etc.
- Always escape output: `esc_html()`, `esc_attr()`, `esc_url()`, `wp_kses_post()`
- Use `$wpdb->prepare()` for ALL database queries with variables — never concatenate
- Validate nonces before processing form data: `wp_verify_nonce()` or `check_ajax_referer()`

### 3. PHPCS (WordPress Coding Standards)

PHPCS checks coding style against WordPress Coding Standards. Covers spacing, naming conventions, hook documentation, and security patterns.

**Key rules:**
- Use tabs for indentation (WordPress standard), not spaces
- Function names: `snake_case` (not camelCase)
- Hook callbacks should be documented with `@hooked` or inline comments
- Use Yoda conditions: `if ( 'value' === $var )` not `if ( $var === 'value' )`
- Spaces inside parentheses: `if ( $condition )` not `if ($condition)`
- Use `wp_die()` instead of `die()` or `exit()`
- Escape all output, even in admin context
- Prefix all function names, classes, and globals with the plugin/theme prefix

**Adding PHPCS config to a project:**
Create `.phpcs.xml.dist` in the project root:
```xml
<?xml version="1.0"?>
<ruleset name="Project Standards">
    <description>Project coding standards</description>
    <rule ref="WordPress"/>
    <rule ref="WordPress-Extra"/>
    <rule ref="WordPress-Docs"/>

    <arg name="extensions" value="php"/>
    <file>.</file>

    <exclude-pattern>vendor/*</exclude-pattern>
    <exclude-pattern>node_modules/*</exclude-pattern>
    <exclude-pattern>plugin-update-checker/*</exclude-pattern>
</ruleset>
```

### 4. PHPMD (Mess Detector)

PHPMD checks for code complexity, poor design, and naming issues.

**Common triggers and fixes:**
- **CyclomaticComplexity**: Functions with too many if/else branches. Break into smaller functions.
- **NPathComplexity**: Too many possible execution paths. Simplify logic.
- **ExcessiveMethodLength**: Functions over ~100 lines. Extract sub-functions.
- **UnusedFormalParameter**: Function parameters that are never used. Remove or prefix with `$_` if required by a hook signature.
- **BooleanArgumentFlag**: Functions that take `true`/`false` to change behavior. Use separate functions or an options array instead.

### 5. PHPCPD (Copy/Paste Detector)

Detects duplicated code blocks. If PHPCPD finds clones, extract the repeated logic into a shared function.

**Common patterns that trigger duplication:**
- Similar shortcode render functions that differ only in a few variables → use a shared renderer with parameters
- Repeated database query patterns → create a query builder or helper function
- Similar admin page handlers → use a base class or shared template

### 6. PHP Parallel Lint

Fast syntax checker. Catches `parse error`, unclosed brackets, invalid PHP. If this fails, nothing else will work. Always run your code through `php -l` mentally.

### 7. Rector (Deprecation Check)

Rector checks for deprecated PHP and WordPress function usage. Runs in dry-run mode (no code changes).

**Common deprecations to avoid:**
- `create_function()` → use closures
- `each()` → use `foreach`
- `mysql_*` functions → use `$wpdb` methods
- `ereg*()` → use `preg_*()` functions

### 8. Composer Audit

Checks `composer.lock` for known vulnerabilities in dependencies. Requires a `composer.lock` file to exist.

**For WordPress plugins that use Composer:**
- Always commit `composer.lock`
- Run `composer audit` periodically
- Keep dependencies updated

### 9. ESLint (JavaScript)

Requires an ESLint config file in the project. Without one, ESLint is skipped.

**Setting up ESLint for a WordPress project:**
Create `.eslintrc.json`:
```json
{
    "env": {
        "browser": true,
        "jquery": true,
        "es2021": true
    },
    "extends": "eslint:recommended",
    "globals": {
        "wp": "readonly",
        "ajaxurl": "readonly"
    },
    "rules": {
        "no-unused-vars": "warn",
        "no-console": "warn",
        "eqeqeq": "error"
    }
}
```

### 10. Stylelint (CSS)

Requires a Stylelint config file. Without one, Stylelint is skipped.

**Setting up Stylelint:**
Create `.stylelintrc.json`:
```json
{
    "extends": "stylelint-config-standard",
    "rules": {
        "selector-class-pattern": null,
        "no-descending-specificity": null
    }
}
```

### 11. npm Audit

Checks `package-lock.json` for known vulnerabilities. Requires a `package-lock.json` to exist.

## LLM Review Layer

After static analysis, an LLM reviews the code for issues tools can't catch:
- Business logic errors
- Architecture problems (logic in templates, God classes)
- Missing security patterns (nonce checks, capability checks, data validation)
- WordPress API misuse (wrong hooks, incorrect filter returns)
- Performance issues (N+1 queries, missing caching, unindexed queries)
- Accessibility concerns in HTML output

The LLM receives the static analysis results as context, so it can confirm or dismiss tool findings and explain why they matter.

## Project Setup Checklist

When creating a new project or cleaning up an existing one, ensure:

- [ ] `WalterChecks.json` exists in repo root with correct profile, root, and excludes
- [ ] `.gitignore` excludes `vendor/`, `node_modules/`, build artifacts, and any other non-reviewable files
- [ ] `phpstan.neon` exists (for PHP projects) with WordPress stubs if applicable
- [ ] `.phpcs.xml.dist` exists with appropriate coding standard rules
- [ ] `.eslintrc.json` exists if the project has JavaScript files
- [ ] `.stylelintrc.json` exists if the project has CSS files that should be linted
- [ ] `composer.json` and `composer.lock` exist if using PHP dependencies
- [ ] `package.json` and `package-lock.json` exist if using npm dependencies
- [ ] Third-party/vendored code directories are listed in WalterChecks.json `exclude`
- [ ] Plugin/theme header is present in main PHP file (for auto-detection to work)

## WordPress Plugin Starter Config

For a typical Off Walter / Boston Web Group WordPress plugin:

**WalterChecks.json:**
```json
{
    "name": "Plugin Name",
    "profile": "wordpress",
    "root": "plugin/",
    "exclude": [
        "plugin-update-checker/"
    ],
    "phpstan_level": 5
}
```

**plugin/composer.json** (minimal, for PHPStan stubs):
```json
{
    "name": "bwg/plugin-name",
    "description": "Plugin description",
    "require-dev": {
        "szepeviktor/phpstan-wordpress": "^2.0",
        "php-stubs/wordpress-stubs": "^6.0"
    }
}
```

**plugin/phpstan.neon:**
```neon
includes:
    - vendor/szepeviktor/phpstan-wordpress/extension.neon

parameters:
    level: 5
    paths:
        - .
    excludePaths:
        - vendor
        - node_modules
        - plugin-update-checker
```

**plugin/.phpcs.xml.dist:**
```xml
<?xml version="1.0"?>
<ruleset name="BWG Plugin Standards">
    <rule ref="WordPress"/>
    <arg name="extensions" value="php"/>
    <file>.</file>
    <exclude-pattern>vendor/*</exclude-pattern>
    <exclude-pattern>plugin-update-checker/*</exclude-pattern>
</ruleset>
```

## Review Workflow

1. WalterChecks produces a markdown report
2. Feed the report to Claude Code: `"Read /path/to/report.md and fix all CRITICAL and WARNING issues"`
3. Claude Code makes fixes on a branch
4. Run WalterChecks again in PR mode: `python review.py pr <repo> --branch <fix-branch> --latest`
5. Repeat until clean

## Severity Levels in Reports

- **CRITICAL**: Security vulnerabilities, data loss risks, crashes. Fix immediately.
- **WARNING**: Bugs, bad practices, performance issues. Fix before shipping.
- **INFO**: Style issues, minor improvements, suggestions. Fix when convenient.
