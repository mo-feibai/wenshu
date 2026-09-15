# -*- coding: utf-8 -*-
"""同步可下载文件到 /files/：SQL 脚本 + OpenAPI 规格 + assets.yml 资产；输出 file_keys.json"""
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
DEST = APP / "public/files"
KEYS_PATH = APP / "scripts/file_keys.json"


def is_spec(p: pathlib.Path) -> bool:
    if p.suffix.lower() not in (".yml", ".yaml", ".json"):
        return False
    return ("api" in p.parts or "接口" in p.stem or "openapi" in p.stem.lower()
            or "Apifox" in p.stem)


def parse_assets_yml(path: pathlib.Path):
    items, cur = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
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
    return items


def main():
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    files = []
    for p in sorted(DOCS.rglob("*")):
        if p.is_dir() or ".git" in p.parts:
            continue
        if p.suffix.lower() in (".sql", ".md") or is_spec(p):
            files.append(p)
    for proj_dir in sorted([d for d in DOCS.iterdir() if d.is_dir() and d.name != ".git"]):
        assets_yml = proj_dir / "assets.yml"
        if assets_yml.exists():
            for item in parse_assets_yml(assets_yml):
                p = proj_dir / item["path"]
                if p.exists() and p not in files:
                    files.append(p)
    stems = Counter(p.stem for p in files)
    keys = {}
    for p in files:
        rel = p.relative_to(DOCS).as_posix()
        key = p.stem if stems[p.stem] == 1 else f"{p.parent.name}__{p.stem}"
        target = DEST / (key + p.suffix)
        n = 2
        while target.exists():
            target = DEST / f"{key}-{n}{p.suffix}"
            n += 1
        shutil.copy2(p, target)
        keys[rel] = target.name
    KEYS_PATH.write_text(json.dumps(keys, ensure_ascii=False, indent=1), encoding="utf-8")
    print("files synced:", len(files))


if __name__ == "__main__":
    main()
