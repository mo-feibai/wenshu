"""Build WenShu from synthetic fixtures without touching the source checkout.

The public boundary is the build CLI and its generated HTML/search/download assets.
No real document library or runtime configuration is read. Unit tests substitute
only the external process executor; the actual CLI inherits child stdout/stderr.
"""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
from typing import Callable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO))
from tests.baseline.tempdirs import TemporaryDirectory, temporary_report_target, write_json_report

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "build"
BOUNDARY = "Build/artifact/index validation only; not browser search or online deployment."
REQUIRED_INPUTS = (
    "package.json", "package-lock.json", "astro.config.mjs",
    "src/content.config.ts", "scripts/sync_files.py", "scripts/gen_site_content.py",
)
STATIC_INPUTS = (
    "public/favicon.svg", "public/logo.svg", "public/swagger/index.html",
    "public/vendor/swagger-ui/swagger-ui.css", "public/vendor/swagger-ui/swagger-ui-bundle.js",
    "public/fonts/MapleMono-CN-Regular.woff2", "public/fonts/MapleMono-CN-SemiBold.woff2",
    "public/fonts/MapleMono-CN-Bold.woff2",
)
WATCHED_OUTPUTS = (
    "src/content/docs", "src/data/assets.json", "scripts/file_keys.json",
    "public/files", "public/openapi", "dist", ".astro", "node_modules",
    "server_config.json", "sessions.json", "shares.json", ".npmrc", "logs", "backups",
)
SKIP_NAMES = {
    ".git", ".astro", ".cache", "__pycache__", "node_modules", "dist", "logs", "backups",
    ".npmrc", "server_config.json", "sessions.json", "shares.json",
}
Executor = Callable[..., int]


class BuildFailure(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def _absolute(path) -> Path:
    # Normalize lexical '..' without following any links or reparse points.
    return Path(os.path.abspath(path))


def _temporary_parent(repo: Path) -> Path | None:
    configured = os.environ.get("WENSHU_TEST_TMPDIR")
    if not configured:
        return None
    parent = _absolute(configured)
    _reject_links(parent)
    if parent.is_relative_to(repo) and not parent.is_relative_to(repo / "tests/.baseline-tmp"):
        raise BuildFailure("isolation", "In-repository WENSHU_TEST_TMPDIR must be in tests/.baseline-tmp, not application or fixture inputs.")
    return parent


def _lstat(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _reject_links(path: Path) -> None:
    absolute = _absolute(path)
    for part in [*reversed(absolute.parents), absolute]:
        info = _lstat(part)
        if info is not None and _is_link(info):
            raise BuildFailure("isolation", f"Links/reparse points are not permitted: {part}")


def _source_files(app: Path) -> list[Path]:
    _reject_links(app)
    files = []
    for relative in (*REQUIRED_INPUTS, *STATIC_INPUTS):
        path = app / relative
        _reject_links(path)
        if not path.is_file():
            raise BuildFailure("inputs", f"Missing required app input: custom/{relative}")
        files.append(path)
    src = app / "src"
    stack = [src]
    while stack:
        directory = stack.pop()
        _reject_links(directory)
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(app).as_posix()
            if path.name in SKIP_NAMES or path.name.startswith(".env") or rel in {
                "src/content/docs", "src/data/assets.json",
            }:
                continue
            _reject_links(path)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                files.append(path)
            else:
                raise BuildFailure("inputs", f"Not a regular input file: custom/{rel}")
    return sorted(set(files))


def copy_app(source_app: Path, destination_app: Path) -> list[str]:
    """Copy only necessary source inputs; never follow links or generated data."""
    source_app, destination_app = _absolute(source_app), _absolute(destination_app)
    if destination_app.is_relative_to(source_app) or source_app.is_relative_to(destination_app):
        raise BuildFailure("isolation", "The temporary app must not overlap the source app.")
    _reject_links(destination_app)
    if destination_app.exists():
        raise BuildFailure("isolation", "The copy destination must be a new owned directory.")
    sources = _source_files(source_app)
    destination_app.mkdir(parents=True)
    copied = []
    for source in sources:
        _reject_links(source)
        relative = source.relative_to(source_app)
        target = destination_app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        copied.append(relative.as_posix())
    return copied


def _metadata(path: Path) -> dict:
    """Fingerprint metadata only for protected/generated trees, never their contents."""
    rows = {}
    stack = [path]
    while stack:
        current = stack.pop()
        info = _lstat(current)
        if info is None:
            rows[str(current.relative_to(path))] = None
            continue
        rows[str(current.relative_to(path))] = (
            info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_ino, info.st_dev, getattr(info, "st_file_attributes", 0),
        )
        if stat.S_ISDIR(info.st_mode) and not _is_link(info):
            stack.extend(sorted(current.iterdir()))
    return rows


def _source_state(app: Path) -> dict:
    inputs = {}
    for path in _source_files(app):
        before = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise BuildFailure("integrity", "A source input changed while it was fingerprinted.")
        inputs[path.relative_to(app).as_posix()] = (digest, _metadata(path))
    return {"inputs": inputs, "outputs": {name: _metadata(app / name) for name in WATCHED_OUTPUTS}}


def _copy_fixture_tree(source: Path, target: Path) -> None:
    _reject_links(source)
    if not source.is_dir():
        raise BuildFailure("fixtures", "Missing synthetic build fixture directory.")
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        _reject_links(path)
        if path.is_dir():
            _copy_fixture_tree(path, target / path.name)
        elif path.is_file():
            shutil.copy2(path, target / path.name, follow_symlinks=False)
        else:
            raise BuildFailure("fixtures", "Synthetic fixtures must be regular files/directories.")


def _runtime_paths(node, npm_cli) -> tuple[Path, Path]:
    selected = str(node) if node else shutil.which("node")
    if not selected:
        raise BuildFailure("runtime", "Node is missing; supply --node PATH (>=22.12.0).")
    node_path = _absolute(selected)
    if not node_path.is_file() or node_path.suffix.lower() in {".cmd", ".bat"}:
        raise BuildFailure("runtime", "--node must name an existing Node executable, not a wrapper.")
    candidates = []
    if npm_cli:
        supplied = _absolute(npm_cli)
        candidates.append(supplied)
        if supplied.suffix.lower() in {".cmd", ".bat"}:
            candidates = [supplied.parent / "node_modules/npm/bin/npm-cli.js"]
    else:
        candidates.extend([
            node_path.parent / "node_modules/npm/bin/npm-cli.js",
            node_path.parent.parent / "lib/node_modules/npm/bin/npm-cli.js",
            node_path.parent.parent / "share/nodejs/npm/bin/npm-cli.js",
        ])
        wrapper = shutil.which("npm") or shutil.which("npm.cmd")
        if wrapper:
            resolved = Path(wrapper).resolve()
            candidates.extend([resolved, resolved.parent / "node_modules/npm/bin/npm-cli.js"])
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() in {".js", ".cjs", ".mjs"}:
            return node_path, candidate
    raise BuildFailure("runtime", "npm CLI JavaScript is missing; supply --npm-cli PATH/to/npm-cli.js.")


def _child_environment(root: Path, app: Path, docs: Path, node: Path) -> dict[str, str]:
    # Only OS process facts survive. Inherited npm settings, tokens, Python paths,
    # Node options and WenShu paths cannot leak into the copied build.
    env = {key: os.environ[key] for key in (
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "OS", "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL",
    ) if key in os.environ}
    areas = {name: root / name for name in (
        "home", "config", "cache", "data", "appdata", "localappdata", "npm-cache", "npm-prefix", "tmp", "bin",
    )}
    for path in areas.values():
        path.mkdir()
    user_config, global_config = root / "npm-user-empty.ini", root / "npm-global-empty.ini"
    user_config.write_text("", encoding="utf-8")
    global_config.write_text("", encoding="utf-8")
    bin_dir = areas["bin"]
    for name, executable in (("python", Path(sys.executable)), ("python3", Path(sys.executable)), ("node", node)):
        if os.name == "nt":
            wrapper = bin_dir / (name + ".cmd")
            wrapper.write_text(f'@echo off\n"{executable}" ' + ("-B " if name.startswith("python") else "") + "%*\n", encoding="utf-8")
        else:
            wrapper = bin_dir / name
            wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(executable)) + (" -B" if name.startswith("python") else "") + ' "$@"\n', encoding="utf-8")
            wrapper.chmod(0o700)
    system_paths = ["/usr/local/bin", "/usr/bin", "/bin"]
    if os.name == "nt":
        windows = env.get("SYSTEMROOT") or env.get("WINDIR") or "C:/Windows"
        system_paths = [str(Path(windows) / "System32"), windows]
        env.setdefault("COMSPEC", str(Path(windows) / "System32/cmd.exe"))
        env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    env.update({
        "PATH": os.pathsep.join([str(bin_dir), str(node.parent), *system_paths]),
        "HOME": str(areas["home"]), "USERPROFILE": str(areas["home"]),
        "XDG_CONFIG_HOME": str(areas["config"]), "XDG_CACHE_HOME": str(areas["cache"]),
        "XDG_DATA_HOME": str(areas["data"]), "APPDATA": str(areas["appdata"]),
        "LOCALAPPDATA": str(areas["localappdata"]),
        "TEMP": str(areas["tmp"]), "TMP": str(areas["tmp"]), "TMPDIR": str(areas["tmp"]),
        "NPM_CONFIG_USERCONFIG": str(user_config), "NPM_CONFIG_GLOBALCONFIG": str(global_config),
        "NPM_CONFIG_CACHE": str(areas["npm-cache"]), "NPM_CONFIG_PREFIX": str(areas["npm-prefix"]),
        "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false", "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_PROGRESS": "false", "ASTRO_TELEMETRY_DISABLED": "1", "DO_NOT_TRACK": "1", "CI": "true",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
        "WENSHU_APP": str(app), "WENSHU_DOCS": str(docs),
        "WENSHU_BUILD_RUNTIME_REPORT": str(root / "runtime.json"),
    })
    return env


def _execute(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> int:
    # Do not capture pipes: the Windows sandbox can reject piped child_process I/O.
    return subprocess.run(list(argv), cwd=cwd, env=dict(env), check=False).returncode


def _require_version(value: str, minimum: tuple[int, int, int], runtime: str) -> None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match or tuple(map(int, match.groups())) < minimum:
        required = ".".join(map(str, minimum))
        raise BuildFailure("runtime", f"{runtime} must be >= {required}; observed {value or 'unknown'}.")


def _normalized(parts) -> str:
    return " ".join("".join(parts).split())


class _RenderedPage(HTMLParser):
    """Read public HTML semantics, not Astro internals or executable page scripts."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.text, self.headings, self.links, self.resources = [], [], [], []
        self._heading, self._anchor, self._ignored = None, None, 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for attribute in ("href", "src"):
            if attribute in attrs:
                self.resources.append(attrs[attribute])
        if tag in {"script", "style"}:
            self._ignored += 1
        if self._ignored:
            return
        if tag in {"p", "div", "h1", "h2", "h3", "span", "td", "section"}:
            self.text.append(" ")
        if tag == "h1":
            self._heading = []
        if tag == "a":
            self._anchor = {"href": attrs.get("href", ""), "parts": []}

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._ignored = max(0, self._ignored - 1)
            return
        if self._ignored:
            return
        if tag == "h1" and self._heading is not None:
            self.headings.append(_normalized(self._heading))
            self._heading = None
        if tag == "a" and self._anchor is not None:
            self.links.append((self._anchor["href"], _normalized(self._anchor["parts"])))
            self._anchor = None
        if tag in {"p", "div", "h1", "h2", "h3", "span", "td", "section"}:
            self.text.append(" ")

    def handle_data(self, data):
        if self._ignored:
            return
        self.text.append(data)
        if self._heading is not None:
            self._heading.append(data)
        if self._anchor is not None:
            self._anchor["parts"].append(data)


def _artifact(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if not relative or relative.startswith("/") or "\\" in relative or any(part in {"", ".", ".."} for part in parts):
        raise BuildFailure("smoke", "Artifact paths must be relative without traversal.")
    path = root.joinpath(*parts)
    _reject_links(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise BuildFailure("smoke", f"Missing or empty public artifact: {relative}")
    return path


def _check(name: str, operation) -> dict:
    try:
        detail = operation()
        return {"name": name, "status": "passed", "detail": detail or "verified"}
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
        return {"name": name, "status": "failed", "detail": str(exc)[:1200]}


def _check_page(dist: Path, expected: dict) -> str:
    page = _RenderedPage(_artifact(dist, expected["path"]).read_text(encoding="utf-8"))
    if expected["heading"] not in page.headings:
        raise BuildFailure("smoke", f"{expected['path']}: missing rendered h1 {expected['heading']!r}")
    text = _normalized(page.text)
    for label in expected.get("texts", []):
        if label not in text:
            raise BuildFailure("smoke", f"{expected['path']}: missing rendered label/content {label!r}")
    for link in expected.get("links", []):
        if not any(href == link["href"] and link.get("text", "") in label for href, label in page.links):
            raise BuildFailure("smoke", f"{expected['path']}: missing link {link['href']} labeled {link.get('text', '')!r}")
    for href in expected.get("absent_links", []):
        if any(target == href for target, _ in page.links):
            raise BuildFailure("smoke", f"{expected['path']}: unexpected document link {href}")
    return f"h1, {len(expected.get('texts', []))} labels and {len(expected.get('links', []))} links"


def _regular_files(root: Path) -> list[Path]:
    _reject_links(root)
    if not root.is_dir():
        raise BuildFailure("smoke", f"Missing public artifact directory: {root.name}")
    files, stack = [], [root]
    while stack:
        for path in sorted(stack.pop().iterdir()):
            _reject_links(path)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                files.append(path)
            else:
                raise BuildFailure("smoke", "Generated public assets must be regular files.")
    return files


def _check_download(dist: Path, fixture_root: Path, expected: dict) -> str:
    output = _artifact(dist, expected["path"])
    source = _artifact(fixture_root, expected["source"])
    if output.read_bytes() != source.read_bytes():
        raise BuildFailure("smoke", f"{expected['path']}: output differs from raw fixture bytes")
    return "raw fixture bytes preserved"


def _check_download_allowlist(dist: Path, allowed: list[str]) -> str:
    actual = {path.relative_to(dist / "files").as_posix() for path in _regular_files(dist / "files")}
    if actual != set(allowed):
        raise BuildFailure("smoke", f"Public download allowlist differs; missing={sorted(set(allowed) - actual)}, unexpected={sorted(actual - set(allowed))}")
    return f"{len(actual)} declared downloads only; unlisted attachment excluded"


def _check_search_index(dist: Path, declared: list[dict]) -> str:
    items = json.loads(_artifact(dist, "search-index.json").read_text(encoding="utf-8"))
    if not isinstance(items, list) or sorted(items, key=lambda item: item["id"]) != sorted(declared, key=lambda item: item["id"]):
        raise BuildFailure("smoke", "search-index.json differs from the four independently declared document records")
    for item in declared:
        _artifact(dist, item["url"].lstrip("/") + "index.html")
    return "4 independently declared public search records and document targets"


def _check_pagefind(dist: Path, expected: dict) -> str:
    _artifact(dist, "pagefind/pagefind.js")
    entry = json.loads(_artifact(dist, "pagefind/pagefind-entry.json").read_text(encoding="utf-8"))
    if not isinstance(entry, dict):
        raise BuildFailure("smoke", "Pagefind entry metadata must be a JSON object.")
    languages = entry.get("languages", {})
    if entry.get("version") != expected["version"] or not isinstance(languages, dict) or not languages:
        raise BuildFailure("smoke", "Pagefind entry must declare the locked version and generated languages")
    if any(not isinstance(language, dict) for language in languages.values()):
        raise BuildFailure("smoke", "Pagefind language metadata must be JSON objects.")
    counts = [language.get("page_count") for language in languages.values()]
    if not all(isinstance(count, int) and not isinstance(count, bool) and count > 0 for count in counts) or sum(counts) < expected["minimum_pages"]:
        raise BuildFailure("smoke", f"Pagefind index must cover at least {expected['minimum_pages']} fixture pages")
    files = _regular_files(dist / "pagefind")
    for suffix in (".pf_meta", ".pf_index", ".pf_fragment"):
        matches = [path for path in files if path.name.endswith(suffix)]
        if not matches or any(path.stat().st_size == 0 for path in matches):
            raise BuildFailure("smoke", f"Missing or empty generated Pagefind {suffix} artifacts")
    wasm = [path for path in files if path.name.startswith("wasm.") and path.suffix in {".pagefind", ".wasm"}]
    if not wasm or any(path.stat().st_size == 0 for path in wasm):
        raise BuildFailure("smoke", "Missing or empty Pagefind WebAssembly asset")
    return f"Pagefind {entry['version']}: {sum(counts)} indexed pages with metadata/index/fragments/WASM; no browser search executed"


def _check_swagger(dist: Path, expected: dict) -> str:
    text = _artifact(dist, "swagger/index.html").read_text(encoding="utf-8")
    page = _RenderedPage(text)
    for reference in expected["swagger_references"]:
        if reference not in page.resources:
            raise BuildFailure("smoke", f"Swagger missing active local reference {reference}")
    for reference in expected["swagger_paths"]:
        if reference not in text:
            raise BuildFailure("smoke", f"Swagger missing preserved local spec/font path {reference}")
    return "local Swagger vendor references, synthetic fixed spec and three font paths"


def _check_styles(dist: Path, pages: list[dict]) -> str:
    styles = set()
    for expected in pages:
        page = _RenderedPage(_artifact(dist, expected["path"]).read_text(encoding="utf-8"))
        linked = {url for url in page.resources if url.startswith("/_astro/") and url.endswith(".css")}
        if not linked:
            raise BuildFailure("smoke", f"{expected['path']}: no generated stylesheet link")
        styles.update(linked)
    for url in styles:
        _artifact(dist, url.lstrip("/"))
    return f"{len(styles)} nonempty generated stylesheets linked from all eight core pages"


def check_artifacts(dist: Path, fixture_root: Path = FIXTURES) -> list[dict]:
    """Compare generated public artifacts with independent fixture expectations."""
    dist, fixture_root = Path(dist), Path(fixture_root)
    try:
        _reject_links(fixture_root / "expectations.json")
        expected = json.loads((fixture_root / "expectations.json").read_text(encoding="utf-8"))
        pages = expected["pages"]
        if not isinstance(pages, list) or not pages:
            raise BuildFailure("fixtures", "No independent rendered-page expectations were declared.")
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
        return [{"name": "fixture-expectations", "status": "failed", "detail": str(exc)[:1200]}]
    checks = [_check(f"html:{page['path']}", lambda page=page: _check_page(dist, page)) for page in pages]
    for download in expected["downloads"]:
        checks.append(_check(f"download:{download['path']}", lambda download=download: _check_download(dist, fixture_root, download)))
    checks.append(_check("download-allowlist", lambda: _check_download_allowlist(dist, expected["download_allowlist"])))
    checks.append(_check("search-index", lambda: _check_search_index(dist, expected["search_entries"])))
    checks.append(_check("pagefind-index", lambda: _check_pagefind(dist, expected["pagefind"])))
    for asset in expected["static_assets"]:
        checks.append(_check(f"static:{asset}", lambda asset=asset: f"{_artifact(dist, asset).stat().st_size} bytes"))
    checks.append(_check("swagger-local-paths", lambda: _check_swagger(dist, expected)))
    checks.append(_check("generated-styles", lambda: _check_styles(dist, pages)))
    return checks


def _write_report(path: Path, report: dict, repo: Path) -> None:
    try:
        write_json_report(report, path, repository=repo)
    except (OSError, ValueError) as exc:
        raise BuildFailure("report", str(exc)) from exc


def run_validation(*, repo: Path | None = None, node=None, npm_cli=None,
                   report_path: Path | None = None, executor: Executor | None = None) -> dict:
    """Run the isolated public build boundary and always return a structured verdict."""
    repo = _absolute(repo or REPO)
    app_source = repo / "custom"
    result = {
        "schema_version": 1, "status": "failed", "checks": [], "failures": [],
        "python": {"executable": sys.executable, "version": sys.version.split()[0]},
        "node": {}, "npm": {},
        "source_unchanged": {"status": "unknown", "input_files": 0, "metadata_roots": len(WATCHED_OUTPUTS)},
        "temporary_isolation": {"status": "unknown", "cleaned": False}, "boundary": BOUNDARY,
    }
    temporary, before, root = None, None, None
    stage = "inputs"
    publish_report = False
    run = executor or _execute
    try:
        if report_path is not None:
            stage = "report"
            temporary_report_target(report_path, repository=repo)
            publish_report = True
        stage = "inputs"
        before = _source_state(app_source)
        result["source_unchanged"]["input_files"] = len(before["inputs"])
        node_path, cli = _runtime_paths(node, npm_cli)
        result["node"]["executable"] = str(node_path)
        result["npm"]["cli"] = str(cli)
        stage = "isolation"
        temp_parent = _temporary_parent(repo)
        temporary = TemporaryDirectory(prefix="wenshu-build-", dir=temp_parent)
        root = _absolute(temporary.name)
        result["temporary_isolation"]["root"] = str(root)
        app, docs = root / "app", root / "docs"
        copied = copy_app(app_source, app)
        _copy_fixture_tree(FIXTURES / "docs", docs)
        _copy_fixture_tree(FIXTURES / "public", app / "public")
        env = _child_environment(root, app, docs, node_path)
        result["checks"].append({"name": "copied-inputs", "status": "passed", "files": len(copied)})
        stage = "runtime"
        # Only executable-package version metadata is read here, never npm user configuration.
        probe = "const fs=require('node:fs'),p=require('node:path');const pkg=JSON.parse(fs.readFileSync(p.join(p.dirname(process.argv[1]),'../package.json'),'utf8'));fs.writeFileSync(process.env.WENSHU_BUILD_RUNTIME_REPORT,JSON.stringify({node:process.version,npm:pkg.version}));"
        code = run([str(node_path), "-e", probe, str(cli)], cwd=app, env=env)
        if code != 0:
            raise BuildFailure(stage, f"Runtime preflight exited {code}; verify --node and --npm-cli.")
        versions = json.loads(Path(env["WENSHU_BUILD_RUNTIME_REPORT"]).read_text(encoding="utf-8"))
        if not isinstance(versions, dict):
            raise BuildFailure("runtime", "Runtime preflight did not return a Node/npm version object.")
        result["node"]["version"], result["npm"]["version"] = versions.get("node", ""), versions.get("npm", "")
        _require_version(result["node"]["version"], (22, 12, 0), "Node")
        _require_version(result["npm"]["version"], (9, 6, 5), "npm")
        result["checks"].append({"name": "runtime-minimums", "status": "passed"})
        stage = "install"
        code = run([str(node_path), str(cli), "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--include=dev", "--include=optional"], cwd=app, env=env)
        if code != 0:
            raise BuildFailure(stage, f"Isolated npm ci exited {code}; inspect the inherited install output.")
        result["checks"].append({"name": "locked-install", "status": "passed", "ignore_scripts": True})
        stage = "build"
        script = "build:win" if os.name == "nt" else "build"
        code = run([str(node_path), str(cli), "run", script], cwd=app, env=env)
        if code != 0:
            raise BuildFailure(stage, f"Isolated npm run {script} exited {code}; inspect inherited producer/Astro/Pagefind output.")
        result["checks"].append({"name": "build-pipeline", "status": "passed", "script": script})
        stage = "smoke"
        smoke_checks = check_artifacts(app / "dist", FIXTURES)
        result["checks"].extend(smoke_checks)
        for check in smoke_checks:
            if check["status"] != "passed":
                result["failures"].append({"stage": "smoke", "check": check["name"], "message": check["detail"]})
    except Exception as exc:
        # Any build/copy/fixture protocol error is a failed verdict, never xfail
        # or an uncaught exception that loses the requested JSON evidence.
        failed_stage = exc.stage if isinstance(exc, BuildFailure) else stage
        result["failures"].append({"stage": failed_stage, "message": str(exc)[:1200]})
        result["checks"].append({"name": failed_stage, "status": "failed"})
    finally:
        if before is not None:
            try:
                if _source_state(app_source) != before:
                    raise BuildFailure("integrity", "Source input fingerprints or protected output metadata changed.")
                result["source_unchanged"]["status"] = "passed"
                result["checks"].append({"name": "source-unchanged", "status": "passed"})
            except (OSError, RuntimeError) as exc:
                result["source_unchanged"]["status"] = "failed"
                result["failures"].append({"stage": "integrity", "message": str(exc)[:1200]})
        if temporary is not None:
            try:
                temporary.cleanup()
                if root.exists():
                    raise BuildFailure("cleanup", "The owned temporary build directory still exists.")
                result["temporary_isolation"].update({"status": "passed", "cleaned": True})
                result["checks"].append({"name": "temporary-cleanup", "status": "passed"})
            except (OSError, RuntimeError) as exc:
                result["temporary_isolation"]["status"] = "failed"
                result["failures"].append({"stage": "cleanup", "message": str(exc)[:1200]})
        elif root is None:
            result["temporary_isolation"].update({"status": "passed", "cleaned": True})
    result["status"] = "failed" if result["failures"] else "passed"
    if publish_report:
        try:
            _write_report(Path(report_path), result, repo)
        except (OSError, RuntimeError) as exc:
            result["status"] = "failed"
            result["failures"].append({"stage": "report", "message": str(exc)[:1200]})
    return result


def main(argv: Sequence[str] | None = None, *, repo: Path | None = None,
         executor: Executor | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", help="Node executable (>=22.12.0); no runtimes are installed")
    parser.add_argument("--npm-cli", help="npm CLI JavaScript; .cmd siblings can be resolved without running wrappers")
    parser.add_argument("--report", type=Path, help="New JSON file in an existing temporary directory; never overwrite source or reports")
    args = parser.parse_args(argv)
    result = run_validation(repo=repo, node=args.node, npm_cli=args.npm_cli,
                            report_path=args.report, executor=executor)
    print(f"WenShu isolated build: {result['status']}")
    for failure in result["failures"]:
        print(f"[{failure['stage']}] {failure['message']}", file=sys.stderr)
    print(BOUNDARY)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
