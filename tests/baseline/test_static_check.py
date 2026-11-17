"""Contracts for the static-check CLI's filesystem and failure behavior."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import unittest
from unittest import mock

from tests.baseline.tempdirs import TemporaryDirectory


REPOSITORY = Path(__file__).resolve().parents[2]


class StaticCheckContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(
            prefix="wenshu-static-test-", dir=os.environ.get("WENSHU_TEST_TMPDIR")
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        checker = self.root / "custom" / "tests" / "static_check.py"
        checker.parent.mkdir(parents=True)
        shutil.copyfile(REPOSITORY / "custom" / "tests" / "static_check.py", checker)
        spec = importlib.util.spec_from_file_location("wenshu_static_fixture", checker)
        self.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.checker)

    def capture(self, action):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            action()
        return output.getvalue()

    def run_cli(self, files=(), git_error=None):
        def git_listing(argv, **kwargs):
            self.assertEqual(argv[:2], ["git", "ls-files"])
            if git_error is not None:
                raise git_error
            listed = [rel for rel in files if "*.json" not in argv or rel.endswith(".json")]
            if "-z" not in argv:
                listed = [
                    '"' + "".join(chr(byte) if 32 <= byte < 127 else f"\\{byte:03o}"
                                  for byte in rel.encode("utf-8")) + '"'
                    if not rel.isascii() else rel for rel in listed
                ]
            separator = "\0" if "-z" in argv else "\n"
            return subprocess.CompletedProcess(argv, 0, stdout=separator.join(listed))

        output = io.StringIO()
        checker = self.root / "custom" / "tests" / "static_check.py"
        with contextlib.redirect_stdout(output), mock.patch("subprocess.run", side_effect=git_listing):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(checker), run_name="__main__")
        return stopped.exception.code, output.getvalue()

    def test_missing_tracked_input_is_not_silently_skipped(self):
        for relative in ("custom/missing.json", "custom/missing.py"):
            with self.subTest(relative=relative):
                status, output = self.run_cli(files=(relative,))
                self.assertNotEqual(status, 0, "A tracked file that cannot be inspected must block validation")
                self.assertIn(relative, output)
                self.assertIn("FAIL", output)

    def test_unreadable_tracked_input_blocks_without_error_detail(self):
        relative = "custom/unreadable.txt"
        blocked = self.root / relative
        blocked.write_text("synthetic text", encoding="utf-8")
        detail = "synthetic I/O detail that must remain private"
        original_read = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError(detail)
            return original_read(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read_text):
            status, output = self.run_cli(files=(relative,))
        self.assertNotEqual(status, 0, "Unreadable tracked text must not be skipped")
        self.assertIn(relative, output)
        self.assertNotIn(detail, output)

    def test_non_ascii_git_path_is_actually_parsed(self):
        relative = "custom/说明 示例.json"
        (self.root / relative).write_text("synthetic invalid JSON", encoding="utf-8")
        status, output = self.run_cli(files=(relative,))
        self.assertNotEqual(status, 0)
        self.assertIn(f"FAIL json {relative}:", output, "Quoted Git paths must not be treated as missing files")

    def test_tracked_runtime_state_blocks_without_parsing_or_scanning(self):
        relative = "custom/server_config.json"
        value = "ghp_" + "B" * 24
        (self.root / relative).write_text("not JSON: " + value, encoding="utf-8")
        status, output = self.run_cli(files=(relative,))
        self.assertNotEqual(status, 0)
        self.assertIn("protected runtime", output)
        self.assertNotIn("json custom/server_config.json", output, "Runtime state must not be parsed")
        self.assertNotIn("secret(", output, "Runtime state must not be secret-scanned")
        self.assertNotIn(value, output)

    def test_secret_detection_never_echoes_the_synthetic_value(self):
        value = "ghp_" + "T" * 24
        relative = "custom/leaked.py"
        (self.root / relative).write_text(f"synthetic = {value!r}\n", encoding="utf-8")
        status, output = self.run_cli(files=(relative,))
        self.assertNotEqual(status, 0)
        self.assertIn("secret(github token)", output)
        self.assertNotIn(value, output, "Even test markers must not be copied into failure logs")

    def test_git_unavailable_blocks_without_runtime_json_fallback(self):
        runtime = self.root / "custom" / "server_config.json"
        runtime.write_text("not valid JSON: synthetic runtime sentinel", encoding="utf-8")
        status, output = self.run_cli(git_error=subprocess.CalledProcessError(128, ["git", "ls-files"]))
        self.assertNotEqual(status, 0, "Unavailable Git must block the static gate")
        self.assertNotIn("server_config.json", output, "Git failure must not trigger a runtime-state read")
        self.assertNotIn("SKIP secrets", output, "Secret scanning must not silently succeed without Git")
        self.assertIn("FAIL", output)

    def test_invalid_baseline_python_blocks_the_cli(self):
        baseline = self.root / "tests" / "baseline"
        baseline.mkdir(parents=True)
        (baseline / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        status, output = self.run_cli()
        self.assertNotEqual(status, 0, "New baseline Python must be syntax-checked")
        self.assertIn("tests", output)
        self.assertIn("broken.py", output)

    def test_syntax_validation_does_not_create_bytecode_files(self):
        (self.root / "custom" / "example.py").write_text("answer = 42\n", encoding="utf-8")
        output = self.capture(self.checker.check_syntax)
        self.assertIn("2 files", output, "The checker and synthetic Python file must both be inspected")
        created = [path.relative_to(self.root).as_posix() for path in self.root.rglob("*.pyc")]
        self.assertEqual(created, [], "Static validation must not write source-tree bytecode")
