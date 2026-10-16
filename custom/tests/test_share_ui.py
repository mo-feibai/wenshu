# -*- coding: utf-8 -*-
"""端到端：模块分享全链路（真实文档 + 站点产物检查）。"""
import datetime, http.cookiejar, json, re, sys, urllib.error, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8903"
CFG = json.load(open("/opt/wenshu/app/server_config.json"))
PASSWORD = CFG["password"]
TOPIC = "助农"
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
    # 非 ASCII 路径按 RFC 逐段编码（HTTP 请求行必须是 ASCII）
    sp = urllib.parse.urlsplit(url)
    safe_path = urllib.parse.quote(sp.path, safe="/%:@!$&'()*+,;=~-._~")
    req = urllib.request.Request(urllib.parse.urlunsplit((sp.scheme, sp.netloc, safe_path, sp.query, sp.fragment)),
                                 data=data, method=method, headers=headers or {})
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        r = op.open(req)
        return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")

def login():
    o = opener()
    form = urllib.parse.urlencode({"password": PASSWORD, "next": "/"}).encode()
    call(o, BASE + "/login", "POST", form, {"Content-Type": "application/x-www-form-urlencoded"})
    return o

print("=== 登录并创建主题模块分享（真实主题：助农） ===")
o = login()
st, h, b = call(o, BASE + "/api/module-docs?topic=" + urllib.parse.quote(TOPIC), "GET")
docs = json.loads(b).get("docs", [])
check("module-docs 返回助农当前版", len(docs) > 0, f"{len(docs)} 篇")

# 有效期必须是未来时间（相对计算，避免硬编码日期过期后整条测试失败）
soon = (datetime.datetime.now() + datetime.timedelta(days=1)).isoformat(timespec="seconds")
st, h, b = call(o, BASE + "/api/share", "POST",
                json.dumps({"scope": "module", "topic": TOPIC, "expires": soon}).encode(),
                {"Content-Type": "application/json"})
res = json.loads(b)
check("创建助农模块分享", st == 200 and res.get("scope") == "module", f"{st} {res.get('doc_count')} 篇")
mtoken, murl = res.get("token", ""), res.get("url", "")
print("  分享链接:", murl)

print("=== 访客访问 ===")
v = opener()
st, h, b = call(v, murl)
loc = h.get("Location", "")
check("入口 303 → 主题页 ?share=1",
      st == 303 and loc.startswith("/topics/") and loc.endswith("?share=1"), f"{st} {loc}")
ck = h.get("Set-Cookie", "").split(";")[0]
check("入口下发 ws_share cookie", ck.startswith("ws_share="), ck[:40])

st, h, b = call(v, BASE + "/topics/" + urllib.parse.quote(TOPIC) + "/?share=1", cookie=ck)
check("访客可打开主题落地页", st == 200, st)
check("落地页含分享本主题按钮（服务端渲染，访客模式由 CSS 隐藏）", "mshare-open" in b)
check("落地页导航含分享管理入口", "/shares/" in b)

print("=== 站点产物：分享相关元素 ===")
st, h, b = call(v, BASE + "/shares/", cookie=ck)
check("分享管理页需登录（访客被拒）", st in (303, 401) or "该分享不包含此内容" in b, st)
st, h, b = call(o, BASE + "/shares/")
check("登录后分享管理页可访问", st == 200 and "分享管理" in b, st)

print("=== 模块内搜索（真实语料） ===")
# 搜索词必须真实出现在该主题语料里（payLegs 属组合支付主题，不在助农，故不作为预期命中）
for term, expect_hit in [("curl", True), ("deptId", True), ("助农", True),
                         ("zzzz不存在zzz", False)]:
    qs = urllib.parse.urlencode({"q": term, "topic": TOPIC})
    st, h, b = call(v, BASE + "/api/share/search?" + qs, cookie=ck)
    d = json.loads(b)
    n = d.get("count", 0)
    if expect_hit:
        check(f"模块内搜索命中「{term}」", st == 200 and n > 0, f"{n} 条")
    else:
        check(f"模块内搜索「{term}」无结果", st == 200 and n == 0, f"{n} 条")

# 搜索结果不越界：所有结果都属于该主题
qs = urllib.parse.urlencode({"q": "接口", "topic": TOPIC})
st, h, b = call(v, BASE + "/api/share/search?" + qs, cookie=ck)
res_all = json.loads(b).get("results", [])
ids = {r["id"] for r in res_all}
st2, h2, b2 = call(o, BASE + "/api/module-docs?topic=" + urllib.parse.quote(TOPIC), "GET")
allowed = {d["id"] for d in json.loads(b2).get("docs", [])}
check("搜索结果全部落在模块当前版集合内", ids <= allowed, f"越界={ids - allowed}")
check("搜索结果带片段", all(r.get("snippet") for r in res_all[:3]))

print("=== 附件白名单（真实文档的下载链接）===")
st, h, b = call(v, BASE + "/topics/" + urllib.parse.quote(TOPIC) + "/?share=1", cookie=ck)
# 落地页链接是百分号编码的，用 /docs/ 前缀取第一个文档链接并解码
m = re.search(r'href="(/docs/[^"]+?)"', b)
check("落地页含文档链接", bool(m), (m.group(1) if m else "")[:80])
if m:
    doc_url = urllib.parse.unquote(m.group(1).replace("&amp;", "&"))
    st, h, b = call(v, BASE + doc_url, cookie=ck)
    check("访客可打开模块内文档", st == 200, doc_url)
    fl = re.findall(r'href="(/files/[^"]+)"', b)
    print("  该文档引用的 /files/ 链接:", fl[:3])
    if fl:
        target = urllib.parse.unquote(fl[0].replace("&amp;", "&"))
        st2, h2, b2 = call(v, BASE + target, cookie=ck)
        check("模块内附件可下载", st2 == 200, f"{target} → {st2}")
        # 模块外附件必须被拒
        st3, h3, b3 = call(v, BASE + "/files/" +
                           urllib.parse.quote("组合支付payLegs接口变更文档-v1.md"), cookie=ck)
        check("模块外附件被拒", "该分享不包含此附件" in b3 or st3 != 200,
              f"{st3} {'含拒绝文案' if '该分享不包含此附件' in b3 else ''}")
    else:
        print("  （该文档无附件，跳过下载用例）")

print("=== 越权边界 ===")
# 站点外壳页（主题/看板/总览）不属于任何分享 → 回退登录跳转，站点主人可正常进站
for label, path in [("未分享主题", "/topics/" + urllib.parse.quote("考勤") + "/?share=1"),
                     ("待办看板", "/pending/?share=1"),
                     ("总览页", "/?share=1"),
                     ("Pagefind 全站索引", "/pagefind/pagefind.js")]:
    st, h, b = call(v, BASE + path, cookie=ck)
    # 资源类（/pagefind/）走无权页；页面类走登录跳转，两者都不泄露内容
    ok = (st == 303 and "/login" in h.get("Location", "")) or "不包含此内容" in b
    check(f"访客访问{label}被拒", ok, f"{st} {h.get('Location','')[:40]}")
# 关键回归：模块外文档页必须是无权页，且页面提供「以站点密码登录」出口
st, h, b = call(v, BASE + "/docs/xingtai/api/" + urllib.parse.quote("用户管理排序字段-前端接口变更") + "/", cookie=ck)
check("访客访问模块外文档显示无权页并留登录出口",
      st == 200 and "不包含此内容" in b and "以站点密码登录" in b, st)

print("=== 分享管理 ===")
st, h, b = call(o, BASE + "/api/shares", "GET")
lst = json.loads(b).get("shares", [])
mine = [s for s in lst if s["token"] == mtoken]
check("分享列表含本次创建的模块分享", len(mine) == 1, mine[0]["doc_count"] if mine else None)
check("列表含类型/文档数/创建时间/到期", all(k in mine[0] for k in ("scope", "doc_count", "created", "expires", "state")) if mine else False)
st, h, b = call(o, BASE + "/api/shares/revoke", "POST", json.dumps({"token": mtoken}).encode(),
                {"Content-Type": "application/json"})
check("管理页撤销接口可用", st == 200, b[:80])
st, h, b = call(v, BASE + "/s/" + mtoken)
check("撤销后入口显示已撤销", "分享已被撤销" in b, st)
st, h, b = call(v, BASE + "/topics/" + urllib.parse.quote(TOPIC) + "/?share=1", cookie=ck)
# 撤销后失效说明页才是正确行为（撤销≠「不含此内容」），且绝不能回落到登录页
check("撤销后落地页显示失效说明（非登录跳转）",
      st == 200 and "分享已被撤销" in b, f"{st} {b[:60]}")
# 注意：失效说明页里含「重新登录站点」链接，所以不能用 b 里有没有 /login 判断是否跳转
st, h, b = call(opener(), BASE + "/topics/" + urllib.parse.quote(TOPIC) + "/?share=1")
# ?share=1 只是访客视图标志、不是凭证：从未拿到凭证的深链给「分享链接未激活」说明页
# （跳登录页会让人误以为站点要密码），且绝不泄露内容
check("全新访客（无 cookie 深链）显示未激活说明页且不泄露内容",
      st == 200 and "分享链接未激活" in b and "该分享不包含此内容" not in b, f"{st} {b[:50]}")
# 反向保护：不带 ?share=1 的站点页面对无凭证访客仍跳登录（站点主人要能进站）
st, h, b = call(opener(), BASE + "/")
check("全新访客访问站点根仍跳登录页",
      st == 303 and "/login" in h.get("Location", ""), f"{st} {h.get('Location','')}")

print("=== 分享提示元素与接线（防「元素加了但 JS 没接线」） ===")
# 真实踩过：文档页加了 #share-tip 元素却忘了在创建成功后显示它，提示永远不可见。
doc_page = docs[0]["url"] if docs else ""
st, h, b = call(o, BASE + doc_page, "GET")
check("文档页含转发提示元素", 'id="share-tip"' in b, st)
check("文档页转发提示已接线（创建成功后由 JS 显示）",
      "getElementById('share-tip')" in b, doc_page)
st, h, b = call(o, BASE + "/projects/xingtai-smart-store-api/", "GET")
check("项目页含转发提示元素", 'id="pshare-tip"' in b, st)
check("项目页转发提示已接线", "getElementById('pshare-tip')" in b, "")

print()
print(f"结果: {sum(R)}/{len(R)} 通过")
sys.exit(0 if all(R) else 1)