#!/usr/bin/env python3
"""Run the repository's non-mutating frontend delivery checks.

The checker deliberately treats the frontend repository as an input.  It does
not install packages, rewrite lockfiles, or infer browser/integration status
from a successful static build.  Browser evidence is accepted only when it
names the same target and source revision that this invocation inspected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
CHECK_TIMEOUT_SECONDS = 300
LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb")
LOCK_TO_MANAGER = {
    "pnpm-lock.yaml": "pnpm",
    "package-lock.json": "npm",
    "yarn.lock": "yarn",
    "bun.lock": "bun",
    "bun.lockb": "bun",
}
REQUIRED_CHECKS = ("lint", "typecheck", "unit", "build")
SENSITIVE_CHANGE_PREFIXES = (
    "frontend-factor-mining/src/",
    "frontend-factor-mining/public/",
)
SENSITIVE_CHANGE_FILES = {
    "frontend-factor-mining/package.json",
    "frontend-factor-mining/vite.config.js",
    "backend_api_python/app/routes/factor_mining.py",
}
SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+|token\s*[=:]\s*|password\s*[=:]\s*|secret\s*[=:]\s*)[^\s,;]+"
)
CREDENTIAL_URL_RE = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"']+")
UNIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9:/])/(?:[^\s\"']+/)+[^\s\"']*")
TOKEN_RE = re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_\-]{12,}|github_pat_[A-Za-z0-9_\-]{12,}|sk-[A-Za-z0-9_\-]{12,})\b")
RECORD_RE = re.compile(r"<!--\s*frontend-delivery\s*:\s*(\{.*?\})\s*-->")


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or run non-mutating frontend delivery checks. "
            "Static checks never imply browser, integration, or deployment completion."
        )
    )
    parser.add_argument("--target", choices=("main", "standalone"), default="main")
    parser.add_argument(
        "--source",
        help="Frontend source directory. Defaults to FRONTEND_SRC_PATH or ./QuantDinger-Vue for main; ./frontend-factor-mining for standalone.",
    )
    parser.add_argument("--report", required=True, help="JSON report path to create or replace")
    parser.add_argument("--run", action="store_true", help="Execute available checks with bounded timeouts")
    parser.add_argument("--browser-report", help="Browser verifier JSON report to validate")
    parser.add_argument(
        "--check-changes",
        metavar="BASE_REF",
        help="Check changed frontend/API files have a doc/web_doc delivery record against BASE_REF",
    )
    return parser


def resolve_source(args: argparse.Namespace) -> Path:
    if args.source:
        return Path(args.source)
    if args.target == "main":
        return Path(os.environ.get("FRONTEND_SRC_PATH", "QuantDinger-Vue"))
    return Path("frontend-factor-mining")


def display_path(path: Path, base: Path | None = None) -> str:
    """Return a short, non-sensitive path for human-facing report fields."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if base is not None:
        try:
            return resolved.relative_to(base.resolve()).as_posix()
        except (ValueError, OSError):
            pass
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return f"<external>/{resolved.name}"


def sanitize_text(value: Any, limit: int = 2000) -> str:
    text = str(value or "")
    text = CREDENTIAL_URL_RE.sub(r"\1<redacted>@", text)
    text = TOKEN_RE.sub("<redacted-token>", text)
    text = SECRET_RE.sub("<redacted-secret>", text)
    text = WINDOWS_PATH_RE.sub("<path>", text)
    text = UNIX_PATH_RE.sub("<path>", text)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def run_process(argv: list[str], cwd: Path, timeout: float = CHECK_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run argv without a shell and return a sanitized, bounded result."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        return {
            "status": "blocked",
            "reason": "command-not-found",
            "command": sanitize_text(" ".join(argv)),
            "durationSeconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(
            part.decode("utf-8", "replace") if isinstance(part, bytes) else part
            for part in (exc.stdout, exc.stderr) if part
        )
        return {
            "status": "failed",
            "reason": "timeout",
            "command": sanitize_text(" ".join(argv)),
            "durationSeconds": round(time.monotonic() - started, 3),
            "output": sanitize_text(output),
        }
    except OSError as exc:
        return {
            "status": "blocked",
            "reason": "process-error",
            "command": sanitize_text(" ".join(argv)),
            "error": sanitize_text(exc),
            "durationSeconds": round(time.monotonic() - started, 3),
        }
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    result: dict[str, Any] = {
        "status": "passed" if completed.returncode == 0 else "failed",
        "exitCode": completed.returncode,
        "command": sanitize_text(" ".join(argv)),
        "durationSeconds": round(time.monotonic() - started, 3),
    }
    if output.strip():
        result["output"] = sanitize_text(output)
    return result


# Kept as a small public seam so tests and callers can exercise command behavior.
run_check = run_process


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def git_output(source: Path, *args: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return completed.returncode == 0, completed.stdout.strip()


def source_revision(source: Path) -> str | None:
    ok, value = git_output(source, "rev-parse", "HEAD")
    if ok and re.fullmatch(r"[0-9a-fA-F]{4,40}", value):
        return value
    return None


def tracked_files(source: Path, names: Iterable[str]) -> tuple[list[str], list[str]]:
    tracked: list[str] = []
    untracked_existing: list[str] = []
    ok, output = git_output(source, "ls-files", "--", *names)
    if ok:
        tracked = [line.strip() for line in output.splitlines() if line.strip()]
    for name in names:
        if (source / name).is_file() and name not in tracked:
            untracked_existing.append(name)
    return tracked, untracked_existing


def parse_package_manager(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    match = re.fullmatch(r"(npm|pnpm|yarn|bun)@([0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*)", text)
    return (match.group(1), match.group(2)) if match else (None, None)


def package_metadata(target: str, source: Path, package: dict[str, Any]) -> dict[str, Any]:
    declared_name, declared_version = parse_package_manager(package.get("packageManager"))
    tracked, ignored = tracked_files(source, LOCKFILES)
    tracked_locks = [name for name in LOCKFILES if name in tracked]
    existing_untracked = [name for name in LOCKFILES if name in ignored]
    genuinely_ignored = [name for name in existing_untracked if git_output(source, "check-ignore", "--", name)[0]]
    selectable_untracked = [name for name in existing_untracked if name not in genuinely_ignored]
    problems: list[str] = []

    if len(tracked_locks) > 1:
        problems.append("multiple tracked lockfiles require an explicit single package manager")
    selected_lock = tracked_locks[0] if len(tracked_locks) == 1 else None
    if not tracked_locks and len(selectable_untracked) == 1 and declared_name:
        selected_lock = selectable_untracked[0]
    elif not tracked_locks and len(selectable_untracked) > 1:
        problems.append("multiple untracked lockfiles require resolution")
    manager = declared_name
    version = declared_version
    if manager is None and selected_lock:
        manager = LOCK_TO_MANAGER[selected_lock]
        problems.append("packageManager is missing; inferred manager is inspection-only")
    if manager and selected_lock and LOCK_TO_MANAGER[selected_lock] != manager:
        problems.append("packageManager does not match the tracked lockfile")
    if not declared_name or not declared_version:
        problems.append("packageManager must declare the package manager and version")
    if manager is None:
        problems.append("package manager is unknown")
    if len(tracked_locks) == 0 and target == "main":
        problems.append("no tracked lockfile")
    if not selected_lock:
        problems.append("no unambiguous lockfile is available")
    return {
        "name": manager,
        "version": version,
        "declared": f"{declared_name}@{declared_version}" if declared_name and declared_version else None,
        "lockfile": selected_lock,
        "lockfileTracked": selected_lock in tracked_locks if selected_lock else False,
        "trackedLockfiles": tracked_locks,
        "ignoredLockfiles": genuinely_ignored,
        "problems": problems,
    }


def check_record(name: str, status: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": status}
    result.update(fields)
    return result


def package_manager_command(manager: str | None, windows: bool | None = None) -> list[str] | None:
    if manager not in {"pnpm", "npm", "yarn", "bun"}:
        return None
    if windows is None:
        windows = os.name == "nt"
    if manager == "npm" and windows:
        node = shutil.which("node")
        if node:
            cli = Path(node).parent / "node_modules/npm/bin/npm-cli.js"
            if cli.is_file():
                return [node, str(cli)]
        return None
    binary = shutil.which(manager)
    return [binary] if binary else None


def script_is_mutating(command: Any) -> bool:
    if not isinstance(command, str):
        return True
    lowered = command.lower()
    return "--fix" in lowered or "--write" in lowered or " prettier -w" in f" {lowered}"


def empty_external_report(target: str, source: Path, reason: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "target": target,
        "sourceRevision": None,
        "source": display_path(source),
        "frontend": {
            "status": "blocked",
            "checks": [check_record("source", "blocked", reason=reason)],
            "untested": ["lint", "typecheck", "unit", "build"],
        },
        "browser": {"status": "unverified", "mode": None},
        "integration": {"status": "unverified"},
        "deployment": {"status": "unverified"},
        "untested": ["browser", "integration", "deployment"],
    }


def collect_report(
    target: str,
    source: Path,
    run: bool = False,
    browser_report: Path | None = None,
) -> dict[str, Any]:
    source = Path(source)
    if not source.is_dir():
        report = empty_external_report(target, source, "source directory is missing")
        if browser_report:
            report = attach_browser(report, browser_report, source)
        return report
    package_path = source / "package.json"
    package = read_json(package_path)
    if package is None:
        report = empty_external_report(target, source, "package.json is missing or invalid")
        report["sourceRevision"] = source_revision(source)
        if browser_report:
            report = attach_browser(report, browser_report, source)
        return report

    expected_name = "quantdinger-vue" if target == "main" else "quantdinger-factor-mining-ui"
    if package.get("name") != expected_name:
        report = empty_external_report(target, source, "package identity does not match the requested frontend target")
        report["sourceRevision"] = source_revision(source)
        if browser_report:
            report = attach_browser(report, browser_report, source)
        return report

    metadata = package_metadata(target, source, package)
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    revision = source_revision(source)
    checks: list[dict[str, Any]] = []
    package_problems = list(metadata["problems"])
    if package_problems:
        checks.append(check_record("package-manager", "blocked", reason="; ".join(package_problems)))
    else:
        checks.append(check_record("package-manager", "passed", lockfile=metadata["lockfile"]))

    required_script_names = {
        "lint": "lint:nofix" if "lint:nofix" in scripts else "lint",
        "typecheck": "typecheck",
        "unit": "test:unit",
        "build": "build",
    }
    manager_binary = package_manager_command(metadata["name"])
    manager_available = bool(manager_binary)
    dependencies_available = (source / "node_modules").is_dir()
    installed_version_matches = True
    if run and manager_available:
        version_result = run_check(manager_binary + ["--version"], source, 15)
        actual_version = str(version_result.get("output", "")).strip()
        expected_version = str(metadata["version"] or "").split("+", 1)[0]
        installed_version_matches = version_result.get("status") == "passed" and actual_version == expected_version
        checks.append(check_record("package-manager-version", "passed" if installed_version_matches else "blocked",
                                   expected=expected_version, actual=sanitize_text(actual_version, 100)))
    for name in REQUIRED_CHECKS:
        script_name = required_script_names[name]
        if script_name not in scripts:
            checks.append(check_record(name, "blocked", reason=f"script {script_name} is missing"))
            continue
        script_command = scripts.get(script_name)
        lifecycle = (script_command, scripts.get("pre" + script_name, ""), scripts.get("post" + script_name, ""))
        if name == "lint" and any(script_is_mutating(command) for command in lifecycle):
            checks.append(check_record(name, "blocked", reason="lint script is mutating; use a non-fixing lint script"))
            continue
        if not manager_available:
            checks.append(check_record(name, "blocked", reason=f"package manager {metadata['name'] or 'unknown'} is unavailable"))
            continue
        if run and not installed_version_matches:
            checks.append(check_record(name, "blocked", reason="installed package manager version does not match packageManager"))
            continue
        if package_problems:
            checks.append(check_record(name, "blocked", reason="package manager metadata requires resolution"))
            continue
        if not dependencies_available:
            checks.append(check_record(name, "blocked", reason="node_modules is missing; checker never installs dependencies"))
            continue
        if not run:
            checks.append(check_record(name, "unverified", script=script_name))
            continue
        argv = (manager_binary or []) + ["run", script_name]
        result = dict(run_check(argv, source, CHECK_TIMEOUT_SECONDS))
        checks.append(check_record(name, result.pop("status", "failed"), script=script_name, **result))

    failed = any(check["status"] == "failed" for check in checks)
    blocked = any(check["status"] == "blocked" for check in checks)
    unverified = any(check["status"] == "unverified" for check in checks)
    if failed:
        frontend_status = "failed"
    elif blocked:
        frontend_status = "blocked"
    elif unverified or not run:
        frontend_status = "unverified"
    else:
        frontend_status = "passed"
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "target": target,
        "sourceRevision": revision,
        "source": display_path(source),
        "frontend": {
            "status": frontend_status,
            "packageManager": {
                key: value for key, value in metadata.items() if key != "problems"
            },
            "checks": checks,
            "untested": [check["name"] for check in checks if check["status"] in {"blocked", "unverified"}],
        },
        "browser": {"status": "unverified", "mode": None},
        "integration": {"status": "unverified"},
        "deployment": {"status": "unverified"},
        "untested": ["browser", "integration", "deployment"],
    }
    if browser_report:
        report = attach_browser(report, browser_report, source)
    return report


def expected_route(target: str) -> str:
    return "/#/factor-mining" if target == "main" else "/"


def safe_artifact_path(value: Any, report_dir: Path) -> str | None:
    path = validated_artifact_path(value, report_dir)
    return path.relative_to(report_dir.resolve()).as_posix() if path else None


def validated_artifact_path(value: Any, report_dir: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", value):
        return None
    try:
        resolved = (report_dir / path).resolve()
        resolved.relative_to(report_dir.resolve())
    except (ValueError, OSError):
        return None
    return resolved


def artifact_exists(value: Any, report_dir: Path) -> bool:
    path = validated_artifact_path(value, report_dir)
    return bool(path and path.is_file())


def screenshot_viewport(item: Any) -> tuple[int | None, int | None, Any]:
    if isinstance(item, str):
        match = re.search(r"(\d{3,5})x(\d{3,5})", item)
        return (int(match.group(1)), int(match.group(2)), item) if match else (None, None, item)
    if isinstance(item, dict):
        viewport = item.get("viewport") if isinstance(item.get("viewport"), dict) else item
        return (viewport.get("width"), viewport.get("height"), item)
    return None, None, item


def evidence_quality(evidence: dict[str, Any], target: str, revision: str | None, report_dir: Path) -> list[str]:
    problems: list[str] = []
    if evidence.get("schemaVersion") != 1:
        problems.append("browser evidence schemaVersion must be 1")
    if evidence.get("target") != target:
        problems.append("browser evidence target does not match requested target")
    if revision and evidence.get("sourceRevision") != revision:
        problems.append("browser evidence sourceRevision does not match inspected source")
    if not revision:
        problems.append("source revision is unavailable; browser evidence cannot be matched")
    if evidence.get("route") != expected_route(target):
        problems.append("browser evidence route does not match target route")
    mode = evidence.get("mode")
    if mode not in {"mock", "integration"}:
        problems.append("browser evidence mode must be mock or integration")
    if evidence.get("status") != "passed":
        problems.append("browser evidence status is not passed")
    provenance = evidence.get("source")
    if not isinstance(provenance, dict) or provenance.get("status") != "passed" or provenance.get("revisionVerified") is not True or provenance.get("bundleVerified") is not True:
        problems.append("browser source and served bundle provenance is not verified")
    checks = evidence.get("checks")
    if not isinstance(checks, list) or not checks or any(not isinstance(item, dict) or item.get("status") != "passed" for item in checks):
        problems.append("browser checks are missing, failed, or blocked")
    elif mode == "mock":
        ids = {item.get("id") for item in checks if isinstance(item.get("id"), str)}
        required_ids = {f"{name}-{width}" for name in ("mock-submit", "submitted-job-visible", "job-polling", "mock-refresh", "mock-refresh-detail") for width in (1440, 390)}
        if not required_ids.issubset(ids):
            problems.append("mock browser checks must prove submit, polling, and refresh for both viewports")
    screenshots = evidence.get("screenshots")
    sizes: set[tuple[int, int]] = set()
    if not isinstance(screenshots, list):
        problems.append("browser evidence screenshots are missing")
    else:
        for item in screenshots:
            width, height, raw = screenshot_viewport(item)
            if type(width) is int and type(height) is int:
                sizes.add((width, height))
            if isinstance(raw, dict) and not artifact_exists(raw.get("path"), report_dir):
                problems.append("browser screenshot artifact is missing")
            elif isinstance(raw, str) and not artifact_exists(raw, report_dir):
                problems.append("browser screenshot artifact is missing")
    if (1440, 900) not in sizes or (390, 844) not in sizes:
        problems.append("browser evidence must include 1440x900 and 390x844 screenshots")
    for name, count_key in (("console", "errorCount"), ("network", "unexpectedErrorCount"), ("axe", "criticalCount")):
        section = evidence.get(name)
        if not isinstance(section, dict) or section.get("status") != "passed" or section.get(count_key) != 0:
            problems.append(f"browser evidence {name} has errors or is missing")
        elif not artifact_exists(section.get("path"), report_dir):
            problems.append(f"browser evidence {name} artifact is missing")
    states = evidence.get("states")
    expected_states = {"loading", "empty", "success", "error", "unauthorized"}
    if not isinstance(states, dict) or not expected_states.issubset(states):
        problems.append("browser evidence does not cover all five UI states")
    else:
        for state in expected_states:
            value = states.get(state)
            if not isinstance(value, dict) or value.get("status") not in {"passed", "verified"}:
                problems.append(f"browser state {state} is not verified")
    return problems


def attach_browser(report: dict[str, Any], browser_path: Path, source: Path) -> dict[str, Any]:
    evidence = read_json(browser_path)
    if evidence is None:
        report["browser"] = {"status": "blocked", "mode": None, "reason": "browser report is invalid JSON"}
        return report
    report_dir = browser_path.parent
    problems = evidence_quality(evidence, report["target"], report.get("sourceRevision"), report_dir)
    mode = evidence.get("mode")
    browser_summary: dict[str, Any] = {
        "status": "passed" if not problems else "blocked",
        "mode": mode if mode in {"mock", "integration"} else None,
        "route": expected_route(report["target"]),
        "screenshots": [
            {"viewport": {key: item.get("viewport", {}).get(key) for key in ("width", "height")} if isinstance(item.get("viewport"), dict) else None,
             "path": safe_artifact_path(item.get("path"), report_dir)}
            if isinstance(item, dict) else safe_artifact_path(item, report_dir)
            for item in (evidence.get("screenshots") if isinstance(evidence.get("screenshots"), list) else [])
        ],
        "limitations": [sanitize_text(item) for item in (evidence.get("limitations") if isinstance(evidence.get("limitations"), list) else []) if isinstance(item, str)],
    }
    if problems:
        browser_summary["reason"] = "; ".join(problems)
    for name, count_key in (("console", "errorCount"), ("network", "unexpectedErrorCount"), ("axe", "criticalCount")):
        if isinstance(evidence.get(name), dict):
            raw_section = evidence[name]
            section = {"status": raw_section.get("status") if raw_section.get("status") in {"passed", "failed", "blocked"} else "blocked",
                       count_key: raw_section.get(count_key) if type(raw_section.get(count_key)) is int else None,
                       "path": safe_artifact_path(raw_section.get("path"), report_dir)}
            browser_summary[name] = section
    report["browser"] = browser_summary
    report["untested"] = [item for item in report.get("untested", []) if item != "browser"]
    if problems:
        report["untested"].append("browser")
        report["integration"] = {"status": "unverified"}
    elif mode == "integration":
        report["integration"] = {"status": "unverified", "scope": "read-only"}
    else:
        report["integration"] = {"status": "unverified", "mode": "mock"}
        if "integration" not in report["untested"]:
            report["untested"].append("integration")
    if mode == "integration":
        report["integration"] = {"status": "unverified", "scope": "read-only"}
        integration = evidence.get("integration")
        if isinstance(integration, dict) and integration.get("scope") == "read-only":
            readonly = integration.get("readOnly")
            reported = readonly.get("status") if isinstance(readonly, dict) else None
            accepted_status = reported if reported in {"passed", "blocked", "failed", "unverified"} else "unverified"
            report["integration"]["readOnly"] = {"status": "blocked" if problems else accepted_status}
    return report


def strip_fenced_blocks(text: str) -> str:
    output: list[str] = []
    marker: str | None = None
    length = 0
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            fence = match.group(1)
            if marker is None:
                marker, length = fence[0], len(fence)
            elif fence[0] == marker and len(fence) >= length:
                marker, length = None, 0
            continue
        if marker is None:
            output.append(line)
    return "\n".join(output)


def changed_paths(base_ref: str) -> tuple[bool, list[str]]:
    ok, committed = git_output(ROOT, "diff", "--name-only", f"{base_ref}...HEAD")
    if not ok:
        return False, []
    work_ok, worktree = git_output(ROOT, "diff", "--name-only", "HEAD")
    docs_ok, new_docs = git_output(ROOT, "ls-files", "--others", "--exclude-standard", "--", "doc/web_doc/")
    if not work_ok or not docs_ok:
        return False, []
    return True, sorted({line.strip().replace("\\", "/") for output in (committed, worktree, new_docs) for line in output.splitlines() if line.strip()})


def delivery_records(paths: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative in paths:
        path = ROOT / relative
        if not path.is_file() or not relative.startswith("doc/web_doc/") or path.suffix.lower() != ".md":
            continue
        text = strip_fenced_blocks(path.read_text(encoding="utf-8", errors="replace"))
        for match in RECORD_RE.finditer(text):
            try:
                value = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def collect_change_report(base_ref: str) -> dict[str, Any]:
    ok, paths = changed_paths(base_ref)
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "target": "change-coverage",
        "changedPaths": paths,
        "sourceRevision": source_revision(ROOT),
        "frontend": {"status": "unverified", "checks": [], "untested": ["browser", "integration", "deployment"]},
        "browser": {"status": "unverified", "mode": None},
        "integration": {"status": "unverified"},
        "deployment": {"status": "unverified"},
        "untested": ["frontend execution", "browser", "integration", "deployment"],
    }
    if not ok:
        report["frontend"]["checks"].append(check_record("change-coverage", "blocked", reason="base ref is unavailable"))
        return report
    sensitive = [path for path in paths if path.startswith(SENSITIVE_CHANGE_PREFIXES) or path in SENSITIVE_CHANGE_FILES]
    docs = [path for path in paths if path.startswith("doc/web_doc/") and path.endswith(".md")]
    if not sensitive:
        report["frontend"]["checks"].append(check_record("change-coverage", "passed", reason="no tracked UI/API consumer changes"))
        return report
    records = delivery_records(docs)
    if not records:
        report["frontend"]["checks"].append(check_record("change-coverage", "blocked", reason="sensitive changes need a doc/web_doc frontend-delivery record"))
        return report
    valid = False
    reasons: list[str] = []
    for record in records:
        scope = record.get("scope")
        if scope in {"backend-only", "no-frontend-impact"}:
            if any(path.startswith("frontend-factor-mining/") for path in sensitive):
                reasons.append("backend-only cannot cover a tracked frontend source change")
                continue
            if not record.get("reason") or not record.get("consumers") or not record.get("compatibilityEvidence"):
                reasons.append("backend scope record requires reason, consumers, and compatibilityEvidence")
                continue
            if scope == "backend-only" and not record.get("frontendTODO"):
                reasons.append("backend-only record requires frontendTODO")
                continue
            valid = True
            continue
        if scope != "full-stack":
            reasons.append("delivery record scope must be full-stack or backend-only")
            continue
        if record.get("target") not in {"main", "standalone"}:
            reasons.append("full-stack record needs target main or standalone")
            continue
        if not record.get("frontendRepository") or not record.get("frontendRef") or not record.get("evidence"):
            reasons.append("full-stack record needs frontendRepository, frontendRef, and evidence")
            continue
        if "backend_api_python/app/routes/factor_mining.py" in sensitive and record.get("target") != "main":
            reasons.append("factor_mining API consumer changes require main frontend evidence")
            continue
        valid = True
    status = "passed" if valid else "blocked"
    report["frontend"]["checks"].append(check_record("change-coverage", status, changed=sensitive, reason="; ".join(reasons) if reasons else "documented"))
    return report


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def exit_code(report: dict[str, Any], change_mode: bool = False) -> int:
    status = report.get("frontend", {}).get("status")
    if status in {"blocked", "failed"}:
        return 1
    if any(check.get("status") in {"failed", "blocked"} for check in report.get("frontend", {}).get("checks", [])):
        return 1
    if report.get("browser", {}).get("status") in {"failed", "blocked"}:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    report_path = Path(args.report)
    if args.check_changes:
        report = collect_change_report(args.check_changes)
    else:
        source = resolve_source(args)
        report = collect_report(args.target, source, run=args.run, browser_report=Path(args.browser_report) if args.browser_report else None)
    write_report(report_path, report)
    print(json.dumps({"status": report.get("frontend", {}).get("status"), "report": display_path(report_path)}, ensure_ascii=False))
    return exit_code(report, bool(args.check_changes))


if __name__ == "__main__":
    raise SystemExit(main())
