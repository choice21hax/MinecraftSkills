"""Read loader metadata from mod JARs without executing anything.

Supports fabric.mod.json, quilt.mod.json, META-INF/mods.toml (Forge) and META-INF/neoforge.mods.toml.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from typing import Any

from mcat.security.archive import ArchiveLimits, SafeZip, UnsafeArchiveError

_JAR_LIMITS = ArchiveLimits(max_entries=100_000, max_read_bytes=4 * 1024**2)


@dataclass
class JarDep:
    mod_id: str
    kind: str  # required | optional | incompatible
    constraint: str


@dataclass
class JarMod:
    loader: str  # fabric | quilt | forge | neoforge
    mod_id: str
    version: str
    name: str = ""
    environment: str = "both"  # client | server | both
    dependencies: list[JarDep] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    mixins: list[str] = field(default_factory=list)
    nested_jars: list[str] = field(default_factory=list)
    license: str | None = None


def _as_constraint(v: Any) -> str:
    if isinstance(v, list):
        return " || ".join(str(x) for x in v)
    return str(v)


def parse_fabric_mod_json(raw: bytes) -> JarMod:
    # fabric.mod.json allows raw control characters inside strings in practice
    data = json.loads(raw.decode("utf-8-sig", errors="replace"), strict=False)
    env = {"*": "both", "client": "client", "server": "server"}.get(str(data.get("environment", "*")), "both")
    deps = []
    for key, kind in (
        ("depends", "required"),
        ("recommends", "optional"),
        ("suggests", "optional"),
        ("breaks", "incompatible"),
        ("conflicts", "incompatible"),
    ):
        for mid, c in (data.get(key) or {}).items():
            deps.append(JarDep(str(mid), kind, _as_constraint(c)))
    mixins = []
    for m in data.get("mixins", []) or []:
        mixins.append(m if isinstance(m, str) else str(m.get("config", "")))
    lic = data.get("license")
    return JarMod(
        loader="fabric",
        mod_id=str(data.get("id", "")),
        version=str(data.get("version", "")),
        name=str(data.get("name", "")),
        environment=env,
        dependencies=deps,
        provides=[str(p) for p in data.get("provides", []) or []],
        mixins=mixins,
        nested_jars=[str(j.get("file")) for j in data.get("jars", []) or [] if isinstance(j, dict)],
        license=", ".join(lic) if isinstance(lic, list) else (str(lic) if lic else None),
    )


def parse_quilt_mod_json(raw: bytes) -> JarMod:
    data = json.loads(raw.decode("utf-8-sig", errors="replace"), strict=False)
    ql = data.get("quilt_loader", {})
    deps = []
    for d in ql.get("depends", []) or []:
        if isinstance(d, str):
            deps.append(JarDep(d, "required", "*"))
        elif isinstance(d, dict):
            deps.append(
                JarDep(
                    str(d.get("id")),
                    "optional" if d.get("optional") else "required",
                    _as_constraint(d.get("versions", "*")),
                )
            )
    for d in ql.get("breaks", []) or []:
        mid = d if isinstance(d, str) else d.get("id")
        deps.append(JarDep(str(mid), "incompatible", "*"))
    env = (data.get("minecraft") or {}).get("environment", "*")
    return JarMod(
        loader="quilt",
        mod_id=str(ql.get("id", "")),
        version=str(ql.get("version", "")),
        name=str((ql.get("metadata") or {}).get("name", "")),
        environment={"*": "both", "client": "client", "dedicated_server": "server"}.get(env, "both"),
        dependencies=deps,
    )


def parse_mods_toml(raw: bytes, loader: str) -> list[JarMod]:
    data = tomllib.loads(raw.decode("utf-8-sig", errors="replace"))
    mods = []
    for m in data.get("mods", []) or []:
        mid = str(m.get("modId", ""))
        deps = []
        for d in (data.get("dependencies") or {}).get(mid, []) or []:
            if "type" in d:  # NeoForge: required|optional|incompatible|discouraged
                kind = {
                    "required": "required",
                    "optional": "optional",
                    "incompatible": "incompatible",
                    "discouraged": "optional",
                }.get(str(d["type"]).lower(), "optional")
            else:  # Forge: mandatory=true/false
                kind = "required" if d.get("mandatory", False) else "optional"
            deps.append(JarDep(str(d.get("modId", "")), kind, str(d.get("versionRange", "*"))))
        sides = {str(d.get("side", "BOTH")).upper() for d in (data.get("dependencies") or {}).get(mid, [])}
        env = "client" if sides == {"CLIENT"} else "both"
        mods.append(
            JarMod(
                loader=loader,
                mod_id=mid,
                version=str(m.get("version", "")),
                name=str(m.get("displayName", "")),
                environment=env,
                dependencies=deps,
                mixins=[str(x.get("config")) for x in data.get("mixins", []) or [] if isinstance(x, dict)],
                license=str(data.get("license")) if data.get("license") else None,
            )
        )
    return mods


def _manifest_version(zf: SafeZip) -> str | None:
    try:
        text = zf.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
    except (KeyError, UnsafeArchiveError):
        return None
    for line in text.splitlines():
        if line.startswith("Implementation-Version:"):
            return line.split(":", 1)[1].strip()
    return None


def read_jar(data: bytes) -> tuple[list[JarMod], list[str]]:
    """Return (mods, problems). Never raises for malformed metadata; reports it instead."""
    problems: list[str] = []
    try:
        zf = SafeZip(data, _JAR_LIMITS)
    except UnsafeArchiveError as e:
        return [], [f"unreadable jar: {e}"]
    mods: list[JarMod] = []
    try:
        names = set(zf.members)
        if "fabric.mod.json" in names:
            try:
                mods.append(parse_fabric_mod_json(zf.read("fabric.mod.json")))
            except (ValueError, UnsafeArchiveError) as e:
                problems.append(f"invalid fabric.mod.json: {e}")
        if "quilt.mod.json" in names:
            try:
                mods.append(parse_quilt_mod_json(zf.read("quilt.mod.json")))
            except (ValueError, UnsafeArchiveError) as e:
                problems.append(f"invalid quilt.mod.json: {e}")
        for meta, loader in (("META-INF/neoforge.mods.toml", "neoforge"), ("META-INF/mods.toml", "forge")):
            if meta in names:
                try:
                    found = parse_mods_toml(zf.read(meta), loader)
                except (tomllib.TOMLDecodeError, UnsafeArchiveError) as e:
                    problems.append(f"invalid {meta}: {e}")
                    continue
                impl = _manifest_version(zf)
                for m in found:
                    if m.version == "${file.jarVersion}" and impl:
                        m.version = impl
                mods.extend(found)
    finally:
        zf.close()
    return mods, problems
