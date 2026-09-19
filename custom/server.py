# -*- coding: utf-8 -*-
"""文枢 · 轻量服务：静态站点 + 共享密码登录 + 单文档临时分享 + Admin API（AI/插件用）
依赖：仅 Python 标准库
"""
import datetime
import http.server
import json
import pathlib
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from html import escape

BASE = pathlib.Path(__file__).resolve().parent
ROOT = BASE / "dist"
CONFIG_PATH = BASE / "server_config.json"
SHARES_PATH = BASE / "shares.json"
SESSIONS_PATH = BASE / "sessions.json"

config = {}
if CONFIG_PATH.exists():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
PASSWORD = config.get("password", "")
PORT = int(config.get("port", 8903))
SESSION_TTL = 7 * 24 * 3600
DOCS = pathlib.Path(config.get("docs_dir", "/opt/wenshu/docs"))
API_TOKEN = config.get("api_token", "")

sessions = {}
shares = {}
lock = threading.Lock()
WRITE_LOCK = threading.Lock()

PUBLIC_PREFIXES = ("/fonts/",)
PUBLIC_FILES = ("/favicon.svg", "/logo.svg")
SHARE_ASSET_PREFIXES = ("/fonts/", "/_astro/", "/favicon.svg", "/logo.svg")

DOC_TYPES = ["sql", "api", "adr", "analysis", "scheme", "report", "other"]
STATUS_SETS = {
    "sql": ["pending", "executed", "archived"],
    "api": ["pending", "implemented", "archived"],
    "adr": ["accepted", "archived"],
    "analysis": ["draft", "final", "archived"],
    "scheme": ["draft", "final", "archived"],
    "report": ["draft", "final", "archived"],
    "other": ["draft", "final", "archived"],
}
DEFAULT_STATUS = {"sql": "pending", "api": "pending", "adr": "accepted",
                  "analysis": "draft", "scheme": "draft", "report": "draft", "other": "draft"}
TYPE_SUBDIR = {"sql": "sql", "api": "api", "adr": "adr"}


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def now_ts():
    return time.time()


def load_shares():
    if SHARES_PATH.exists():
        try:
            data = json.loads(SHARES_PATH.read_text(encoding="utf-8"))
            t = now_ts()
            for token, item in data.items():
                if item.get("expires", 0) > t:
                    shares[token] = item
        except Exception:
            pass


def save_shares():
    SHARES_PATH.write_text(json.dumps(shares, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sessions():
    if SESSIONS_PATH.exists():
        try:
            data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
            t = now_ts()
            for token, expires in data.items():
                if expires > t:
                    sessions[token] = expires
        except Exception:
            pass


def save_sessions():
    SESSIONS_PATH.write_text(json.dumps(sessions), encoding="utf-8")


def purge_expired():
    t = now_ts()
    changed = False
    sessions_changed = False
    with lock:
        for k in [k for k, v in sessions.items() if v < t]:
            del sessions[k]
            sessions_changed = True
        for k in [k for k, v in shares.items() if v.get("expires", 0) < t]:
            del shares[k]
            changed = True
        if changed:
            save_shares()
        if sessions_changed:
            save_sessions()


def safe_next(value: str) -> str:
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def extract_doc_files(target_path: str):
    """从被分享文档的构建产物中提取它自己的附件链接（仅限 /files/ 下）。"""
    rel = urllib.parse.unquote(target_path).split("?", 1)[0].lstrip("/")
    index_file = ROOT / rel / "index.html"
    try:
        index_file.relative_to(ROOT)
    except ValueError:
        return []
    if not index_file.exists():
        return []
    html = index_file.read_text(encoding="utf-8", errors="ignore")
    files = {urllib.parse.unquote(m.group(1)) for m in re.finditer(r'href="(/files/[^"#?]+)"', html)}
    return sorted(files)


# ---------------- 文档库读写（Admin API 用） ----------------

def parse_md(text: str):
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    block = text[4:end]
    rest = text[end + 5:]
    meta, arrays, cur = {}, {}, None
    for line in block.splitlines():
        if line.startswith("  - ") and cur:
            raw = line[4:].strip()
            arrays[cur].append(json.loads(raw) if raw.startswith('"') else raw)
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v == "":
                cur = k
                arrays[k] = []
            else:
                cur = None
                if v.startswith('"'):
                    meta[k] = json.loads(v)
                elif re.fullmatch(r"-?\d+", v):
                    meta[k] = int(v)
                else:
                    meta[k] = v
    for k, arr in arrays.items():
        meta[k] = arr
    return meta, rest


def render_md(meta: dict, rest: str) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {json.dumps(str(item), ensure_ascii=False)}")
        elif isinstance(v, int):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {json.dumps(str(v), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n" + rest


def parse_sql_meta(text: str):
    if not text.startswith("-- @meta"):
        return None, text
    lines = text.splitlines()
    meta, i = {}, 1
    while i < len(lines) and lines[i].strip() != "-- @end":
        line = lines[i].strip()
        if line.startswith("-- ") and ":" in line:
            k, v = line[3:].split(":", 1)
            k, v = k.strip(), v.strip()
            if k == "related":
                meta[k] = [x.strip() for x in v.split(",") if x.strip()]
            else:
                meta[k] = v
        i += 1
    rest = "\n".join(lines[i + 1:]) if i < len(lines) else ""
    return meta, rest


def render_sql_meta(meta: dict, rest: str) -> str:
    lines = ["-- @meta"]
    for k, v in meta.items():
        if isinstance(v, list):
            if not v:
                continue
            lines.append(f"-- {k}: {', '.join(str(x) for x in v)}")
        else:
            lines.append(f"-- {k}: {v}")
    lines.append("-- @end")
    return "\n".join(lines) + "\n\n" + rest.lstrip("\n")


def read_entries():
    out = []
    if not DOCS.exists():
        return out
    for p in sorted(DOCS.rglob("*")):
        if p.is_dir() or ".git" in p.parts:
            continue
        suffix = p.suffix.lower()
        rel = p.relative_to(DOCS).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if suffix == ".md":
            meta, _ = parse_md(text)
            if not meta:
                continue
            out.append({"path": p, "rel": rel, "id": rel[:-3], "kind": "md", "meta": meta, "text": text})
        elif suffix == ".sql":
            meta, _ = parse_sql_meta(text)
            if not meta:
                continue
            out.append({"path": p, "rel": rel, "id": rel[:-4], "kind": "sql", "meta": meta, "text": text})
    return out


def find_entry(entries, doc_id: str):
    low = doc_id.strip().rstrip("/").lower()
    for e in entries:
        if e["id"].lower() == low:
            return e
    return None


def resolve_ref(entries, ref: str):
    low = ref.strip().lower()
    for e in entries:
        if e["id"].lower() == low:
            return e
    for ext in (".yml", ".yaml", ".json", ".sql", ".md", ".xlsx", ".pdf", ".png", ".html", ".csv"):
        if (DOCS / (ref + ext)).exists():
            return ref
    return None


def doc_url(doc_id: str) -> str:
    return "/docs/" + "/".join(urllib.parse.quote(seg) for seg in doc_id.lower().split("/")) + "/"


def save_entry_fields(entry, fields: dict):
    if entry["kind"] == "md":
        meta, rest = parse_md(entry["text"])
        meta.update(fields)
        text = render_md(meta, rest)
    else:
        meta, rest = parse_sql_meta(entry["text"])
        meta.update(fields)
        text = render_sql_meta(meta, rest)
    entry["path"].write_text(text, encoding="utf-8")
    entry["text"] = text
    entry["meta"].update(fields)


CHANGELOG_RE = re.compile(r"^##\s+本版变更\s*$", re.M)
ITEM_RE = re.compile(r"^(?:[-*+]|\d+\.)\s+")


def changelog_list_end(text: str, start: int) -> int:
    """从 start 起，吃掉空行与连续列表项；遇到任何非列表内容立即停止（绝不越界删除）。"""
    end = start
    for m in re.finditer(r"[^\n]*\n?", text[start:]):
        stripped = m.group(0).strip()
        if stripped == "":
            continue
        if ITEM_RE.match(stripped):
            end = start + m.end()
            continue
        break
    return end


def changelog_md(changes) -> str:
    items = "\n".join("- " + str(c).strip() for c in changes if str(c).strip())
    return f"## 本版变更\n\n{items}\n\n"


def inject_changelog_md(rest: str, changes) -> str:
    section = changelog_md(changes)
    m = CHANGELOG_RE.search(rest)
    if m:
        head_end = rest.find("\n", m.end())
        head_end = len(rest) if head_end == -1 else head_end + 1
        end = changelog_list_end(rest, head_end)
        return rest[:m.start()] + section + rest[end:].lstrip("\n")
    m1 = re.match(r"(#\s+[^\n]*\n)", rest)
    if m1:
        return rest[:m1.end()] + "\n" + section + rest[m1.end():].lstrip("\n")
    return section + rest.lstrip("\n")


def inject_changelog_sql(rest: str, changes) -> str:
    items = "\n".join("--   - " + str(c).strip() for c in changes if str(c).strip())
    block = f"-- 本版变更:\n{items}\n\n"
    m = re.search(r"^--\s*本版变更[^\n]*\n(?:--\s*-\s[^\n]*\n)*", rest, re.M)
    if m:
        return rest[:m.start()] + block + rest[m.end():].lstrip("\n")
    return block + rest.lstrip("\n")


def run_git(args, cwd=None):
    proc = subprocess.run(["git"] + args, cwd=str(cwd or DOCS), capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def run_build():
    proc = subprocess.run(["npm", "run", "build"], cwd=str(BASE), capture_output=True, text=True, timeout=900)
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode == 0, "\n".join(out.splitlines()[-8:])


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>登录 · 文枢</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<style>
  @font-face { font-family: 'Maple Mono CN'; src: url('/fonts/MapleMono-CN-Regular.woff2') format('woff2'); font-weight: 400; font-display: swap; }
  @font-face { font-family: 'Maple Mono CN'; src: url('/fonts/MapleMono-CN-Bold.woff2') format('woff2'); font-weight: 700; font-display: swap; }
  * { box-sizing: border-box; }
  html { font-synthesis: none; -webkit-font-smoothing: antialiased; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0a0e1a; color: #e8edf7;
    font-family: 'Maple Mono CN', Consolas, monospace;
  }
  .card {
    width: min(400px, calc(100% - 40px)); background: #111a2e;
    border: 1px solid #1d2a45; border-radius: 18px; padding: 32px 30px 28px;
    box-shadow: 0 24px 60px rgba(0, 0, 0, .45);
  }
  .brand { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
  .brand img { width: 40px; height: 40px; }
  .brand h1 { margin: 0; font-size: 24px; letter-spacing: 3px; color: #6ea8fe; }
  .sub { margin: 0 0 24px; color: #8d99b3; font-size: 12.5px; letter-spacing: .5px; }
  label { display: block; font-size: 12px; color: #8d99b3; margin-bottom: 8px; }
  input[type=password] {
    width: 100%; height: 42px; padding: 0 14px; font-size: 14px;
    background: #0f1627; color: #e8edf7; border: 1px solid #1d2a45; border-radius: 10px;
    outline: none; font-family: inherit;
  }
  input[type=password]:focus { border-color: #6ea8fe; box-shadow: 0 0 0 3px rgba(110,168,254,.14); }
  button {
    width: 100%; height: 42px; margin-top: 18px; cursor: pointer;
    background: linear-gradient(135deg, #6ea8fe, #a78bfa); color: #0a0e1a;
    border: 0; border-radius: 10px; font-size: 14px; font-weight: 700; font-family: inherit;
    letter-spacing: 2px;
  }
  button:hover { filter: brightness(1.08); }
  .error { margin-top: 14px; color: #f87171; font-size: 12.5px; }
  .foot { margin-top: 20px; color: #5d6a86; font-size: 11.5px; }
</style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <div class="brand"><img src="/logo.svg" alt="" /><h1>文枢</h1></div>
    <p class="sub">文档脉络 · 版本有迹 · 人机共读</p>
    <label for="password">访问密码</label>
    <input id="password" name="password" type="password" autofocus autocomplete="current-password" />
    <input type="hidden" name="next" value="{{NEXT}}" />
    <button type="submit">进入文枢</button>
    {{ERROR}}
    <div class="foot">内网服务 · 登录状态保留 7 天</div>
  </form>
</body>
</html>
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "WenShu/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] {fmt % args}")

    # ---------- 工具 ----------
    def cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = urllib.parse.unquote(v)
        return out

    def queue_cookie(self, name, value, max_age):
        if not hasattr(self, "_pending_cookies"):
            self._pending_cookies = []
        self._pending_cookies.append(
            f"{name}={urllib.parse.quote(value)}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
        )

    def send_response(self, code, message=None):
        super().send_response(code, message)
        for cookie in getattr(self, "_pending_cookies", []):
            self.send_header("Set-Cookie", cookie)
        self._pending_cookies = []

    def is_authed(self):
        sid = self.cookies().get("ws_sid")
        return bool(sid and sessions.get(sid, 0) > now_ts())

    def share_grant(self):
        token = self.cookies().get("ws_share")
        item = shares.get(token) if token else None
        if item and item.get("expires", 0) > now_ts():
            item["files"] = extract_doc_files(item["path"])
            return item
        return None

    def share_covers(self, grant, path):
        target = urllib.parse.unquote(grant.get("path", ""))
        if path == target or path.startswith(target):
            return True
        if any(path.startswith(p) for p in SHARE_ASSET_PREFIXES):
            return True
        if path.startswith("/files/"):
            return path in set(grant.get("files", []))
        return False

    def redirect(self, location, code=303):
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def json_response(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def login_page(self, error=""):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        nxt = safe_next(qs.get("next", ["/"])[0])
        body = LOGIN_HTML.replace("{{ERROR}}", f'<div class="error">{escape(error)}</div>' if error else "")
        body = body.replace("{{NEXT}}", escape(nxt, quote=True))
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- Admin API ----------
    def api_auth(self):
        key = self.headers.get("X-API-Key", "")
        return bool(API_TOKEN) and secrets.compare_digest(key, API_TOKEN)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        return json.loads(body.decode("utf-8")) if body else {}

    def entry_brief(self, e):
        m = e["meta"]
        return {
            "id": e["id"], "title": m.get("title", ""), "project": m.get("project", ""),
            "domain": m.get("domain", ""), "topic": m.get("topic", ""), "type": m.get("type", ""),
            "status": m.get("status", ""), "date": m.get("date", ""),
            "version": m.get("version"), "url": doc_url(e["id"]),
            "summary": m.get("summary", ""), "related": m.get("related", []),
            "supersedes": m.get("supersedes"), "superseded_by": m.get("superseded_by"),
        }

    def admin_pending(self):
        entries = read_entries()
        items = [self.entry_brief(e) for e in entries if e["meta"].get("status") == "pending"]
        return self.json_response(200, {"count": len(items), "items": items})

    def admin_context(self, qs):
        entries = read_entries()
        project_f = (qs.get("project", [""])[0] or "").strip()
        topic_f = (qs.get("topic", [""])[0] or "").strip()
        query = (qs.get("query", [""])[0] or "").strip().lower()
        tree = {}
        for e in entries:
            m = e["meta"]
            proj = str(m.get("project", "?"))
            topic = str(m.get("topic", "?"))
            if project_f and proj != project_f:
                continue
            if topic_f and topic != topic_f:
                continue
            if query:
                hay = " ".join(str(m.get(k, "")) for k in ("title", "topic", "summary", "project")).lower()
                if query not in hay and query not in e["text"][:3000].lower():
                    continue
            tree.setdefault(proj, {}).setdefault(topic, []).append(e)
        projects = []
        for proj, topics in sorted(tree.items()):
            tlist = []
            for topic, items in sorted(topics.items()):
                tlist.append({
                    "name": topic,
                    "count": len(items),
                    "pending": sum(1 for e in items if e["meta"].get("status") == "pending"),
                    "docs": [self.entry_brief(e) for e in sorted(items, key=lambda x: str(x["meta"].get("date", "")), reverse=True)],
                })
            projects.append({
                "name": proj,
                "count": sum(t["count"] for t in tlist),
                "pending": sum(t["pending"] for t in tlist),
                "topics": tlist,
            })
        pending = [self.entry_brief(e) for e in entries if e["meta"].get("status") == "pending"]
        return self.json_response(200, {"count": len(entries), "pending_count": len(pending),
                                        "projects": projects, "pending": pending})

    def admin_doc(self, qs):
        doc_id = (qs.get("id", [""])[0] or "").strip()
        entries = read_entries()
        e = find_entry(entries, doc_id)
        if not e:
            return self.json_response(404, {"error": "文档不存在: " + doc_id})
        if e["kind"] == "md":
            meta, rest = parse_md(e["text"])
        else:
            meta, rest = parse_sql_meta(e["text"])
        brief = self.entry_brief(e)
        brief["body"] = rest.lstrip("\n")
        brief["meta"] = meta
        return self.json_response(200, brief)

    def admin_write(self, mutate):
        with WRITE_LOCK:
            entries = read_entries()
            try:
                info = mutate(entries)
            except ApiError as exc:
                return self.json_response(exc.code, {"error": exc.message})
            except Exception as exc:  # noqa: BLE001
                return self.json_response(500, {"error": f"内部错误: {exc}"})
            ok, tail = run_build()
            if not ok:
                run_git(["checkout", "--", "."])
                return self.json_response(500, {"error": "构建失败，已回滚", "detail": tail})
            msg = info.pop("commit_message", "chore: wenshu api update")
            run_git(["add", "-A"])
            code, _, _ = run_git(["commit", "-m", msg])
            _, commit, _ = run_git(["rev-parse", "--short", "HEAD"])
            info.update({"ok": True, "built": True, "commit": commit if code == 0 else ""})
            return self.json_response(200, info)

    def admin_create(self, payload):
        def mutate(entries):
            project = str(payload.get("project", "")).strip()
            topic = str(payload.get("topic", "")).strip()
            dtype = str(payload.get("type", "sql")).strip()
            title = str(payload.get("title", "")).strip()
            content = str(payload.get("content", ""))
            if not project:
                raise ApiError(400, "project 必填")
            if not topic:
                raise ApiError(400, "topic 必填")
            if dtype not in DOC_TYPES:
                raise ApiError(400, "type 必须是: " + "/".join(DOC_TYPES))
            if not title:
                raise ApiError(400, "title 必填")
            if not content.strip():
                raise ApiError(400, "content 必填")
            domain = str(payload.get("domain", "")).strip()
            status = str(payload.get("status", DEFAULT_STATUS[dtype])).strip()
            if status not in STATUS_SETS[dtype]:
                raise ApiError(400, f"{dtype} 的状态只能是 {STATUS_SETS[dtype]}")
            if status == "archived":
                raise ApiError(400, "新建文档不能直接置为 archived（归档只能来自升版自动归档或后续人工状态操作）")
            related = payload.get("related") or []
            if not isinstance(related, list):
                raise ApiError(400, "related 必须是数组")
            bad = [r for r in related if not resolve_ref(entries, str(r))]
            if bad:
                raise ApiError(400, "related 引用不存在: " + ", ".join(map(str, bad)))
            date = str(payload.get("date") or datetime.date.today().isoformat())
            if dtype in TYPE_SUBDIR:
                parts = [project] + ([domain] if domain else []) + [TYPE_SUBDIR[dtype]]
            elif domain:
                parts = [project, domain]
            else:
                parts = [project, "文档"]
            target_dir = DOCS.joinpath(*parts)
            if dtype == "sql":
                fname = str(payload.get("filename") or "").strip()
                if not fname:
                    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", topic).strip("-") or "migration"
                    fname = f"迁移-{date.replace('-', '')}-{slug}"
                if not fname.endswith(".sql"):
                    fname += ".sql"
            else:
                fname = re.sub(r'[\\/:*?"<>|]', "-", title)
                if not fname.endswith(".md"):
                    fname += ".md"
            path = target_dir / fname
            if path.exists():
                raise ApiError(409, "文件已存在: " + path.relative_to(DOCS).as_posix())
            head = {"title": path.stem if dtype == "sql" else title, "project": project}
            if domain:
                head["domain"] = domain
            head.update({"topic": topic, "type": dtype, "status": status})
            if payload.get("version"):
                head["version"] = int(payload["version"])
            head["date"] = date
            if related:
                head["related"] = [str(r) for r in related]
            if payload.get("summary"):
                head["summary"] = str(payload["summary"])
            path.parent.mkdir(parents=True, exist_ok=True)
            if dtype == "sql":
                path.write_text(render_sql_meta(head, content.lstrip("\n")), encoding="utf-8")
            else:
                path.write_text(render_md(head, "\n" + content.lstrip("\n")), encoding="utf-8")
            rel = path.relative_to(DOCS).as_posix()
            doc_id = rel.rsplit(".", 1)[0]
            return {"id": doc_id, "url": doc_url(doc_id), "path": rel,
                    "commit_message": f"feat: 新建文档 {doc_id}"}

        return self.admin_write(mutate)

    def admin_version(self, payload):
        def mutate(entries):
            src_id = str(payload.get("id", "")).strip()
            content = str(payload.get("content", ""))
            if not src_id:
                raise ApiError(400, "id 必填")
            if not content.strip():
                raise ApiError(400, "content 必填")
            entry = find_entry(entries, src_id)
            if not entry:
                raise ApiError(404, "文档不存在: " + src_id)
            meta = entry["meta"]
            dtype = str(meta.get("type", "other"))
            status = str(meta.get("status") or "").strip()
            changes = payload.get("changes")
            if dtype == "sql":
                if status != "executed":
                    raise ApiError(400, f"当前 SQL 状态为「{status or '未设置'}」：未执行（pending）的 SQL 请用 wenshu_update 或 wenshu_patch 原地修改，仅已执行（executed）后才出新迁移")
            elif dtype == "api":
                if status != "implemented":
                    raise ApiError(400, f"当前接口文档状态为「{status or '未设置'}」：未实现（pending/draft）请用 wenshu_update 或 wenshu_patch 原地修改，仅已实现（implemented）且改动较大时才升版")
                if payload.get("major_change") is not True:
                    raise ApiError(400, "升版被拒：需要明确声明 major_change=true（仅当当前版本已实现且改动较大：新增/删除接口、契约实质变化或大范围改写）")
                if not (isinstance(changes, list) and [c for c in changes if str(c).strip()]):
                    raise ApiError(400, "升版被拒：必须填写 changes（一条一个改动点）")
            if isinstance(changes, list) and [c for c in changes if str(c).strip()]:
                content = inject_changelog_sql(content, changes) if dtype == "sql" else inject_changelog_md(content, changes)
            old_version = int(meta.get("version") or 0)
            new_version = old_version + 1 if payload.get("version") is None else int(payload["version"])
            date = str(payload.get("date") or datetime.date.today().isoformat())
            if dtype == "sql":
                fname = str(payload.get("filename") or "").strip()
                if not fname:
                    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(meta.get("topic", ""))).strip("-") or "migration"
                    fname = f"迁移-{date.replace('-', '')}-{slug}"
                if not fname.endswith(".sql"):
                    fname += ".sql"
                path = entry["path"].parent / fname
                if path.exists():
                    raise ApiError(409, "文件已存在: " + path.name)
                head = {"title": path.stem, "project": meta.get("project", ""), "topic": meta.get("topic", ""),
                        "type": "sql", "status": str(payload.get("status") or "pending"), "date": date}
                if meta.get("domain"):
                    head["domain"] = meta["domain"]
                head["related"] = [str(r) for r in (payload.get("related") or [entry["id"]])]
                path.write_text(render_sql_meta(head, content.lstrip("\n")), encoding="utf-8")
                new_id = path.relative_to(DOCS).as_posix().rsplit(".", 1)[0]
                return {"id": new_id, "url": doc_url(new_id),
                        "path": path.relative_to(DOCS).as_posix(), "related": head["related"],
                        "commit_message": f"feat: 新增迁移 {new_id}"}
            base = re.sub(r"-v\d+$", "", entry["path"].stem)
            old_title = re.sub(r"[（(]\s*v\d+\s*[)）]\s*$", "", str(meta.get("title", "")))
            old_title = re.sub(r"[- ]v\d+\s*$", "", old_title).strip()
            title = str(payload.get("title") or (old_title + f"（v{new_version}）"))
            path = entry["path"].parent / f"{base}-v{new_version}.md"
            if path.exists():
                raise ApiError(409, "文件已存在: " + path.name)
            head = dict(meta)
            head.pop("superseded_by", None)
            head.pop("revision", None)
            head.pop("updated", None)
            head.update({"title": title, "version": new_version,
                         "status": str(payload.get("status") or "pending"), "date": date,
                         "supersedes": entry["id"]})
            if payload.get("related"):
                head["related"] = [str(r) for r in payload["related"]]
            path.write_text(render_md(head, "\n" + content.lstrip("\n")), encoding="utf-8")
            new_id = path.relative_to(DOCS).as_posix().rsplit(".", 1)[0]
            save_entry_fields(entry, {"superseded_by": new_id, "status": "archived"})
            return {"id": new_id, "url": doc_url(new_id),
                    "path": path.relative_to(DOCS).as_posix(), "supersedes": entry["id"],
                    "commit_message": f"feat: 版本化 {new_id}"}

        return self.admin_write(mutate)

    def admin_status(self, payload):
        def mutate(entries):
            doc_id = str(payload.get("id", "")).strip()
            status = str(payload.get("status", "")).strip()
            if not doc_id or not status:
                raise ApiError(400, "id 与 status 必填")
            entry = find_entry(entries, doc_id)
            if not entry:
                raise ApiError(404, "文档不存在: " + doc_id)
            dtype = str(entry["meta"].get("type", "other"))
            allowed = STATUS_SETS.get(dtype, STATUS_SETS["other"])
            if status not in allowed:
                raise ApiError(400, f"{dtype} 的状态只能是 {allowed}")
            old = entry["meta"].get("status")
            if old == status:
                return {"id": entry["id"], "status": status, "unchanged": True,
                        "url": doc_url(entry["id"]), "commit_message": "chore: noop status"}
            save_entry_fields(entry, {"status": status})
            return {"id": entry["id"], "status": status, "previous": old,
                    "url": doc_url(entry["id"]), "commit_message": f"chore: {entry['id']} {old} -> {status}"}

        return self.admin_write(mutate)

    def admin_meta(self, payload):
        def mutate(entries):
            doc_id = str(payload.get("id", "")).strip()
            if not doc_id:
                raise ApiError(400, "id 必填")
            entry = find_entry(entries, doc_id)
            if not entry:
                raise ApiError(404, "文档不存在: " + doc_id)
            fields = {}
            for k in ("topic", "domain", "summary", "title"):
                if payload.get(k) is not None:
                    v = str(payload[k]).strip()
                    if not v:
                        raise ApiError(400, f"{k} 不能为空")
                    fields[k] = v
            if payload.get("related") is not None:
                related = payload["related"]
                if not isinstance(related, list):
                    raise ApiError(400, "related 必须是数组")
                bad = [r for r in related if not resolve_ref(entries, str(r))]
                if bad:
                    raise ApiError(400, "related 引用不存在: " + ", ".join(map(str, bad)))
                fields["related"] = [str(r) for r in related]
            changes = payload.get("changes")
            if changes is not None:
                if not isinstance(changes, list) or not [c for c in changes if str(c).strip()]:
                    raise ApiError(400, "changes 必须是非空数组")
                if entry["kind"] != "md":
                    raise ApiError(400, "changes 仅支持 md 文档")
                changes = [str(c).strip() for c in changes if str(c).strip()]
            if not fields and changes is None:
                raise ApiError(400, "没有可更新的字段（topic/domain/summary/related/title/changes）")
            if changes is not None:
                meta, rest = parse_md(entry["text"])
                meta.update(fields)
                rest = inject_changelog_md(rest.lstrip("\n"), changes)
                text = render_md(meta, "\n" + rest.lstrip("\n"))
                entry["path"].write_text(text, encoding="utf-8")
                entry["text"] = text
                entry["meta"].update(fields)
            else:
                save_entry_fields(entry, fields)
            updated = dict(fields)
            if changes is not None:
                updated["changes"] = changes
            return {"id": entry["id"], "updated": updated, "url": doc_url(entry["id"]),
                    "commit_message": f"chore: 更新元数据 {entry['id']}"}

        return self.admin_write(mutate)

    def admin_patch(self, payload):
        def mutate(entries):
            doc_id = str(payload.get("id", "")).strip()
            reason = str(payload.get("reason", "")).strip()
            edits = payload.get("edits")
            if not doc_id:
                raise ApiError(400, "id 必填")
            if not reason:
                raise ApiError(400, "reason 必填（一句话说明补丁原因，会写入 git 记录）")
            if len(reason) > 200:
                raise ApiError(400, "reason 不能超过 200 字")
            bulk = bool(payload.get("bulk"))
            max_items, max_one, max_total = (40, 20000, 20000) if bulk else (5, 300, 800)
            if not isinstance(edits, list) or not edits:
                raise ApiError(400, "edits 必须是非空数组（{find, replace} 列表）")
            if len(edits) > max_items:
                raise ApiError(400, f"最多 {max_items} 条替换；超出补丁上限时：未实现版本请分批补丁，已实现版本再考虑升版")
            clean, total = [], 0
            for i, ed in enumerate(edits):
                if not isinstance(ed, dict):
                    raise ApiError(400, f"第 {i + 1} 条必须是 {{find, replace}} 对象")
                find = str(ed.get("find", ""))
                replace = str(ed.get("replace", ""))
                if not find:
                    raise ApiError(400, f"第 {i + 1} 条 find 不能为空")
                if len(find) > max_one or len(replace) > max_one:
                    raise ApiError(400, f"第 {i + 1} 条替换超过 {max_one} 字；超出补丁上限时：未实现版本请分批补丁，已实现版本再考虑升版")
                total += len(find) + len(replace)
                clean.append((find, replace))
            if total > max_total:
                raise ApiError(400, f"替换合计超过 {max_total} 字；超出补丁上限时：未实现版本请分批补丁，已实现版本再考虑升版")
            entry = find_entry(entries, doc_id)
            if not entry:
                raise ApiError(404, "文档不存在: " + doc_id)
            meta = entry["meta"]
            if str(meta.get("superseded_by") or "").strip():
                raise ApiError(400, "该文档已被新版替代（历史版本），请对当前版打补丁或出新版")
            status = str(meta.get("status") or "").strip()
            if status == "archived":
                raise ApiError(400, "归档文档不可修改，请出新版")
            if entry["kind"] == "sql":
                if status != "pending":
                    raise ApiError(400, "仅未执行（pending）的 SQL 可打补丁；已执行脚本不可就地修改，请出新迁移")
                cur_meta, rest = parse_sql_meta(entry["text"])
                if cur_meta is None:
                    raise ApiError(400, "文档格式异常（缺少 -- @meta）")
            else:
                cur_meta, rest = parse_md(entry["text"])
                if cur_meta is None:
                    raise ApiError(400, "文档格式异常（缺少 frontmatter）")
            for i, (find, replace) in enumerate(clean):
                n = rest.count(find)
                if n != 1:
                    raise ApiError(400, f"第 {i + 1} 条 find 命中 {n} 次（必须恰好 1 次）：{find[:40]}")
                rest = rest.replace(find, replace, 1)
            revision = int(meta.get("revision") or 0) + 1
            updated = datetime.date.today().isoformat()
            meta["revision"] = revision
            meta["updated"] = updated
            text = render_sql_meta(meta, rest) if entry["kind"] == "sql" else render_md(meta, rest)
            entry["path"].write_text(text, encoding="utf-8")
            entry["text"] = text
            entry["meta"].update({"revision": revision, "updated": updated})
            return {"id": entry["id"], "patches": len(clean), "reason": reason,
                    "revision": revision, "updated": updated, "url": doc_url(entry["id"]),
                    "commit_message": f"chore: 补丁 {entry['id']} — {reason}（不升版）"}

        return self.admin_write(mutate)

    def admin_update(self, payload):
        def mutate(entries):
            doc_id = str(payload.get("id", "")).strip()
            content = str(payload.get("content", ""))
            reason = str(payload.get("reason", "")).strip()
            if not doc_id:
                raise ApiError(400, "id 必填")
            if not content.strip():
                raise ApiError(400, "content 必填")
            if not reason:
                raise ApiError(400, "reason 必填（一句话说明本次更新，会写入 git 记录）")
            if len(reason) > 200:
                raise ApiError(400, "reason 不能超过 200 字")
            entry = find_entry(entries, doc_id)
            if not entry:
                raise ApiError(404, "文档不存在: " + doc_id)
            meta = entry["meta"]
            status = str(meta.get("status") or "").strip()
            if str(meta.get("superseded_by") or "").strip():
                raise ApiError(400, "该文档已被新版替代（历史版本），不可原地更新")
            if status == "archived":
                raise ApiError(400, "归档文档不可修改，请出新版")
            if status not in ("pending", "draft"):
                raise ApiError(400, f"仅未实现/未定稿（pending/draft）的文档可原地完整更新；当前状态「{status or '未设置'}」，小改动请用 wenshu_patch，已实现且改动较大请升版")
            exp = payload.get("expected_revision")
            cur_rev = int(meta.get("revision") or 0)
            if exp is not None and int(exp) != cur_rev:
                raise ApiError(409, f"revision 不匹配（当前 {cur_rev}，传入 {int(exp)}）：文档可能已被更新，请重新读取后再提交")
            changes = payload.get("changes")
            if changes is not None:
                if not isinstance(changes, list) or not [c for c in changes if str(c).strip()]:
                    raise ApiError(400, "changes 必须是非空数组")
                changes = [str(c).strip() for c in changes if str(c).strip()]
            revision = cur_rev + 1
            updated = datetime.date.today().isoformat()
            meta["revision"] = revision
            meta["updated"] = updated
            body = content.lstrip("\n")
            if entry["kind"] == "sql":
                if changes is not None:
                    body = inject_changelog_sql(body, changes)
                text = render_sql_meta(meta, body)
            else:
                if changes is not None:
                    body = inject_changelog_md(body, changes)
                text = render_md(meta, "\n" + body.lstrip("\n"))
            entry["path"].write_text(text, encoding="utf-8")
            entry["text"] = text
            entry["meta"].update({"revision": revision, "updated": updated})
            return {"id": entry["id"], "mode": "updated", "revision": revision, "updated": updated,
                    "url": doc_url(entry["id"]), "status": status,
                    "commit_message": f"chore: 原地更新 {entry['id']} — {reason}（不升版）"}

        return self.admin_write(mutate)

    def admin_delete(self, payload):
        def mutate(entries):
            doc_id = str(payload.get("id", "")).strip()
            if not doc_id:
                raise ApiError(400, "id 必填")
            entry = find_entry(entries, doc_id)
            if not entry:
                raise ApiError(404, "文档不存在: " + doc_id)
            rel = entry["path"].relative_to(DOCS).as_posix()
            entry["path"].unlink()
            for other in entries:
                if other is entry:
                    continue
                m = other["meta"]
                changed = None
                if isinstance(m.get("related"), list) and entry["id"] in m["related"]:
                    m["related"] = [x for x in m["related"] if x != entry["id"]]
                    changed = {"related": m["related"]}
                if changed is None and m.get("supersedes") == entry["id"]:
                    changed = {"supersedes": ""}
                if changed is None and m.get("superseded_by") == entry["id"]:
                    changed = {"superseded_by": ""}
                if changed is not None:
                    save_entry_fields(other, changed)
            return {"id": entry["id"], "deleted": rel, "url": "/",
                    "commit_message": f"chore: 删除文档 {entry['id']}"}

        return self.admin_write(mutate)

    # ---------- GET ----------
    def do_GET(self):
        purge_expired()
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path == "/login":
            return self.login_page()
        if path == "/logout":
            self.queue_cookie("ws_sid", "", 0)
            self.queue_cookie("ws_share", "", 0)
            return self.redirect("/login")
        if path.startswith("/s/"):
            return self.consume_share(path[3:])
        if path.startswith("/api/admin/"):
            if not self.api_auth():
                return self.json_response(401, {"error": "API Key 无效"})
            qs = urllib.parse.parse_qs(parsed.query)
            if path == "/api/admin/pending":
                return self.admin_pending()
            if path == "/api/admin/context":
                return self.admin_context(qs)
            if path == "/api/admin/doc":
                return self.admin_doc(qs)
            return self.json_response(404, {"error": "unknown admin endpoint"})
        if self.is_authed() or path in PUBLIC_FILES or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return super().do_GET()
        grant = self.share_grant()
        if grant and self.share_covers(grant, path):
            return super().do_GET()
        return self.redirect("/login?next=" + urllib.parse.quote(self.path))

    def consume_share(self, token):
        item = shares.get(token)
        if not item or item.get("expires", 0) < now_ts():
            return self.redirect("/login")
        if "files" not in item:
            item["files"] = extract_doc_files(item["path"])
            with lock:
                save_shares()
        remain = int(item["expires"] - now_ts())
        self.queue_cookie("ws_share", token, max(remain, 60))
        target = item["path"]
        if "?" not in target:
            target += "?share=1"
        return self.redirect(target)

    # ---------- POST ----------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if parsed.path == "/login":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            password = form.get("password", [""])[0]
            nxt = safe_next(form.get("next", ["/"])[0])
            if PASSWORD and password == PASSWORD:
                sid = secrets.token_urlsafe(24)
                with lock:
                    sessions[sid] = now_ts() + SESSION_TTL
                    save_sessions()
                self.queue_cookie("ws_sid", sid, SESSION_TTL)
                return self.redirect(nxt)
            return self.login_page(error="密码不正确，请重试")
        if parsed.path == "/api/share":
            if not self.is_authed():
                return self.json_response(401, {"error": "未登录"})
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                return self.json_response(400, {"error": "请求格式错误"})
            target = payload.get("path", "")
            expires_raw = payload.get("expires", "")
            if not isinstance(target, str) or not target.startswith("/docs/"):
                return self.json_response(400, {"error": "只能分享单篇文档"})
            try:
                exp_ts = datetime.datetime.fromisoformat(expires_raw).timestamp()
            except Exception:
                return self.json_response(400, {"error": "到期时间格式错误"})
            if exp_ts <= now_ts():
                return self.json_response(400, {"error": "到期时间必须晚于现在"})
            token = secrets.token_urlsafe(16)
            with lock:
                shares[token] = {"path": target, "expires": exp_ts, "files": extract_doc_files(target)}
                save_shares()
            host = self.headers.get("Host", f"127.0.0.1:{PORT}")
            return self.json_response(200, {"url": f"http://{host}/s/{token}", "expires": expires_raw})
        if parsed.path.startswith("/api/admin/"):
            if not self.api_auth():
                return self.json_response(401, {"error": "API Key 无效"})
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                return self.json_response(400, {"error": "JSON 格式错误"})
            if parsed.path == "/api/admin/create":
                return self.admin_create(payload)
            if parsed.path == "/api/admin/version":
                return self.admin_version(payload)
            if parsed.path == "/api/admin/status":
                return self.admin_status(payload)
            if parsed.path == "/api/admin/meta":
                return self.admin_meta(payload)
            if parsed.path == "/api/admin/patch":
                return self.admin_patch(payload)
            if parsed.path == "/api/admin/update":
                return self.admin_update(payload)
            if parsed.path == "/api/admin/delete":
                return self.admin_delete(payload)
            return self.json_response(404, {"error": "unknown admin endpoint"})
        return self.json_response(404, {"error": "not found"})


def main():
    load_shares()
    load_sessions()
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"文枢服务已启动: http://0.0.0.0:{PORT}  (密码在 server_config.json)")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
