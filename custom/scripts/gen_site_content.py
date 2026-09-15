# -*- coding: utf-8 -*-
"""从文档库生成站点内容：
- md 原样引入，related 中指向规格/资产的引用拆到 attachments
- sql 由 -- @meta 生成 md 页面（含附件下载）
- 生成 src/data/assets.json（主题页「相关文件」）
"""
import json
import os
import pathlib
import re
import shutil
from collections import Counter

_SCRIPT = pathlib.Path(__file__).resolve()
APP = pathlib.Path(os.environ.get("WENSHU_APP") or _SCRIPT.parents[1])
DOCS = pathlib.Path(os.environ.get("WENSHU_DOCS", "/opt/wenshu/docs"))
if not DOCS.is_absolute():
    DOCS = (APP / DOCS).resolve()
TARGET = APP / "src/content/docs"
KEYS_PATH = APP / "scripts/file_keys.json"
ASSETS_JSON = APP / "src/data/assets.json"


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
            meta[k] = [x.strip() for x in v.split(",") if x.strip()] if k == "related" else v
        i += 1
    rest = "\n".join(lines[i + 1:]) if i < len(lines) else ""
    return meta, rest


def parse_frontmatter(text: str):
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
    meta.update(arrays)
    return meta, rest


def render_frontmatter(meta: dict) -> str:
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
    return "\n".join(lines)


def main():
    keys = json.loads(KEYS_PATH.read_text(encoding="utf-8")) if KEYS_PATH.exists() else {}
    key_by_ref = {}
    for rel, name in keys.items():
        key_by_ref[rel.rsplit(".", 1)[0]] = name

    entry_ids = set()
    for p in DOCS.rglob("*"):
        if p.is_dir() or ".git" in p.parts:
            continue
        if p.suffix.lower() in (".md", ".sql"):
            entry_ids.add(p.relative_to(DOCS).as_posix().rsplit(".", 1)[0])

    def split_refs(refs):
        keep, att = [], []
        for ref in refs:
            ref = str(ref).strip()
            if not ref:
                continue
            if ref in entry_ids:
                keep.append(ref)
            elif ref in key_by_ref:
                att.append("/files/" + key_by_ref[ref])
            else:
                keep.append(ref)
        return keep, att

    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)
    count = {"md": 0, "sql": 0, "att": 0}

    for p in sorted(DOCS.rglob("*")):
        if p.is_dir() or ".git" in p.parts:
            continue
        rel = p.relative_to(DOCS)
        suffix = p.suffix.lower()
        if suffix == ".md":
            text = p.read_text(encoding="utf-8")
            meta, rest = parse_frontmatter(text)
            if meta:
                related, new_att = split_refs(meta.get("related", []) or [])
                att = [str(x) for x in (meta.get("attachments") or [])] + new_att
                if att:
                    meta["attachments"] = att
                    count["att"] += len(new_att)
                if "related" in meta:
                    if related:
                        meta["related"] = related
                    else:
                        meta.pop("related")
                text = render_frontmatter(meta) + "\n" + rest
            out = TARGET / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            count["md"] += 1
        elif suffix == ".sql":
            text = p.read_text(encoding="utf-8")
            meta, body = parse_sql_meta(text)
            rel_posix = p.relative_to(DOCS).as_posix()
            fname = keys.get(rel_posix, p.stem + ".sql")
            related_raw = meta.get("related", [])
            if isinstance(related_raw, str):
                related_raw = [x.strip() for x in related_raw.split(",") if x.strip()]
            related, att = split_refs(related_raw)
            fm = {
                "title": meta.get("title", p.stem),
                "project": meta.get("project", ""),
                "topic": meta.get("topic", ""),
                "type": "sql",
                "status": meta.get("status", "executed"),
                "date": meta.get("date", ""),
                "attachment": "/files/" + fname,
            }
            if meta.get("domain"):
                fm["domain"] = meta["domain"]
            if meta.get("version"):
                fm["version"] = int(meta["version"])
            if related:
                fm["related"] = related
            if att:
                fm["attachments"] = att
                count["att"] += len(att)
            body_md = "\n".join([
                "", "## 脚本内容", "", "```sql", body.rstrip(), "```", "",
                f"原始文件：[下载 /files/{fname}](/files/{fname})", "",
            ])
            out = TARGET / rel.with_suffix(".md")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render_frontmatter(fm) + "\n" + body_md, encoding="utf-8")
            count["sql"] += 1

    assets = []
    for proj_dir in sorted([d for d in DOCS.iterdir() if d.is_dir() and d.name != ".git"]):
        yml = proj_dir / "assets.yml"
        if not yml.exists():
            continue
        items, cur = [], None
        for line in yml.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^- path: "(.*)"$', line)
            if m:
                if cur:
                    items.append(cur)
                cur = {"path": m.group(1)}
                continue
            m = re.match(r'^  (\w+): "(.*)"$', line)
            if m and cur:
                cur[m.group(1)] = m.group(2)
        if cur:
            items.append(cur)
        for item in items:
            rel = f"{proj_dir.name}/{item['path']}"
            if rel in keys:
                assets.append({
                    "project": proj_dir.name,
                    "topic": item.get("topic", ""),
                    "date": item.get("date", ""),
                    "name": pathlib.PurePosixPath(item["path"]).name,
                    "url": "/files/" + keys[rel],
                })
    ASSETS_JSON.parent.mkdir(parents=True, exist_ok=True)
    ASSETS_JSON.write_text(json.dumps(assets, ensure_ascii=False, indent=1), encoding="utf-8")
    print("generated:", count, "| assets:", len(assets))


if __name__ == "__main__":
    main()
