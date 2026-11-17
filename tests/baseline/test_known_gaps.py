"""Precise existing-bug signatures and tests of the strict reporting gate."""
import io
import json
import unittest
import urllib.parse
from functools import partial

from tests.baseline.known_gaps import KnownGapFailure, StrictGapResult, known_gap
from tests.baseline.sandbox import ServerSandbox, TEST_API_TOKEN
from tests.baseline.synthetic_gaps import SYNTHETIC_GAPS
from tests.baseline.test_contracts import DOCUMENTS, PAGES, login_cookie, post_json


def run_probe(case):
    return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0,
                                   resultclass=partial(StrictGapResult, registry=SYNTHETIC_GAPS)).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(case))


class PublicKnownGapTests(unittest.TestCase):
    def test_failed_git_add_must_not_report_a_successful_committed_write(self):
        with ServerSandbox(faults=("git-add",)) as sandbox:
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成添加失败", "filename": "synthetic-add-fault.sql", "content": "SELECT 606;"},
                {"X-API-Key": TEST_API_TOKEN})
            payload = json.loads(response.body)
            self.assertIn(response.status, {500, 502, 503}, response.body)
            self.assertIsNot(payload.get("ok"), True)
            self.assertTrue(payload.get("error"))
            self.assertNotEqual(payload.get("commit"), "synthetic-baseline-commit")
            self.assertNotIn("built", payload)

    def test_failed_git_commit_must_not_report_a_successful_committed_write(self):
        with ServerSandbox(faults=("git-commit",)) as sandbox:
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成提交失败", "filename": "synthetic-commit-fault.sql", "content": "SELECT 707;"},
                {"X-API-Key": TEST_API_TOKEN})
            payload = json.loads(response.body)
            self.assertIn(response.status, {500, 502, 503}, response.body)
            self.assertIsNot(payload.get("ok"), True)
            self.assertTrue(payload.get("error"))
            self.assertNotIn("built", payload)
            self.assertNotEqual(payload.get("commit"), "synthetic-baseline-commit")

    def test_failed_build_must_not_leave_a_new_untracked_document(self):
        with ServerSandbox(docs=DOCUMENTS, faults=("build",)) as sandbox:
            original = (sandbox.docs / "sample/api/中文接口.md").read_bytes()
            target = sandbox.docs / "synthetic-project/sql/synthetic-build-fault.sql"
            self.assertFalse(target.exists())
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成构建失败", "filename": "synthetic-build-fault.sql", "content": "SELECT 808;"},
                {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 500)
            self.assertEqual(json.loads(response.body),
                {"error": "构建失败，已回滚（改动已存入 build-failed-rescue.patch）",
                 "detail": "synthetic build failure"})
            self.assertEqual((sandbox.docs / "sample/api/中文接口.md").read_bytes(), original)
            readback = sandbox.request("GET", "/api/admin/doc?id=synthetic-project/sql/synthetic-build-fault",
                                       headers={"X-API-Key": TEST_API_TOKEN})
            if target.is_file() or readback.status == 200:
                self.fail("Build reports rollback but the new untracked file remains: target=%r readback=%s"
                          % (target.is_file(), readback.status))
            self.assertFalse(target.exists())
            self.assertEqual(readback.status, 404)

    def test_public_font_prefix_must_not_expose_a_private_document(self):
        private = b"synthetic-private-font-traversal-canary"
        with ServerSandbox(dist={"docs/private/index.html": private,
                                 "fonts/allowed.woff2": b"synthetic-allowed-font"}) as sandbox:
            control = sandbox.request("GET", "/docs/private/index.html")
            self.assertEqual(control.status, 303)
            self.assertNotIn(private, control.body)
            self.assertEqual(sandbox.request("GET", "/fonts/allowed.woff2").body,
                             b"synthetic-allowed-font")
            response = sandbox.request("GET", "/fonts/%2e%2e/docs/private/index.html")
            self.assertIn(response.status, {303, 401, 403, 404})
            self.assertNotIn(private, response.body)

    def test_single_share_must_not_escape_its_document_with_encoded_dot_segments(self):
        private = b"synthetic-private-single-traversal-canary"
        shares = {"synthetic-bound-single": {"scope": "doc", "path": "/docs/public/doc/",
                                              "expires": 4102444800}}
        with ServerSandbox(shares=shares, dist={"docs/public/doc/index.html": b"synthetic-shared-page",
                                               "docs/private/index.html": private}) as sandbox:
            cookie = {"Cookie": "ws_share=synthetic-bound-single"}
            allowed = sandbox.request("GET", "/docs/public/doc/?share=1", headers=cookie)
            self.assertEqual((allowed.status, allowed.body), (200, b"synthetic-shared-page"))
            control = sandbox.request("GET", "/docs/private/index.html?share=1", headers=cookie)
            self.assertEqual(control.status, 200)
            self.assertIn("该分享不包含此内容".encode("utf-8"), control.body)
            self.assertNotIn(private, control.body)
            response = sandbox.request("GET",
                "/docs/public/doc/%2e%2e/%2e%2e/private/index.html?share=1", headers=cookie)
            self.assertNotIn(private, response.body)
            self.assertIn(response.status, {200, 303, 401, 403, 404})
            if response.status == 200:
                self.assertIn("该分享不包含此内容".encode("utf-8"), response.body)

    def test_logout_must_revoke_a_previously_copied_session_cookie(self):
        with ServerSandbox() as sandbox:
            cookie = login_cookie(sandbox)
            self.assertEqual(sandbox.request("GET", "/api/shares", headers={"Cookie": cookie}).status, 200)
            logout = sandbox.request("GET", "/logout", headers={"Cookie": cookie})
            self.assertEqual(logout.status, 303)
            self.assertEqual(logout.header("Location"), "/login")
            self.assertIn("Max-Age=0", logout.header("Set-Cookie"))
            replay = sandbox.request("GET", "/api/shares", headers={"Cookie": cookie})
            payload = json.loads(replay.body)
            self.assertEqual(replay.status, 401)
            self.assertEqual(payload, {"error": "未登录"})

    def test_create_must_validate_project_domain_and_filename_before_writing(self):
        # All absolute attack paths are owned siblings inside this disposable tree.
        with ServerSandbox() as sandbox:
            observations = []
            for field in ["project", "domain", "filename"]:
                directory = sandbox.root / ("synthetic-escaped-" + field)
                expected = directory / "sql" / "canary.sql"
                payload = {"project": "synthetic-project", "topic": "合成逃逸测试", "type": "sql",
                           "title": "合成逃逸测试", "filename": "canary.sql", "content": "SELECT 4242;"}
                if field == "filename":
                    expected = directory / "canary.sql"
                    payload[field] = str(expected)
                else:
                    payload[field] = str(directory)
                self.assertTrue(expected.is_relative_to(sandbox.root))
                self.assertFalse(expected.is_relative_to(sandbox.docs))
                self.assertFalse(expected.exists())
                response = post_json(sandbox, "/api/admin/create", payload, {"X-API-Key": TEST_API_TOKEN})
                message = json.loads(response.body).get("error", "")
                if expected.exists():
                    self.assertEqual(response.status, 500)
                    self.assertTrue(message.startswith("内部错误: "))
                    self.assertIn(b"SELECT 4242;", expected.read_bytes())
                    observations.append("write-before-validation")
                else:
                    self.assertIn(response.status, {400, 403})
                    observations.append("safely-rejected")
            self.assertEqual(observations, ["safely-rejected"] * 3,
                             "A partial containment change must not be hidden as the old bug signature")

    def test_bulk_meta_must_not_mutate_the_first_item_when_a_later_id_is_invalid(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            path = "/api/admin/doc?" + urllib.parse.urlencode(
                {"id": "sample/api/中文接口"})
            headers = {"X-API-Key": TEST_API_TOKEN}
            before = sandbox.request("GET", path, headers=headers)
            self.assertEqual(before.status, 200)
            self.assertEqual(json.loads(before.body)["meta"]["topic"], "测试主题")
            original = (sandbox.docs / "sample/api/中文接口.md").read_bytes()
            untouched = (sandbox.docs / "sample/api/另一个接口.md").read_bytes()
            response = post_json(sandbox, "/api/admin/meta",
                {"ids": ["sample/api/中文接口", "synthetic-missing/doc"], "topic": "合成不应落盘主题"}, headers)
            self.assertEqual(response.status, 404)
            self.assertEqual(json.loads(response.body), {"error": "文档不存在: synthetic-missing/doc"})
            after = sandbox.request("GET", path, headers=headers)
            self.assertEqual(after.status, 200)
            self.assertEqual((sandbox.docs / "sample/api/另一个接口.md").read_bytes(), untouched)
            payload = json.loads(after.body)
            file_changed = (sandbox.docs / "sample/api/中文接口.md").read_bytes() != original
            self.assertEqual(payload, json.loads(before.body))
            self.assertFalse(file_changed)


class StrictGapResultTests(unittest.TestCase):
    def test_setup_cannot_promote_a_marker_by_calling_the_tagged_method(self):
        class Probe(unittest.TestCase):
            def setUp(self):
                self.test_observation()

            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                raise KnownGapFailure("WS-008", "synthetic setup delegation marker")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_teardown_cannot_promote_a_marker_by_calling_the_tagged_method(self):
        class Probe(unittest.TestCase):
            def setUp(self):
                self.in_fixture = False

            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                if self.in_fixture:
                    raise KnownGapFailure("WS-008", "synthetic teardown delegation marker")

            def tearDown(self):
                self.in_fixture = True
                self.test_observation()

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_cleanup_cannot_promote_a_marker_by_calling_the_tagged_method(self):
        class Probe(unittest.TestCase):
            def setUp(self):
                self.in_fixture = False

            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                if self.in_fixture:
                    raise KnownGapFailure("WS-008", "synthetic cleanup delegation marker")
                self.addCleanup(self.cleanup_observation)

            def cleanup_observation(self):
                self.in_fixture = True
                self.test_observation()

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_only_matching_registered_marker_is_an_expected_failure(self):
        class Probe(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_precise_bug(self):
                raise KnownGapFailure("WS-008", "synthetic verified bug signature")

        result = run_probe(Probe)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.expectedFailures), 1)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.gap_records[0]["id"], "WS-008")
        self.assertEqual(result.gap_records[0]["outcome"], "expected_failure")
        self.assertEqual(result.gap_records[0]["reason"], "synthetic verified bug signature")
        self.assertEqual(set(result.gap_records[0]), {"id", "test", "outcome", "reason"})
        json.dumps(result.gap_records, ensure_ascii=False, allow_nan=False)

    def test_tagged_ordinary_assertion_is_not_suppressed(self):
        class Probe(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                self.assertEqual(200, 403, "unexpected ordinary observation")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])
        self.assertEqual(result.gap_records[0]["outcome"], "failure")

    def test_tagged_infrastructure_exception_is_not_suppressed(self):
        class Probe(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                raise RuntimeError("synthetic connection failure")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.expectedFailures, [])
        self.assertEqual(result.gap_records[0]["outcome"], "error")

    def test_setup_assertion_is_not_suppressed(self):
        class Probe(unittest.TestCase):
            def setUp(self):
                self.fail("synthetic fixture assertion")

            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                raise KnownGapFailure("WS-008", "must not execute")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_matching_marker_from_setup_is_not_suppressed(self):
        class Probe(unittest.TestCase):
            def setUp(self):
                raise KnownGapFailure("WS-008", "synthetic setup marker")

            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                pass

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_matching_marker_from_teardown_is_not_suppressed(self):
        class Probe(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                pass

            def tearDown(self):
                raise KnownGapFailure("WS-008", "synthetic teardown marker")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_matching_marker_from_cleanup_is_not_suppressed(self):
        class Probe(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                def broken_cleanup():
                    raise KnownGapFailure("WS-008", "synthetic cleanup marker")
                self.addCleanup(broken_cleanup)

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_mismatched_registered_marker_is_not_suppressed(self):
        class Probe(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                raise KnownGapFailure("WS-006", "wrong registered gap")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_unregistered_marker_is_not_suppressed(self):
        class Probe(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                raise KnownGapFailure("WS-unregistered", "unknown marker")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_untagged_marker_is_not_suppressed(self):
        class Probe(unittest.TestCase):
            def test_observation(self):
                raise KnownGapFailure("WS-008", "untagged marker")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])
        self.assertEqual(result.gap_records, [])

    def test_passing_tagged_test_is_an_unexpected_success(self):
        class Probe(unittest.TestCase):
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                pass

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.unexpectedSuccesses), 1)
        self.assertEqual(result.gap_records[0]["outcome"], "unexpected_success")

    def test_unittest_expected_failure_cannot_hide_an_ordinary_assertion(self):
        class Probe(unittest.TestCase):
            @unittest.expectedFailure
            def test_observation(self):
                self.fail("ordinary failure")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_unittest_expected_failure_cannot_hide_infrastructure_errors(self):
        class Probe(unittest.TestCase):
            @unittest.expectedFailure
            @known_gap("WS-008", SYNTHETIC_GAPS)
            def test_observation(self):
                raise RuntimeError("synthetic infrastructure error")

        result = run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_decorator_rejects_unknown_ids(self):
        with self.assertRaisesRegex(ValueError, "Unregistered known-gap ID"):
            known_gap("WS-unregistered")

    def test_decorator_sets_metadata_without_wrapping_the_function(self):
        def observation():
            pass

        self.assertIs(known_gap("WS-008", SYNTHETIC_GAPS)(observation), observation)
        self.assertEqual(observation.__known_gap_id__, "WS-008")

    def test_decorator_rejects_multiple_gap_ids_on_one_method(self):
        def observation():
            pass

        known_gap("WS-008", SYNTHETIC_GAPS)(observation)
        with self.assertRaisesRegex(ValueError, "only one known gap"):
            known_gap("WS-008", SYNTHETIC_GAPS)(observation)
