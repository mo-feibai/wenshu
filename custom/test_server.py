# -*- coding: utf-8 -*-
"""验证 文枢服务：鉴权 + 临时分享链接

默认针对合成示例文档（sample-docs）执行，可用环境变量覆盖：
WENSHU_BASE / WENSHU_CONFIG / WENSHU_PASSWORD /
WENSHU_TEST_DOC_A / WENSHU_TEST_DOC_B / WENSHU_TEST_ATTACH_OK / WENSHU_TEST_ATTACH_OTHER
"""
import http.cookiejar
import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("WENSHU_BASE", "http://127.0.0.1:8903")
CFG = pathlib.Path(os.environ.get("WENSHU_CONFIG", "/opt/wenshu/app/server_config.json"))
PASSWORD = os.environ.get("WENSHU_PASSWORD", "")
if not PASSWORD and CFG.exists():
    PASSWORD = json.loads(CFG.read_text(encoding="utf-8")).get("password", "")

DOC_A_ID = os.environ.get("WENSHU_TEST_DOC_A", "demo-project/api/示例接口文档")
DOC_B_ID = os.environ.get("WENSHU_TEST_DOC_B", "demo-project/文档/术语表")
ATTACH_OK = os.environ.get("WENSHU_TEST_ATTACH_OK", "示例测试页面.html")
ATTACH_OTHER = os.environ.get("WENSHU_TEST_ATTACH_OTHER", "迁移-20260901-示例建表.sql")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar)), jar


def call(op, url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        resp = op.open(req)
        return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


results = []

def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS " if cond else "FAIL ") + name + (" | " + detail if detail else ""))


def doc_url(doc_id: str) -> str:
    return "/docs/" + urllib.parse.quote(doc_id, safe="/") + "/"


doc_a = doc_url(DOC_A_ID)
doc_b = doc_url(DOC_B_ID)
doc_a_url = BASE + doc_a

# 1. 未登录访问首页 → 跳登录
anon, _ = opener()
code, headers, _ = call(anon, BASE + "/")
check("未登录访问首页被重定向到 /login", code == 303 and "/login" in headers.get("Location", ""), f"{code} -> {headers.get('Location')}")

# 2. 登录
form = urllib.parse.urlencode({"password": PASSWORD, "next": "/"}).encode()
code, headers, _ = call(anon, BASE + "/login", method="POST", data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
check("正确密码登录成功", code == 303)
check("下发会话 Cookie", "ws_sid" in headers.get("Set-Cookie", ""), headers.get("Set-Cookie", "").split(";")[0])

# 3. 登录后访问首页
code, _, _ = call(anon, BASE + "/")
check("登录后访问首页 200", code == 200)

# 4. 创建分享链接
future = "2026-09-30T23:59"
payload = json.dumps({"path": doc_a, "expires": future}).encode("utf-8")
code, _, body = call(anon, BASE + "/api/share", method="POST", data=payload, headers={"Content-Type": "application/json"})
share_url = ""
if code == 200:
    share_url = json.loads(body.decode("utf-8")).get("url", "")
check("创建分享链接", code == 200 and "/s/" in share_url, share_url)

# 5. 过期时间校验
past = json.dumps({"path": doc_a, "expires": "2020-01-01T00:00"}).encode("utf-8")
code, _, _ = call(anon, BASE + "/api/share", method="POST", data=past, headers={"Content-Type": "application/json"})
check("过期时间早于现在被拒绝", code == 400)

# 6. 分享访客访问
visitor, _ = opener()
token_path = urllib.parse.urlparse(share_url).path
code, headers, _ = call(visitor, BASE + token_path)
loc = headers.get("Location", "")
loc_parts = urllib.parse.urlparse(loc)
check("访客打开分享链接被放行并跳转", code == 303 and urllib.parse.unquote(loc_parts.path) == urllib.parse.unquote(doc_a), f"{code} -> {loc}")
check("分享视图标记 share=1", "share=1" in loc_parts.query)
check("访客获得分享 Cookie", "ws_share" in headers.get("Set-Cookie", ""))

# 7. 访客访问被分享文档
code, _, _ = call(visitor, doc_a_url)
check("访客可访问被分享文档", code == 200)

# 8. 访客访问其他文档 → 拒绝
other_url = BASE + doc_b
code, headers, _ = call(visitor, other_url)
check("访客访问其他文档被拒绝", code == 303, f"{code} -> {headers.get('Location')}")

# 8b. 访客被限制：搜索索引 / 其他附件 / 其他页面 均不可访问；本文档附件可下载
code, _, _ = call(visitor, BASE + "/pagefind/pagefind.js")
check("访客不能访问全文搜索索引", code == 303, f"{code}")
code, _, _ = call(visitor, BASE + "/files/" + urllib.parse.quote(ATTACH_OK))
check("访客可下载本文档附件", code == 200, f"{code}")
code, _, _ = call(visitor, BASE + "/files/" + urllib.parse.quote(ATTACH_OTHER))
check("访客不能下载其他附件", code == 303, f"{code}")
code, _, _ = call(visitor, BASE + "/files/")
check("访客不能浏览附件目录", code == 303, f"{code}")
code, _, _ = call(visitor, BASE + "/pending/")
check("访客不能访问待办看板", code == 303, f"{code}")

# 9. 未登录调用分享 API → 401
anon2, _ = opener()
code, _, _ = call(anon2, BASE + "/api/share", method="POST", data=payload, headers={"Content-Type": "application/json"})
check("未登录不能创建分享", code == 401)

# 10. 登录用户访问被保护资源正常
code, _, _ = call(anon, BASE + "/pending/")
check("登录后访问待办看板 200", code == 200)

failed = [r for r in results if not r[1]]
print()
print(f"结果: {len(results) - len(failed)}/{len(results)} 通过")
if failed:
    raise SystemExit(1)
