"""Own all runtime state before importing WenShu; never load its live config.

The only production seam replaced is subprocess.run. Each sandbox is sequential,
loopback-only, and disposable. Tests observe HTTP or this owned file namespace.
"""
import difflib
import http.client
import http.server
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from unittest import mock

from tests.baseline.tempdirs import TemporaryDirectory


TEST_PASSWORD = "synthetic-wenshu-password-not-a-real-credential"
TEST_API_TOKEN = "synthetic-wenshu-api-token-not-a-real-credential"
SERVER_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "custom" / "server.py"
_SANDBOX_LOCK = threading.Lock()


class SandboxViolation(RuntimeError):
    """An operation crossed the sandbox's explicit safety boundary."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple
    body: bytes

    def header(self, name):
        return next((value for key, value in self.headers if key.lower() == name.lower()), None)


class ServerSandbox:
    def __init__(self, *, docs=None, dist=None, shares=None, faults=(), links=None):
        self._faults = list(faults)
        # link 只在真实文件系统上建立（junction），用于验证静态读取不跟随越界链接。
        self._links = dict(links or {})
        if any(name not in {"git-add", "git-commit", "build"} for name in self._faults):
            raise ValueError("Unregistered synthetic subprocess fault")
        self._staged = None
        self._commit = "synthetic-initial-commit"
        self._share_seed = dict(shares or {})
        self._links_made = []
        self._doc_seed = dict(docs or {})
        self._dist_seed = dict(dist or {})
        self._temp = None
        self._server = None
        self._thread = None
        self._patch = None
        self._owns_lock = False
        self._violations = []

    def __enter__(self):
        if not _SANDBOX_LOCK.acquire(blocking=False):
            raise SandboxViolation("HTTP sandboxes must run sequentially")
        self._owns_lock = True
        try:
            self._temp = TemporaryDirectory(prefix="wenshu-baseline-",
                                                     dir=os.environ.get("WENSHU_TEST_TMPDIR"))
            self.root = pathlib.Path(self._temp.name).resolve()
            self.app = self.root / "app"
            self.docs = self.root / "docs"
            self.dist = self.app / "dist"
            self.app.mkdir()
            self.docs.mkdir()
            self.dist.mkdir()
            self._seed_tree(self.docs, self._doc_seed)
            self._seed_tree(self.dist, self._dist_seed)
            self._make_links()
            self._tracked = self._document_bytes()
            # Copy this single source file, not its directory or runtime neighbors.
            copied_source = self.app / "server.py"
            shutil.copyfile(SERVER_SOURCE, copied_source)
            config = {"password": TEST_PASSWORD, "api_token": TEST_API_TOKEN,
                      "port": 0, "docs_dir": str(self.docs)}
            (self.app / "server_config.json").write_text(json.dumps(config), encoding="utf-8")
            (self.app / "sessions.json").write_text("{}", encoding="utf-8")
            (self.app / "shares.json").write_text(json.dumps(self._share_seed), encoding="utf-8")
            self._patch = mock.patch("subprocess.run", new=self._run_command)
            self._patch.start()
            spec = importlib.util.spec_from_file_location("wenshu_sandbox_" + uuid.uuid4().hex,
                                                         copied_source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Same persisted-state bootstrap as main(), without its public bind.
            module.load_shares()
            module.load_sessions()
            self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
            self.address = self._server.server_address
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={"poll_interval": 0.01},
                                            name="wenshu-baseline-http", daemon=True)
            self._thread.start()
            self._check_safety()
            return self
        except BaseException:
            self._close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        try:
            self._close()
        finally:
            self._check_safety()
            if self._faults:
                raise SandboxViolation("A scripted subprocess fault was not exercised")

    @staticmethod
    def _seed_tree(base, files):
        for relative, content in files.items():
            target = (base / relative).resolve()
            if not target.is_relative_to(base):
                raise SandboxViolation("Fixture paths must remain in their owned tree")
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")

    def _make_links(self):
        """Create directory links before subprocess.run is replaced.

        A junction or symlink is the only faithful model of a filesystem escape;
        the target stays inside this sandbox's own disposable root.
        """
        for relative, target in self._links.items():
            link = self.root / relative
            target = pathlib.Path(target)
            if not target.is_absolute():
                # 相对目标按沙箱根解析，测试不必知道随机临时目录名。
                target = self.root / target
            if not target.is_relative_to(self.root):
                raise SandboxViolation("A link target must remain in its owned tree")
            target.mkdir(parents=True, exist_ok=True)
            if not link.parent.is_dir():
                raise SandboxViolation("A link's parent directory must exist first")
            if link.exists() or link.is_symlink():
                raise SandboxViolation("A link must not replace an existing path")
            if os.name == "nt":
                completed = subprocess.run(["cmd", "/d", "/c", "mklink", "/J",
                                            str(link), str(target)],
                                           capture_output=True, text=True)
                if completed.returncode != 0:
                    raise SandboxViolation("Could not create a synthetic junction: "
                                           + (completed.stderr or completed.stdout))
            else:
                os.symlink(target, link, target_is_directory=True)
            self._links_made.append(link)

    def _drop_links(self):
        for link in reversed(self._links_made):
            try:
                if os.name == "nt":
                    os.rmdir(link)
                else:
                    link.unlink()
            except OSError:
                pass
        self._links_made = []

    def _document_bytes(self):
        return {path.relative_to(self.docs).as_posix(): path.read_bytes()
                for path in self.docs.rglob("*") if path.is_file() and ".git" not in path.parts}

    def _reject_command(self):
        message = "Unexpected subprocess command or options; no real command executed"
        self._violations.append(message)
        raise SandboxViolation(message)

    def _faulted(self, name):
        if name in self._faults:
            self._faults.remove(name)
            return True
        return False

    def _run_command(self, args, **kwargs):
        if not isinstance(args, (list, tuple)) or not all(isinstance(arg, str) for arg in args):
            return self._reject_command()
        command = tuple(args)
        options = {"cwd": str(self.docs), "capture_output": True, "text": True}
        if command == ("npm", "run", "build"):
            if kwargs != {**options, "cwd": str(self.app), "timeout": 900}:
                return self._reject_command()
            failed = self._faulted("build")
            return subprocess.CompletedProcess(args, 1 if failed else 0, "",
                                               "synthetic build failure" if failed else "")
        if kwargs != options:
            return self._reject_command()
        if command == ("git", "add", "-A"):
            if self._faulted("git-add"):
                return subprocess.CompletedProcess(args, 1, "", "synthetic git add failure")
            self._staged = self._document_bytes()
            return subprocess.CompletedProcess(args, 0, "", "")
        if len(command) == 4 and command[:3] == ("git", "commit", "-m") and command[3]:
            if self._faulted("git-commit"):
                return subprocess.CompletedProcess(args, 1, "", "synthetic git commit failure")
            if self._staged is None or self._staged == self._tracked:
                return subprocess.CompletedProcess(args, 1, "", "synthetic nothing staged")
            self._tracked = self._staged
            self._staged = None
            self._commit = "synthetic-baseline-commit"
            return subprocess.CompletedProcess(args, 0, "synthetic commit created", "")
        if command == ("git", "rev-parse", "--short", "HEAD"):
            return subprocess.CompletedProcess(args, 0, self._commit, "")
        if command == ("git", "diff", "HEAD"):
            current = self._document_bytes()
            chunks = []
            for relative, original in sorted(self._tracked.items()):
                changed = current.get(relative, b"")
                if changed != original:
                    chunks.extend(difflib.unified_diff(
                        original.decode("utf-8").splitlines(keepends=True),
                        changed.decode("utf-8").splitlines(keepends=True),
                        fromfile="a/" + relative, tofile="b/" + relative))
            return subprocess.CompletedProcess(args, 0, "".join(chunks), "")
        if command == ("git", "checkout", "--", "."):
            # Model Git accurately: tracked paths restore; untracked files remain.
            for relative, original in self._tracked.items():
                target = self.docs / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original)
            self._staged = None
            return subprocess.CompletedProcess(args, 0, "", "")
        return self._reject_command()

    def _check_safety(self):
        if self._violations:
            raise SandboxViolation(self._violations[0])

    def request(self, method, target, *, body=None, headers=None):
        self._check_safety()
        connection = http.client.HTTPConnection(*self.address, timeout=3)
        try:
            # http.client preserves encoded dot segments; no URL normalization.
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            result = Response(response.status, tuple(response.getheaders()), response.read())
        finally:
            connection.close()
        self._check_safety()
        return result

    def _close(self):
        try:
            if self._server is not None:
                if self._thread is not None and self._thread.is_alive():
                    self._server.shutdown()
                self._server.server_close()
                if self._thread is not None:
                    self._thread.join(timeout=3)
                    if self._thread.is_alive():
                        raise SandboxViolation("sandbox HTTP thread did not stop")
        finally:
            try:
                if self._patch is not None:
                    self._patch.stop()
                    self._patch = None
            finally:
                try:
                    # 链接必须先摘掉，否则临时树清理会跟随链接删到外面。
                    self._drop_links()
                    if self._temp is not None:
                        self._temp.cleanup()
                        self._temp = None
                finally:
                    if self._owns_lock:
                        self._owns_lock = False
                        _SANDBOX_LOCK.release()
