"""Behavioral tests for frontend delivery reports without frontend dependencies."""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_frontend_delivery.py"


def load_checker():
    if not SCRIPT.is_file():
        raise AssertionError("Frontend delivery checker has not been implemented")
    spec = importlib.util.spec_from_file_location("frontend_delivery_checker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_fixture(directory, scripts=None, manager="pnpm@10.33.4", locks=None, target="main"):
    source = Path(directory) / "frontend"
    source.mkdir()
    package = {
        "name": "quantdinger-vue" if target == "main" else "quantdinger-factor-mining-ui", "scripts": scripts if scripts is not None else {
            "lint:nofix": "eslint src", "typecheck": "tsc --noEmit",
            "test:unit": "node --test", "build": "vite build",
        },
    }
    if manager:
        package["packageManager"] = manager
    (source / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / ".gitignore").write_text("node_modules\npackage-lock.json\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True)
    for filename in locks if locks is not None else ["pnpm-lock.yaml"]:
        (source / filename).write_text("{}", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "-f", filename],
                       check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "add", "package.json", ".gitignore"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "fixture"],
                   check=True, capture_output=True)
    return source


class FrontendDeliveryCliTests(unittest.TestCase):
    def test_missing_source_writes_blocked_report_and_nonzero_exit(self):
        # Silently skipping a missing external checkout must fail this test.
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--target", "main", "--source",
                 str(Path(directory) / "missing"), "--report", str(report_path)],
                capture_output=True, text=True, timeout=20, check=False,
            )
            self.assertTrue(report_path.is_file(), result.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(report["frontend"]["status"], "blocked")
            self.assertEqual(report["browser"]["status"], "unverified")
            self.assertEqual(report["integration"]["status"], "unverified")
            self.assertEqual(report["deployment"]["status"], "unverified")
            self.assertNotIn(directory, json.dumps(report))


class FrontendDeliveryBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.command_patch = mock.patch.object(self.checker.shutil, "which", return_value=sys.executable)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    def test_missing_typecheck_is_blocked_even_with_other_scripts(self):
        source = source_fixture(self.directory.name, scripts={
            "lint:nofix": "eslint src", "test:unit": "node --test", "build": "vite build",
        })
        report = self.checker.collect_report("main", source)
        checks = {check["name"]: check for check in report["frontend"]["checks"]}
        self.assertEqual(report["frontend"]["status"], "blocked")
        self.assertEqual(checks["typecheck"]["status"], "blocked")

    def test_package_manager_uses_tracked_pnpm_and_ignores_npm_lock(self):
        source = source_fixture(self.directory.name)
        (source / "package-lock.json").write_text("{}", encoding="utf-8")
        report = self.checker.collect_report("main", source)
        package_manager = report["frontend"]["packageManager"]
        self.assertEqual(package_manager["name"], "pnpm")
        self.assertEqual(package_manager["version"], "10.33.4")
        self.assertEqual(package_manager["lockfile"], "pnpm-lock.yaml")
        self.assertEqual(package_manager["ignoredLockfiles"], ["package-lock.json"])
        self.assertNotEqual(report["frontend"]["status"], "blocked")

    def test_ambiguous_tracked_locks_are_blocked(self):
        source = source_fixture(self.directory.name, manager=None,
                                locks=["pnpm-lock.yaml", "yarn.lock"])
        report = self.checker.collect_report("main", source)
        self.assertEqual(report["frontend"]["status"], "blocked")

    def test_inspection_does_not_claim_static_or_browser_completion(self):
        source = source_fixture(self.directory.name)
        report = self.checker.collect_report("main", source)
        self.assertEqual(report["frontend"]["status"], "unverified")
        for area in ("browser", "integration", "deployment"):
            self.assertEqual(report[area]["status"], "unverified")

    def test_mutating_lint_is_blocked(self):
        source = source_fixture(self.directory.name, scripts={
            "lint": "eslint src --fix", "typecheck": "tsc --noEmit",
            "test:unit": "node --test", "build": "vite build",
        }, target="standalone")
        report = self.checker.collect_report("standalone", source)
        lint = next(check for check in report["frontend"]["checks"] if check["name"] == "lint")
        self.assertEqual(lint["status"], "blocked")

    def test_real_command_failure_has_failed_status(self):
        result = self.checker.run_check(
            [sys.executable, "-c", "raise SystemExit(7)"], Path(self.directory.name), 5)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exitCode"], 7)

    def test_timeout_is_bounded_and_failed(self):
        result = self.checker.run_check(
            [sys.executable, "-c", "import time; time.sleep(5)"], Path(self.directory.name), 0.1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "timeout")

    def test_failed_script_does_not_prevent_other_checks(self):
        source = source_fixture(self.directory.name)
        def execute(argv, cwd, timeout):
            if argv[-1] == "lint:nofix":
                return {"status": "failed", "exitCode": 1, "output": "lint failure"}
            return {"status": "passed", "exitCode": 0, "output": "10.33.4"}
        with mock.patch.object(self.checker.shutil, "which", return_value=sys.executable), \
                mock.patch.object(self.checker, "run_check", side_effect=execute):
            report = self.checker.collect_report("main", source, run=True)
        checks = {check["name"]: check for check in report["frontend"]["checks"]}
        self.assertEqual(checks["lint"]["status"], "failed")
        for name in ("typecheck", "unit", "build"):
            self.assertEqual(checks[name]["status"], "passed")
        self.assertEqual(report["frontend"]["status"], "failed")
        self.assertEqual(report["browser"]["status"], "unverified")

    def test_browser_evidence_cannot_substitute_another_target_or_revision(self):
        source = source_fixture(self.directory.name)
        for target, revision in [("standalone", "wrong"), ("main", "wrong")]:
            with self.subTest(target=target):
                evidence = Path(self.directory.name) / "browser.json"
                evidence.write_text(json.dumps({"schemaVersion": 1, "target": target,
                    "sourceRevision": revision, "mode": "mock", "status": "passed"}), encoding="utf-8")
                report = self.checker.collect_report("main", source, browser_report=evidence)
                self.assertEqual(report["browser"]["status"], "blocked")
                self.assertEqual(report["integration"]["status"], "unverified")

    def test_command_output_does_not_leak_paths_credentials_or_tokens(self):
        secret = "ghp_123456789012345678901234567890123456"
        raw = ("https://private-user:private-pass@example.invalid/source token=" + secret
               + " password=private-pass " + self.directory.name + " C:\\Users\\private\\file")
        result = self.checker.run_check(
            [sys.executable, "-c", "print(" + repr(raw) + ")"], Path(self.directory.name), 5)
        encoded = json.dumps(result)
        for forbidden in (secret, "private-user", "private-pass", self.directory.name, "C:\\\\Users"):
            self.assertNotIn(forbidden, encoded)

    def test_environment_default_does_not_choose_untracked_frontend_vue(self):
        parser = self.checker.argument_parser()
        with mock.patch.dict(os.environ, {"FRONTEND_SRC_PATH": "explicit-source"}):
            args = parser.parse_args(["--target", "main", "--report", "out.json"])
            self.assertEqual(self.checker.resolve_source(args), Path("explicit-source"))

    def test_missing_dependencies_are_blocked_without_installing(self):
        source = source_fixture(self.directory.name)
        (source / "node_modules").rmdir()
        with mock.patch.object(self.checker.shutil, "which", return_value=sys.executable):
            report = self.checker.collect_report("main", source, run=True)
        self.assertEqual(report["frontend"]["status"], "blocked")
        self.assertFalse((source / "node_modules").exists())

    def test_invalid_browser_artifacts_fail_cli_even_if_static_passed(self):
        report = {"frontend": {"status": "passed"}, "browser": {"status": "blocked"}}
        self.assertNotEqual(self.checker.exit_code(report), 0)

    def test_mutating_lifecycle_hook_cannot_hide_behind_nonfixing_lint(self):
        source = source_fixture(self.directory.name, scripts={
            "lint:nofix": "eslint src", "prelint:nofix": "eslint src --fix",
            "typecheck": "tsc --noEmit", "test:unit": "node --test", "build": "vite build",
        })
        report = self.checker.collect_report("main", source)
        checks = {check["name"]: check for check in report["frontend"]["checks"]}
        self.assertEqual(checks["lint"]["status"], "blocked")

    def test_invalid_browser_shape_is_blocked_instead_of_crashing(self):
        source = source_fixture(self.directory.name)
        evidence = Path(self.directory.name) / "browser.json"
        evidence.write_text(json.dumps({"screenshots": None, "limitations": None}), encoding="utf-8")
        report = self.checker.collect_report("main", source, browser_report=evidence)
        self.assertEqual(report["browser"]["status"], "blocked")

    def test_main_version_is_read_from_source_instead_of_pinned_in_checker(self):
        source = source_fixture(self.directory.name, manager="pnpm@11.0.1")
        report = self.checker.collect_report("main", source)
        self.assertNotEqual(report["frontend"]["status"], "blocked")
        self.assertEqual(report["frontend"]["packageManager"]["version"], "11.0.1")

    def test_standalone_explicit_manager_can_use_new_untracked_lock(self):
        source = source_fixture(self.directory.name, manager="npm@11.0.0", locks=[], target="standalone")
        (source / "package-lock.json").write_text("{}", encoding="utf-8")
        (source / ".gitignore").write_text("node_modules\n", encoding="utf-8")
        report = self.checker.collect_report("standalone", source)
        self.assertEqual(report["frontend"]["packageManager"]["lockfile"], "package-lock.json")
        self.assertFalse(report["frontend"]["packageManager"]["lockfileTracked"])

    def test_artifact_paths_reject_absolute_and_escape_paths(self):
        outside = Path(self.directory.name) / "private.json"
        outside.write_text("{}", encoding="utf-8")
        artifacts = Path(self.directory.name) / "artifacts"
        artifacts.mkdir()
        self.assertFalse(self.checker.artifact_exists(str(outside), artifacts))
        self.assertFalse(self.checker.artifact_exists("../private.json", artifacts))

    def test_browser_summary_drops_extra_fields_and_uses_relative_artifact_paths(self):
        source = source_fixture(self.directory.name)
        folder = Path(self.directory.name) / "artifacts"
        folder.mkdir()
        for filename in ("desktop.png", "mobile.png", "console.json", "network.json", "axe.json"):
            (folder / filename).write_text("{}", encoding="utf-8")
        evidence = {"schemaVersion": 1, "target": "main", "sourceRevision": self.checker.source_revision(source),
                    "route": "/#/factor-mining", "mode": "mock", "status": "passed",
                    "screenshots": [
                        {"viewport": {"width": 1440, "height": 900}, "path": "desktop.png"},
                        {"viewport": {"width": 390, "height": 844}, "path": "mobile.png"}],
                    "console": {"status": "passed", "errorCount": 0, "path": "console.json", "headers": {"token": "do-not-copy"}},
                    "network": {"status": "passed", "unexpectedErrorCount": 0, "path": "network.json"},
                    "axe": {"status": "passed", "criticalCount": 0, "path": "axe.json"},
                    "states": {name: {"status": "passed"} for name in ("loading", "empty", "error", "success", "unauthorized")},
                    "source": {"status": "passed", "revisionVerified": True, "bundleVerified": True},
                    "checks": [{"id": f"{name}-{width}", "status": "passed"}
                               for name in ("mock-submit", "submitted-job-visible", "job-polling", "mock-refresh", "mock-refresh-detail")
                               for width in (1440, 390)],
                    "limitations": []}
        evidence_path = folder / "browser.json"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        report = self.checker.collect_report("main", source, browser_report=evidence_path)
        self.assertEqual(report["browser"]["status"], "passed")
        self.assertEqual(report["browser"]["screenshots"][0]["path"], "desktop.png")
        self.assertNotIn("headers", report["browser"]["console"])
        self.assertNotIn("do-not-copy", json.dumps(report))
        self.assertEqual(report["integration"]["status"], "unverified")

        # Reusing another server or caller-declared revision cannot prove this bundle.
        for field in ("revisionVerified", "bundleVerified"):
            with self.subTest(unverified=field):
                modified = json.loads(json.dumps(evidence))
                modified["source"][field] = False
                evidence_path.write_text(json.dumps(modified), encoding="utf-8")
                changed = self.checker.collect_report("main", source, browser_report=evidence_path)
                self.assertEqual(changed["browser"]["status"], "blocked")
        for checks in ([], [{"id": "mock-submit-1440", "status": "failed"}]):
            with self.subTest(checks=checks):
                modified = {**evidence, "checks": checks}
                evidence_path.write_text(json.dumps(modified), encoding="utf-8")
                changed = self.checker.collect_report("main", source, browser_report=evidence_path)
                self.assertEqual(changed["browser"]["status"], "blocked")
        modified = {**evidence, "mode": "integration", "integration": {"status": "unverified", "scope": "read-only", "readOnly": {"status": "passed"}}}
        evidence_path.write_text(json.dumps(modified), encoding="utf-8")
        changed = self.checker.collect_report("main", source, browser_report=evidence_path)
        self.assertEqual(changed["integration"]["status"], "unverified")
        self.assertEqual(changed["integration"]["scope"], "read-only")

    def test_target_cannot_be_claimed_by_an_unrelated_package(self):
        source = source_fixture(self.directory.name, target="standalone")
        report = self.checker.collect_report("main", source)
        self.assertEqual(report["frontend"]["status"], "blocked")

    def test_invalid_browser_evidence_cannot_claim_read_only_integration_passed(self):
        source = source_fixture(self.directory.name)
        folder = Path(self.directory.name) / "artifacts"
        folder.mkdir()
        for filename in ("desktop.png", "mobile.png", "console.json", "network.json", "axe.json"):
            (folder / filename).write_text("{}", encoding="utf-8")
        revision = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                                  check=True, capture_output=True, text=True).stdout.strip()
        evidence = {
            "schemaVersion": 1, "target": "main", "sourceRevision": revision,
            "mode": "integration", "route": "/#/factor-mining", "status": "passed",
            "source": {"status": "passed", "revisionVerified": True, "bundleVerified": True},
            "checks": [{"id": "integration-job-list-1440", "status": "passed"}],
            "screenshots": [
                {"viewport": {"width": 1440, "height": 900}, "path": "desktop.png"},
                {"viewport": {"width": 390, "height": 844}, "path": "mobile.png"}],
            "console": {"status": "passed", "errorCount": 0, "path": "console.json"},
            "network": {"status": "passed", "unexpectedErrorCount": 0, "path": "network.json"},
            "axe": {"status": "passed", "criticalCount": 0, "path": "axe.json"},
            "states": {name: {"status": "passed"} for name in ("loading", "empty", "error", "success", "unauthorized")},
            "integration": {"status": "unverified", "scope": "read-only", "readOnly": {"status": "passed"}},
        }
        evidence_path = folder / "browser.json"
        for changed in (
            {"target": "standalone"}, {"sourceRevision": "0" * 40},
            {"source": {"status": "unverified", "revisionVerified": False, "bundleVerified": False}},
            {"checks": []}, {"route": "/"}, {"screenshots": []},
        ):
            with self.subTest(changed=changed):
                evidence_path.write_text(json.dumps({**evidence, **changed}), encoding="utf-8")
                report = self.checker.collect_report("main", source, browser_report=evidence_path)
                self.assertEqual(report["browser"]["status"], "blocked")
                self.assertEqual(report["integration"]["status"], "unverified")
                self.assertIn(report["integration"]["readOnly"]["status"], ("blocked", "unverified"))

    def test_package_manager_version_mismatch_blocks_execution(self):
        source = source_fixture(self.directory.name)
        with mock.patch.object(self.checker.shutil, "which", return_value=sys.executable), \
                mock.patch.object(self.checker, "run_check", return_value={"status": "passed", "output": "9.0.0", "exitCode": 0}):
            report = self.checker.collect_report("main", source, run=True)
        self.assertEqual(report["frontend"]["status"], "blocked")

    def test_windows_npm_uses_cli_beside_selected_node(self):
        runtime = Path(self.directory.name) / "node-runtime"
        runtime.mkdir()
        node = runtime / "node.exe"
        node.touch()
        cli = runtime / "node_modules/npm/bin/npm-cli.js"
        cli.parent.mkdir(parents=True)
        cli.write_text("// fixture", encoding="utf-8")
        def which(command):
            return str(node) if command == "node" else "different-volta-npm.exe"
        with mock.patch.object(self.checker.shutil, "which", side_effect=which):
            self.assertEqual(self.checker.package_manager_command("npm", windows=True), [str(node), str(cli)])


class FrontendChangeCoverageTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = source_fixture(self.directory.name)
        self.frontend = self.repo / "frontend-factor-mining/src/App.vue"
        self.api = self.repo / "backend_api_python/app/routes/factor_mining.py"
        self.backend_test = self.repo / "backend_api_python/tests/test_example.py"
        for path in (self.frontend, self.api, self.backend_test):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "baseline"], check=True, capture_output=True)
        self.root_patch = mock.patch.object(self.checker, "ROOT", self.repo)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def record(self, value, fenced=False):
        path = self.repo / "doc/web_doc/06-example.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "<!-- frontend-delivery: " + json.dumps(value) + " -->"
        path.write_text("```json\n" + text + "\n```" if fenced else text, encoding="utf-8")

    def coverage_check(self):
        report = self.checker.collect_change_report("HEAD")
        return report, report["frontend"]["checks"][0]

    def test_ui_change_without_coverage_record_is_blocked(self):
        self.frontend.write_text("changed\n", encoding="utf-8")
        report, check = self.coverage_check()
        self.assertEqual(check["status"], "blocked")
        self.assertNotEqual(self.checker.exit_code(report, change_mode=True), 0)

    def test_untracked_document_can_cover_tracked_worktree_change(self):
        self.frontend.write_text("changed\n", encoding="utf-8")
        self.record({"scope": "full-stack", "target": "standalone", "frontendRepository": "QuantDinger",
                     "frontendRef": "abc1234", "evidence": "unverified"})
        report, check = self.coverage_check()
        self.assertEqual(check["status"], "passed")
        self.assertEqual(report["frontend"]["status"], "unverified")
        self.assertEqual(report["browser"]["status"], "unverified")

    def test_fenced_template_is_not_delivery_evidence(self):
        self.frontend.write_text("changed\n", encoding="utf-8")
        self.record({"scope": "full-stack", "target": "standalone", "frontendRepository": "QuantDinger",
                     "frontendRef": "abc1234", "evidence": "unverified"}, fenced=True)
        _, check = self.coverage_check()
        self.assertEqual(check["status"], "blocked")

    def test_backend_test_only_needs_no_ui_delivery(self):
        self.backend_test.write_text("changed\n", encoding="utf-8")
        _, check = self.coverage_check()
        self.assertEqual(check["status"], "passed")

    def test_ui_change_cannot_claim_backend_only(self):
        self.frontend.write_text("changed\n", encoding="utf-8")
        self.record({"scope": "backend-only", "reason": "deferred", "frontendTODO": "add UI",
                     "consumers": ["main"], "compatibilityEvidence": "unchanged payload"})
        _, check = self.coverage_check()
        self.assertEqual(check["status"], "blocked")

    def test_api_change_cannot_use_standalone_as_main_delivery(self):
        self.api.write_text("changed\n", encoding="utf-8")
        self.record({"scope": "full-stack", "target": "standalone", "frontendRepository": "QuantDinger",
                     "frontendRef": "abc1234", "evidence": "unverified"})
        _, check = self.coverage_check()
        self.assertEqual(check["status"], "blocked")

    def test_no_frontend_impact_requires_consumers_and_compatibility_evidence(self):
        self.api.write_text("changed\n", encoding="utf-8")
        self.record({"scope": "no-frontend-impact", "reason": "internal logging"})
        _, check = self.coverage_check()
        self.assertEqual(check["status"], "blocked")
        self.record({"scope": "no-frontend-impact", "reason": "internal logging", "consumers": ["main"],
                     "compatibilityEvidence": "response/request/status unchanged"})
        _, check = self.coverage_check()
        self.assertEqual(check["status"], "passed")

    def test_api_backend_only_defer_keeps_ui_unverified(self):
        self.api.write_text("changed\n", encoding="utf-8")
        self.record({"scope": "backend-only", "reason": "phased rollout", "frontendTODO": "add mode selector",
                     "consumers": ["main"], "compatibilityEvidence": "existing mode preserved"})
        report, check = self.coverage_check()
        self.assertEqual(check["status"], "passed")
        self.assertEqual(report["frontend"]["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
