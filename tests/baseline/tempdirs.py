"""Owned test trees without changing existing ACLs or cleanup permissions.

On Windows, mkdir's default mode inherits the caller's existing sandbox grant.
The 0o700 mode used by tempfile instead installs a private DACL that can exclude
that restricted token. POSIX test trees remain owner-only.
"""

import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile


def _is_link(path):
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _require_temporary_location(target, repository):
    value = os.environ.get("WENSHU_TEST_TMPDIR")
    scratch = Path(value).resolve() if value else None
    namespace = repository / "tests" / ".baseline-tmp"
    if target.is_relative_to(repository) and not (
        scratch is not None and scratch.is_relative_to(namespace) and target.is_relative_to(scratch)
    ):
        raise ValueError("Temporary outputs may not write source data; use an owned temporary directory")
    roots = [Path(tempfile.gettempdir()).resolve()]
    roots.extend(Path(os.environ[name]).resolve() for name in ("RUNNER_TEMP", "WENSHU_TEST_TMPDIR")
                 if os.environ.get(name))
    if not any(target.is_relative_to(root) for root in roots):
        raise ValueError("Temporary outputs require a temporary target")


def temporary_report_target(path, *, repository=None):
    repository = Path(repository or Path(__file__).resolve().parents[2]).resolve()
    requested = Path(path)
    if ".." in requested.parts:
        raise ValueError("Report paths must not contain parent traversal")
    target = requested.absolute()
    for part in (target, *target.parents):
        try:
            if _is_link(part):
                raise ValueError("Refusing a linked report path")
        except FileNotFoundError:
            continue
    target = target.resolve()
    _require_temporary_location(target, repository)
    if not target.parent.is_dir():
        raise ValueError("The temporary report directory must already exist")
    if target.exists():
        raise FileExistsError("Refusing to overwrite an existing report")
    return target


def write_json_report(report, path, *, repository=None):
    target = temporary_report_target(path, repository=repository)
    with target.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")


class TemporaryDirectory:
    def __init__(self, suffix="", prefix="wenshu-test-", dir=None):
        for component in (prefix, suffix):
            if not isinstance(component, str) or any(char in component for char in ("/", "\\", "\0")):
                raise ValueError("Temporary names must be single text path components")
        requested = Path(dir or os.environ.get("WENSHU_TEST_TMPDIR") or tempfile.gettempdir())
        if ".." in requested.parts:
            raise ValueError("Temporary parent must not contain parent traversal")
        parent = requested.absolute()
        if any(_is_link(part) for part in (parent, *parent.parents)):
            raise ValueError("Temporary parent must not traverse links or reparse points")
        self._parent = parent.resolve(strict=True)
        if not self._parent.is_dir():
            raise NotADirectoryError(self._parent)
        _require_temporary_location(self._parent, Path(__file__).resolve().parents[2])
        for _ in range(10):
            candidate = self._parent / (prefix + secrets.token_hex(16) + suffix)
            try:
                candidate.mkdir(mode=0o777 if os.name == "nt" else 0o700)
            except FileExistsError:
                continue
            self._path = candidate
            self._identity = candidate.resolve(strict=True)
            self.name = str(candidate)
            self._closed = False
            break
        else:
            raise FileExistsError("Could not allocate a unique owned temporary tree")

    def __enter__(self):
        if self._closed:
            raise RuntimeError("Temporary tree has already been cleaned")
        return self.name

    def __exit__(self, exc_type, exc, traceback):
        self.cleanup()

    def cleanup(self):
        if self._closed:
            return
        try:
            if _is_link(self._path) or self._path.resolve(strict=True) != self._identity:
                raise RuntimeError("Refusing cleanup of a replaced or redirected test tree")
            if self._identity.parent != self._parent:
                raise RuntimeError("Refusing cleanup outside the owned test namespace")
            shutil.rmtree(self._path)
        except FileNotFoundError:
            pass
        self._closed = True
