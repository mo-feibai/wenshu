# -*- coding: utf-8 -*-
"""静态检查（CI 与本地均可运行，无需 WSL 或运行中的服务）：
1) custom 下全部 .py 语法编译
2) JSON 文件合法性（跳过 node_modules/dist/.astro 等产物）
3) 跟踪文件密钥扫描（git ls-files 范围；允许 example 占位值）
用法：python custom/tests/static_check.py
"""
import json
import pathlib
import py_compile
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKIP_PARTS = {"node_modules", "dist", ".astro", ".cortexkit", "__pycache__", "hextra", "starlight", "assets", "downloads"}
BAD = 0


def fail(msg):
    global BAD
    BAD += 1
    print("FAIL " + msg)


def check_syntax():
    files = [p for p in (ROOT / "custom").rglob("*.py") if not (SKIP_PARTS & set(p.parts))]
    for p in files:
        try:
            py_compile.compile(str(p), doraise=True)
        except Exception as e:  # noqa: BLE001
            fail(f"syntax {p.relative_to(ROOT)}: {e}")
    print(f"PASS syntax ({len(files)} files)")


def check_json():
    # 只检查 git 跟踪的 JSON（生成产物不入库，无需校验）；utf-8-sig 容忍 BOM
    try:
        tracked = subprocess.run(["git", "ls-files", "*.json"], cwd=str(ROOT),
                                  capture_output=True, text=True, check=True).stdout.splitlines()
    except Exception:  # noqa: BLE001
        tracked = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.json")
                   if not (SKIP_PARTS & set(p.parts))]
    for rel in tracked:
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            fail(f"json {rel}: {e}")
    print(f"PASS json ({len(tracked)} tracked files)")


SECRET_PATTERNS = [
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "github token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github pat"),
    (re.compile(r"sk-[A-Za-z0-9]{24,}"), "openai-style key"),
    (re.compile(r'"api_token"\s*:\s*"(?!")[^"]{8,}"'), "api_token literal"),
    (re.compile(r'"password"\s*:\s*"(?!change-me)(?!")[^"]{6,}"'), "password literal"),
]
ALLOW = {"custom/server_config.example.json"}


def check_secrets():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True, check=True).stdout
    except Exception as e:  # noqa: BLE001
        print(f"SKIP secrets (git unavailable: {e})")
        return
    n = 0
    for rel in out.splitlines():
        if rel in ALLOW:
            continue
        p = ROOT / rel
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        n += 1
        for rx, name in SECRET_PATTERNS:
            m = rx.search(text)
            if m:
                fail(f"secret({name}) in {rel}: {m.group(0)[:40]!r}")
    print(f"PASS secrets scan ({n} tracked files)")


if __name__ == "__main__":
    check_syntax()
    check_json()
    check_secrets()
    print("RESULT:", "FAIL" if BAD else "OK")
    sys.exit(1 if BAD else 0)
