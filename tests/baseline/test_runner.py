"""Public contracts for the explicit, fail-closed baseline runner."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from tests.baseline import run
from tests.baseline.known_gaps import KnownGapFailure, known_gap
from tests.baseline.synthetic_gaps import SYNTHETIC_GAPS
from tests.baseline.tempdirs import TemporaryDirectory


class RunnerContracts(unittest.TestCase):
    def test_required_ci_fixture_inputs_are_not_ignored_by_git(self):
        repository = Path(__file__).resolve().parents[2]
        assets = "tests/baseline/fixtures/build/docs/nebula-test/assets/"
        for name, expected in (("dispatch-diagram.svg", 1), ("not-listed.txt", 1),
                               ("unlisted-guard-canary.bin", 0)):
            with self.subTest(name=name):
                code = subprocess.run(["git", "check-ignore", "--no-index", "--quiet", "--", assets + name],
                                      cwd=repository, check=False).returncode
                self.assertEqual(code, expected, "Only the two required synthetic assets may enter a clean checkout")

    def test_cli_stdout_is_json_and_test_diagnostics_remain_on_stderr(self):
        diagnostic = "synthetic test-body diagnostic"

        def gap_case(gap_id):
            class Observation(unittest.TestCase):
                @known_gap(gap_id, SYNTHETIC_GAPS)
                def test_observation(self):
                    print(diagnostic)
                    raise KnownGapFailure(gap_id, "synthetic precise observation")
            return Observation("test_observation")

        class Ordinary(unittest.TestCase):
            def test_ordinary(self):
                pass

        def load_module(name):
            if name == run.MODULES[0]:
                return unittest.TestSuite(gap_case(gap_id) for gap_id in SYNTHETIC_GAPS)
            return unittest.TestSuite([Ordinary("test_ordinary")])

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(unittest.TestLoader, "loadTestsFromName", side_effect=load_module):
            with mock.patch.object(run, "KNOWN_GAPS", SYNTHETIC_GAPS):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    status = run.main([])
        self.assertEqual(status, 0)
        self.assertNotIn(diagnostic, output.getvalue(), "Test logs must not corrupt the CLI JSON transport")
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["expected_failures"], 1)
        self.assertIn(diagnostic, errors.getvalue(), "Diagnostics must be retained rather than hidden")

    def test_report_publication_is_utf8_json_in_a_temporary_directory(self):
        with TemporaryDirectory(prefix="wenshu-report-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            target = root / "baseline.json"
            run.write_report({"schema_version": 1, "status": "passed", "label": "隔离验证"}, target, repo=source)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                             {"schema_version": 1, "status": "passed", "label": "隔离验证"})

    def test_report_publication_does_not_overwrite_an_existing_file(self):
        with TemporaryDirectory(prefix="wenshu-report-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            target = root / "baseline.json"
            target.write_text("synthetic manual report sentinel", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                run.write_report({"status": "passed"}, target, repo=source)
            self.assertEqual(target.read_text(encoding="utf-8"), "synthetic manual report sentinel")

    def test_report_publication_refuses_source_data(self):
        with TemporaryDirectory(prefix="wenshu-report-test-") as temporary:
            source = Path(temporary) / "source"
            custom = source / "custom"
            custom.mkdir(parents=True)
            target = custom / "server_config.json"
            target.write_text("synthetic manual source sentinel", encoding="utf-8")
            with self.assertRaises(ValueError):
                run.write_report({"status": "passed"}, target, repo=source)
            self.assertEqual(target.read_text(encoding="utf-8"), "synthetic manual source sentinel")

    def test_whitelist_is_complete_and_rejects_empty_or_duplicate_selections(self):
        self.assertEqual(run.MODULES, (
            "tests.baseline.test_harness", "tests.baseline.test_contracts",
            "tests.baseline.test_known_gaps", "tests.baseline.test_build_site",
            "tests.baseline.test_static_check", "tests.baseline.test_theme",
            "tests.baseline.test_tempdirs", "tests.baseline.test_runner",
        ))
        with mock.patch.object(unittest.TestLoader, "loadTestsFromName") as imported:
            for selected in ((), ("tests.baseline.test_tempdirs",) * 2):
                with self.subTest(selected=selected), self.assertRaises(ValueError):
                    run.load_suite(selected)
        imported.assert_not_called()

    def test_empty_whitelisted_module_is_an_error(self):
        with mock.patch.object(unittest.TestLoader, "loadTestsFromName", return_value=unittest.TestSuite()):
            with self.assertRaisesRegex(ValueError, "No tests"):
                run.load_suite(["tests.baseline.test_tempdirs"])

    def test_legacy_modules_are_rejected_without_importing_them(self):
        with mock.patch.object(unittest.TestLoader, "loadTestsFromName") as imported:
            with self.assertRaises(ValueError):
                run.load_suite(["custom.tests.test_module_share"])
        imported.assert_not_called()

    def test_matching_registered_gap_is_explicitly_reported(self):
        class Known(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_bug(self):
                raise KnownGapFailure("WS-008", "synthetic exact bug observation")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Known)
        report = run.run_suite(suite, registry=SYNTHETIC_GAPS, stream=io.StringIO())
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["expected_failures"], 1)
        self.assertEqual(report["coverage"], {"WS-008": 1})
        self.assertEqual(report["known_gaps"][0]["outcome"], "expected_failure")

    def test_resolved_gap_is_not_silently_kept_quarantined(self):
        class Resolved(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_bug(self):
                pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Resolved)
        report = run.run_suite(suite, registry=SYNTHETIC_GAPS, stream=io.StringIO())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["unexpected_successes"], 1)
        self.assertEqual(report["known_gaps"][0]["outcome"], "unexpected_success")

    def test_skipped_checks_are_not_a_successful_baseline(self):
        class Skipped(unittest.TestCase):
            @unittest.skip("synthetic unavailable check")
            def test_unavailable(self):
                pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Skipped)
        report = run.run_suite(suite, registry={}, stream=io.StringIO())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["skipped"], 1)

    def test_unregistered_gap_tag_blocks_before_execution(self):
        calls = []

        class Unknown(unittest.TestCase):
            def test_bug(self):
                calls.append("executed")

        Unknown.test_bug.__known_gap_id__ = "WS-999"
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Unknown)
        report = run.run_suite(suite, registry={}, stream=io.StringIO())
        self.assertEqual(report["status"], "failed")
        self.assertIn("Unregistered", "; ".join(report["validation_errors"]))
        self.assertEqual(calls, [])

    def test_missing_registered_gap_blocks_before_execution(self):
        calls = []

        class Ordinary(unittest.TestCase):
            def test_ordinary(self):
                calls.append("executed")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Ordinary)
        report = run.run_suite(suite, registry=SYNTHETIC_GAPS, stream=io.StringIO())
        self.assertEqual(report["status"], "failed")
        self.assertIn("Missing", "; ".join(report["validation_errors"]))
        self.assertEqual(calls, [])

    def test_duplicate_known_gap_cases_block_before_execution(self):
        calls = []

        class Duplicate(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_bug(self):
                calls.append("executed")
                raise KnownGapFailure("WS-008", "synthetic exact bug observation")

        suite = unittest.TestSuite([Duplicate("test_bug"), Duplicate("test_bug")])
        report = run.run_suite(suite, registry=SYNTHETIC_GAPS, stream=io.StringIO())
        self.assertEqual(report["status"], "failed")
        self.assertIn("Duplicate", "; ".join(report["validation_errors"]))
        self.assertEqual(calls, [], "Invalid coverage must be rejected before test actions")

    def test_zero_tests_never_reports_a_pass(self):
        report = run.run_suite(unittest.TestSuite(), registry={}, stream=io.StringIO())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["tests_run"], 0)
        self.assertIn("No tests", "; ".join(report["validation_errors"]))

    def test_an_unregistered_regression_blocks_the_report(self):
        class Regression(unittest.TestCase):
            def test_regression(self):
                raise AssertionError("synthetic unexpected regression")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Regression)
        report = run.run_suite(suite, registry={}, stream=io.StringIO())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["tests_run"], 1)
        self.assertEqual(report["failures"], 1)
