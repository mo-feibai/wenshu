# -*- coding: utf-8 -*-
"""生成目录的 sha256 清单（迁移校验用）"""
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
exclude = set(sys.argv[3].split(",")) if len(sys.argv) > 3 and sys.argv[3] else set()

items = {}
for p in sorted(root.rglob("*")):
    if p.is_dir():
        continue
    try:
        rel = p.relative_to(root)
    except ValueError:
        continue
    if set(rel.parts) & exclude:
        continue
    items[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()

out.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{len(items)} files -> {out}")
