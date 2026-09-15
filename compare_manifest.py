# -*- coding: utf-8 -*-
"""比对迁移清单（Windows 源 vs WSL 目标）"""
import json
import sys

a = json.load(open(sys.argv[1], encoding="utf-8"))
b = json.load(open(sys.argv[2], encoding="utf-8"))
a = {k.replace("\\", "/"): v for k, v in a.items()}
b = {k.replace("\\", "/"): v for k, v in b.items()}
ka, kb = set(a), set(b)
mismatch = [k for k in (ka & kb) if a[k] != b[k]]
print(f"windows: {len(a)} files | wsl: {len(b)} files")
print(f"only-in-windows: {sorted(ka - kb)[:10]}")
print(f"only-in-wsl: {sorted(kb - ka)[:10]}")
print(f"hash-mismatch: {mismatch[:10]}")
if a == b:
    print("RESULT: OK - 全部一致")
else:
    print("RESULT: MISMATCH")
    raise SystemExit(1)
