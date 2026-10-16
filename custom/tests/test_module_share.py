# -*- coding: utf-8 -*-
"""模块分享服务端验证：核心流程 + 12 条负向用例。"""
import datetime, http.cookiejar, json, os, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8903"
CFG = json.load(open("/opt/wenshu/app/server_config.json"))
KEY, PASSWORD = CFG["api_token"], CFG["password"]
DOCS = "/opt/wenshu/docs"

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

def call(op, url, method="GET", data=None, headers=None, cookie=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        r = op.open(req)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

def api(path, payload=None, method="POST"):
    st, h, b = call(opener(), BASE + path, method,
                    None if payload is None else json.dumps(payload).encode(),
                    {"Content-Type": "application/json", "X-API-Key": KEY})
    return st, (json.loads(b) if b else {})

def adm(op, path, payload=None, method="POST"):
    """用登录 cookie 调站内接口，返回 (status, json)。"""
    st, h, b = call(op, BASE + path, method,
                    None if payload is None else json.dumps(payload).encode(),
                    {"Content-Type": "application/json"})
    try:
        return st, json.loads(b)
    except Exception:
        return st, {"raw": b[:200].decode("utf-8", "replace")}

def login():
    o = opener()
    form = urllib.parse.urlencode({"password": PASSWORD, "next": "/"}).encode()
    call(o, BASE + "/login", "POST", form, {"Content-Type": "application/x-www-form-urlencoded"})
    return o

def dpath(doc_id):
    return "/docs/" + "/".join(urllib.parse.quote(p) for p in doc_id.split("/")) + "/"

soon = (datetime.datetime.now() + datetime.timedelta(hours=2)).isoformat(timespec="seconds")
run = os.urandom(3).hex()
TOPIC = "临时模块分享" + run

print("=== 准备数据 ===")
o = login()
doc_ids = []
for i, stt in enumerate(["implemented", "implemented"]):
    st, res = api("/api/admin/create", {"project": "modtest", "topic": TOPIC, "type": "api",
                       "title": f"临时模块{i}{run}", "status": stt,
                       "content": f"# 临时模块{i}\n\n## 基本信息\n\n口令标记 ZK{run} 与字段 payLegs。\n"})
    if st == 200:
        doc_ids.append(res["id"])
    else:
        print("  create fail:", st, res)
check("setup 建两篇当前版", len(doc_ids) == 2, doc_ids)
if len(doc_ids) != 2:
    print(sum(R), "/", len(R)); sys.exit(1)

st, res = api("/api/admin/version", {"id": doc_ids[0], "changes": ["临时验证升版"],
                                      "major_change": True,
                                      "content": "# 临时模块0 v2\n\n## 基本信息\n\n口令标记 ZK%s 与字段 payLegs。\n" % run})
check("setup 升版产生历史版", st == 200, res.get("error", ""))

st, res = adm(o, "/api/module-docs?topic=" + urllib.parse.quote(TOPIC), method="GET")
check("模块当前版=2（历史版被排除）", st == 200 and res.get("count") == 2,
      f"{st} count={res.get('count')}")

print("=== 创建分享 ===")
st, res = adm(o, "/api/share", {"scope": "module", "topic": TOPIC, "expires": soon})
check("创建模块分享", st == 200 and res.get("scope") == "module", f"{st} {res}")
mtoken, murl = res.get("token", ""), res.get("url", "")

st, sres = adm(o, "/api/share", {"path": dpath(doc_ids[1]), "expires": soon})
check("创建单篇分享", st == 200, f"{st} {sres}")
stoken, surl = sres.get("token", ""), sres.get("url", "")

print("=== 访客访问 ===")
v = opener()
st, h, b = call(v, murl)
loc = h.get("Location", "")
check("模块入口 303 到主题页且不带 token",
      st == 303 and loc.startswith("/topics/") and "s=" not in loc, f"{st} -> {loc}")
mcookie = h.get("Set-Cookie", "").split(";")[0]

# 同一浏览器再打开单篇入口：服务端必须把两个 token 并入同一个 ws_share cookie（逗号分隔）。
# 曾经测试用「ws_share=A; ws_share=B」两个同名 cookie 模拟，那不是浏览器的真实形态
# （同名同域 cookie 会互相覆盖），导致多凭证场景的回归被漏掉。
st2, h2, _ = call(v, surl)
merged_hdr = h2.get("Set-Cookie", "").split(";")[0]
check("多 token cookie 合并（单 cookie 逗号分隔）",
      merged_hdr.startswith("ws_share=") and mtoken in merged_hdr and stoken in merged_hdr,
      merged_hdr[:90])
combined = "ws_share=" + mtoken + "," + stoken
scookie = "ws_share=" + stoken

st, h, b = call(v, BASE + dpath(doc_ids[1]), cookie=combined)
loc = h.get("Location", "")
# 访客访问「分享覆盖但 URL 缺 ?share=1」的页面时，服务端会 303 补参（防止掉出分享模式）
if st == 303 and "share=1" in loc:
    st, h, b = call(v, BASE + loc, cookie=combined)
check("模块内当前版文档可访问（缺 share 参数由服务端补 303）", st == 200, st)

st, h, b = call(v, BASE + dpath(doc_ids[0]), cookie=combined)
check("模块内已升版的新版可访问", st == 200, st)

print("=== 多凭证并存回归 ===")
# 文档页不知道自己属于哪个主题/项目，只会发 path 参数。同一浏览器同时持有
# 「模块分享 + 其中某篇的单篇分享」时，服务端必须优先返回覆盖该文档的模块
# scope；若先命中单篇会返回 scope=doc，前端就会隐藏模块侧栏（历史 bug）。
st, h, b = call(v, BASE + "/api/share/ctx?path=" + urllib.parse.quote(dpath(doc_ids[1])),
                cookie=combined)
try:
    cj = json.loads(b)
except Exception:
    cj = {}
check("回归 多凭证并存时 ctx 返回模块 scope（不退回单篇）",
      st == 200 and cj.get("scope") == "topic" and len(cj.get("docs", [])) == 2,
      f"{st} scope={cj.get('scope')} docs={len(cj.get('docs', []))}")

# 反向保护：只有单篇凭证时仍是 doc scope，不能把单篇分享误升级成模块视图
st, h, b = call(opener(), BASE + "/api/share/ctx?path=" + urllib.parse.quote(dpath(doc_ids[1])),
                cookie=scookie)
try:
    dj = json.loads(b)
except Exception:
    dj = {}
check("回归 仅单篇凭证时 ctx 仍返回 doc scope",
      st == 200 and dj.get("scope") == "doc", f"{st} scope={dj.get('scope')}")

print("=== 负向用例 ===")
outside = dpath("xingtai-smart-store-api/助农/api/助农售卖配置接口-v1")
st, h, b = call(v, BASE + outside, cookie=combined)
check("负向1 模块外文档被拒", "该分享不包含此内容" in b.decode("utf-8", "replace"), st)

st, h, b = call(opener(), BASE + "/topics/" + urllib.parse.quote(TOPIC) + "/", cookie=scookie)
# 站点外壳页不属于任何分享 → 跳登录（无法区分纯访客与 session 过期的站点主人，
# 两者都带 ws_share cookie；访客点不到越界入口由前端消除入口来保证，见 test_share_ui.py）
check("负向2 单篇token 访问模块页被拒（跳登录）",
      st == 303 and "/login" in h.get("Location", ""),
      f"{st} {h.get('Location','')}")

st, h, b = call(opener(), BASE + "/s/deadbeefdeadbeef")
check("负向3 无效 token 显示失效页", "分享链接无效" in b.decode("utf-8", "replace"), st)

st, r = adm(o, "/api/shares/revoke", {"token": mtoken})
check("撤销模块分享", st == 200 and r.get("revoked"), f"{st} {r}")
st, h, b = call(opener(), BASE + "/s/" + mtoken)
check("负向4 撤销后显示已撤销", "分享已被撤销" in b.decode("utf-8", "replace"), st)
st, h, b = call(v, BASE + "/topics/" + urllib.parse.quote(TOPIC) + "/", cookie=combined)
# combined 里还有另一份有效的单篇分享：既不是失效说明页（token 仍有效），
# 也不该是无权页的正常内容；外壳页一律跳登录（站点主人日常要能进站）。
check("负向4b 撤销后仍持有另一份有效分享→外壳页跳登录",
      st == 303 and "/login" in h.get("Location", ""), f"{st} {h.get('Location','')}")
st, h, b = call(opener(), BASE + "/s/" + mtoken, cookie=scookie)
check("负向4c 仅剩撤销的模块cookie→显示已撤销失效页",
      "分享已被撤销" in b.decode("utf-8", "replace"), st)

# 负向5：真实到期路径（不手改 shares.json，因此测试不需要 root 权限）
# 服务端只清理「过期超过 SHARE_EXPIRED_KEEP(30天)」的记录，刚过期的记录保留用于区分提示文案
short_exp = (datetime.datetime.now() + datetime.timedelta(seconds=3)).isoformat(timespec="seconds")
st, res = adm(o, "/api/share", {"scope": "doc", "path": dpath(doc_ids[0]), "expires": short_exp})
etok = res.get("token", "")
time.sleep(4)
st, h, b = call(opener(), BASE + "/s/" + etok)
check("负向5 到期显示已过期", bool(etok) and "分享已过期" in b.decode("utf-8", "replace"), f"{st} {etok[:8]}")

print("=== 重建一个模块分享继续验证 ===")
o = login()
st, res = adm(o, "/api/share", {"scope": "module", "topic": TOPIC, "expires": soon})
mtoken2 = res.get("token", "")
v3 = opener()
st, h, _ = call(v3, BASE + "/s/" + mtoken2)
ck = h.get("Set-Cookie", "").split(";")[0]

q = urllib.parse.urlencode({"q": "payLegs", "topic": TOPIC})
st, h, b = call(v3, BASE + "/api/share/search?" + q, cookie=ck)
sj = json.loads(b) if b else {}
ids_found = [x["id"] for x in sj.get("results", [])]
check("负向6 搜索命中且不越界", st == 200 and ids_found and all(i.startswith("modtest/") for i in ids_found),
      f"{st} {ids_found}")
check("负向6b 搜索结果含片段", bool(sj.get("results", [{}])[0].get("snippet")),
      str(sj.get("results", [{}])[0].get("snippet", ""))[:50])

st, h, b = call(opener(), BASE + "/api/share/ctx?topic=" + urllib.parse.quote(TOPIC))
check("负向7 无 cookie 调 ctx 返回 404", st == 404, st)

st, h, b = call(v3, BASE + "/files/" + urllib.parse.quote("助农售卖配置接口-OpenAPI-v1.md"), cookie=ck)
check("负向8 模块外附件被拒", "该分享不包含此附件" in b.decode("utf-8", "replace"), st)

st, h, b = call(v3, BASE + "/api/share/ctx?topic=" + urllib.parse.quote(TOPIC), cookie=ck)
cj = json.loads(b) if b else {}
check("ctx 返回模块清单(2篇)", st == 200 and cj.get("scope") in ("topic", "project") and len(cj.get("docs", [])) == 2,
      f"{st} {cj.get('scope')} {len(cj.get('docs', []))}")

st, res = api("/api/admin/create", {"project": "modtest", "topic": TOPIC, "type": "api",
                   "title": f"临时模块多余{run}", "content": "# 临时多余\n\n正文。\n"})
extra = res.get("id")
extra_url = dpath(extra)
api("/api/admin/delete", {"id": extra})
st, h, b = call(v3, BASE + extra_url, cookie=ck)
check("负向10 删除后直链变无权", "该分享不包含此内容" in b.decode("utf-8", "replace"), st)
st, h, b = call(v3, BASE + "/api/share/ctx?topic=" + urllib.parse.quote(TOPIC), cookie=ck)
check("负向10b 清单同步少一篇", len(json.loads(b).get("docs", [])) == 2,
      len(json.loads(b).get("docs", [])))

st, h, b = call(v3, BASE + dpath(doc_ids[1]), cookie=ck)
loc = h.get("Location", "")
if st == 303 and "share=1" in loc:
    st, h, b = call(v3, BASE + loc, cookie=ck)
check("负向11 中文 id 授权成功（含服务端补参）", st == 200, st)

st, r = adm(o, "/api/shares", method="GET")
check("分享列表可用", st == 200 and r.get("count", 0) >= 1, f"{st} count={r.get('count')}")
states = {s["scope"] for s in r.get("shares", [])}
check("分享列表含模块与单篇", "doc" in states and ("topic" in states or "project" in states), states)

print("=== 淘汰豁免验证 ===")
# 注意：模块分享 mtoken 已在上面撤销，因此此刻 modtest 文档「本应」重新成为候选。
# 这里验证的是豁免的作用范围边界——新建一个仍然有效的模块分享，其文档不应进候选。
st, keep = adm(o, "/api/share", {
    "scope": "module", "topic": TOPIC, "projects": ["modtest"],
    "expires": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 86400))})
keep_tok = keep.get("token", "")
st, res = api("/api/admin/review?overrides=" + urllib.parse.quote('{"T_A2":1,"T_B":1,"T_C":1}'), method="GET")
cids = [c["id"] for c in res.get("candidates", [])]
leaked = [i for i in cids if i.startswith("modtest/")]
check("负向13 分享中的模块文档不进淘汰候选", keep_tok and not leaked, f"token={keep_tok[:8]} leaked={leaked}")

print("=== 清理 ===")
mine = {mtoken2, sres.get("token", ""), "expiredtokentest", keep_tok}
st, r = adm(o, "/api/shares", method="GET")
for s in r.get("shares", []):
    if s["token"] in mine:
        adm(o, "/api/shares/revoke", {"token": s["token"]})
st, ctxr = api("/api/admin/context?project=modtest", method="GET")
for proj in ctxr.get("projects", []):
    for t in proj.get("topics", []):
        for d in t["docs"]:
            api("/api/admin/delete", {"id": d["id"], "force": True})
st, ctx2 = api("/api/admin/context?project=modtest", method="GET")
left = sum(len(t["docs"]) for p in ctx2.get("projects", []) for t in p.get("topics", []))
print("清理后剩余测试文档:", left, "| 撤销了本测试的",
      len([s for s in r.get("shares", []) if s["token"] in mine]), "条分享")

print()
print(f"结果: {sum(R)}/{len(R)} 通过")
# 失败必须返回非零，否则 run_integration.sh 会误报 ALL-PASS
sys.exit(0 if all(R) else 1)