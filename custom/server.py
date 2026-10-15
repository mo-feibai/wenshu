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
# 分享过期后仍保留一段时间（不再授权，仅用于区分「已过期」与「链接无效」的提示文案）
SHARE_EXPIRED_KEEP = 30 * 24 * 3600
DOCS = pathlib.Path(config.get("docs_dir", "/opt/wenshu/docs"))
API_TOKEN = config.get("api_token", "")

sessions = {}
shares = {}
lock = threading.RLock()
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
# 状态中文名（与站点 helpers.ts 的 statusText 保持一致，供 /api/status-options 使用）
STATUS_LABELS = {"pending": "待处理", "executed": "已执行", "implemented": "已实现",
                 "draft": "草稿", "final": "定稿", "accepted": "已采纳", "archived": "已归档"}


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
                # 过期记录保留 SHARE_EXPIRED_KEEP 天：不再授权，但能让访客看到「已过期」而非「链接无效」
                if item.get("expires", 0) > t - SHARE_EXPIRED_KEEP:
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
        for k in [k for k, v in shares.items() if v.get("expires", 0) < t - SHARE_EXPIRED_KEEP]:
            del shares[k]
            _SHARE_FILE_CACHE.pop(k, None)
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
    """文档 URL。与前端 helpers.ts 的 docUrl 保持一致：不做小写化，仅逐段编码。"""
    return "/docs/" + "/".join(urllib.parse.quote(seg) for seg in doc_id.split("/")) + "/"


def doc_path_key(url: str) -> str:
    """把任意形态的 doc URL 归一化为比较键：解码 → 去尾斜杠 → 小写。

    库内所有 id 比较（find_entry / resolve_ref）都是大小写不敏感的，
    这里沿用同一约定，避免服务端与前端 URL 生成规则不一致导致授权失败。
    """
    return urllib.parse.unquote(url or "").split("?")[0].rstrip("/").lower()


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


def remove_entry_fields(entry, keys):
    if entry["kind"] == "md":
        meta, rest = parse_md(entry["text"])
        for k in keys:
            meta.pop(k, None)
        text = render_md(meta, rest)
    else:
        meta, rest = parse_sql_meta(entry["text"])
        for k in keys:
            meta.pop(k, None)
        text = render_sql_meta(meta, rest)
    entry["path"].write_text(text, encoding="utf-8")
    entry["text"] = text
    for k in keys:
        entry["meta"].pop(k, None)


# ---------------- 模块分享：当前版快照 ----------------

_SNAPSHOT = None              # 不可变快照；读侧无锁，写侧整体替换
_SHARE_FILE_CACHE = {}        # token -> {"files": set, "at": epoch}
FILE_CACHE_TTL = 300


def _entry_is_current(entry, superseded):
    """是否「当前版」：没有新版本指向它 + 自身未被标记取代 + 非 archived + 未淘汰。

    superseded_by 挂在旧版上（表示「我被谁取代」），因此它自身非空即历史版；
    supersedes 是新版指向旧版的正向链接，取其反向集合可兼容未回填 superseded_by 的情况。
    pending/draft 也算当前版（未实现的文档照样可以分享给外部）。
    """
    m = entry["meta"]
    if str(m.get("status") or "").strip() == "archived":
        return False
    if str(m.get("retired_at") or "").strip():
        return False
    if str(m.get("superseded_by") or "").strip():
        return False
    return entry["id"] not in superseded


def build_snapshot():
    """构建快照：取代反向索引 + doc URL 索引 + 按 topic 分组的当前版清单。

    「是否当前版」需要知道有没有别的文档用 supersedes 指向它，这是反向关系，
    只能全库扫描得出。因此这里一次性算好后缓存，写操作后失效重建，
    避免每次文档页请求都 O(N) 扫库。
    """
    entries = read_entries()
    superseded = set()
    for e in entries:
        sp = str(e["meta"].get("supersedes") or "").strip()
        if sp:
            superseded.add(sp)
    by_url = {}
    modules = {}
    by_project = {}
    kinds = {}
    for e in entries:
        by_url[doc_path_key(doc_url(e["id"]))] = e
        topic = str(e["meta"].get("topic") or "").strip()
        project = str(e["meta"].get("project") or "").strip()
        dtype = str(e["meta"].get("type") or "other").strip() or "other"
        if dtype not in STATUS_SETS:
            dtype = "other"
        if project:
            kinds.setdefault(project, {}).setdefault(dtype, 0)
            kinds[project][dtype] += 1
        if not _entry_is_current(e, superseded):
            continue
        if topic:
            modules.setdefault(topic, []).append(e)
        if project:
            by_project.setdefault(project, []).append(e)
    for lst in list(modules.values()) + list(by_project.values()):
        lst.sort(key=lambda x: (str(x["meta"].get("date") or ""), x["id"]), reverse=True)
    return {"by_url": by_url, "modules": modules, "by_project": by_project, "kinds": kinds}


def snapshot():
    global _SNAPSHOT
    if _SNAPSHOT is None:
        with lock:
            if _SNAPSHOT is None:
                _SNAPSHOT = build_snapshot()
    return _SNAPSHOT


def invalidate_snapshot():
    """任何写操作后调用：快照与附件白名单都失效重建。"""
    global _SNAPSHOT
    with lock:
        _SNAPSHOT = None
        _SHARE_FILE_CACHE.clear()


def module_scope(item):
    """分享记录 → 规范化 scope；单篇分享返回 None。

    scope 为多维模型，形如：
      {"kind": "topic",    "topic": "助农", "projects": {...} | None}
      {"kind": "project",  "project": "xingtai", "type": "sql" | None}
    只允许这两种 module 组合（project-type 是 project 的特化）。
    projects 为空表示该 topic 名下的全部项目都包含（与主题页跨项目合并的展示一致）。
    """
    if not item or item.get("scope") != "module":
        return None
    topic = str(item.get("topic") or "").strip()
    if topic:
        raw = item.get("projects")
        if isinstance(raw, str):
            raw = [raw]
        projs = {str(p).strip() for p in (raw or []) if str(p).strip()}
        return {"kind": "topic", "topic": topic, "project": "", "type": "",
                "projects": projs or None}
    project = str(item.get("project") or "").strip()
    if not project:
        return None
    dtype = str(item.get("type") or "").strip()
    return {"kind": "project", "project": project, "type": dtype, "topic": "", "projects": None}


def scope_label(scope):
    """分享管理页/失效页用的作用域标签。"""
    if not scope:
        return ""
    if scope["kind"] == "topic":
        return "主题 · " + scope["topic"]
    if scope["type"]:
        return f"项目 · {scope['project']} · {scope['type']}"
    return "项目 · " + scope["project"]


def scope_landing(scope):
    """分享落地页 URL（访客点 /s/<token> 后被重定向到这里）。"""
    if not scope:
        return ""
    if scope["kind"] == "topic":
        return "/topics/" + urllib.parse.quote(scope["topic"]) + "/?share=1"
    q = "?share=1" + ("&type=" + urllib.parse.quote(scope["type"]) if scope["type"] else "")
    return "/projects/" + urllib.parse.quote(scope["project"]) + "/" + q


def scope_entries(scope):
    """作用域覆盖的文档（当前版 + 动态跟随）。"""
    if not scope:
        return []
    snap = snapshot()
    if scope["kind"] == "topic":
        lst = snap["modules"].get(scope["topic"], [])
        if scope["projects"]:
            lst = [e for e in lst if str(e["meta"].get("project") or "").strip() in scope["projects"]]
        return lst
    lst = snap["by_project"].get(scope["project"], [])
    if scope["type"]:
        lst = [e for e in lst if str(e["meta"].get("type") or "other").strip() == scope["type"]]
    return lst


def module_entries(item):
    """分享记录 → 作用域内当前版文档（兼容旧调用名）。"""
    return scope_entries(module_scope(item))


def scope_matches_entry(scope, entry):
    """该文档是否落在作用域内（用于文档页鉴权与链接过滤）。"""
    if not scope or not entry:
        return False
    meta = entry["meta"]
    if scope["kind"] == "topic":
        if str(meta.get("topic") or "").strip() != scope["topic"]:
            return False
        if scope["projects"] and str(meta.get("project") or "").strip() not in scope["projects"]:
            return False
        return True
    if str(meta.get("project") or "").strip() != scope["project"]:
        return False
    if scope["type"] and str(meta.get("type") or "other").strip() != scope["type"]:
        return False
    return True


def share_files(token, item):
    """某分享的附件白名单：单篇=该文档引用的 /files/；模块=模块内全部文档的并集。带 TTL 缓存。"""
    now = now_ts()
    hit = _SHARE_FILE_CACHE.get(token)
    if hit and hit["at"] + FILE_CACHE_TTL > now:
        return hit["files"]
    files = set()
    if module_scope(item):
        for e in module_entries(item):
            files.update(extract_doc_files(doc_url(e["id"])))
    else:
        files.update(extract_doc_files(item.get("path", "")))
    with lock:
        _SHARE_FILE_CACHE[token] = {"files": files, "at": now}
    return files


def share_valid(item):
    return bool(item) and not item.get("revoked_at") and item.get("expires", 0) > now_ts()


def share_brief(token, item):
    scope = module_scope(item)
    brief = {
        "token": token,
        "scope": scope["kind"] if scope else "doc",
        "path": item.get("path", ""),
        "expires": item.get("expires", 0),
        "created": item.get("created"),
        "revoked": bool(item.get("revoked_at")),
        "revoked_at": item.get("revoked_at"),
    }
    if scope:
        brief["kind"] = scope["kind"]
        brief["topic"] = scope["topic"]
        brief["project"] = scope["project"]
        brief["type"] = scope["type"]
        briefs = sorted({str(p) for p in (scope.get("projects") or []) if p})
        brief["projects"] = briefs
        entries = module_entries(item)
        brief["doc_count"] = len(entries)
        brief["label"] = scope_label(scope)
        brief["docs"] = [{"id": e["id"], "title": str(e["meta"].get("title", "")),
                          "type": str(e["meta"].get("type", "")),
                          "status": str(e["meta"].get("status", ""))} for e in entries]
    else:
        entry = snapshot()["by_url"].get(doc_path_key(item.get("path", "")))
        title = str(entry["meta"].get("title", "")) if entry else ""
        brief["doc_count"] = 1 if entry else 0
        brief["label"] = f"单篇 · {title}" if title else f"单篇 · {item.get('path', '')}"
        brief["docs"] = [{"id": entry["id"], "title": title,
                          "type": str(entry["meta"].get("type", "")),
                          "status": str(entry["meta"].get("status", ""))}] if entry else []
    return brief


def _entry_body(entry):
    """文档正文（不含 frontmatter / -- @meta），搜索与摘要共用。"""
    if entry["kind"] == "md":
        _, rest = parse_md(entry["text"])
    else:
        _, rest = parse_sql_meta(entry["text"])
    return rest


def _snippet(body, term, width=110):
    """截取命中位置前后片段，返回 (片段, 是否前面被截断)。"""
    idx = body.lower().find(term)
    if idx < 0:
        return None, False
    start = max(0, idx - width // 2)
    end = min(len(body), idx + len(term) + width)
    frag = body[start:end].strip()
    if "\n" in frag:
        frag = " ".join(frag.split())
    return frag, start > 0


def search_module(items, term, limit=50):
    """在给定文档集合内做朴素子串搜索（中文无需分词）。返回按命中数降序的结果。"""
    q = term.strip()
    if not q:
        return []
    ql = q.lower()
    out = []
    for e in items:
        m = e["meta"]
        body = _entry_body(e)
        bl = body.lower()
        title = str(m.get("title", ""))
        hay = " ".join(str(m.get(k, "")) for k in ("title", "topic", "project", "summary")).lower()
        if ql not in bl and ql not in hay:
            continue
        frag, head = _snippet(body, ql) or ("", False)
        count = bl.count(ql) + hay.count(ql)
        out.append({
            "id": e["id"], "title": title,
            "type": str(m.get("type", "")), "status": str(m.get("status", "")),
            "url": doc_url(e["id"]), "hits": count,
            "snippet": (("…" if head else "") + frag) if frag else (title or body[:110].strip()),
        })
    out.sort(key=lambda x: (-x["hits"], x["title"]))
    return out[:limit]


# ---------------- 生命周期淘汰 ----------------

RETENTION_PATH = BASE / "retention.json"
RETENTION_DEFAULTS = {
    "T_A1": 90, "T_A2": 90, "T_B": 60, "T_C": 90, "T_D": 7, "T_E": 180,
    "GRACE_STALE_RETIRED": 7, "GRACE_RETIRED_DELETE": 30,
}


def load_retention():
    try:
        data = json.loads(RETENTION_PATH.read_text(encoding="utf-8"))
        out = dict(RETENTION_DEFAULTS)
        for k in out:
            if isinstance(data.get(k), int) and data[k] > 0:
                out[k] = data[k]
        return out
    except Exception:
        return dict(RETENTION_DEFAULTS)


def last_active(entry):
    """文档最后活跃日：git 最后提交日（兜底 frontmatter.updated）。"""
    code, out, _ = run_git(["log", "-1", "--format=%cs", "--", entry["rel"]])
    if code == 0 and out:
        try:
            return datetime.date.fromisoformat(out)
        except ValueError:
            pass
    for k in ("updated", "date"):
        v = entry["meta"].get(k)
        if v:
            try:
                return datetime.date.fromisoformat(str(v)[:10])
            except ValueError:
                pass
    return datetime.date.today()


def _inbound_refs(entries, entry):
    n = 0
    for other in entries:
        if other is entry:
            continue
        m = other["meta"]
        if entry["id"] in [str(x) for x in (m.get("related") or [])]:
            n += 1
        if m.get("supersedes") == entry["id"]:
            n += 1
    return n


def _has_active_share(entry):
    """是否有有效分享引用这篇文档（模块/项目分享按作用域豁免其全部当前版文档）。"""
    key = doc_path_key(doc_url(entry["id"]))
    for item in shares.values():
        if not share_valid(item):
            continue
        scope = module_scope(item)
        if scope:
            # B1：主题/项目两种 scope 都要能豁免，交给统一匹配函数
            if scope_matches_entry(scope, entry):
                return True
            continue
        if doc_path_key(item.get("path", "")) == key:
            return True
    return False


def _mutate_status(entries, payload):
    """状态翻转的唯一实现：admin_status（API Key）与 api_status（页面 session）共用。"""
    doc_id = str(payload.get("id", "")).strip()
    status = str(payload.get("status", "")).strip()
    if not doc_id or not status:
        raise ApiError(400, "id 与 status 必填")
    entry = find_entry(entries, doc_id)
    if not entry:
        raise ApiError(404, "文档不存在: " + doc_id)
    if str(entry["meta"].get("retired_at") or "").strip():
        raise ApiError(400, "已淘汰文档只读，请先 restore")
    dtype = str(entry["meta"].get("type", "other"))
    allowed = STATUS_SETS.get(dtype, STATUS_SETS["other"])
    if status not in allowed:
        raise ApiError(400, f"{dtype} 的状态只能是 {allowed}")
    old = entry["meta"].get("status")
    if old == status:
        return {"id": entry["id"], "status": status, "unchanged": True,
                "url": doc_url(entry["id"]), "commit_message": "chore: noop status"}
    # archived 是单向终态：旧版本被新版取代后不应再回到待办被重新实现
    if old == "archived":
        raise ApiError(400, "已归档是单向终态，不能翻回其它状态；需要恢复请出新版本")
    save_entry_fields(entry, {"status": status})
    _auto_clear_stale(entry)
    return {"id": entry["id"], "status": status, "previous": old,
            "url": doc_url(entry["id"]), "commit_message": f"chore: {entry['id']} {old} -> {status}"}


def _review_item(e, cls, stage, today):
    m = e["meta"]
    idle = (today - last_active(e)).days
    action = {"candidate": "标记过期", "retire_pending": "淘汰隐藏",
              "delete_pending": "建议删除", "review_only": "人工审阅"}.get(stage, "")
    return {
        "id": e["id"], "title": m.get("title", ""), "project": m.get("project", ""),
        "topic": m.get("topic", ""), "type": m.get("type", ""), "status": m.get("status", ""),
        "cls": cls, "stage": stage, "idle": idle, "last_active": last_active(e).isoformat(),
        "stale_at": m.get("stale_at"), "retired_at": m.get("retired_at"),
        "action": action, "url": doc_url(e["id"]),
    }


def review_entries(entries, overrides=None):
    """扫描全库，返回淘汰候选清单（只读）。"""
    th = load_retention()
    if overrides:
        for k in th:
            if isinstance(overrides.get(k), int) and overrides[k] > 0:
                th[k] = overrides[k]
    today = datetime.date.today()
    out = []
    for e in entries:
        m = e["meta"]
        status = str(m.get("status") or "")
        dtype = str(m.get("type") or "other")
        stale_at = m.get("stale_at")
        retired_at = m.get("retired_at")
        if dtype == "sql" and status == "executed":
            continue
        if status in ("implemented", "final", "accepted") and not str(m.get("superseded_by") or "").strip():
            idle = (today - last_active(e)).days
            if idle >= th["T_E"]:
                out.append(_review_item(e, "E", "review_only", today))
            continue
        if _inbound_refs(entries, e) > 0:
            continue
        if m.get("attachment") or m.get("attachments"):
            continue
        if _has_active_share(e):
            continue
        if stale_at or retired_at:
            stage = "candidate"
            if stale_at:
                try:
                    if (today - datetime.date.fromisoformat(str(stale_at)[:10])).days >= th["GRACE_STALE_RETIRED"]:
                        stage = "retire_pending"
                except ValueError:
                    pass
            if retired_at:
                try:
                    if (today - datetime.date.fromisoformat(str(retired_at)[:10])).days >= th["GRACE_RETIRED_DELETE"]:
                        stage = "delete_pending"
                except ValueError:
                    pass
            cls = "A1" if str(m.get("superseded_by") or "").strip() else "A2"
            out.append(_review_item(e, cls, stage, today))
            continue
        idle = (today - last_active(e)).days
        cls = None
        if status == "archived":
            cls = "A1" if str(m.get("superseded_by") or "").strip() else "A2"
            if idle < th["T_A1"]:
                cls = None
        elif status in ("pending", "draft"):
            if idle >= th["T_B"]:
                cls = "B"
        elif idle >= th["T_C"]:
            cls = "C"
        if cls is None:
            proj = str(m.get("project", ""))
            title = str(m.get("title", ""))
            if (proj == "demo" or any(k in title for k in ("临时", "测试", "示例"))) and idle >= th["T_D"]:
                cls = "D"
        if cls:
            out.append(_review_item(e, cls, "candidate", today))
    order = {"delete_pending": 0, "retire_pending": 1, "candidate": 2, "review_only": 3}
    out.sort(key=lambda x: (order.get(x["stage"], 9), -x["idle"]))
    return out


def _auto_clear_stale(entry):
    """写操作后自动清除 stale_at（编辑即复活）。"""
    if entry["meta"].get("stale_at"):
        remove_entry_fields(entry, ["stale_at"])


def recent_deletions(days=30):
    """最近 N 天删除的文档（从 git 历史读），用于看板「最近删除」与一键恢复。"""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    code, out, _ = run_git(["log", "--diff-filter=D", "--since=" + since,
                            "--format=%H|%cs|%s", "--name-only"])
    if code != 0 or not out:
        return []
    commits = []
    cur = None
    for line in out.splitlines():
        if "|" in line:
            if cur:
                commits.append(cur)
            sha, d, msg = line.split("|", 2)
            cur = {"sha": sha, "date": d[:10], "message": msg, "files": []}
        elif line.strip() and cur is not None:
            cur["files"].append(line.strip())
    if cur:
        commits.append(cur)
    res = []
    for c in commits:
        for f in c["files"]:
            if f.endswith((".md", ".sql")):
                res.append({"file": f, "sha": c["sha"], "date": c["date"], "message": c["message"]})
    return res


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


SHARE_NOTICE_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="robots" content="noindex" />
<title>{{TITLE}}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<style>
  @font-face { font-family: 'Maple Mono CN'; src: url('/fonts/MapleMono-CN-Regular.woff2') format('woff2'); font-weight: 400; font-display: swap; }
  * { box-sizing: border-box; }
  html { font-synthesis: none; -webkit-font-smoothing: antialiased; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0a0e1a; color: #e8edf7;
    font-family: 'Maple Mono CN', Consolas, monospace;
  }
  .card {
    width: min(440px, calc(100% - 40px)); background: #111a2e;
    border: 1px solid #1d2a45; border-radius: 18px; padding: 34px 30px 28px;
    box-shadow: 0 24px 60px rgba(0,0,0,.45);
  }
  h1 { margin: 0 0 10px; font-size: 20px; letter-spacing: 1px; color: #6ea8fe; }
  p { margin: 0 0 10px; font-size: 13.5px; line-height: 1.85; color: #c3ccdf; }
  .muted { color: #8d99b3; font-size: 12.5px; }
  .foot { margin-top: 22px; padding-top: 16px; border-top: 1px solid #1d2a45; }
  a.btn {
    display: inline-block; margin-top: 14px; padding: 9px 18px; border-radius: 10px;
    background: linear-gradient(135deg, #6ea8fe, #a78bfa); color: #0a0e1a;
    font-size: 13px; font-weight: 700; text-decoration: none; letter-spacing: 1px;
  }
</style>
</head>
<body>
  <div class="card">
    <h1>{{TITLE}}</h1>
    {{BODY}}
    <div class="foot"><span class="muted">{{FOOT}}</span></div>
  </div>
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

    def share_tokens(self):
        """cookie 里可能同时持有多个分享 token（逗号分隔，向后兼容旧的单值）。"""
        raw = self.cookies().get("ws_share") or ""
        return [t for t in (x.strip() for x in raw.split(",")) if t]

    def remember_share_token(self, token, max_age):
        """把 token 并入 cookie，保留其他仍有效的 token（上限 16 个防无限增长）。"""
        toks = [t for t in self.share_tokens()
                if t != token and share_valid(shares.get(t))]
        toks.append(token)
        self.queue_cookie("ws_share", ",".join(toks[-16:]), max_age)

    def share_grants(self):
        return [shares[t] for t in self.share_tokens() if share_valid(shares.get(t))]

    def grant_denial(self, item, path):
        """返回 None 表示该分享覆盖此路径；否则返回不可访问的原因文案。"""
        if any(path.startswith(p) for p in SHARE_ASSET_PREFIXES):
            return None
        if path.startswith("/files/"):
            token = next((t for t in self.share_tokens() if shares.get(t) is item), "")
            return None if path in share_files(token, item) else "该分享不包含此附件"
        scope = module_scope(item)
        if not scope:
            target = urllib.parse.unquote(item.get("path", "")).rstrip("/")
            if path.rstrip("/") == target or path.rstrip("/").startswith(target + "/"):
                return None
            return "该分享不包含此内容"
        # 模块/项目分享：落地页 + 作用域内的当前版文档
        bare = path.split("?")[0].rstrip("/")
        if scope["kind"] == "topic":
            if bare == "/topics/" + scope["topic"]:
                return None
        else:
            if bare == "/projects/" + scope["project"]:
                return None
        entry = snapshot()["by_url"].get(doc_path_key(path))
        if entry and any(e["id"] == entry["id"] for e in module_entries(item)):
            return None
        return "该分享不包含此内容"

    def grant_allows(self, path):
        grants = self.share_grants()
        for item in grants:
            if self.grant_denial(item, path) is None:
                return True
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

    def notice_page(self, title, lines, foot="文枢 · 分享链接", code=200, link=None, links=None):
        body = "".join(f"<p>{escape(t)}</p>" for t in lines)
        for text, href in ([link] if link else []) + list(links or []):
            body += f'<a class="btn" href="{escape(href, quote=True)}">{escape(text)}</a>'
        page = (SHARE_NOTICE_HTML.replace("{{TITLE}}", escape(title))
                .replace("{{BODY}}", body)
                .replace("{{FOOT}}", escape(foot)))
        data = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def share_invalid_page(self, token):
        """分享链接不可用的四种原因，给出可区分的文案。"""
        item = shares.get(token)
        if item is None:
            return self.notice_page(
                "分享链接无效",
                ["这个链接不存在，或已被分享者删除。", "请向分享者索取新的链接。"],
                link=("重新登录站点", "/login"))
        if item.get("revoked_at"):
            stamp = datetime.datetime.fromtimestamp(float(item["revoked_at"])).strftime("%Y-%m-%d %H:%M")
            return self.notice_page(
                "分享已被撤销",
                ["分享者已主动撤销这份分享，链接即刻失效。", f"撤销时间：{stamp}"],
                link=("重新登录站点", "/login"))
        if item.get("expires", 0) <= now_ts():
            stamp = datetime.datetime.fromtimestamp(float(item.get("expires", 0))).strftime("%Y-%m-%d %H:%M")
            return self.notice_page(
                "分享已过期",
                [f"这份分享的有效期已于 {stamp} 结束。", "如仍需查看，请向分享者申请延长。"],
                link=("重新登录站点", "/login"))
        return self.notice_page("分享不可用", ["链接状态异常，请向分享者索取新链接。"])

    def share_denied_page(self, reason, item=None):
        # 「返回分享首页」应回到这份分享自己的落地页（单篇=文档页，模块=主题页），
        # 并额外给出「以站点密码登录」出口，避免站点主人被卡死在这页。
        home = "/"
        scope = module_scope(item) if item else None
        if scope:
            home = scope_landing(scope)
        elif item and item.get("path"):
            home = item["path"] + ("&" if "?" in item["path"] else "?") + "share=1"
        nxt = "/login?next=" + urllib.parse.quote(self.path)
        return self.notice_page(
            "该分享不包含此内容",
            [reason, "这份分享只覆盖被分享者开放的那部分文档。"],
            links=[("返回分享首页", home), ("以站点密码登录", nxt)])

    # ---------- 分享 API（凭 ws_share cookie 鉴权） ----------
    def _pick_share(self, qs):
        """按客户端给出的上下文定位它所处的那一份分享（多token 时不会认错作用域）。"""
        grants = self.share_grants()
        if not grants:
            return None, []
        topic = (qs.get("topic", [""])[0] or "").strip()
        project = (qs.get("project", [""])[0] or "").strip()
        dtype = (qs.get("type", [""])[0] or "").strip()
        path = (qs.get("path", [""])[0] or "").strip()
        # A2：项目类型分享没有 topic，必须独立成一条匹配分支
        if project:
            for item in grants:
                sc = module_scope(item)
                if not sc or sc["kind"] != "project" or sc["project"] != project:
                    continue
                if dtype and sc["type"] and sc["type"] != dtype:
                    continue
                return item, grants
        if topic:
            for item in grants:
                sc = module_scope(item)
                if sc and sc["kind"] == "topic" and sc["topic"] == topic and (
                        not project or not sc["projects"] or project in sc["projects"]):
                    return item, grants
        key = doc_path_key(path)
        for item in grants:
            if not module_scope(item) and doc_path_key(item.get("path", "")) == key:
                return item, grants
        # 作用域分享的文档页：按文档反查它属于哪个分享（页面本身不知道自己的主题/项目）
        if path:
            entry = snapshot()["by_url"].get(key)
            if entry:
                for item in grants:
                    if scope_matches_entry(module_scope(item), entry):
                        return item, grants
        return None, grants

    def api_share_ctx(self, qs):
        """访客侧上下文：我是哪个模块的访客 + 该模块当前可见文档清单（驱动侧栏与标题）。"""
        picked, grants = self._pick_share(qs)
        if not picked:
            return self.json_response(404, {"error": "当前浏览器没有覆盖此页面的分享", "grants": len(grants)})
        scope = module_scope(picked)
        entry = snapshot()["by_url"].get(doc_path_key(qs.get("path", [""])[0] or ""))
        if not scope:
            return self.json_response(200, {
                "scope": "doc", "expires": picked.get("expires", 0), "docs": [], "search": True,
                "topic": str(entry["meta"].get("topic", "")) if entry else "",
                "project": str(entry["meta"].get("project", "")) if entry else "",
                "type": str(entry["meta"].get("type", "")) if entry else "",
            })
        entries = module_entries(picked)
        return self.json_response(200, {
            "scope": scope["kind"], "kind": scope["kind"],
            "topic": scope["topic"], "project": scope["project"], "type": scope["type"],
            "label": scope_label(scope),
            "expires": picked.get("expires", 0),
            "projects": sorted(scope["projects"]) if scope["projects"] else [],
            "search": True,
            "docs": [{"id": e["id"], "title": str(e["meta"].get("title", "")),
                      "type": str(e["meta"].get("type", "")), "status": str(e["meta"].get("status", "")),
                      "url": doc_url(e["id"])} for e in entries],
        })

    def api_share_search(self, qs):
        """分享作用域内全文搜索：实时读文档库源文件做朴素子串匹配（中文无需分词）。"""
        term = (qs.get("q", [""])[0] or "").strip()
        if not term:
            return self.json_response(400, {"error": "缺少搜索词"})
        if len(term) > 100:
            return self.json_response(400, {"error": "搜索词过长"})
        picked, grants = self._pick_share(qs)
        if not picked:
            return self.json_response(404, {"error": "当前分享不覆盖此范围", "grants": len(grants)})
        scope = module_scope(picked)
        if scope:
            pool = module_entries(picked)
        else:
            entry = snapshot()["by_url"].get(doc_path_key(qs.get("path", [""])[0] or ""))
            pool = [entry] if entry else []
        results = search_module(pool, term)
        return self.json_response(200, {"scope": scope["topic"] if scope else "doc",
                                        "count": len(results), "results": results})

    # ---------- 分享管理（凭站点登录鉴权） ----------
    def api_shares_list(self):
        out = []
        for token, item in shares.items():
            brief = share_brief(token, item)
            if brief["revoked"]:
                brief["state"] = "revoked"
            elif item.get("expires", 0) <= now_ts():
                brief["state"] = "expired"
            else:
                brief["state"] = "active"
            out.append(brief)
        out.sort(key=lambda x: (-float(x.get("expires") or 0), x.get("created") or 0))
        return self.json_response(200, {"count": len(out), "shares": out})

    def api_module_docs(self, qs):
        """某主题当前版文档清单（主题页「分享本主题」二次确认用，登录即可访问）。"""
        topic = (qs.get("topic", [""])[0] or "").strip()
        if not topic:
            return self.json_response(400, {"error": "topic 必填"})
        entries = snapshot()["modules"].get(topic, [])
        return self.json_response(200, {
            "topic": topic, "count": len(entries),
            "docs": [{"id": e["id"], "title": str(e["meta"].get("title", "")),
                      "type": str(e["meta"].get("type", "")),
                      "status": str(e["meta"].get("status", "")),
                      "project": str(e["meta"].get("project", "")), "url": doc_url(e["id"])}
                     for e in entries],
        })

    def api_share_revoke(self, payload):
        token = str(payload.get("token", "")).strip()
        item = shares.get(token)
        if not item:
            return self.json_response(404, {"error": "分享不存在"})
        if item.get("revoked_at"):
            return self.json_response(200, {"token": token, "revoked": True, "unchanged": True})
        with lock:
            item["revoked_at"] = now_ts()
            _SHARE_FILE_CACHE.pop(token, None)
            save_shares()
        return self.json_response(200, {"token": token, "revoked": True,
                                        "scope": scope_label(module_scope(item)) or "doc"})

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
        lifecycle = None
        if m.get("retired_at"):
            lifecycle = "retired"
        elif m.get("stale_at"):
            lifecycle = "stale"
        return {
            "id": e["id"], "title": m.get("title", ""), "project": m.get("project", ""),
            "domain": m.get("domain", ""), "topic": m.get("topic", ""), "type": m.get("type", ""),
            "status": m.get("status", ""), "date": m.get("date", ""),
            "version": m.get("version"), "url": doc_url(e["id"]),
            "summary": m.get("summary", ""), "related": m.get("related", []),
            "supersedes": m.get("supersedes"), "superseded_by": m.get("superseded_by"),
            "lifecycle": lifecycle,
        }

    def admin_pending(self):
        entries = read_entries()
        items = [self.entry_brief(e) for e in entries if e["meta"].get("status") == "pending"]
        stale = [i for i in items if i.get("lifecycle") == "stale"]
        active = [i for i in items if not i.get("lifecycle")]
        return self.json_response(200, {"count": len(items), "active": active, "stale": stale})

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
            invalidate_snapshot()
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
        return self.admin_write(lambda entries: _mutate_status(entries, payload))

    def api_status(self, payload):
        """页面用的状态切换入口（session 门禁）。与 admin_status 共用同一 mutate。"""
        if not self.is_authed():
            return self.json_response(401, {"error": "未登录"})
        return self.admin_write(lambda entries: _mutate_status(entries, payload))

    def api_status_options(self, qs):
        """合法状态集合。页面据此渲染按钮，避免前端硬编码与服务端漂移。"""
        if not self.is_authed():
            return self.json_response(401, {"error": "未登录"})
        dtype = (qs.get("type", ["other"])[0] or "other").strip()
        allowed = STATUS_SETS.get(dtype, STATUS_SETS["other"])
        return self.json_response(200, {"type": dtype, "options": allowed,
                                        "text": {k: STATUS_LABELS.get(k, k) for k in allowed}})

    def admin_meta(self, payload):
        def mutate(entries):
            doc_id = str(payload.get("id", "")).strip()
            if not doc_id:
                raise ApiError(400, "id 必填")
            entry = find_entry(entries, doc_id)
            if not entry:
                raise ApiError(404, "文档不存在: " + doc_id)
            if str(entry["meta"].get("retired_at") or "").strip():
                raise ApiError(400, "已淘汰文档只读，请先 restore")
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
            _auto_clear_stale(entry)
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
            if str(meta.get("retired_at") or "").strip():
                raise ApiError(400, "已淘汰文档只读，请先 restore")
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
            _auto_clear_stale(entry)
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
            if str(meta.get("retired_at") or "").strip():
                raise ApiError(400, "已淘汰文档只读，请先 restore")
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
            _auto_clear_stale(entry)
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
            meta = entry["meta"]
            if str(meta.get("superseded_by") or "").strip() and not payload.get("force"):
                raise ApiError(400, "该文档是已被新版替代的历史版本；确认要删除请传 force=true")
            # 使指向该文档的单篇分享立即失效（模块分享按作用域动态跟随，不整体作废）
            target = doc_path_key(doc_url(entry["id"]))
            dead = [k for k, v in shares.items()
                    if not module_scope(v) and doc_path_key(v.get("path", "")) == target]
            for k in dead:
                del shares[k]
            if dead:
                save_shares()
            rel = entry["path"].relative_to(DOCS).as_posix()
            entry["path"].unlink()
            # 清引用
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
            # 清 file_keys.json 失效条目
            keys_path = BASE / "scripts" / "file_keys.json"
            if keys_path.exists():
                try:
                    keys = json.loads(keys_path.read_text(encoding="utf-8"))
                    if rel in keys:
                        keys.pop(rel, None)
                        keys_path.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
            # 删空目录
            d = entry["path"].parent
            while d != DOCS and d.exists():
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                        d = d.parent
                    else:
                        break
                except OSError:
                    break
            return {"id": entry["id"], "deleted": rel, "url": "/",
                    "commit_message": f"chore: 删除文档 {entry['id']}"}

        return self.admin_write(mutate)

    def admin_review(self, qs):
        entries = read_entries()
        overrides = None
        ov = qs.get("overrides", [""])[0] if qs.get("overrides") else ""
        if ov:
            try:
                overrides = json.loads(ov)
            except Exception:
                return self.json_response(400, {"error": "overrides 必须是 JSON 对象"})
        cands = review_entries(entries, overrides)
        return self.json_response(200, {
            "generated_at": datetime.date.today().isoformat(),
            "thresholds": load_retention(),
            "count": len(cands),
            "candidates": cands,
            "recent_deletions": recent_deletions(),
        })

    def admin_retire(self, payload):
        def mutate(entries):
            doc_id = str(payload.get("id", "")).strip()
            action = str(payload.get("action", "")).strip()
            force = bool(payload.get("force"))
            reason = str(payload.get("reason", "")).strip()
            if not doc_id:
                raise ApiError(400, "id 必填")
            if action not in ("stale", "retired", "restore"):
                raise ApiError(400, "action 必须是 stale / retired / restore")
            entry = find_entry(entries, doc_id)
            if not entry:
                raise ApiError(404, "文档不存在: " + doc_id)
            meta = entry["meta"]
            status = str(meta.get("status") or "").strip()
            if status == "archived":
                raise ApiError(400, "归档文档不可修改，请出新版")
            today = datetime.date.today().isoformat()
            if action == "restore":
                if not meta.get("stale_at") and not meta.get("retired_at"):
                    return {"id": entry["id"], "action": "restore", "message": "无需恢复（无淘汰标记）",
                            "url": doc_url(entry["id"]), "commit_message": "chore: noop retire"}
                remove_entry_fields(entry, ["stale_at", "retired_at"])
                return {"id": entry["id"], "action": "restore", "message": "已恢复（清除淘汰标记）",
                        "url": doc_url(entry["id"]), "commit_message": f"chore: 淘汰 {entry['id']} — restore"}
            if action == "stale":
                if meta.get("stale_at"):
                    return {"id": entry["id"], "action": "stale", "message": "已标记过期",
                            "stale_at": meta["stale_at"], "url": doc_url(entry["id"]),
                            "commit_message": "chore: noop retire"}
                if not force:
                    cands = review_entries(entries)
                    if not any(c["id"] == entry["id"] for c in cands):
                        raise ApiError(400, "该文档不在候选清单中（需 force=true + reason 才能标记）")
                if not reason:
                    raise ApiError(400, "force 标记需要 reason")
                save_entry_fields(entry, {"stale_at": today})
                return {"id": entry["id"], "action": "stale", "stale_at": today,
                        "message": f"已标记过期，{load_retention()['GRACE_STALE_RETIRED']} 天后可淘汰",
                        "url": doc_url(entry["id"]), "commit_message": f"chore: 淘汰 {entry['id']} — stale（{reason}）"}
            # action == "retired"
            if meta.get("retired_at"):
                return {"id": entry["id"], "action": "retired", "message": "已淘汰",
                        "retired_at": meta["retired_at"], "url": doc_url(entry["id"]),
                        "commit_message": "chore: noop retire"}
            stale_at = meta.get("stale_at")
            if not stale_at:
                raise ApiError(400, "需先标记过期（stale）才能淘汰")
            th = load_retention()
            try:
                d0 = datetime.date.fromisoformat(str(stale_at)[:10])
                if (datetime.date.today() - d0).days < th["GRACE_STALE_RETIRED"] and not force:
                    raise ApiError(400, f"冷静期未满（需 {th['GRACE_STALE_RETIRED']} 天，或 force=true + reason）")
            except ValueError:
                pass
            if not reason:
                raise ApiError(400, "force 淘汰需要 reason")
            save_entry_fields(entry, {"retired_at": today})
            return {"id": entry["id"], "action": "retired", "retired_at": today,
                    "message": f"已淘汰隐藏，{th['GRACE_RETIRED_DELETE']} 天后可删除",
                    "url": doc_url(entry["id"]), "commit_message": f"chore: 淘汰 {entry['id']} — retired（{reason}）"}

        return self.admin_write(mutate)

    # ---------- GET ----------
    def do_GET(self):
        purge_expired()
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if path == "/login":
            return self.login_page()
        if path == "/logout":
            self.queue_cookie("ws_sid", "", 0)
            self.queue_cookie("ws_share", "", 0)
            return self.redirect("/login")
        if path.startswith("/s/"):
            return self.consume_share(path[3:])
        if path == "/api/share/ctx":
            return self.api_share_ctx(qs)
        if path == "/api/share/search":
            return self.api_share_search(qs)
        if path == "/api/shares":
            if not self.is_authed():
                return self.json_response(401, {"error": "未登录"})
            return self.api_shares_list()
        if path == "/api/module-docs":
            if not self.is_authed():
                return self.json_response(401, {"error": "未登录"})
            return self.api_module_docs(qs)
        if path.startswith("/api/admin/"):
            if not self.api_auth():
                return self.json_response(401, {"error": "API Key 无效"})
            if path == "/api/admin/pending":
                return self.admin_pending()
            if path == "/api/admin/context":
                return self.admin_context(qs)
            if path == "/api/admin/doc":
                return self.admin_doc(qs)
            if path == "/api/admin/review":
                return self.admin_review(qs)
            return self.json_response(404, {"error": "unknown admin endpoint"})
        if path == "/api/status-options":
            return self.api_status_options(qs)
        if path == "/api/review":
            if not self.is_authed():
                return self.json_response(401, {"error": "未登录"})
            return self.admin_review(qs)
        if self.is_authed() or path in PUBLIC_FILES or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return super().do_GET()
        grants = self.share_grants()
        if grants:
            reasons = [self.grant_denial(g, path) for g in grants]
            if any(r is None for r in reasons):
                return super().do_GET()
            # 站点外壳页（根目录、主题、看板等）不属于任何分享，但站点主人日常要访问：
            # 回退到登录跳转而不是「无权页」，否则打开站点根会像坏掉一样。
            # /docs/ 与 /files/ 保留无权页——那里「不包含此内容」才是有用的信息。
            if not (path.startswith("/docs/") or path.startswith("/files/")):
                return self.redirect("/login?next=" + urllib.parse.quote(self.path))
            denied_item = next((g for g in grants if g is not None), grants[0])
            return self.share_denied_page(reasons[0], denied_item)
        # 曾经持有分享但现已全部失效（过期/撤销）→ 给失效说明页，而不是让人误以为需要密码
        for token in self.share_tokens():
            if token in shares:
                return self.share_invalid_page(token)
        return self.redirect("/login?next=" + urllib.parse.quote(self.path))

    def consume_share(self, token):
        item = shares.get(token)
        if not share_valid(item):
            return self.share_invalid_page(token)
        remain = int(item["expires"] - now_ts())
        self.remember_share_token(token, max(remain, 60))
        scope = module_scope(item)
        if scope:
            # 落地页不带 token：凭证只在入口出现一次，之后靠 cookie 判定
            return self.redirect(scope_landing(scope))
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
        if parsed.path == "/api/status":
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                return self.json_response(400, {"error": "请求格式错误"})
            return self.api_status(payload)
        if parsed.path == "/api/share":
            if not self.is_authed():
                return self.json_response(401, {"error": "未登录"})
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                return self.json_response(400, {"error": "请求格式错误"})
            try:
                exp_ts = datetime.datetime.fromisoformat(str(payload.get("expires", ""))).timestamp()
            except Exception:
                return self.json_response(400, {"error": "到期时间格式错误"})
            if exp_ts <= now_ts():
                return self.json_response(400, {"error": "到期时间必须晚于现在"})
            scope_kind = str(payload.get("scope") or "doc")
            token = secrets.token_urlsafe(16)
            host = self.headers.get("Host", f"127.0.0.1:{PORT}")
            # 注意：快照解析放在加锁之外（snapshot 内部会取 lock）
            snap = snapshot()
            if scope_kind == "module":
                topic = str(payload.get("topic", "")).strip()
                project = str(payload.get("project", "")).strip()
                dtype = str(payload.get("type", "")).strip()
                # Q10 白名单：topic 与 project 互斥；type 必须与 project 同时出现
                if topic and project:
                    return self.json_response(400, {"error": "topic 与 project 不能同时指定"})
                if dtype and not project:
                    return self.json_response(400, {"error": "指定 type 时必须同时指定 project"})
                if topic:
                    projects = payload.get("projects")
                    if isinstance(projects, str):
                        projects = [projects]
                    projects = sorted({str(p).strip() for p in (projects or []) if str(p).strip()})
                    covered = [e for e in snap["modules"].get(topic, [])
                               if not projects or str(e["meta"].get("project") or "").strip() in projects]
                    if not covered:
                        return self.json_response(400, {"error": f"主题「{topic}」下没有可分享的当前版文档"})
                    record = {"scope": "module", "topic": topic, "projects": projects,
                              "expires": exp_ts, "created": now_ts(), "files": []}
                elif project:
                    if dtype and dtype not in STATUS_SETS:
                        return self.json_response(400, {"error": f"未知类型: {dtype}"})
                    covered = snap["by_project"].get(project, [])
                    if dtype:
                        covered = [e for e in covered
                                   if str(e["meta"].get("type") or "other").strip() == dtype]
                    if not covered:
                        what = f"项目「{project}」"
                        if dtype:
                            what += f" 的 {dtype} 类型"
                        return self.json_response(400, {"error": f"{what}下没有可分享的当前版文档"})
                    record = {"scope": "module", "topic": "", "projects": [],
                              "project": project, "type": dtype,
                              "expires": exp_ts, "created": now_ts(), "files": []}
                else:
                    return self.json_response(400, {"error": "模块分享需要 topic 或 project"})
            else:
                target = payload.get("path", "")
                if not isinstance(target, str) or not target.startswith("/docs/"):
                    return self.json_response(400, {"error": "单篇分享只能分享文档页"})
                target = urllib.parse.quote(urllib.parse.unquote(target), safe="/")
                if not snap["by_url"].get(doc_path_key(target)):
                    return self.json_response(400, {"error": "文档不存在或未构建"})
                record = {"scope": "doc", "path": target, "expires": exp_ts,
                          "created": now_ts(), "files": extract_doc_files(target)}
            with lock:
                shares[token] = record
                save_shares()
            return self.json_response(200, {
                "url": f"http://{host}/s/{token}", "expires": payload.get("expires"),
                "scope": record["scope"], "token": token,
                "topic": record.get("topic", ""),
                "project": record.get("project", ""),
                "type": record.get("type", ""),
                "doc_count": len(covered) if scope_kind == "module" else 1,
            })
        if parsed.path == "/api/shares/revoke":
            if not self.is_authed():
                return self.json_response(401, {"error": "未登录"})
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                return self.json_response(400, {"error": "JSON 格式错误"})
            return self.api_share_revoke(payload)
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
            if parsed.path == "/api/admin/retire":
                return self.admin_retire(payload)
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
