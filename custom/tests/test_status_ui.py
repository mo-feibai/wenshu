# -*- coding: utf-8 -*-
"""页面状态切换：8 条验证。用临时文档，不动存量。"""
import http.cookiejar, json, pathlib, time
import urllib.error, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8903"
CFG = json.load(open("/opt/wenshu/app/server_config.json"))
KEY, PASSWORD = CFG["api_token"], CFG["password"]
DIST = pathlib.Path("/opt/wenshu/app/dist")
R = []


def check(name, cond, detail=""):
    R.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def opener():
    return urllib.request.build_opener(NoRedir(),
                                       urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(op, url, method="GET", data=None, headers=None):
    sp = urllib.parse.urlsplit(url)
    safe_path = urllib.parse.quote(sp.path, safe="/%:@!$&'()*+,;=~-._~")
    req = urllib.request.Request(
        urllib.parse.urlunsplit((sp.scheme, sp.netloc, safe_path, sp.query, sp.fragment)),
        data=data, method=method, headers=headers or {})
    try:
        r = op.open(req)
        return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def jcall(op, path, payload=None, method="POST", key_auth=False):
    hdr = {"Content-Type": "application/json"}
    if key_auth:
        hdr["X-API-Key"] = KEY
    st, h, b = call(op, BASE + path, method,
                    None if payload is None else json.dumps(payload).encode(), hdr)
    try:
        return st, json.loads(b)
    except Exception:
        return st, {"raw": b[:200]}


def login():
    o = opener()
    call(o, BASE + "/login", "POST",
         urllib.parse.urlencode({"password": PASSWORD, "next": "/"}).encode(),
         {"Content-Type": "application/x-www-form-urlencoded"})
    return o


STAMP = "stswitch" + str(int(time.time()))[-5:]

o = login()
print("=== 准备临时文档 ===")
st, r = jcall(opener(), "/api/admin/create",
              {"project": "statustest", "topic": STAMP, "type": "api",
               "title": f"状态切换测试{STAMP}", "status": "pending",
               "content": "# 状态切换测试\n\n验证页面手动切换状态。\n"}, key_auth=True)
check("1 创建临时文档", st == 200 and r.get("id"), f"{st} {r}")
tid = r["id"]  # 以服务端返回的真实 id 为准（会落在 project/api/ 子目录）

print("=== 合法状态集合 ===")
st, opts = jcall(o, "/api/status-options?type=api", None, "GET")
check("2 登录可取集合且与 STATUS_SETS 一致",
      st == 200 and opts.get("options") == ["pending", "implemented", "archived"]
      and opts.get("text", {}).get("implemented") == "已实现", f"{st} {opts}")
st, _ = jcall(opener(), "/api/status-options?type=api", None, "GET")
check("2b 未登录取集合被拒", st == 401, st)

print("=== 页面切换 ===")
st, r = jcall(o, "/api/status", {"id": tid, "status": "implemented"})
check("3 pending→implemented 成功",
      st == 200 and r.get("status") == "implemented" and r.get("built") and r.get("commit"), f"{st} {r}")
doc_url_path = urllib.parse.unquote(r.get("url", "")).strip("/")
page = DIST / doc_url_path / "index.html"
html = page.read_text(encoding="utf-8", errors="ignore") if page.exists() else ""
check("3b 产物徽标已刷新为已实现", "已实现" in html, str(page))
check("3c 页面含状态切换容器", "status-switch" in html and "/api/status" in html, "")

print("=== 守卫 ===")
st, r = jcall(o, "/api/status", {"id": tid, "status": "archived"})
check("4 切换为归档", st == 200 and r.get("status") == "archived", f"{st} {r}")
st, r = jcall(o, "/api/status", {"id": tid, "status": "pending"})
check("5 归档是单向终态（翻回被拒）", st == 400 and "终态" in r.get("error", ""), f"{st} {r}")
st, r = jcall(o, "/api/status", {"id": tid, "status": "executed"})
check("6 类型不匹配被拒（api 不能 executed）", st == 400, f"{st} {r}")
st, _ = jcall(opener(), "/api/status", {"id": tid, "status": "implemented"})
check("7 未登录切换被拒", st == 401, st)
st, r = jcall(o, "/api/status", {"id": tid, "status": "archived"})
check("8 同状态重复切换为 noop", st == 200 and r.get("unchanged"), f"{st} {r}")

pend = (DIST / "pending" / "index.html").read_text(encoding="utf-8", errors="ignore")
check("9 看板含行内切换按钮", 'data-status-btn' in pend and 'data-to="archived"' in pend, "")

print("=== 清理 ===")
jcall(opener(), "/api/admin/delete", {"id": tid, "force": True}, key_auth=True)
st, ctxr = jcall(opener(), "/api/admin/context?project=statustest", None, "GET", key_auth=True)
for proj in ctxr.get("projects", []):
    for t in proj.get("topics", []):
        for d in t["docs"]:
            jcall(opener(), "/api/admin/delete", {"id": d["id"], "force": True}, key_auth=True)
left = list(pathlib.Path("/opt/wenshu/docs/statustest").rglob("*")) \
    if pathlib.Path("/opt/wenshu/docs/statustest").exists() else []
check("10 清理完成", not left, str(left))

print("\n结果: %d/%d 通过" % (sum(R), len(R)))