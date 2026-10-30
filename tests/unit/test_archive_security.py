import stat
import zipfile
from pathlib import Path

import pytest

from mcat.security.archive import (
    ArchiveLimits,
    SafeZip,
    UnsafeArchiveError,
    ensure_fresh_output,
    normalize_member,
    safe_join,
)
from tests.conftest import write_zip


@pytest.mark.parametrize("name", ["../evil", "a/../../evil", "/etc/passwd", "C:/x", "a\\..\\..\\b", "x\x00y"])
def test_normalize_rejects_traversal(name):
    with pytest.raises(UnsafeArchiveError):
        normalize_member(name)


def test_normalize_accepts_normal():
    assert normalize_member("./overrides//config/a.json") == "overrides/config/a.json"


def test_zip_slip_rejected_on_open(tmp_path):
    p = write_zip(tmp_path / "slip.zip", {"../../outside.txt": "x"})
    with pytest.raises(UnsafeArchiveError, match="traversal"):
        SafeZip(p)


def test_symlink_entry_rejected(tmp_path):
    p = tmp_path / "link.zip"
    with zipfile.ZipFile(p, "w") as z:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    with pytest.raises(UnsafeArchiveError, match="symlink"):
        SafeZip(p)


def test_compression_bomb_rejected(tmp_path):
    p = tmp_path / "bomb.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", b"\0" * (20 * 1024**2))
    with pytest.raises(UnsafeArchiveError, match="ratio"):
        SafeZip(p)


def test_entry_count_limit(tmp_path):
    p = write_zip(tmp_path / "many.zip", {f"f{i}": "x" for i in range(20)})
    with pytest.raises(UnsafeArchiveError, match="too many"):
        SafeZip(p, ArchiveLimits(max_entries=10))


def test_not_a_zip(tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(b"not a zip")
    with pytest.raises(UnsafeArchiveError, match="not a valid ZIP"):
        SafeZip(p)


def test_read_limit(tmp_path):
    p = write_zip(tmp_path / "a.zip", {"big.txt": "x" * 2000})
    with SafeZip(p, ArchiveLimits(max_read_bytes=100)) as z, pytest.raises(UnsafeArchiveError):
        z.read("big.txt")


def test_extract_never_overwrites(tmp_path):
    p = write_zip(tmp_path / "a.zip", {"overrides/config/a.txt": "new"})
    dest = tmp_path / "out"
    (dest / "config").mkdir(parents=True)
    (dest / "config" / "a.txt").write_text("old")
    with SafeZip(p) as z, pytest.raises(UnsafeArchiveError, match="overwrite"):
        z.extract_all(dest, prefix="overrides/")
    assert (dest / "config" / "a.txt").read_text() == "old"


def test_extract_prefix(tmp_path):
    p = write_zip(tmp_path / "a.zip", {"overrides/config/a.txt": "1", "modrinth.index.json": "{}"})
    with SafeZip(p) as z:
        written = z.extract_all(tmp_path / "out", prefix="overrides/")
    assert [w.relative_to(tmp_path / "out").as_posix() for w in written] == ["config/a.txt"]


def test_safe_join(tmp_path):
    assert safe_join(tmp_path, "a/b") == (tmp_path / "a/b").resolve()
    with pytest.raises(UnsafeArchiveError):
        safe_join(tmp_path, "../x")


def test_ensure_fresh_output(tmp_path):
    out = tmp_path / "o"
    ensure_fresh_output(out)
    (out / "f").write_text("x")
    with pytest.raises(UnsafeArchiveError, match="not empty"):
        ensure_fresh_output(out)
    link = tmp_path / "l"
    link.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(UnsafeArchiveError, match="symlink"):
        ensure_fresh_output(link)


def test_inspect_symlinked_dir_not_followed(tmp_path: Path):
    from mcat.parsers.source import PackSource

    root = tmp_path / "pack"
    (root / "mods").mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("KEY")
    (root / "config").symlink_to(secret)
    src = PackSource(root)
    assert "config" in src.symlinks
    assert not any("id_rsa" in n for n in src.names())
