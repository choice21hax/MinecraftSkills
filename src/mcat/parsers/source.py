"""Uniform read-only access to a pack given as an archive or a directory."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from mcat.security.archive import SafeZip, UnsafeArchiveError, normalize_member


class PackSource:
    """Read-only view over a ZIP-like archive or a directory. Never writes to the source."""

    def __init__(self, path: Path):
        self.path = path.resolve() if not path.is_symlink() else path
        self.symlinks: list[str] = []
        self._zip: SafeZip | None = None
        self._files: dict[str, int] = {}
        if path.is_dir():
            self.kind = "directory"
            self._scan_dir()
        elif path.is_file():
            self.kind = "archive"
            self._zip = SafeZip(path)
        else:
            raise FileNotFoundError(f"no such pack: {path}")

    def _scan_dir(self) -> None:
        root = self.path
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for d in list(dirnames):
                full = Path(dirpath, d)
                if full.is_symlink():
                    self.symlinks.append(str(full.relative_to(root)))
                    dirnames.remove(d)  # never follow symlinked directories
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for f in filenames:
                full = Path(dirpath, f)
                rel = full.relative_to(root).as_posix()
                if full.is_symlink():
                    self.symlinks.append(rel)
                    continue
                self._files[rel] = full.stat().st_size

    def names(self) -> list[str]:
        if self._zip:
            return self._zip.names()
        return sorted(self._files)

    def size(self, name: str) -> int:
        if self._zip:
            return self._zip.size(name)
        return self._files[name]

    def exists(self, name: str) -> bool:
        if self._zip:
            return normalize_member(name) in self._zip.members
        return name in self._files

    def read(self, name: str, max_bytes: int = 64 * 1024**2) -> bytes:
        if self._zip:
            return self._zip.read(name, max_bytes)
        if name not in self._files:
            raise KeyError(name)
        if self._files[name] > max_bytes:
            raise UnsafeArchiveError(f"{name} exceeds read limit")
        return (self.path / name).read_bytes()

    def read_text(self, name: str, max_bytes: int = 16 * 1024**2) -> str:
        return self.read(name, max_bytes).decode("utf-8", errors="replace")

    def hashes(self, name: str, max_bytes: int = 512 * 1024**2) -> dict[str, str]:
        data = self.read(name, max_bytes)
        return {"sha1": hashlib.sha1(data).hexdigest(), "sha512": hashlib.sha512(data).hexdigest()}  # noqa: S324

    def close(self) -> None:
        if self._zip:
            self._zip.close()

    def __enter__(self) -> PackSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
