"""Pack creation: spec -> resolved lock -> packwiz-format directory and optional .mrpack export.

packwiz's on-disk format is written natively (packwiz itself is a Go CLI that is often not installed);
when the ``packwiz`` binary is present, ``packwiz refresh`` is run afterwards as an independent check.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import tomli_w

from mcat import __version__
from mcat.loaders import LoaderCatalog
from mcat.models import ModVersion
from mcat.providers.curseforge import CurseForgeProvider, CurseForgeUnavailable
from mcat.providers.modrinth import ModrinthProvider
from mcat.resolver.core import Request, Resolution, Resolver, lock_dict
from mcat.resolver.spec import PackSpec
from mcat.resolver.unify import HashLookup, unify
from mcat.security.archive import ensure_fresh_output, normalize_member

MRPACK_ALLOWED_DOWNLOAD_HOSTS = ("cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com")
_PACKWIZ_LOADER_KEY = {"fabric": "fabric", "neoforge": "neoforge", "forge": "forge"}
_MRPACK_LOADER_KEY = {"fabric": "fabric-loader", "neoforge": "neoforge", "forge": "forge"}


class PackBuildError(Exception):
    pass


def make_fetchers(
    need_curseforge: bool, *, minecraft: str | None = None, loader: str | None = None
) -> tuple[Any, Any, list[str]]:
    """Return (fetch, fetch_version, warnings) backed by the real providers.

    CurseForge projects can have thousands of files, so its fetcher asks the API for the target's files
    (server-side filters, paginated) and falls back to the newest page only to explain alternatives.
    """
    mr = ModrinthProvider()
    cf: CurseForgeProvider | None = None
    warnings: list[str] = []
    if need_curseforge:
        try:
            cf = CurseForgeProvider()
        except CurseForgeUnavailable as e:
            warnings.append(str(e))

    def fetch(provider: str, project: str) -> list[ModVersion]:
        if provider == "modrinth":
            return mr.versions(project)
        if cf is None:
            raise CurseForgeUnavailable(f"curseforge:{project} requested but no CurseForge API key is configured")
        found = cf.versions(project, minecraft=minecraft, loader=loader) if minecraft else []
        return found or cf.versions(project, limit=50)

    def fetch_version(provider: str, version_id: str) -> ModVersion | None:
        if provider == "modrinth":
            return mr.version(version_id)
        return cf.version(version_id) if cf is not None else None

    return fetch, fetch_version, warnings


def resolve_spec(
    spec: PackSpec,
    *,
    fetch: Any = None,
    fetch_version: Any = None,
    catalog: LoaderCatalog | None = None,
    hash_lookup: HashLookup | None = None,
) -> tuple[Resolution, dict[str, Any], list[str]]:
    warnings: list[str] = []
    if fetch is None:
        fetch, fetch_version, warnings = make_fetchers(
            any(m.provider == "curseforge" for m in spec.mods), minecraft=spec.minecraft, loader=spec.loader
        )
    catalog = catalog or LoaderCatalog()
    support = catalog.support(spec.loader, spec.minecraft)
    if not support.supported:
        raise PackBuildError(f"{spec.loader} does not support Minecraft {spec.minecraft} ({support.evidence})")
    loader_version = spec.loader_version
    if loader_version == "latest":
        loader_version = support.recommended or (support.loader_versions[0] if support.loader_versions else "")
        if not loader_version:
            raise PackBuildError(f"no {spec.loader} loader version found for {spec.minecraft}")
    elif loader_version not in support.loader_versions:
        raise PackBuildError(f"{spec.loader} {loader_version} is not published for Minecraft {spec.minecraft}")
    resolver = Resolver(
        fetch,
        spec.minecraft,
        spec.loader,
        spec.channels,
        include_optional=spec.include_optional,
        exclude=spec.exclude,
        fetch_version=fetch_version,
    )
    res = resolver.resolve([Request(m.provider, m.id, m.version, m.side, m.optional) for m in spec.mods])
    identity = unify(res, hash_lookup or ModrinthProvider().versions_by_hash)
    if identity.lookup_error:
        warnings.append(
            f"cross-provider duplicate check could not run ({identity.lookup_error}); "
            "the same mod may be listed from both providers"
        )
    target = {"minecraft": spec.minecraft, "loader": spec.loader, "loader_version": loader_version}
    lock = lock_dict(res, target=target, spec_digest=spec.digest, java=support.java, tool_version=__version__)
    lock["pack"] = {"name": spec.name, "version": spec.version}
    lock["loader_evidence"] = support.evidence
    lock["provider_identity"] = {
        "method": "curseforge sha1 -> modrinth version_files",
        "checked": identity.checked,
        "merged": identity.merged,
        "conflicts": identity.conflicts,
        "unmatched": identity.unmatched,
        "error": identity.lookup_error,
    }
    return res, lock, warnings


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_packwiz(lock: dict[str, Any], out: Path, extra_files: dict[str, bytes] | None = None) -> list[str]:
    """Write a packwiz pack into ``out`` (must be new or empty). Returns notes for restricted files.

    ``extra_files`` (instance-relative path -> bytes) are written and indexed as regular pack files
    (configs, scripts, datapacks...).
    """
    out = ensure_fresh_output(out)
    notes = []
    index_files: list[dict[str, Any]] = []
    for rel, data in sorted((extra_files or {}).items()):
        rel = normalize_member(rel)
        if rel in ("pack.toml", "index.toml", "mcat.lock.json", ".packwizignore") or rel.endswith(".pw.toml"):
            notes.append(f"skipped reserved path from content: {rel}")
            continue
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        index_files.append({"file": rel, "hash": _sha256(data)})
    for m in lock["mods"]:
        f = m["file"]
        if f is None:
            raise PackBuildError(f"{m['slug']} has no file in its selected version")
        meta: dict[str, Any] = {
            "name": m["name"],
            "filename": normalize_member(f["filename"]),
            "side": m["side"] if m["side"] in ("client", "server", "both") else "both",
        }
        if m["provider"] == "modrinth":
            algo = "sha512" if "sha512" in f["hashes"] else "sha1"
            meta["download"] = {"url": f["url"], "hash-format": algo, "hash": f["hashes"][algo]}
            meta["update"] = {"modrinth": {"mod-id": m["project_id"], "version": m["version_id"]}}
        else:
            meta["download"] = {
                "hash-format": "sha1",
                "hash": f["hashes"].get("sha1", ""),
                "mode": "metadata:curseforge",
            }
            meta["update"] = {"curseforge": {"file-id": int(m["version_id"]), "project-id": int(m["project_id"])}}
            if not f.get("download_allowed", True):
                notes.append(
                    f"{m['slug']}: author disallows third-party distribution; users must download it "
                    "manually through CurseForge"
                )
        if m.get("optional"):
            meta["option"] = {"optional": True, "default": False}
        rel = f"mods/{m['slug']}.pw.toml"
        data = tomli_w.dumps(meta).encode()
        (out / "mods").mkdir(exist_ok=True)
        (out / rel).write_bytes(data)
        index_files.append({"file": rel, "hash": _sha256(data), "metafile": True})
    index = tomli_w.dumps({"hash-format": "sha256", "files": sorted(index_files, key=lambda x: x["file"])}).encode()
    (out / "index.toml").write_bytes(index)
    t = lock["target"]
    pack = {
        "name": lock["pack"]["name"],
        "version": lock["pack"]["version"],
        "pack-format": "packwiz:1.1.0",
        "index": {"file": "index.toml", "hash-format": "sha256", "hash": _sha256(index)},
        # packwiz writes the versions table with sorted keys; matching it keeps `packwiz refresh` a no-op
        "versions": dict(
            sorted({"minecraft": t["minecraft"], _PACKWIZ_LOADER_KEY[t["loader"]]: t["loader_version"]}.items())
        ),
    }
    (out / "pack.toml").write_bytes(tomli_w.dumps(pack).encode())
    # the lockfile is MCAT provenance, not instance content: keep packwiz from indexing/distributing it
    (out / ".packwizignore").write_text("mcat.lock.json\n")
    (out / "mcat.lock.json").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    return notes


def packwiz_refresh(out: Path) -> tuple[bool | None, str]:
    exe = shutil.which("packwiz")
    if not exe:
        return None, "packwiz not installed; native format writer used without packwiz cross-check"
    p = subprocess.run([exe, "refresh"], cwd=out, capture_output=True, text=True, timeout=120, check=False)
    return p.returncode == 0, (p.stdout + p.stderr)[-2000:]


def write_mrpack(lock: dict[str, Any], dest: Path) -> list[str]:
    """Export a Modrinth .mrpack. Files not hosted on Modrinth-permitted hosts cannot be included."""
    if dest.exists():
        raise PackBuildError(f"refusing to overwrite {dest}")
    t = lock["target"]
    files, excluded = [], []
    for m in lock["mods"]:
        f = m["file"]
        url = f and f.get("url")
        host = url.split("/")[2] if url and url.startswith("https://") else ""
        if not url or host not in MRPACK_ALLOWED_DOWNLOAD_HOSTS:
            excluded.append(f"{m['slug']} ({m['provider']}): not downloadable from a Modrinth-permitted host")
            continue
        side = m["side"]
        files.append(
            {
                "path": f"mods/{normalize_member(f['filename'])}",
                "hashes": {k: v for k, v in f["hashes"].items() if k in ("sha1", "sha512")},
                "env": {
                    "client": "unsupported" if side == "server" else ("optional" if m.get("optional") else "required"),
                    "server": "unsupported" if side == "client" else ("optional" if m.get("optional") else "required"),
                },
                "downloads": [url],
                "fileSize": f.get("size") or 0,
            }
        )
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": lock["pack"]["version"],
        "name": lock["pack"]["name"],
        "files": files,
        "dependencies": {"minecraft": t["minecraft"], _MRPACK_LOADER_KEY[t["loader"]]: t["loader_version"]},
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "x", zipfile.ZIP_DEFLATED) as z:
        z.writestr("modrinth.index.json", json.dumps(index, indent=2))
    return excluded


def problems_as_dicts(res: Resolution) -> list[dict[str, Any]]:
    return [asdict(p) for p in res.problems]
