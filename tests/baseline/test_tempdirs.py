"""Public filesystem contracts for owned test temporary directories."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.baseline.tempdirs import TemporaryDirectory


class TemporaryDirectoryContracts(unittest.TestCase):
    def setUp(self):
        self.container = TemporaryDirectory(prefix="wenshu-parent-contract-")
        self.addCleanup(self.container.cleanup)
        self.root = Path(self.container.name)
        self.repository = self.root / "repo"
        copied_helper = self.repository / "tests" / "baseline" / "tempdirs.py"
        copied_helper.parent.mkdir(parents=True)
        copied_helper.write_text(Path(__file__).with_name("tempdirs.py").read_text(encoding="utf-8"),
                                 encoding="utf-8")
        spec = importlib.util.spec_from_file_location("owned_tempdirs_fixture", copied_helper)
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.source = self.repository / "custom"
        self.source.mkdir()
        self.sentinel = self.source / "synthetic-source.txt"
        self.sentinel.write_text("synthetic source must stay unchanged", encoding="utf-8")
        self.scratch = self.repository / "tests" / ".baseline-tmp"
        self.scratch.mkdir()

    def test_environment_source_parent_is_rejected_before_creation(self):
        for parent in (self.repository, self.source, self.repository / "tests"):
            with self.subTest(parent=parent.name):
                before = set(parent.iterdir())
                with mock.patch.dict(os.environ, {"WENSHU_TEST_TMPDIR": str(parent)}):
                    with self.assertRaisesRegex(ValueError, "source data"):
                        with self.helper.TemporaryDirectory():
                            pass
                self.assertEqual(set(parent.iterdir()), before)
        self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "synthetic source must stay unchanged")

    def test_explicit_source_parent_cannot_bypass_configured_scratch(self):
        before = set(self.source.iterdir())
        with mock.patch.dict(os.environ, {"WENSHU_TEST_TMPDIR": str(self.scratch)}):
            with self.assertRaisesRegex(ValueError, "source data"):
                with self.helper.TemporaryDirectory(dir=self.source):
                    pass
        self.assertEqual(set(self.source.iterdir()), before)

    def test_repository_scratch_requires_explicit_configuration(self):
        with mock.patch.dict(os.environ, {"WENSHU_TEST_TMPDIR": ""}):
            with self.assertRaisesRegex(ValueError, "source data"):
                with self.helper.TemporaryDirectory(dir=self.scratch):
                    pass
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_configured_repository_scratch_supports_nested_owned_trees(self):
        with mock.patch.dict(os.environ, {"WENSHU_TEST_TMPDIR": str(self.scratch)}):
            with self.helper.TemporaryDirectory() as first:
                with self.helper.TemporaryDirectory(dir=first) as second:
                    fixture = Path(second) / "fixture.txt"
                    fixture.write_text("synthetic nested fixture", encoding="utf-8")
                    self.assertEqual(fixture.read_text(encoding="utf-8"), "synthetic nested fixture")
                self.assertFalse(Path(second).exists())
            self.assertFalse(Path(first).exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_non_temporary_parent_is_rejected_before_creation(self):
        parent = self.root / "manual-parent"
        parent.mkdir()
        allowed = [self.root / name for name in ("os-temp", "runner-temp", "configured-temp")]
        for directory in allowed:
            directory.mkdir()
        with mock.patch("tempfile.gettempdir", return_value=str(allowed[0])), mock.patch.dict(
                os.environ, {"RUNNER_TEMP": str(allowed[1]), "WENSHU_TEST_TMPDIR": str(allowed[2])}):
            with self.assertRaisesRegex(ValueError, "temporary target"):
                with self.helper.TemporaryDirectory(dir=parent):
                    pass
        self.assertEqual(list(parent.iterdir()), [])

    def test_nested_fixture_writes_and_context_cleanup(self):
        parent = os.environ.get("WENSHU_TEST_TMPDIR") or tempfile.gettempdir()
        with TemporaryDirectory(prefix="wenshu-temp-contract-", dir=parent) as name:
            root = Path(name)
            nested = root / "nested"
            nested.mkdir()
            fixture = nested / "fixture.txt"
            fixture.write_text("synthetic fixture", encoding="utf-8")
            self.assertEqual(fixture.read_text(encoding="utf-8"), "synthetic fixture")
        self.assertFalse(root.exists(), "The owned fixture tree must be cleaned")
