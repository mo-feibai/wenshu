# -*- coding: utf-8 -*-
"""全库链接冒烟测试：每篇文档 URL 与每条 related 链接都必须可访问。

这正是当初 25+ 篇「-OpenAPI」等含大写/标点的文档链接 404 的回归网。
只读：不创建任何文档，不触发重建，秒级完成。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8903"
CFG = json.load(open("/opt/wenshu/app/server_config.json"))
KEY = CFG["api_token"]

FAILED = []
TOTAL = 0


def fetch(url, headers=None):
    """按 RFC 逐段编码非 ASCII 路径（HTTP 请求行必须是 ASCII）。"""
    sp = urllib.parse.urlsplit(url)
    if not sp.scheme:  # entry_brief 的 url 是相对路径
        sp = urllib.parse.urlsplit(BASE + ("" if url.startswith("/") else "/") + url)
    safe = urllib.parse.quote(sp.path, safe="/%:@!$&'()*+,;=~-._~")
    req = urllib.request.Request(
        urllib.parse.urlunsplit((sp.scheme, sp.netloc, safe, sp.query, sp.fragment)),
        headers=headers or {})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # noqa: BLE001
        return f"ERR {e}"


def check(url, label):
    global TOTAL
    TOTAL += 1
    st = fetch(url)
    if st != 200:
        FAILED.append(f"{label} -> {st}  {url}")
    return st


ctx = json.loads(urllib.request.urlopen(
    urllib.request.Request(BASE + "/api/admin/context", headers={"X-API-Key": KEY}),
    timeout=60).read().decode("utf-8"))

docs = []
for proj in ctx.get("projects", []):
    for topic in proj.get("topics", []):
        docs.extend(topic.get("docs", []))
id2url = {d["id"]: d.get("url") for d in docs}

print(f"=== 文档 URL（{len(docs)} 篇）===")
for d in docs:
    check(d.get("url") or "", d["id"])

rel_targets = set()
for d in docs:
    for r in d.get("related") or []:
        rel_targets.add(str(r))
print(f"=== related 链接（{len(rel_targets)} 个目标）===")
missing = []
for rid in sorted(rel_targets):
    if rid in id2url and id2url[rid]:
        check(id2url[rid], "related " + rid)
    else:
        missing.append(rid)

print(f"=== 汇总：{TOTAL} 个链接检查，失败 {len(FAILED)} 个 ===")
for f in FAILED[:30]:
    print("  FAIL " + f)
if missing:
    print(f"  WARN related 指向不存在的 id：{missing[:10]}")
raise SystemExit(1 if FAILED else 0)