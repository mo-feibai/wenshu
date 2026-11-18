"""Public HTTP contracts against synthetic, test-owned runtime documents."""
import http.cookies
import json
import os
import time
import unittest
import urllib.parse

from tests.baseline.sandbox import ServerSandbox, TEST_API_TOKEN, TEST_PASSWORD


DOCUMENTS = {
    "sample/api/中文接口.md": """---
title: 中文接口
project: sample
topic: 测试主题
type: api
status: implemented
version: 2
date: 2026-01-01
supersedes: sample/api/中文接口-v1
---
# 中文接口
这是独立的合成接口正文。[证据](/files/证据.txt)
""",
    "sample/api/另一个接口.md": """---
title: 另一个接口
project: sample
topic: 测试主题
type: api
status: implemented
version: 1
date: 2026-01-01
---
# 另一个接口
第二份合成接口正文。
""",
    "sample/api/中文接口-v1.md": """---
title: 中文接口-v1
project: sample
topic: 测试主题
type: api
status: archived
version: 1
date: 2025-01-01
superseded_by: sample/api/中文接口
---
# 中文接口-v1
明确由当前版本取代的合成历史正文。
""",
    "sample/sql/迁移-测试.sql": """-- @meta
-- title: 迁移测试
-- project: sample
-- topic: 测试主题
-- type: sql
-- status: executed
-- date: 2026-01-01
-- @end
SELECT 17;
""",
    "other/api/不共享.md": """---
title: 不共享
project: other
topic: 别的主题
type: api
status: implemented
version: 1
date: 2026-01-01
---
# 不共享
另一个项目的合成私密正文。
""",
}
PAGES = {
    "docs/sample/api/中文接口/index.html": '<html>合成中文接口页面<a href="/files/证据.txt">证据</a></html>',
    "docs/sample/api/另一个接口/index.html": "<html>合成第二接口页面</html>",
    "docs/sample/api/中文接口-v1/index.html": "<html>合成历史接口页面</html>",
    "docs/sample/sql/迁移-测试/index.html": "<html>合成迁移页面</html>",
    "docs/other/api/不共享/index.html": "<html>合成不共享页面</html>",
    "files/证据.txt": "合成附件内容",
    "files/unreferenced.txt": "合成未引用附件",
    "fonts/synthetic.woff2": b"synthetic-font-bytes",
    "projects/sample/index.html": "<html>合成项目首页</html>",
    "topics/测试主题/index.html": "<html>合成主题首页</html>",
}


SHARES = {
    "synthetic-single": {"scope": "doc", "path": "/docs/sample/api/中文接口/", "expires": 4102444800},
    "synthetic-history": {"scope": "doc", "path": "/docs/sample/api/中文接口-v1/", "expires": 4102444800},
    "synthetic-outside": {"scope": "doc", "path": "/docs/other/api/不共享/", "expires": 4102444800},
    "synthetic-project": {"scope": "module", "project": "sample", "expires": 4102444800},
    "synthetic-project-api": {"scope": "module", "project": "sample", "type": "api", "expires": 4102444800},
    "synthetic-topic": {"scope": "module", "topic": "测试主题", "expires": 4102444800},
}


def share_context(sandbox, tokens, **query):
    cookie = "ws_share=" + urllib.parse.quote(",".join(tokens), safe="")
    return sandbox.request("GET", "/api/share/ctx?" + urllib.parse.urlencode(query),
                           headers={"Cookie": cookie})


def post_json(sandbox, path, payload, headers=None):
    return sandbox.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                           headers={"Content-Type": "application/json", **(headers or {})})


def login_cookie(sandbox):
    response = sandbox.request("POST", "/login",
        body=urllib.parse.urlencode({"password": TEST_PASSWORD}),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if response.status != 303:
        raise AssertionError("Synthetic password login did not redirect")
    return "ws_sid=" + response_cookie(response, "ws_sid").value


def encoded(value):
    return urllib.parse.quote(value, safe="/")


def response_cookie(response, name):
    cookies = http.cookies.SimpleCookie()
    for key, value in response.headers:
        if key.lower() == "set-cookie":
            cookies.load(value)
    return cookies[name]


class ShareContextContracts(unittest.TestCase):
    def test_project_share_dominates_single_in_either_cookie_order(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            for tokens in [("synthetic-project", "synthetic-single"),
                           ("synthetic-single", "synthetic-project")]:
                with self.subTest(tokens=tokens):
                    response = share_context(sandbox, tokens, path="/docs/sample/api/中文接口/")
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.body)
                    self.assertEqual(payload["scope"], "project")
                    self.assertEqual({doc["id"] for doc in payload["docs"]},
                        {"sample/api/中文接口", "sample/api/另一个接口", "sample/sql/迁移-测试"})

    def test_topic_share_dominates_single_in_either_cookie_order(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            for tokens in [("synthetic-topic", "synthetic-single"),
                           ("synthetic-single", "synthetic-topic")]:
                with self.subTest(tokens=tokens):
                    response = share_context(sandbox, tokens, path="/docs/sample/api/中文接口/")
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.body)
                    self.assertEqual(payload["scope"], "topic")
                    self.assertEqual(payload["topic"], "测试主题")
                    self.assertEqual({doc["id"] for doc in payload["docs"]},
                        {"sample/api/中文接口", "sample/api/另一个接口", "sample/sql/迁移-测试"})

    def test_explicit_project_type_context_keeps_its_type_filter(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            response = share_context(sandbox, ["synthetic-single", "synthetic-project-api"],
                                     project="sample", type="api")
            self.assertEqual(response.status, 200)
            payload = json.loads(response.body)
            self.assertEqual((payload["scope"], payload["project"], payload["type"]),
                             ("project", "sample", "api"))
            self.assertEqual({doc["id"] for doc in payload["docs"]},
                             {"sample/api/中文接口", "sample/api/另一个接口"})

    def test_single_without_a_covering_module_remains_single(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            response = share_context(sandbox, ["synthetic-single"], path="/docs/sample/api/中文接口/")
            self.assertEqual(response.status, 200)
            payload = json.loads(response.body)
            self.assertEqual(payload["scope"], "doc")
            self.assertEqual(payload["docs"], [])
            self.assertTrue(payload["search"])

    def test_noncovering_module_does_not_replace_an_outside_single(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            response = share_context(sandbox, ["synthetic-project", "synthetic-outside"],
                                     path="/docs/other/api/不共享/")
            self.assertEqual(response.status, 200)
            payload = json.loads(response.body)
            self.assertEqual(payload["scope"], "doc")
            self.assertEqual(payload["project"], "other")
            self.assertEqual(payload["docs"], [])

    def test_historical_single_is_not_promoted_to_a_current_module(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            response = share_context(sandbox, ["synthetic-project", "synthetic-history"],
                                     path="/docs/sample/api/中文接口-v1/")
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.body)["scope"], "doc")
            self.assertEqual(json.loads(response.body)["docs"], [])

    def test_module_alone_does_not_cover_an_explicitly_superseded_history(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            response = share_context(sandbox, ["synthetic-project"], path="/docs/sample/api/中文接口-v1/")
            self.assertEqual(response.status, 404)
            self.assertEqual(json.loads(response.body),
                             {"error": "当前浏览器没有覆盖此页面的分享", "grants": 1})

    def test_expired_or_revoked_module_does_not_override_a_live_single(self):
        shares = {**SHARES,
            "synthetic-expired-module": {"scope": "module", "project": "sample", "expires": time.time() - 60},
            "synthetic-revoked-module": {"scope": "module", "project": "sample", "expires": 4102444800,
                                         "revoked_at": 1700000000}}
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=shares) as sandbox:
            for module in ["synthetic-expired-module", "synthetic-revoked-module"]:
                with self.subTest(module=module):
                    response = share_context(sandbox, [module, "synthetic-single"],
                                             path="/docs/sample/api/中文接口/")
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.body)["scope"], "doc")
                    self.assertEqual(json.loads(response.body)["docs"], [])


class HTTPContracts(unittest.TestCase):
    def test_single_share_opens_only_its_document_and_referenced_attachment(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            owner = login_cookie(sandbox)
            created = post_json(sandbox, "/api/share",
                {"path": "/docs/sample/api/中文接口/", "expires": "2100-01-01T00:00:00+00:00"},
                {"Cookie": owner})
            self.assertEqual(created.status, 200)
            payload = json.loads(created.body)
            self.assertEqual(payload["scope"], "doc")
            self.assertEqual(payload["doc_count"], 1)
            consumed = sandbox.request("GET", "/s/" + payload["token"])
            self.assertEqual(consumed.status, 303)
            self.assertEqual(consumed.header("Location"), encoded("/docs/sample/api/中文接口/") + "?share=1")
            cookie = response_cookie(consumed, "ws_share")
            self.assertTrue(cookie["httponly"])
            self.assertEqual(cookie["samesite"], "Lax")
            visitor = "ws_share=" + cookie.value
            page = sandbox.request("GET", consumed.header("Location"), headers={"Cookie": visitor})
            self.assertEqual(page.status, 200)
            self.assertIn("合成中文接口页面".encode("utf-8"), page.body)
            attachment = sandbox.request("GET", encoded("/files/证据.txt"), headers={"Cookie": visitor})
            self.assertEqual(attachment.status, 200)
            self.assertEqual(attachment.body, "合成附件内容".encode("utf-8"))
            denied = sandbox.request("GET", "/files/unreferenced.txt", headers={"Cookie": visitor})
            self.assertEqual(denied.status, 200)
            self.assertIn("该分享不包含此附件".encode("utf-8"), denied.body)
            self.assertNotIn("合成未引用附件".encode("utf-8"), denied.body)
            outside = sandbox.request("GET", encoded("/docs/other/api/不共享/?share=1"),
                                      headers={"Cookie": visitor})
            self.assertEqual(outside.status, 200)
            self.assertIn("该分享不包含此内容".encode("utf-8"), outside.body)
            self.assertNotIn("合成不共享页面".encode("utf-8"), outside.body)

    def test_project_type_share_exposes_exact_current_api_documents(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            owner = login_cookie(sandbox)
            created = post_json(sandbox, "/api/share",
                {"scope": "module", "project": "sample", "type": "api",
                 "expires": "2100-01-01T00:00:00+00:00"}, {"Cookie": owner})
            self.assertEqual(created.status, 200)
            self.assertEqual(json.loads(created.body)["doc_count"], 2)
            consumed = sandbox.request("GET", "/s/" + json.loads(created.body)["token"])
            self.assertEqual(consumed.status, 303)
            self.assertEqual(consumed.header("Location"), "/projects/sample/?share=1&type=api")
            visitor = "ws_share=" + response_cookie(consumed, "ws_share").value
            context = sandbox.request("GET", "/api/share/ctx?project=sample&type=api",
                                      headers={"Cookie": visitor})
            self.assertEqual(context.status, 200)
            payload = json.loads(context.body)
            self.assertEqual(payload["scope"], "project")
            self.assertEqual(payload["type"], "api")
            self.assertEqual({d["id"] for d in payload["docs"]},
                             {"sample/api/中文接口", "sample/api/另一个接口"})

    def test_revoking_a_share_invalidates_a_previously_copied_visitor_cookie(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            owner = login_cookie(sandbox)
            created = post_json(sandbox, "/api/share",
                {"path": "/docs/sample/api/中文接口/", "expires": "2100-01-01T00:00:00+00:00"},
                {"Cookie": owner})
            self.assertEqual(created.status, 200)
            token = json.loads(created.body)["token"]
            consumed = sandbox.request("GET", "/s/" + token)
            visitor = "ws_share=" + response_cookie(consumed, "ws_share").value
            revoked = post_json(sandbox, "/api/shares/revoke", {"token": token}, {"Cookie": owner})
            self.assertEqual(revoked.status, 200)
            self.assertTrue(json.loads(revoked.body)["revoked"])
            denied = sandbox.request("GET", encoded("/docs/sample/api/中文接口/?share=1"),
                                     headers={"Cookie": visitor})
            self.assertEqual(denied.status, 200)
            self.assertIn("分享已被撤销".encode("utf-8"), denied.body)
            self.assertNotIn("合成中文接口页面".encode("utf-8"), denied.body)
            self.assertEqual(sandbox.request("GET", "/api/share/ctx?project=sample",
                                            headers={"Cookie": visitor}).status, 404)

    def test_expired_share_entry_returns_a_notice_not_document_content(self):
        shares = {"synthetic-expired-share": {"scope": "doc", "path": "/docs/sample/api/中文接口/",
                  "expires": time.time() - 60, "created": time.time() - 120}}
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=shares) as sandbox:
            response = sandbox.request("GET", "/s/synthetic-expired-share")
            self.assertEqual(response.status, 200)
            self.assertIn("分享已过期".encode("utf-8"), response.body)
            self.assertNotIn("合成中文接口页面".encode("utf-8"), response.body)
            self.assertIsNone(response.header("Set-Cookie"))

    def test_wrong_password_returns_the_login_error_without_a_session(self):
        with ServerSandbox() as sandbox:
            response = sandbox.request("POST", "/login", body="password=synthetic-wrong-password",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(response.status, 200)
            self.assertIn("密码不正确，请重试".encode("utf-8"), response.body)
            self.assertIsNone(response.header("Set-Cookie"))
            self.assertEqual(sandbox.request("GET", "/api/shares").status, 401)

    def test_login_rejects_an_external_next_url(self):
        with ServerSandbox() as sandbox:
            response = sandbox.request("POST", "/login",
                body=urllib.parse.urlencode({"password": TEST_PASSWORD,
                                             "next": "//synthetic.invalid/never-visit"}),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(response.status, 303)
            self.assertEqual(response.header("Location"), "/")

    def test_admin_context_requires_the_api_token_even_with_a_login_cookie(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            cookie = login_cookie(sandbox)
            for headers in [{}, {"X-API-Key": "synthetic-wrong-token"}, {"Cookie": cookie}]:
                with self.subTest(headers=list(headers)):
                    denied = sandbox.request("GET", "/api/admin/context", headers=headers)
                    self.assertEqual(denied.status, 401)
                    self.assertEqual(json.loads(denied.body), {"error": "API Key 无效"})
            allowed = sandbox.request("GET", "/api/admin/context",
                                      headers={"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(allowed.status, 200)
            self.assertEqual(json.loads(allowed.body)["count"], 5)
            self.assertEqual({p["name"] for p in json.loads(allowed.body)["projects"]},
                             {"sample", "other"})

    def test_admin_errors_are_json_and_do_not_mutate_owned_documents(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            before = {str(p.relative_to(sandbox.docs)): p.read_bytes()
                      for p in sandbox.docs.rglob("*") if p.is_file()}
            unknown = sandbox.request("GET", "/api/admin/not-a-route",
                                      headers={"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(unknown.status, 404)
            self.assertEqual(json.loads(unknown.body), {"error": "unknown admin endpoint"})
            self.assertEqual(unknown.header("Content-Type"), "application/json; charset=utf-8")
            invalid = sandbox.request("POST", "/api/admin/create", body=b"{}",
                headers={"X-API-Key": TEST_API_TOKEN, "Content-Type": "application/json"})
            self.assertEqual(invalid.status, 400)
            self.assertEqual(json.loads(invalid.body), {"error": "project 必填"})
            missing = sandbox.request("GET", "/api/admin/doc?id=missing/doc",
                                      headers={"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(missing.status, 404)
            self.assertEqual(json.loads(missing.body), {"error": "文档不存在: missing/doc"})
            after = {str(p.relative_to(sandbox.docs)): p.read_bytes()
                     for p in sandbox.docs.rglob("*") if p.is_file()}
            self.assertEqual(after, before)

    def test_unauthenticated_existing_document_redirects_to_login(self):
        with ServerSandbox(dist=PAGES) as sandbox:
            response = sandbox.request("GET", encoded("/docs/sample/api/中文接口/"))
            self.assertEqual(response.status, 303)
            location = dict(response.headers)["Location"]
            self.assertTrue(location.startswith("/login?next="))
            self.assertNotIn("合成中文接口页面".encode("utf-8"), response.body)

    def test_chinese_document_and_attachment_download_urls_round_trip(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            cookie = login_cookie(sandbox)
            page = sandbox.request("GET", encoded("/docs/sample/api/中文接口/"),
                                   headers={"Cookie": cookie})
            self.assertEqual(page.status, 200)
            self.assertIn("合成中文接口页面".encode("utf-8"), page.body)
            download = sandbox.request("GET", encoded("/files/证据.txt"),
                                       headers={"Cookie": cookie})
            self.assertEqual(download.status, 200)
            self.assertEqual(download.body, "合成附件内容".encode("utf-8"))
            self.assertTrue(download.header("Content-Type").startswith("text/plain"))

    def test_public_font_and_icons_are_accessible_without_login(self):
        assets = {"fonts/synthetic.woff2": b"synthetic-font-bytes",
                  "favicon.svg": "<svg>synthetic-favicon</svg>",
                  "logo.svg": "<svg>synthetic-logo</svg>"}
        with ServerSandbox(dist=assets) as sandbox:
            for target, body in [("/fonts/synthetic.woff2", b"synthetic-font-bytes"),
                                 ("/favicon.svg", b"<svg>synthetic-favicon</svg>"),
                                 ("/logo.svg", b"<svg>synthetic-logo</svg>")]:
                with self.subTest(target=target):
                    response = sandbox.request("GET", target)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.body, body)

    def test_encoded_parameter_segments_cannot_escape_static_access_scope(self):
        private = b"synthetic-private-parameter-traversal-canary"
        shared = b"synthetic-shared-parameter-page"
        font = b"synthetic-public-parameter-font"
        shares = {"synthetic-parameter-single": {"scope": "doc",
            "path": "/docs/public/doc/", "expires": 4102444800}}
        with ServerSandbox(shares=shares, dist={
            "docs/public/doc/index.html": shared,
            "docs/private/index.html": private,
            "fonts/allowed.woff2": font,
        }) as sandbox:
            cookie = {"Cookie": "ws_share=synthetic-parameter-single"}
            allowed_font = sandbox.request("GET", "/fonts/allowed.woff2")
            self.assertEqual((allowed_font.status, allowed_font.body), (200, font))
            allowed_doc = sandbox.request("GET", "/docs/public/doc/?share=1", headers=cookie)
            self.assertEqual((allowed_doc.status, allowed_doc.body), (200, shared))
            cases = [
                ("/fonts/x;%2f..%2f..%2fdocs%2fprivate%2findex.html", {}),
                ("/docs/public/doc/x;%2f..%2f..%2f..%2fprivate%2findex.html?share=1", cookie),
            ]
            for target, headers in cases:
                with self.subTest(target=target):
                    denied = sandbox.request("GET", target, headers=headers)
                    self.assertNotIn(private, denied.body)
                    self.assertIn(denied.status, {200, 303, 401, 403, 404})
                    if denied.status == 200:
                        self.assertIn("该分享不包含此内容".encode("utf-8"), denied.body)

    def test_dot_segment_and_encoded_separator_variants_cannot_escape_static_scope(self):
        private = b"synthetic-private-path-variant-canary"
        shared = b"synthetic-shared-path-variant-page"
        font = b"synthetic-public-path-variant-font"
        shares = {"synthetic-path-variant-single": {"scope": "doc",
            "path": "/docs/public/doc/", "expires": 4102444800}}
        with ServerSandbox(shares=shares, dist={
            "docs/public/doc/index.html": shared,
            "docs/private/index.html": private,
            "fonts/allowed.woff2": font,
        }) as sandbox:
            cookie = {"Cookie": "ws_share=synthetic-path-variant-single"}
            allowed_font = sandbox.request("GET", "/fonts/allowed.woff2")
            self.assertEqual((allowed_font.status, allowed_font.body), (200, font))
            allowed_doc = sandbox.request("GET", "/docs/public/doc/?share=1", headers=cookie)
            self.assertEqual((allowed_doc.status, allowed_doc.body), (200, shared))
            font_targets = [
                "/fonts/%2E%2E/docs/private/index.html",
                "/fonts/.%2e/docs/private/index.html",
                "/fonts/%2e./docs/private/index.html",
                "/fonts/../docs/private/index.html",
                "/fonts/%2e%2e%2fdocs%2fprivate%2findex.html",
                "/fonts/%2e%2e%2Fdocs%2Fprivate%2Findex.html",
                "/fonts//%2e%2e//docs//private/index.html",
                "/fonts/%2e%2e/c:%5cignored/docs/private/index.html",
            ]
            share_targets = [
                "/docs/public/doc/%2E%2E/%2E%2E/private/index.html?share=1",
                "/docs/public/doc/.%2e/.%2e/private/index.html?share=1",
                "/docs/public/doc/%2e./%2e./private/index.html?share=1",
                "/docs/public/doc/../../private/index.html?share=1",
                "/docs/public/doc/%2e%2e%2f%2e%2e%2fprivate%2findex.html?share=1",
                "/docs/public/doc/%2e%2e%2F%2e%2e%2Fprivate%2Findex.html?share=1",
                "/docs/public/doc//%2e%2e//%2e%2e//private/index.html?share=1",
                "/docs/public/doc/%2e%2e/%2e%2e/c:%5cignored/private/index.html?share=1",
            ]
            cases = [(target, {}) for target in font_targets]
            cases += [(target, cookie) for target in share_targets]
            for target, headers in cases:
                with self.subTest(target=target):
                    denied = sandbox.request("GET", target, headers=headers)
                    self.assertNotIn(private, denied.body)
                    self.assertIn(denied.status, {200, 303, 401, 403, 404})
                    if denied.status == 200:
                        self.assertIn("该分享不包含此内容".encode("utf-8"), denied.body)

    def test_normalized_static_access_preserves_fonts_and_single_share_boundaries(self):
        private = b"synthetic-private-compatibility-canary"
        font = b"synthetic-public-normalized-font"
        percent_font = b"synthetic-public-literal-percent-font"
        shared = b"synthetic-shared-normalized-page"
        chart = b"synthetic-shared-normalized-chart"
        shares = {"synthetic-normalized-single": {"scope": "doc",
            "path": "/docs/public/doc/", "expires": 4102444800}}
        with ServerSandbox(shares=shares, dist={
            "fonts/allowed.woff2": font,
            "fonts/nested/release..woff2": font,
            "fonts/%2e%2e/allowed.woff2": percent_font,
            "fonts/%2f/encoded.woff2": percent_font,
            "fonts/encoded.woff2": font,
            "allowed.woff2": private,
            "fonts-extra/private.woff2": private,
            "docs/public/doc/index.html": shared,
            "docs/public/doc/assets/chart.svg": chart,
            "docs/public/doc-extra/index.html": private,
            "docs/private/index.html": private,
        }) as sandbox:
            cookie = {"Cookie": "ws_share=synthetic-normalized-single"}
            font_cases = [
                ("/fonts/nested/release..woff2", font),
                ("/fonts/%252e%252e/allowed.woff2", percent_font),
                ("/fonts/%252f/encoded.woff2", percent_font),
            ]
            for target, body in font_cases:
                with self.subTest(target=target):
                    response = sandbox.request("GET", target)
                    self.assertEqual((response.status, response.body), (200, body))
                    self.assertNotIn(private, response.body)
            for target, label in [("/fonts/", b"allowed.woff2"),
                                  ("/fonts/nested/", b"release..woff2")]:
                with self.subTest(target=target):
                    response = sandbox.request("GET", target)
                    self.assertEqual(response.status, 200)
                    self.assertIn(label, response.body)
                    self.assertNotIn(private, response.body)
            redirect = sandbox.request("GET", "/docs/public/doc/", headers=cookie)
            self.assertEqual(redirect.status, 303)
            self.assertEqual(redirect.header("Location"), "/docs/public/doc/?share=1")
            page = sandbox.request("GET", redirect.header("Location"), headers=cookie)
            self.assertEqual((page.status, page.body), (200, shared))
            asset = sandbox.request("GET", "/docs/public/doc/assets/chart.svg", headers=cookie)
            self.assertEqual((asset.status, asset.body), (200, chart))
            platform_cases = [("/fonts/a%5cb/allowed.woff2", {}, font),
                ("/docs/public/doc/a%5cb/index.html?share=1", cookie, shared)]
            for target, headers, body in platform_cases:
                with self.subTest(target=target, platform=os.name):
                    response = sandbox.request("GET", target, headers=headers)
                    if os.name == "nt":
                        self.assertEqual((response.status, response.body), (200, body))
                    else:
                        self.assertEqual(response.status, 404)
                    self.assertNotIn(private, response.body)
            sibling_font = sandbox.request("GET", "/fonts-extra/private.woff2")
            self.assertEqual(sibling_font.status, 303)
            self.assertNotIn(private, sibling_font.body)
            sibling_doc = sandbox.request("GET", "/docs/public/doc-extra/index.html?share=1",
                                          headers=cookie)
            self.assertEqual(sibling_doc.status, 200)
            self.assertIn("该分享不包含此内容".encode("utf-8"), sibling_doc.body)
            self.assertNotIn(private, sibling_doc.body)

    def test_password_form_login_sets_a_secure_session_cookie(self):
        with ServerSandbox() as sandbox:
            response = sandbox.request("POST", "/login",
                body=urllib.parse.urlencode({"password": TEST_PASSWORD,
                                             "next": "/docs/private/"}),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(response.status, 303)
            self.assertIn(("Location", "/docs/private/"), response.headers)
            cookie = response_cookie(response, "ws_sid")
            self.assertTrue(cookie.value)
            self.assertTrue(cookie["httponly"])
            self.assertEqual(cookie["samesite"], "Lax")
            self.assertEqual(cookie["path"], "/")
            self.assertEqual(cookie["max-age"], "604800")
            self.assertEqual(sandbox.request("GET", "/api/shares",
                headers={"Cookie": "ws_sid=" + cookie.value}).status, 200)

    def test_logout_revokes_only_the_session_that_was_presented(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            first = login_cookie(sandbox)
            second = login_cookie(sandbox)
            self.assertNotEqual(first, second)
            for cookie in (first, second):
                self.assertEqual(sandbox.request("GET", "/api/shares",
                    headers={"Cookie": cookie}).status, 200)
            logout = sandbox.request("GET", "/logout", headers={"Cookie": first})
            self.assertEqual(logout.status, 303)
            self.assertEqual(logout.header("Location"), "/login")
            revoked = sandbox.request("GET", "/api/shares", headers={"Cookie": first})
            self.assertEqual(revoked.status, 401)
            self.assertEqual(json.loads(revoked.body), {"error": "未登录"})
            page = encoded("/docs/sample/api/中文接口/")
            revoked_page = sandbox.request("GET", page, headers={"Cookie": first})
            self.assertEqual(revoked_page.status, 303)
            self.assertIn("/login", revoked_page.header("Location"))
            self.assertNotIn("合成中文接口页面".encode("utf-8"), revoked_page.body)
            survivor = sandbox.request("GET", "/api/shares", headers={"Cookie": second})
            self.assertEqual(survivor.status, 200)
            survivor_page = sandbox.request("GET", page, headers={"Cookie": second})
            self.assertEqual(survivor_page.status, 200)
            self.assertIn("合成中文接口页面".encode("utf-8"), survivor_page.body)

    def test_create_rejects_absolute_project_domain_and_filename_without_writing(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            before = {str(p.relative_to(sandbox.docs)): p.read_bytes()
                      for p in sandbox.docs.rglob("*") if p.is_file()}
            for field in ["project", "domain", "filename"]:
                with self.subTest(field=field):
                    escaped = sandbox.root / ("synthetic-rejected-" + field)
                    payload = {"project": "synthetic-project", "topic": "合成拒绝测试",
                               "type": "sql", "title": "合成拒绝测试",
                               "filename": "canary.sql", "content": "SELECT 4242;"}
                    payload[field] = str(escaped)
                    response = post_json(sandbox, "/api/admin/create", payload,
                                         {"X-API-Key": TEST_API_TOKEN})
                    self.assertEqual(response.status, 400)
                    self.assertEqual(json.loads(response.body),
                                     {"error": field + " 必须是文档库内的相对路径"})
                    self.assertFalse(escaped.exists(),
                                     "A rejected create must not write outside DOCS")
            after = {str(p.relative_to(sandbox.docs)): p.read_bytes()
                     for p in sandbox.docs.rglob("*") if p.is_file()}
            self.assertEqual(after, before, "A rejected create must not mutate DOCS")

    def test_create_rejects_dot_segments_and_drive_qualified_segments(self):
        cases = [("project", "sample/../../synthetic-escape", "不能包含上级目录或点路径段"),
                 ("project", "..", "不能包含上级目录或点路径段"),
                 ("project", "sample/./nested", "不能包含上级目录或点路径段"),
                 ("domain", "sample\\..\\synthetic-escape", "不能包含上级目录或点路径段"),
                 ("filename", "..\\canary.sql", "不能包含上级目录或点路径段"),
                 ("filename", "C:\\synthetic-escape\\canary.sql", "必须是文档库内的相对路径"),
                 ("project", "\\\\server\\share", "必须是文档库内的相对路径"),
                 ("domain", "/synthetic-escape", "必须是文档库内的相对路径")]
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            for field, value, reason in cases:
                with self.subTest(field=field, value=value):
                    payload = {"project": "synthetic-project", "topic": "合成拒绝测试",
                               "type": "sql", "title": "合成拒绝测试",
                               "filename": "canary.sql", "content": "SELECT 4242;"}
                    payload[field] = value
                    response = post_json(sandbox, "/api/admin/create", payload,
                                         {"X-API-Key": TEST_API_TOKEN})
                    self.assertEqual(response.status, 400)
                    self.assertEqual(json.loads(response.body), {"error": field + " " + reason})
            self.assertFalse((sandbox.root / "synthetic-escape").exists())
            self.assertFalse((sandbox.docs.parent / "synthetic-escape").exists())

    def test_create_accepts_relative_nested_segments_inside_docs(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            payload = {"project": "sample", "domain": "合成子域", "type": "sql",
                       "topic": "合成主题", "title": "合成迁移",
                       "filename": "合成-迁移.sql", "content": "SELECT 1;"}
            response = post_json(sandbox, "/api/admin/create", payload,
                                 {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 200, response.body)
            body = json.loads(response.body)
            self.assertTrue(body["ok"])
            self.assertTrue(body["built"])
            self.assertEqual(body["path"], "sample/合成子域/sql/合成-迁移.sql")
            created = sandbox.docs / "sample" / "合成子域" / "sql" / "合成-迁移.sql"
            self.assertTrue(created.is_file())
            self.assertIn("SELECT 1;", created.read_text(encoding="utf-8"))

    def test_logout_without_a_session_cookie_still_redirects_safely(self):
        with ServerSandbox() as sandbox:
            for headers in ({}, {"Cookie": "ws_sid=synthetic-stale-session"}):
                with self.subTest(headers=list(headers)):
                    logout = sandbox.request("GET", "/logout", headers=headers)
                    self.assertEqual(logout.status, 303)
                    self.assertEqual(logout.header("Location"), "/login")
                    self.assertIn("Max-Age=0", logout.header("Set-Cookie"))

    def _doc_snapshot(self, sandbox):
        return {str(p.relative_to(sandbox.docs)): p.read_bytes()
                for p in sandbox.docs.rglob("*") if p.is_file()}

    def _admin_doc(self, sandbox, doc_id):
        path = "/api/admin/doc?" + urllib.parse.urlencode({"id": doc_id})
        return sandbox.request("GET", path, headers={"X-API-Key": TEST_API_TOKEN})

    def test_bulk_meta_rejects_the_whole_batch_when_a_later_id_is_unknown(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            before = self._doc_snapshot(sandbox)
            first = json.loads(self._admin_doc(sandbox, "sample/api/中文接口").body)
            response = post_json(sandbox, "/api/admin/meta",
                {"ids": ["sample/api/中文接口", "synthetic-missing/doc"],
                 "topic": "合成不应落盘主题"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 404)
            self.assertEqual(json.loads(response.body),
                             {"error": "文档不存在: synthetic-missing/doc"})
            self.assertEqual(self._doc_snapshot(sandbox), before,
                             "A rejected batch must leave every document byte-identical")
            self.assertEqual(json.loads(self._admin_doc(sandbox, "sample/api/中文接口").body), first)

    def test_bulk_meta_rejects_the_whole_batch_when_a_later_status_does_not_fit(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            before = self._doc_snapshot(sandbox)
            first = json.loads(self._admin_doc(sandbox, "sample/api/中文接口").body)
            response = post_json(sandbox, "/api/admin/meta",
                {"ids": ["sample/api/中文接口", "sample/sql/迁移-测试"],
                 "type": "api"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.body), {"error":
                "sample/sql/迁移-测试 当前状态 executed 不适用于类型 api，请先切换状态"})
            self.assertEqual(self._doc_snapshot(sandbox), before,
                             "A later invalid status must not let the first document change")
            self.assertEqual(json.loads(self._admin_doc(sandbox, "sample/api/中文接口").body), first)

    def test_bulk_meta_updates_every_listed_document_when_all_targets_are_valid(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            response = post_json(sandbox, "/api/admin/meta",
                {"ids": ["sample/api/中文接口", "sample/api/另一个接口"],
                 "topic": "合成批量主题"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 200, response.body)
            body = json.loads(response.body)
            self.assertEqual(body["ids"], ["sample/api/中文接口", "sample/api/另一个接口"])
            self.assertEqual(body["count"], 2)
            self.assertEqual(body["updated"], {"topic": "合成批量主题"})
            for doc_id in ["sample/api/中文接口", "sample/api/另一个接口"]:
                with self.subTest(doc_id=doc_id):
                    readback = json.loads(self._admin_doc(sandbox, doc_id).body)
                    self.assertEqual(readback["meta"]["topic"], "合成批量主题")
                    path = sandbox.docs / (doc_id + ".md")
                    self.assertIn('topic: "合成批量主题"', path.read_text(encoding="utf-8"))

    def test_failed_git_add_is_reported_as_an_error_not_as_a_commit(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, faults=("git-add",)) as sandbox:
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成添加失败", "filename": "synthetic-add-fault.sql",
                 "content": "SELECT 606;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 500, response.body)
            body = json.loads(response.body)
            self.assertNotIn("ok", body)
            self.assertNotIn("built", body)
            self.assertNotIn("commit", body)
            self.assertEqual(body["error"], "Git add 失败：改动已写入文档库并完成构建，但未提交")
            self.assertEqual(body["detail"], "synthetic git add failure")

    def test_failed_git_add_leaves_the_written_document_readable_without_a_commit(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, faults=("git-add",)) as sandbox:
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成添加失败", "filename": "synthetic-add-fault.sql",
                 "content": "SELECT 606;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 500, response.body)
            created = sandbox.docs / "synthetic-project/sql/synthetic-add-fault.sql"
            self.assertTrue(created.is_file())
            self.assertIn("SELECT 606;", created.read_text(encoding="utf-8"))
            readback = self._admin_doc(sandbox, "synthetic-project/sql/synthetic-add-fault")
            self.assertEqual(readback.status, 200)
            self.assertEqual(json.loads(readback.body)["body"].strip(), "SELECT 606;")

    def test_successful_write_still_reports_a_real_commit(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成添加成功", "filename": "synthetic-add-ok.sql",
                 "content": "SELECT 600;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 200, response.body)
            body = json.loads(response.body)
            self.assertTrue(body["ok"])
            self.assertTrue(body["built"])
            self.assertEqual(body["commit"], "synthetic-baseline-commit")

    def test_failed_git_commit_is_reported_as_an_error_not_as_a_commit(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, faults=("git-commit",)) as sandbox:
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成提交失败", "filename": "synthetic-commit-fault.sql",
                 "content": "SELECT 707;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 500, response.body)
            body = json.loads(response.body)
            self.assertNotIn("ok", body)
            self.assertNotIn("built", body)
            self.assertNotIn("commit", body)
            self.assertEqual(body["error"], "Git commit 失败：改动已写入文档库并完成构建，但未提交")
            self.assertEqual(body["detail"], "synthetic git commit failure")

    def test_failed_git_commit_leaves_the_written_document_readable_without_a_commit(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, faults=("git-commit",)) as sandbox:
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成提交失败", "filename": "synthetic-commit-fault.sql",
                 "content": "SELECT 707;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 500, response.body)
            created = sandbox.docs / "synthetic-project/sql/synthetic-commit-fault.sql"
            self.assertTrue(created.is_file())
            self.assertIn("SELECT 707;", created.read_text(encoding="utf-8"))
            readback = self._admin_doc(sandbox, "synthetic-project/sql/synthetic-commit-fault")
            self.assertEqual(readback.status, 200)
            self.assertEqual(json.loads(readback.body)["body"].strip(), "SELECT 707;")

    def test_a_commit_failure_is_reported_once_and_the_next_write_still_commits(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, faults=("git-commit",)) as sandbox:
            failed = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成提交失败", "filename": "synthetic-commit-fault.sql",
                 "content": "SELECT 707;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(failed.status, 500, failed.body)
            recovered = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成提交恢复", "filename": "synthetic-commit-ok.sql",
                 "content": "SELECT 708;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(recovered.status, 200, recovered.body)
            body = json.loads(recovered.body)
            self.assertTrue(body["ok"])
            self.assertTrue(body["built"])
            self.assertEqual(body["commit"], "synthetic-baseline-commit")


    def test_failed_build_removes_the_new_untracked_document_and_its_directories(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, faults=("build",)) as sandbox:
            before = self._doc_snapshot(sandbox)
            target = sandbox.docs / "synthetic-project/sql/synthetic-build-fault.sql"
            self.assertFalse(target.exists())
            response = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成构建失败", "filename": "synthetic-build-fault.sql",
                 "content": "SELECT 808;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 500, response.body)
            body = json.loads(response.body)
            self.assertEqual(body["error"], "构建失败，已回滚（改动已存入 build-failed-rescue.patch）")
            self.assertEqual(body["detail"], "synthetic build failure")
            self.assertFalse(target.exists(), "Rollback must delete the new untracked document")
            self.assertFalse((sandbox.docs / "synthetic-project").exists(),
                             "Rollback must remove the directories the failed write created")
            self.assertEqual(self._doc_snapshot(sandbox), before,
                             "Rollback must leave every pre-existing document byte-identical")
            readback = self._admin_doc(sandbox, "synthetic-project/sql/synthetic-build-fault")
            self.assertEqual(readback.status, 404)

    def test_failed_build_restores_existing_documents_instead_of_deleting_them(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, faults=("build",)) as sandbox:
            target = sandbox.docs / "sample/api/中文接口.md"
            original = target.read_bytes()
            response = post_json(sandbox, "/api/admin/meta",
                {"ids": ["sample/api/中文接口"], "topic": "合成回滚主题"},
                {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(response.status, 500, response.body)
            self.assertTrue(target.is_file(),
                            "Rollback must not delete a document that existed before the write")
            self.assertEqual(target.read_bytes(), original)
            readback = json.loads(self._admin_doc(sandbox, "sample/api/中文接口").body)
            self.assertEqual(readback["meta"]["topic"], "测试主题")

    def test_a_build_failure_is_reported_once_and_the_next_write_still_commits(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, faults=("build",)) as sandbox:
            failed = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成构建失败", "filename": "synthetic-build-fault.sql",
                 "content": "SELECT 808;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(failed.status, 500, failed.body)
            recovered = post_json(sandbox, "/api/admin/create",
                {"project": "synthetic-project", "topic": "合成写入测试", "type": "sql",
                 "title": "合成构建恢复", "filename": "synthetic-build-ok.sql",
                 "content": "SELECT 809;"}, {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(recovered.status, 200, recovered.body)
            body = json.loads(recovered.body)
            self.assertTrue(body["ok"])
            self.assertTrue(body["built"])
            self.assertEqual(body["commit"], "synthetic-baseline-commit")
            self.assertFalse((sandbox.docs / "synthetic-project/sql/synthetic-build-fault.sql").exists())

    def test_directory_link_inside_a_public_prefix_cannot_reach_outside_files(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES,
                           links={"app/dist/fonts/leak": "outside"}) as sandbox:
            (sandbox.root / "outside/secret.txt").write_text("outside-canary", encoding="utf-8")
            leaked = sandbox.request("GET", "/fonts/leak/secret.txt")
            self.assertEqual(leaked.status, 404, leaked.body)
            self.assertNotIn(b"outside-canary", leaked.body)

    def test_directory_link_inside_a_public_prefix_cannot_reach_private_documents(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES,
                           links={"app/dist/fonts/docsleak": "docs"}) as sandbox:
            leaked = sandbox.request("GET", encoded("/fonts/docsleak/sample/api/中文接口.md"))
            self.assertEqual(leaked.status, 404, leaked.body)
            self.assertNotIn("这是独立的合成接口正文".encode("utf-8"), leaked.body)

    def test_directory_link_is_not_listed_even_inside_a_public_prefix(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES,
                           links={"app/dist/fonts/leak": "outside"}) as sandbox:
            (sandbox.root / "outside/secret.txt").write_text("outside-canary", encoding="utf-8")
            listing = sandbox.request("GET", "/fonts/leak/")
            self.assertEqual(listing.status, 404, listing.body)
            self.assertNotIn(b"secret.txt", listing.body)

    def test_head_requires_the_same_authorization_as_get(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            get_denied = sandbox.request("GET", "/projects/sample/index.html")
            head_denied = sandbox.request("HEAD", "/projects/sample/index.html")
            self.assertEqual(get_denied.status, 303, get_denied.body)
            self.assertEqual(head_denied.status, 303)
            self.assertEqual(head_denied.header("Location"), get_denied.header("Location"))
            self.assertEqual(head_denied.body, b"")
            public = sandbox.request("HEAD", "/fonts/synthetic.woff2")
            self.assertEqual(public.status, 200)
            self.assertEqual(public.header("Content-Length"), "20")

    def test_head_under_a_share_mirrors_the_get_status_and_length(self):
        cookie = {"Cookie": "ws_share=synthetic-single"}
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            for target in [encoded("/docs/sample/api/中文接口/?share=1"),
                           encoded("/docs/other/api/不共享/?share=1")]:
                get_response = sandbox.request("GET", target, headers=cookie)
                head_response = sandbox.request("HEAD", target, headers=cookie)
                self.assertEqual(head_response.status, get_response.status, target)
                self.assertEqual(head_response.header("Content-Length"),
                                 str(len(get_response.body)), target)
                self.assertEqual(head_response.body, b"", target)
    def test_login_next_falls_back_to_the_site_root_when_the_browser_would_leave_the_site(self):
        hostile = ["/\\evil.example", "/\t/evil.example", "/\\/evil.example",
                   "/\r\n/evil.example", "//evil.example"]
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            for raw in hostile:
                with self.subTest(next_value=raw):
                    response = sandbox.request("POST", "/login",
                        body=urllib.parse.urlencode({"password": TEST_PASSWORD, "next": raw}).encode("utf-8"),
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
                    self.assertEqual(response.status, 303)
                    self.assertEqual(response.header("Location"), "/")

    def test_login_next_keeps_legitimate_internal_paths(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            for raw in ["/", "/docs/sample/api/中文接口/", "/pending/?tab=1"]:
                with self.subTest(next_value=raw):
                    response = sandbox.request("POST", "/login",
                        body=urllib.parse.urlencode({"password": TEST_PASSWORD, "next": raw}).encode("utf-8"),
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
                    self.assertEqual(response.status, 303)
                    # 头部只能是 latin-1：中文目标按 URL 规则转义后仍指向同一站内路径。
                    expected = urllib.parse.quote(raw, safe="/?&=#%:+~")
                    self.assertEqual(response.header("Location"), expected)
                    self.assertEqual(urllib.parse.unquote(expected), raw)

    def test_login_page_still_renders_a_rejected_next_without_becoming_a_redirect(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            response = sandbox.request("GET", "/login?next=" + urllib.parse.quote("/\\evil.example"))
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.header("Location"))
            self.assertIn(b'name="next"', response.body)
    def test_malformed_content_length_is_rejected_without_dropping_the_connection(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            for raw in ["abc", "12x", "", "  "]:
                with self.subTest(content_length=raw):
                    response = sandbox.request("POST", "/login", body=b"",
                                               headers={"Content-Length": raw})
                    self.assertEqual(response.status, 400, response.body)
                    self.assertEqual(json.loads(response.body)["error"], "Content-Length 非法")
            # 同一连接复用不应被上一个坏请求污染：坏请求之后仍能正常登录。
            ok = sandbox.request("POST", "/login",
                body=urllib.parse.urlencode({"password": TEST_PASSWORD}).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(ok.status, 303)

    def test_negative_or_oversized_content_length_is_refused_without_blocking(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            for raw in ["-1", "-9999", "999999999", str(17 * 1024 * 1024)]:
                with self.subTest(content_length=raw):
                    response = sandbox.request("POST", "/login", body=b"",
                                               headers={"Content-Length": raw})
                    self.assertEqual(response.status, 413, response.body)
                    self.assertEqual(json.loads(response.body)["error"], "请求体过大或长度非法")

    def test_a_normal_post_body_is_still_read_and_authorized(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            ok = sandbox.request("POST", "/login",
                body=urllib.parse.urlencode({"password": TEST_PASSWORD}).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(ok.status, 303)
            self.assertIsNotNone(response_cookie(ok, "ws_sid").value)
            empty = sandbox.request("POST", "/login", body=b"")
            self.assertEqual(empty.status, 200)

    def test_head_mirrors_get_status_and_headers_on_dynamic_routes(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            for target in ["/login", "/api/shares", "/api/status-options?type=api",
                           "/api/share/ctx", "/api/admin/pending",
                           "/s/" + "synthetic-single", "/s/synthetic-missing-token"]:
                with self.subTest(target=target):
                    got = sandbox.request("GET", target)
                    head = sandbox.request("HEAD", target)
                    self.assertEqual(head.status, got.status, target)
                    self.assertEqual(head.header("Location"), got.header("Location"), target)
                    self.assertEqual(head.header("Content-Length"),
                                     got.header("Content-Length"), target)
                    self.assertEqual(head.body, b"", target)

    def test_head_on_the_login_page_is_not_a_redirect_loop(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            head = sandbox.request("HEAD", "/login")
            self.assertEqual(head.status, 200, head.header("Location"))
            self.assertIsNone(head.header("Location"))
            self.assertEqual(head.body, b"")
            # 旧实现会 303 到 /login?next=/login，再套一层变成 ?next=...%3Fnext%3D/login。
            again = sandbox.request("GET", "/login?next=/login")
            self.assertEqual(again.status, 200)
            self.assertIsNone(again.header("Location"))

    def test_head_mirrors_get_on_authenticated_and_api_key_routes(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES, shares=SHARES) as sandbox:
            cookie = login_cookie(sandbox)
            key = {"X-API-Key": TEST_API_TOKEN}
            for target, headers in [("/api/shares", {"Cookie": cookie}),
                                    ("/api/status-options?type=api", {"Cookie": cookie}),
                                    ("/api/module-docs?project=sample", {"Cookie": cookie}),
                                    ("/api/admin/pending", key),
                                    ("/api/admin/context?project=sample", key)]:
                with self.subTest(target=target):
                    got = sandbox.request("GET", target, headers=headers)
                    head = sandbox.request("HEAD", target, headers=headers)
                    self.assertEqual(got.status, 200, target)
                    self.assertEqual(head.status, got.status, target)
                    self.assertEqual(head.header("Content-Length"),
                                     got.header("Content-Length"), target)
                    self.assertEqual(head.body, b"", target)
    def test_non_ascii_api_key_is_rejected_with_401_instead_of_dropping_the_connection(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            # 请求头可以携带任意非 ASCII 字节；旧实现把它们交给 secrets.compare_digest
            # 比较 str，会抛 TypeError 并让线程直接断开连接，连 401 都拿不到。
            for label, raw_key in [("latin-1", b"k" + bytes([0xE9]) + b"y"),
                                   ("utf-8 chinese", bytes([0xE5, 0xAF, 0x86, 0xE9, 0x92, 0xA5])),
                                   ("raw high byte", bytes([0x80, 0x80]))]:
                with self.subTest(label=label):
                    got = sandbox.request("GET", "/api/admin/pending",
                                          headers={"X-API-Key": raw_key})
                    self.assertEqual(got.status, 401, label)
                    self.assertEqual(json.loads(got.body.decode("utf-8"))["error"], "API Key 无效")

    def test_non_ascii_api_key_is_rejected_on_every_admin_route(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            bad = {"X-API-Key": bytes([0xFF, 0xFE])}
            doc_id = "sample/api/中文接口"
            targets = [("GET", "/api/admin/pending", None),
                       ("GET", "/api/admin/doc?id=" + encoded(doc_id), None),
                       ("POST", "/api/admin/status", {"id": doc_id, "status": "pending"}),
                       ("POST", "/api/admin/create", {"project": "synthetic-project",
                                                      "topic": "合成主题",
                                                      "type": "sql",
                                                      "title": "合成文档",
                                                      "content": "SELECT 1;"})]
            for method, target, payload in targets:
                with self.subTest(target=target):
                    if method == "GET":
                        got = sandbox.request("GET", target, headers=bad)
                    else:
                        got = sandbox.request("POST", target,
                                              body=json.dumps(payload).encode("utf-8"),
                                              headers={"Content-Type": "application/json", **bad})
                    self.assertEqual(got.status, 401, target)
            # 非 ASCII 尝试之后服务仍然可用，且合法 Key 依旧能通过。
            ok = sandbox.request("GET", "/api/admin/pending", headers={"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(ok.status, 200)
            self.assertEqual(json.loads(ok.body.decode("utf-8"))["count"], 0)

    def test_non_ascii_api_key_does_not_leak_internal_error_text(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            got = sandbox.request("GET", "/api/admin/pending",
                                  headers={"X-API-Key": bytes([0xE9])})
            body = got.body.decode("utf-8")
            self.assertNotIn("TypeError", body)
            self.assertNotIn("compare_digest", body)
            self.assertNotIn("Traceback", body)
    def test_create_rejects_a_non_numeric_version_before_writing(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            # 版本号是请求参数：非法值必须在写盘前按 400 拒绝，不能掉进通用异常
            # 分支变成 500，更不能把内部异常文本回显出去。
            before = self._doc_snapshot(sandbox)
            bad_values = ["abc", "1.5", "-2", "0", "", "  x  ", [], {"a": 1}]
            for index, bad in enumerate(bad_values):
                with self.subTest(value=repr(bad)):
                    payload = {"project": "synthetic-project", "topic": "合成主题",
                               "type": "sql", "title": "合成版本%d" % index,
                               "content": "SELECT 1;", "version": bad}
                    got = post_json(sandbox, "/api/admin/create", payload,
                                    {"X-API-Key": TEST_API_TOKEN})
                    self.assertEqual(got.status, 400, repr(bad))
                    body = json.loads(got.body.decode("utf-8"))
                    self.assertEqual(body.get("error"), "version 必须是正整数")
                    self.assertNotIn("内部错误", json.dumps(body, ensure_ascii=False))
                    self.assertNotIn("invalid literal", json.dumps(body, ensure_ascii=False))
            self.assertEqual(self._doc_snapshot(sandbox), before)

    def test_version_endpoint_rejects_a_non_numeric_explicit_version(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            before = self._doc_snapshot(sandbox)
            payload = {"id": "sample/sql/迁移-测试", "content": "SELECT 9;",
                       "changes": ["合成变更"], "version": "abc"}
            got = post_json(sandbox, "/api/admin/version", payload,
                            {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(got.status, 400)
            body = json.loads(got.body.decode("utf-8"))
            self.assertEqual(body.get("error"), "version 必须是正整数")
            self.assertEqual(self._doc_snapshot(sandbox), before)

    def test_create_accepts_a_positive_integer_version(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            payload = {"project": "synthetic-project", "topic": "合成主题",
                       "type": "sql", "title": "合成版本号",
                       "content": "SELECT 1;", "version": 2}
            got = post_json(sandbox, "/api/admin/create", payload,
                            {"X-API-Key": TEST_API_TOKEN})
            self.assertEqual(got.status, 200)
            body = json.loads(got.body.decode("utf-8"))
            self.assertTrue(body.get("ok") and body.get("built"))
            text = (sandbox.docs / body["path"]).read_text(encoding="utf-8")
            self.assertIn("version: 2", text)
    def test_repeated_login_failures_are_throttled_with_retry_after(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            # 登录必须限速：连续失败达到阈值后返回 429 并给出 Retry-After，
            # 而不是继续接受无限制的密码猜测。
            form = {"Content-Type": "application/x-www-form-urlencoded"}
            for attempt in range(1, 6):
                got = sandbox.request("POST", "/login", body=b"password=wrong",
                                      headers=form)
                self.assertEqual(got.status, 200, attempt)
            blocked = sandbox.request("POST", "/login", body=b"password=wrong",
                                      headers=form)
            self.assertEqual(blocked.status, 429)
            retry_after = blocked.header("Retry-After")
            self.assertIsNotNone(retry_after)
            self.assertGreater(int(retry_after), 0)
            self.assertIsNone(blocked.header("Set-Cookie"))
            self.assertIn("尝试次数过多", blocked.body.decode("utf-8"))

    def test_throttling_also_refuses_the_correct_password_while_the_window_is_open(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            # 用尽次数后连正确密码也要先拒：否则爆破打到成功为止，限速形同虚设。
            form = {"Content-Type": "application/x-www-form-urlencoded"}
            for _ in range(5):
                sandbox.request("POST", "/login", body=b"password=wrong", headers=form)
            got = sandbox.request(
                "POST", "/login",
                body=urllib.parse.urlencode({"password": TEST_PASSWORD}).encode(),
                headers=form)
            self.assertEqual(got.status, 429)
            self.assertIsNone(got.header("Set-Cookie"))

    def test_a_successful_login_clears_the_failure_window(self):
        with ServerSandbox(docs=DOCUMENTS, dist=PAGES) as sandbox:
            # 成功登录要清零：否则正常用户偶发输错几次后会被自己的历史拖住。
            form = {"Content-Type": "application/x-www-form-urlencoded"}
            for _ in range(4):
                sandbox.request("POST", "/login", body=b"password=wrong", headers=form)
            good = sandbox.request(
                "POST", "/login",
                body=urllib.parse.urlencode({"password": TEST_PASSWORD}).encode(),
                headers=form)
            self.assertEqual(good.status, 303)
            self.assertIsNotNone(good.header("Set-Cookie"))
            for _ in range(4):
                sandbox.request("POST", "/login", body=b"password=wrong", headers=form)
            again = sandbox.request(
                "POST", "/login",
                body=urllib.parse.urlencode({"password": TEST_PASSWORD}).encode(),
                headers=form)
            self.assertEqual(again.status, 303)
            self.assertIsNotNone(again.header("Set-Cookie"))
