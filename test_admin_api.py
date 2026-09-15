# -*- coding: utf-8 -*-
"""文枢 Admin API 全流程测试（创建→读→状态→版本→元数据→清理）

默认读取 /opt/wenshu/app/server_config.json，可用环境变量覆盖：
WENSHU_BASE / WENSHU_CONFIG / WENSHU_APP / WENSHU_TEST_RELATED
"""
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import urllib.error
import urllib.request

BASE = os.environ.get("WENSHU_BASE", "http://127.0.0.1:8903")
CFG_PATH = pathlib.Path(os.environ.get("WENSHU_CONFIG", "/opt/wenshu/app/server_config.json"))
CFG = json.loads(CFG_PATH.read_text(encoding="utf-8"))
KEY = CFG["api_token"]
DOCS = pathlib.Path(CFG.get("docs_dir", "/opt/wenshu/docs"))
APP = pathlib.Path(os.environ.get("WENSHU_APP", "/opt/wenshu/app"))
RELATED_DOC = os.environ.get("WENSHU_TEST_RELATED", "demo-project/api/示例接口文档")

results = []


def call(method, path, payload=None, key=KEY):
    req = urllib.request.Request(BASE + path, method=method)
    if key:
        req.add_header("X-API-Key", key)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=600) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {}


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  | {extra}"))


today = datetime.date.today().isoformat()

# 1. 创建 SQL 文档
code, r = call("POST", "/api/admin/create", {
    "project": "文枢测试", "topic": "接口联调", "type": "sql",
    "title": "联调测试索引", "content": "SELECT 1;\n-- 联调测试内容\n",
    "related": [RELATED_DOC],
})
check("create sql (200)", code == 200 and r.get("ok"), f"{code} {r}")
sql_id = r.get("id", "")

# 2. 读取
code, r = call("GET", "/api/admin/doc?id=" + urllib.request.quote(sql_id, safe=""))
check("read sql doc", code == 200 and "联调测试内容" in r.get("body", ""), f"{code} {r}")
check("read status=pending", r.get("status") == "pending", r.get("status"))

# 3. 状态翻转
code, r = call("POST", "/api/admin/status", {"id": sql_id, "status": "executed"})
check("status -> executed", code == 200 and r.get("status") == "executed", f"{code} {r}")

# 4. 非法状态
code, r = call("POST", "/api/admin/status", {"id": sql_id, "status": "implemented"})
check("invalid status rejected (400)", code == 400, f"{code} {r}")

# 5. 元数据更新
code, r = call("POST", "/api/admin/meta", {"id": sql_id, "summary": "联调用例"})
check("meta update", code == 200, f"{code} {r}")

# 6. 创建 API 文档 v1 并版本化
code, r = call("POST", "/api/admin/create", {
    "project": "文枢测试", "topic": "接口联调", "type": "api",
    "title": "联调测试接口文档", "version": 1,
    "content": "# 联调测试接口文档\n\nv1 内容\n",
})
check("create api v1", code == 200 and r.get("ok"), f"{code} {r}")
api_id = r.get("id", "")

code, r = call("POST", "/api/admin/version", {"id": api_id, "content": "# 联调测试接口文档\n\nv2 内容（变更）\n"})
check("version -> v2", code == 200 and r.get("id", "").endswith("-v2"), f"{code} {r}")
api_v2 = r.get("id", "")

code, r = call("GET", "/api/admin/doc?id=" + urllib.request.quote(api_id, safe=""))
check("old doc backfilled superseded_by", r.get("meta", {}).get("superseded_by") == api_v2,
      str(r.get("meta", {}).get("superseded_by")))

# 7. 非法 related
code, r = call("POST", "/api/admin/create", {
    "project": "文枢测试", "topic": "接口联调", "type": "sql",
    "title": "坏引用", "content": "SELECT 1;", "related": ["不存在的/文档"],
})
check("invalid related rejected (400)", code == 400, f"{code} {r}")

# 8. 清理
test_dir = DOCS / "文枢测试"
if test_dir.exists():
    shutil.rmtree(test_dir)
    subprocess.run(["git", "add", "-A"], cwd=str(DOCS), capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "chore: 清理 Admin API 测试文档"], cwd=str(DOCS), capture_output=True, text=True)
    proc = subprocess.run(["npm", "run", "build"], cwd=str(APP), capture_output=True, text=True, timeout=900)
    print("cleanup rebuild:", "ok" if proc.returncode == 0 else "FAILED")
    code, r = call("GET", "/api/admin/context")
    left = [p["name"] for p in r.get("projects", []) if p["name"] == "文枢测试"]
    check("cleanup (no test project)", not left, str(left))

passed = sum(1 for _, ok in results if ok)
print(f"\n结果: {passed}/{len(results)} 通过")
