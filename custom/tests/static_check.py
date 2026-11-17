# -*- coding: utf-8 -*-
"""静态检查（CI 与本地均可运行，无需 WSL 或运行中的服务）：
1) custom 与 tests 下的 .py 在内存中语法编译
2) JSON 文件合法性（跳过 node_modules/dist/.astro 等产物）
3) 跟踪文件密钥扫描（git ls-files 范围；允许 example 占位值）
用法：python custom/tests/static_check.py
"""
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKIP_PARTS = {"node_modules", "dist", ".astro", ".cortexkit", "__pycache__", ".baseline-tmp", "hextra", "starlight", "assets", "downloads"}
BAD = 0
RUNTIME_FILES = {"custom/server_config.json", "custom/sessions.json", "custom/shares.json"}


def fail(msg):
    global BAD
    BAD += 1
    print("FAIL " + msg)


def check_syntax():
    files = [p for base in (ROOT / "custom", ROOT / "tests") for p in base.rglob("*.py")
             if not (SKIP_PARTS & set(p.relative_to(ROOT).parts))]
    for p in files:
        try:
            compile(p.read_text(encoding="utf-8-sig"), str(p), "exec", dont_inherit=True)
        except Exception as e:  # noqa: BLE001
            fail(f"syntax {p.relative_to(ROOT)}: {e}")
    print(f"PASS syntax ({len(files)} files)")


def check_json():
    # 只检查 git 跟踪的 JSON（生成产物不入库，无需校验）；utf-8-sig 容忍 BOM
    try:
        tracked = [rel for rel in subprocess.run(
            ["git", "ls-files", "-z", "--", "*.json"], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", check=True
        ).stdout.split("\0") if rel]
    except Exception:  # noqa: BLE001
        fail("git listing unavailable for JSON; refusing runtime fallback")
        return
    for rel in tracked:
        if rel in RUNTIME_FILES:
            fail(f"protected runtime path in git listing: {rel}")
            continue
        p = ROOT / rel
        if not p.is_file():
            fail(f"tracked JSON input unavailable: {rel}")
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
        out = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True,
                             text=True, encoding="utf-8", check=True).stdout
    except Exception:  # noqa: BLE001
        fail("git listing unavailable for secrets")
        return
    n = 0
    for rel in filter(None, out.split("\0")):
        if rel in RUNTIME_FILES:
            fail(f"protected runtime path in git listing: {rel}")
            continue
        if rel in ALLOW:
            continue
        p = ROOT / rel
        try:
            if not p.is_file():
                fail(f"tracked input unavailable: {rel}")
                continue
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            fail(f"tracked input unreadable: {rel}")
            continue
        n += 1
        for rx, name in SECRET_PATTERNS:
            m = rx.search(text)
            if m:
                fail(f"secret({name}) in {rel}")
    print(f"PASS secrets scan ({n} tracked files)")


if __name__ == "__main__":
    check_syntax()
    check_json()
    check_secrets()
    print("RESULT:", "FAIL" if BAD else "OK")
    sys.exit(1 if BAD else 0)
