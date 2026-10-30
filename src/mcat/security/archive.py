"""Hardened read-only access to untrusted ZIP-based archives (.zip, .mrpack, .jar).

Protects against ZIP Slip / absolute paths, symlink entries, decompression bombs (entry count, total
size, per-entry size and compression ratio limits), and unsafe overwrites on extraction.
"""

from __future__ import annotations

import io
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class UnsafeArchiveError(Exception):
    """The archive violates a safety rule and must not be processed."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 50_000
    max_total_uncompressed: int = 4 * 1024**3  # 4 GiB - large modpacks with bundled mods exist
    max_entry_uncompressed: int = 1024**3
    max_ratio: float = 200.0  # uncompressed / compressed, for entries > 1 MiB
    max_read_bytes: int = 64 * 1024**2  # for read_member on metadata files


DEFAULT_LIMITS = ArchiveLimits()


def normalize_member(name: str) -> str:
    """Validate an archive member name and return its normalized POSIX form.

    Rejects absolute paths, drive letters, backslash tricks, '..' components and NUL bytes.
    """
    if "\x00" in name:
        raise UnsafeArchiveError(f"NUL byte in member name: {name!r}")
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        raise UnsafeArchiveError(f"absolute path in archive: {name!r}")
    parts = [p for p in PurePosixPath(cleaned).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeArchiveError(f"path traversal in archive: {name!r}")
    return "/".join(parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return info.create_system == 3 and stat.S_ISLNK(mode)


class SafeZip:
    """A validated, read-only view of a ZIP archive. Validation happens on open."""

    def __init__(self, path: Path | bytes, limits: ArchiveLimits = DEFAULT_LIMITS):
        """Open an archive from a path, or from in-memory bytes (e.g. a JAR read out of a pack)."""
        self.path = path if isinstance(path, Path) else None
        self.limits = limits
        label = path.name if isinstance(path, Path) else "<memory>"
        try:
            self._zf = zipfile.ZipFile(io.BytesIO(path) if isinstance(path, bytes) else path)
        except (zipfile.BadZipFile, OSError) as e:
            raise UnsafeArchiveError(f"not a valid ZIP archive: {label}: {e}") from e
        self.members: dict[str, zipfile.ZipInfo] = {}
        self._validate()

    def _validate(self) -> None:
        infos = self._zf.infolist()
        if len(infos) > self.limits.max_entries:
            raise UnsafeArchiveError(f"too many entries ({len(infos)} > {self.limits.max_entries})")
        total = 0
        for info in infos:
            name = normalize_member(info.filename)
            if _is_symlink(info):
                raise UnsafeArchiveError(f"symlink entry not allowed: {info.filename!r}")
            if info.file_size > self.limits.max_entry_uncompressed:
                raise UnsafeArchiveError(f"entry too large: {info.filename!r} ({info.file_size} bytes)")
            if info.file_size > 1024**2 and info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > self.limits.max_ratio:
                    raise UnsafeArchiveError(f"suspicious compression ratio {ratio:.0f}x: {info.filename!r}")
            total += info.file_size
            if total > self.limits.max_total_uncompressed:
                raise UnsafeArchiveError("archive exceeds total uncompressed size limit")
            if name in self.members and not info.is_dir():
                raise UnsafeArchiveError(f"duplicate entry: {name!r}")
            if name:
                self.members[name] = info

    def names(self) -> list[str]:
        return sorted(n for n, i in self.members.items() if not i.is_dir())

    def read(self, name: str, max_bytes: int | None = None) -> bytes:
        info = self.members.get(normalize_member(name))
        if info is None:
            raise KeyError(name)
        limit = max_bytes or self.limits.max_read_bytes
        if info.file_size > limit:
            raise UnsafeArchiveError(f"{name!r} exceeds read limit ({info.file_size} > {limit})")
        with self._zf.open(info) as fh:
            data = fh.read(limit + 1)  # declared size can lie; never read more than the limit
        if len(data) > limit:
            raise UnsafeArchiveError(f"{name!r} decompressed beyond its declared size")
        return data

    def size(self, name: str) -> int:
        return self.members[normalize_member(name)].file_size

    def extract_all(self, dest: Path, *, prefix: str = "", strip_prefix: bool = True) -> list[Path]:
        """Extract members (optionally only those under ``prefix``) into ``dest``.

        ``dest`` must not already contain the target files: existing files are never overwritten.
        """
        dest = dest.resolve()
        written: list[Path] = []
        for name, info in sorted(self.members.items()):
            if info.is_dir() or (prefix and not name.startswith(prefix)):
                continue
            rel = name[len(prefix) :] if (prefix and strip_prefix) else name
            if not rel:
                continue
            target = (dest / rel).resolve()
            if not target.is_relative_to(dest):
                raise UnsafeArchiveError(f"entry escapes destination: {name!r}")
            if target.exists() or target.is_symlink():
                raise UnsafeArchiveError(f"refusing to overwrite existing file: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            remaining = self.limits.max_entry_uncompressed
            with self._zf.open(info) as src, open(target, "xb") as out:
                while chunk := src.read(1024 * 1024):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise UnsafeArchiveError(f"{name!r} decompressed beyond limit")
                    out.write(chunk)
            written.append(target)
        return written

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> SafeZip:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def safe_join(root: Path, rel: str) -> Path:
    """Join an untrusted relative path (e.g. from a manifest) under root, rejecting escapes."""
    norm = normalize_member(rel)
    target = (root / norm).resolve()
    if not target.is_relative_to(root.resolve()):
        raise UnsafeArchiveError(f"path escapes root: {rel!r}")
    return target


def ensure_fresh_output(path: Path, *, allow_existing_empty: bool = True) -> Path:
    """Validate an output directory: it must not exist, or exist and be empty. Never a symlink."""
    if path.is_symlink():
        raise UnsafeArchiveError(f"output path is a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise UnsafeArchiveError(f"output path exists and is not a directory: {path}")
        if any(path.iterdir()) or not allow_existing_empty:
            raise UnsafeArchiveError(f"output directory is not empty: {path} (choose a new directory)")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()
