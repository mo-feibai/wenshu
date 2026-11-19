"""Explicit isolated baseline runner; never discovers legacy service tests."""

import argparse
import contextlib
from functools import partial
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.baseline.known_gaps import KNOWN_GAPS, StrictGapResult
from tests.baseline.tempdirs import temporary_report_target, write_json_report

MODULES = (
    "tests.baseline.test_harness",
    "tests.baseline.test_contracts",
    "tests.baseline.test_known_gaps",
    "tests.baseline.test_build_site",
    "tests.baseline.test_static_check",
    "tests.baseline.test_theme",
    "tests.baseline.test_tempdirs",
    "tests.baseline.test_runner",
)


def load_suite(module_names=None):
    selected = MODULES if module_names is None else tuple(module_names)
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(MODULES):
        raise ValueError("Select unique modules from the explicit baseline whitelist")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in selected:
        batch = loader.loadTestsFromName(name)
        if loader.errors:
            raise ImportError("Baseline import failed: " + "\n".join(loader.errors))
        if not batch.countTestCases():
            raise ValueError("No tests in whitelisted module: " + name)
        suite.addTests(batch)
    return suite


def _report_target(path, repo=None):
    return temporary_report_target(path, repository=repo)


def write_report(report, path, *, repo=None):
    write_json_report(report, path, repository=repo)


def _cases(suite):
    if isinstance(suite, unittest.TestCase):
        return [suite]
    if not isinstance(suite, unittest.TestSuite):
        raise TypeError("Only unittest suites and cases are accepted")
    return [case for child in suite for case in _cases(child)]


def _validation_failure(errors, coverage):
    return {
        "schema_version": 1, "status": "failed", "tests_run": 0,
        "failures": 0, "errors": 0, "skipped": 0, "expected_failures": 0,
        "unexpected_successes": 0, "known_gaps": [], "coverage": coverage,
        "validation_errors": errors, "failure_details": [],
    }


def run_suite(suite, *, registry=None, stream=None):
    registered = KNOWN_GAPS if registry is None else registry
    coverage = {gap_id: 0 for gap_id in registered}
    validation_errors = []
    for case in _cases(suite):
        method = getattr(case, getattr(case, "_testMethodName", ""), None)
        tag = getattr(method, "__known_gap_id__", None)
        if tag is not None:
            if not isinstance(tag, str) or tag not in coverage:
                validation_errors.append("Unregistered known-gap tag on " + case.id())
            else:
                coverage[tag] += 1
    if validation_errors:
        return _validation_failure(validation_errors, coverage)
    missing = [gap_id for gap_id, count in coverage.items() if count == 0]
    if missing:
        return _validation_failure(["Missing known-gap cases: " + ", ".join(missing)], coverage)
    duplicate = [gap_id for gap_id, count in coverage.items() if count > 1]
    if duplicate:
        return _validation_failure(["Duplicate known-gap cases: " + ", ".join(duplicate)], coverage)
    result = unittest.TextTestRunner(
        stream=stream if stream is not None else sys.stderr,
        verbosity=2,
        resultclass=partial(StrictGapResult, registry=registered),
    ).run(suite)
    validation_errors = [] if result.testsRun else ["No tests selected"]
    if result.skipped:
        validation_errors.append("Skipped checks do not constitute accepted baseline coverage")
    return {
        "schema_version": 1,
        "status": "passed" if result.wasSuccessful() and not validation_errors else "failed",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "known_gaps": result.gap_records,
        "coverage": coverage,
        "validation_errors": validation_errors,
        "failure_details": [{"test": test.id(), "traceback": detail}
                            for test, detail in result.failures + result.errors],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", help="Create a new JSON report in a temporary directory")
    args = parser.parse_args(argv)
    report_target = None
    try:
        if args.report:
            report_target = _report_target(args.report)
        with contextlib.redirect_stdout(sys.stderr):
            report = run_suite(load_suite())
    except Exception as error:
        report = _validation_failure([type(error).__name__ + ": " + str(error)], {})
    report["modules"] = list(MODULES)
    if report_target is not None:
        try:
            write_report(report, report_target)
        except Exception as error:
            report["status"] = "failed"
            report["validation_errors"].append("Report publication failed: " + type(error).__name__)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
