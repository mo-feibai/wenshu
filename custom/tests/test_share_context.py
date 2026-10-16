# -*- coding: utf-8 -*-
"""分享上下文回归：同一浏览器同时持有单篇与模块凭证时，不丢失模块侧栏。

使用真实 cookie 解析与上下文接口，只替换内存分享记录和文档快照；不读写文档库，
不创建分享，不需要运行中的服务。
"""
import importlib.util
import pathlib
import unittest
import urllib.parse
from unittest import mock


SERVER_PATH = pathlib.Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("wenshu_share_context_test", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class ShareContextTests(unittest.TestCase):
    def setUp(self):
        self.current = self.entry("share-fixture/api/日常检查接口文档", "api")
        self.second = self.entry("share-fixture/api/验收整改接口文档", "api")
        self.sql = self.entry("share-fixture/sql/迁移-测试", "sql")
        self.historical = self.entry("share-fixture/api/日常检查接口文档-v1", "api", "archived")
        self.outside = self.entry("not-shared/api/无权文档", "api")
        self.current_api = [self.current, self.second]
        entries = self.current_api + [self.sql, self.historical, self.outside]
        snap = {
            "by_url": {server.doc_path_key(self.url(e["id"])): e for e in entries},
            "modules": {"监督管理": self.current_api},
            "by_project": {"share-fixture": self.current_api + [self.sql]},
        }
        expires = 1_800_000_060
        self.grants = {
            "project-api": {"scope": "module", "project": "share-fixture", "type": "api", "expires": expires},
            "topic": {"scope": "module", "topic": "监督管理", "projects": ["share-fixture"], "expires": expires},
            "single": {"scope": "doc", "path": self.url(self.current["id"]), "expires": expires},
            "historical-single": {"scope": "doc", "path": self.url(self.historical["id"]), "expires": expires},
            "outside-single": {"scope": "doc", "path": self.url(self.outside["id"]), "expires": expires},
            "expired-project": {"scope": "module", "project": "share-fixture", "type": "api", "expires": 1_799_999_999},
            "revoked-project": {"scope": "module", "project": "share-fixture", "type": "api", "expires": expires, "revoked_at": 1_799_999_990},
        }
        for patcher in (
            mock.patch.object(server, "shares", self.grants),
            mock.patch.object(server, "snapshot", return_value=snap),
            mock.patch.object(server, "now_ts", return_value=1_800_000_000),
            mock.patch.object(server, "doc_url", side_effect=self.url),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def entry(doc_id, dtype, status="implemented"):
        return {
            "id": doc_id,
            "kind": "md",
            "meta": {
                "project": doc_id.split("/")[0],
                "topic": "监督管理" if dtype == "api" else "迁移",
                "title": doc_id.rsplit("/", 1)[-1],
                "type": dtype,
                "status": status,
            },
        }

    @staticmethod
    def url(doc_id):
        return "/docs/" + "/".join(urllib.parse.quote(part) for part in doc_id.split("/")) + "/"

    def context(self, tokens, entry=None, **scope):
        handler = object.__new__(server.Handler)
        handler.headers = {"Cookie": "ws_share=" + urllib.parse.quote(",".join(tokens))}
        handler.json_response = lambda status, payload: (status, payload)
        qs = {"path": [self.url((entry or self.current)["id"])]}
        qs.update({name: [value] for name, value in scope.items()})
        return handler.api_share_ctx(qs)

    def assert_current_module(self, result, expected_scope):
        status, body = result
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], expected_scope)
        self.assertEqual({d["id"] for d in body["docs"]}, {e["id"] for e in self.current_api})

    def test_project_context_survives_overlapping_single_share_in_both_cookie_orders(self):
        for tokens in (("project-api", "single"), ("single", "project-api")):
            with self.subTest(tokens=tokens):
                result = self.context(tokens)
                self.assert_current_module(result, "project")
                self.assertEqual(result[1]["type"], "api")

    def test_topic_context_survives_overlapping_single_share_in_both_cookie_orders(self):
        for tokens in (("topic", "single"), ("single", "topic")):
            with self.subTest(tokens=tokens):
                self.assert_current_module(self.context(tokens), "topic")

    def test_explicit_project_type_scope_stays_filtered(self):
        result = self.context(("single", "project-api"), project="share-fixture", type="api")
        self.assert_current_module(result, "project")

    def test_single_share_without_covering_module_stays_single(self):
        status, body = self.context(("single",))
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], "doc")
        self.assertEqual(body["docs"], [])

    def test_non_covering_module_does_not_override_single_share(self):
        status, body = self.context(("project-api", "outside-single"), self.outside)
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], "doc")

    def test_historical_single_share_is_not_promoted_to_current_module(self):
        status, body = self.context(("project-api", "historical-single"), self.historical)
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], "doc")
        self.assertEqual(body["docs"], [])

    def test_module_only_cannot_supply_context_for_historical_document(self):
        status, _ = self.context(("project-api",), self.historical)
        self.assertEqual(status, 404)

    def test_expired_or_revoked_module_does_not_override_single_share(self):
        for token in ("expired-project", "revoked-project"):
            with self.subTest(token=token):
                status, body = self.context((token, "single"))
                self.assertEqual(status, 200)
                self.assertEqual(body["scope"], "doc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
