"""Native parsers for modpack formats: Modrinth .mrpack, CurseForge ZIP, packwiz, plain instances.

Parsing is read-only and offline: no downloads, no code execution. Manifest content is untrusted, so
every path it names is normalized and checked for traversal before use.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field
from typing import Any

from mcat.parsers.jarmeta import JarMod, read_jar
from mcat.parsers.source import PackSource
from mcat.security.archive import UnsafeArchiveError, normalize_member

_LOADER_KEYS = {
    "fabric-loader": "fabric",
    "quilt-loader": "quilt",
    "forge": "forge",
    "neoforge": "neoforge",
}


@dataclass
class PackEntry:
    path: str  # e.g. mods/sodium.jar (relative to the instance root)
    name: str
    provider: str | None = None  # modrinth | curseforge | url | local
    project_id: str | None = None
    version_id: str | None = None
    hashes: dict[str, str] = field(default_factory=dict)
    side: str = "both"  # client | server | both | unknown
    downloads: list[str] = field(default_factory=list)
    required: bool = True
    embedded: bool = False  # jar physically present in the source
    jar_mods: list[JarMod] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class PackInfo:
    format: str  # mrpack | curseforge | packwiz | instance
    name: str
    version: str
    minecraft: str | None
    loader: str | None
    loader_version: str | None
    entries: list[PackEntry] = field(default_factory=list)
    override_roots: list[str] = field(default_factory=list)  # archive prefixes mapping to instance root
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PackFormatError(Exception):
    pass


def detect_format(src: PackSource) -> str:
    if src.exists("modrinth.index.json"):
        return "mrpack"
    if src.exists("manifest.json"):
        try:
            m = json.loads(src.read_text("manifest.json"))
            if m.get("manifestType") == "minecraftModpack":
                return "curseforge"
        except ValueError:
            pass
    if src.exists("pack.toml"):
        return "packwiz"
    if any(n.startswith("mods/") and n.endswith(".jar") for n in src.names()):
        return "instance"
    if src.exists("minecraftinstance.json") or src.exists("mmc-pack.json") or src.exists("instance.cfg"):
        return "instance"
    raise PackFormatError("unrecognised pack: no modrinth.index.json, manifest.json, pack.toml or mods/*.jar")


def _scan_embedded_jars(src: PackSource, info: PackInfo, roots: list[str]) -> None:
    known = {e.path for e in info.entries}
    for root in roots:
        prefix = f"{root}/" if root else ""
        for n in src.names():
            if not (n.startswith(prefix + "mods/") and n.endswith(".jar")):
                continue
            rel = n[len(prefix) :]
            if rel.count("/") != 1:
                continue  # nested folders under mods/ are not loaded by the game
            try:
                data = src.read(n, 512 * 1024**2)
            except UnsafeArchiveError as e:
                info.problems.append(f"{n}: {e}")
                continue
            mods, problems = read_jar(data)
            entry = PackEntry(
                path=rel,
                name=(mods[0].name or mods[0].mod_id) if mods else rel.rsplit("/", 1)[-1],
                provider="local",
                hashes={"sha1": hashlib.sha1(data).hexdigest(), "sha512": hashlib.sha512(data).hexdigest()},  # noqa: S324
                side=mods[0].environment if mods else "unknown",
                embedded=True,
                jar_mods=mods,
                problems=problems,
            )
            if rel in known:
                info.problems.append(f"{rel}: present both in manifest and as an override file")
            info.entries.append(entry)


def parse_mrpack(src: PackSource) -> PackInfo:
    idx = json.loads(src.read_text("modrinth.index.json"))
    if idx.get("formatVersion") != 1 or idx.get("game") != "minecraft":
        raise PackFormatError("unsupported modrinth.index.json (formatVersion must be 1, game minecraft)")
    deps: dict[str, str] = idx.get("dependencies", {})
    loader = next((_LOADER_KEYS[k] for k in deps if k in _LOADER_KEYS), None)
    info = PackInfo(
        format="mrpack",
        name=str(idx.get("name", "")),
        version=str(idx.get("versionId", "")),
        minecraft=deps.get("minecraft"),
        loader=loader,
        loader_version=next((deps[k] for k in deps if k in _LOADER_KEYS), None),
        override_roots=["overrides", "client-overrides", "server-overrides"],
    )
    for f in idx.get("files", []):
        path = normalize_member(str(f["path"]))
        env = f.get("env") or {}
        c, s = env.get("client", "required"), env.get("server", "required")
        side = "client" if s == "unsupported" else "server" if c == "unsupported" else "both"
        project_id = version_id = None
        for url in f.get("downloads", []):
            # https://cdn.modrinth.com/data/<project>/versions/<version>/<file>
            parts = url.split("/")
            if "cdn.modrinth.com" in url and len(parts) >= 8 and parts[3] == "data":
                project_id, version_id = parts[4], parts[6]
        info.entries.append(
            PackEntry(
                path=path,
                name=path.rsplit("/", 1)[-1],
                provider="modrinth" if project_id else "url",
                project_id=project_id,
                version_id=version_id,
                hashes=dict(f.get("hashes", {})),
                side=side,
                downloads=list(f.get("downloads", [])),
                required=not (c == "optional" or s == "optional"),
            )
        )
    _scan_embedded_jars(src, info, info.override_roots)
    return info


def parse_curseforge(src: PackSource) -> PackInfo:
    m = json.loads(src.read_text("manifest.json"))
    mc = m.get("minecraft", {})
    primary = (
        next((ld for ld in mc.get("modLoaders", []) if ld.get("primary")), None) or (mc.get("modLoaders") or [{}])[0]
    )
    lid = str(primary.get("id", ""))
    loader, _, lver = lid.partition("-")
    overrides = normalize_member(str(m.get("overrides", "overrides")))
    info = PackInfo(
        format="curseforge",
        name=str(m.get("name", "")),
        version=str(m.get("version", "")),
        minecraft=mc.get("version"),
        loader=loader or None,
        loader_version=lver or None,
        override_roots=[overrides],
    )
    for f in m.get("files", []):
        info.entries.append(
            PackEntry(
                path=f"mods/cf-{f['projectID']}-{f['fileID']}.jar",  # real filename known only via the API
                name=f"curseforge:{f['projectID']}",
                provider="curseforge",
                project_id=str(f["projectID"]),
                version_id=str(f["fileID"]),
                side="unknown",
                required=bool(f.get("required", True)),
            )
        )
    _scan_embedded_jars(src, info, info.override_roots)
    return info


def parse_packwiz(src: PackSource) -> PackInfo:
    pack = tomllib.loads(src.read_text("pack.toml"))
    versions: dict[str, str] = pack.get("versions", {})
    loader = next((k for k in versions if k in ("fabric", "forge", "neoforge", "quilt")), None)
    info = PackInfo(
        format="packwiz",
        name=str(pack.get("name", "")),
        version=str(pack.get("version", "")),
        minecraft=versions.get("minecraft"),
        loader=loader,
        loader_version=versions.get(loader) if loader else None,
        override_roots=[""],
    )
    index_file = normalize_member(str((pack.get("index") or {}).get("file", "index.toml")))
    if not src.exists(index_file):
        info.problems.append(f"index file {index_file} missing")
        return info
    index = tomllib.loads(src.read_text(index_file))
    for f in index.get("files", []):
        if not f.get("metafile"):
            continue
        meta_path = normalize_member(str(f["file"]))
        if not src.exists(meta_path):
            info.problems.append(f"metafile listed in index but missing: {meta_path}")
            continue
        try:
            meta = tomllib.loads(src.read_text(meta_path))
        except tomllib.TOMLDecodeError as e:
            info.problems.append(f"{meta_path}: invalid TOML: {e}")
            continue
        dl = meta.get("download", {})
        upd = meta.get("update", {})
        provider = project_id = version_id = None
        if "modrinth" in upd:
            provider, project_id, version_id = "modrinth", upd["modrinth"].get("mod-id"), upd["modrinth"].get("version")
        elif "curseforge" in upd:
            provider = "curseforge"
            project_id = str(upd["curseforge"].get("project-id"))
            version_id = str(upd["curseforge"].get("file-id"))
        folder = meta_path.rsplit("/", 1)[0] if "/" in meta_path else ""
        hashes = {dl["hash-format"]: dl["hash"]} if dl.get("hash-format") and dl.get("hash") else {}
        info.entries.append(
            PackEntry(
                path=normalize_member(f"{folder}/{meta['filename']}" if folder else str(meta["filename"])),
                name=str(meta.get("name", "")),
                provider=provider or "url",
                project_id=project_id,
                version_id=version_id,
                hashes=hashes,
                side=str(meta.get("side", "both")),
                downloads=[dl["url"]] if dl.get("url") else [],
                required=not (meta.get("option") or {}).get("optional", False),
            )
        )
    _scan_embedded_jars(src, info, [""])
    return info


def parse_instance(src: PackSource) -> PackInfo:
    """A plain game directory (server folder, Prism/MultiMC .minecraft, CurseForge app instance)."""
    root = ""
    for candidate in (".minecraft", "minecraft"):
        if any(n.startswith(candidate + "/mods/") for n in src.names()):
            root = candidate
    info = PackInfo(
        format="instance", name="", version="", minecraft=None, loader=None, loader_version=None, override_roots=[root]
    )
    if src.exists("minecraftinstance.json"):
        try:
            mi = json.loads(src.read_text("minecraftinstance.json"))
            info.name = str(mi.get("name", ""))
            info.minecraft = mi.get("gameVersion")
            ld = (mi.get("baseModLoader") or {}).get("name", "")
            loader, _, lver = ld.partition("-")
            info.loader, info.loader_version = loader or None, lver or None
        except ValueError as e:
            info.problems.append(f"minecraftinstance.json: {e}")
    if src.exists("mmc-pack.json"):
        try:
            for c in json.loads(src.read_text("mmc-pack.json")).get("components", []):
                uid, ver = c.get("uid"), c.get("version")
                if uid == "net.minecraft":
                    info.minecraft = ver
                elif uid in (
                    "net.fabricmc.fabric-loader",
                    "net.minecraftforge",
                    "net.neoforged",
                    "org.quiltmc.quilt-loader",
                ):
                    info.loader = {
                        "net.fabricmc.fabric-loader": "fabric",
                        "net.minecraftforge": "forge",
                        "net.neoforged": "neoforge",
                        "org.quiltmc.quilt-loader": "quilt",
                    }[uid]
                    info.loader_version = ver
        except ValueError as e:
            info.problems.append(f"mmc-pack.json: {e}")
    _scan_embedded_jars(src, info, [root])
    if info.loader is None:  # infer from jar metadata when no instance manifest exists
        loaders = {m.loader for e in info.entries for m in e.jar_mods}
        for jm in (m for e in info.entries for m in e.jar_mods):
            if jm.mod_id == "minecraft":
                info.minecraft = info.minecraft or jm.version
        if len(loaders) == 1:
            info.loader = loaders.pop()
            info.problems.append(f"loader inferred from jar metadata ({info.loader}); verify it")
    return info


def parse_pack(src: PackSource) -> PackInfo:
    fmt = detect_format(src)
    return {
        "mrpack": parse_mrpack,
        "curseforge": parse_curseforge,
        "packwiz": parse_packwiz,
        "instance": parse_instance,
    }[fmt](src)
