"""Safety contracts for the test-owned WenShu HTTP sandbox."""
import http.client
import json
import subprocess
import unittest
import urllib.parse

from tests.baseline.sandbox import ServerSandbox, SandboxViolation, TEST_API_TOKEN
from tests.baseline.test_contracts import DOCUMENTS, post_json


class SandboxHarnessTests(unittest.TestCase):
    def test_unused_fault_is_rejected_at_context_exit(self):
        with self.assertRaisesRegex(SandboxViolation, "fault was not exercised"):
            with ServerSandbox(faults=("git-add",)) as sandbox:
                self.assertEqual(sandbox.request("GET", "/login").status, 200)

    def test_a_consumer_assertion_cannot_hide_an_unused_fault(self):
        with self.assertRaisesRegex(SandboxViolation, "fault was not exercised"):
            with ServerSandbox(faults=("git-add",)):
                raise AssertionError("synthetic consumer assertion must not hide fault mismatch")

    def test_build_failure_is_scripted_and_its_owned_tree_is_cleaned(self):
        with ServerSandbox(docs=DOCUMENTS, faults=("build",)) as sandbox:
            root = sandbox.root
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成构建测试", "type": "other",
                 "title": "合成新文档", "content": "合成写入正文"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 500)
            self.assertEqual(json.loads(response.body)["detail"], "synthetic build failure")
        self.assertFalse(root.exists())

    def test_successful_synthetic_build_and_git_write_is_publicly_readable(self):
        with ServerSandbox() as sandbox:
            created = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "other",
                 "title": "合成新文档", "content": "合成写入正文"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(created.status, 200)
            payload = json.loads(created.body)
            self.assertEqual(payload["id"], "synthetic-project/文档/合成新文档")
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["built"])
            self.assertEqual(payload["commit"], "synthetic-baseline-commit")
            readback = sandbox.request("GET", "/api/admin/doc?" + urllib.parse.urlencode(
                {"id": "synthetic-project/文档/合成新文档"}), headers={"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(readback.status, 200)
            self.assertEqual(json.loads(readback.body)["body"].strip(), "合成写入正文")

    def test_unexpected_subprocess_commands_fail_without_execution_and_restore_the_boundary(self):
        original = subprocess.run
        with self.assertRaises(SandboxViolation):
            with ServerSandbox() as sandbox:
                root = sandbox.root
                subprocess.run(["synthetic-command-that-must-never-execute"])
        self.assertIs(subprocess.run, original)
        self.assertFalse(root.exists())

    def test_cleanup_runs_when_the_http_consumer_raises(self):
        original = subprocess.run
        with self.assertRaisesRegex(RuntimeError, "synthetic consumer failure"):
            with ServerSandbox() as sandbox:
                root = sandbox.root
                self.assertEqual(sandbox.request("GET", "/login").status, 200)
                raise RuntimeError("synthetic consumer failure")
        self.assertFalse(root.exists())
        self.assertIs(subprocess.run, original)

    def test_fixture_paths_cannot_escape_the_owned_namespace(self):
        with self.assertRaisesRegex(SandboxViolation, "Fixture paths must remain"):
            with ServerSandbox(docs={"../synthetic-escape.md": "must not be written"}):
                self.fail("An escaping fixture was accepted")

    def test_copied_server_runs_on_loopback_and_removes_its_owned_tree(self):
        with ServerSandbox() as sandbox:
            root = sandbox.root
            address = sandbox.address
            self.assertEqual(address[0], "127.0.0.1")
            self.assertGreater(address[1], 0)
            response = sandbox.request("GET", "/login")
            self.assertEqual(response.status, 200)
            self.assertIn("登录".encode("utf-8"), response.body)
            self.assertTrue((root / "app" / "server.py").is_file())
        self.assertFalse(root.exists())
        connection = http.client.HTTPConnection(*address, timeout=1)
        try:
            with self.assertRaises(OSError):
                connection.request("GET", "/login")
                connection.getresponse()
        finally:
            connection.close()
