"""Loader compatibility from authoritative upstream metadata (never from assumptions).

Sources:
* Mojang version manifest  - game versions and required Java major version
* meta.fabricmc.net        - Fabric game/loader support
* maven.neoforged.net      - NeoForge versions (version scheme maps to the game version)
* files.minecraftforge.net - Forge promotions + maven metadata
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from mcat.providers.http import HttpClient

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META = "https://meta.fabricmc.net/v2"
NEOFORGE_VERSIONS = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN_META = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"


@dataclass
class LoaderSupport:
    loader: str
    minecraft: str
    supported: bool
    loader_versions: list[str] = field(default_factory=list)  # newest first
    recommended: str | None = None
    java: int | None = None
    evidence: str = ""


def _vkey(v: str) -> tuple:
    """Sort key for dotted numeric versions with optional suffixes. Strings in, never floats."""
    parts = re.split(r"[.\-+]", v)
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts)


def neoforge_to_minecraft(nf_version: str) -> str | None:
    """Map a NeoForge version to its game version using NeoForge's documented scheme.

    1.20.2-1.21.x era: ``<mc minor>.<mc patch>.<build>``, e.g. 21.1.77 -> 1.21.1, 21.0.5 -> 1.21.
    Year-based era (26.x+): ``<mc major>.<mc minor>.<mc patch>.<build>``, e.g. 26.2.0.93 -> 26.2.
    """
    base = nf_version.split("-", 1)[0]
    parts = base.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    if int(parts[0]) >= 26 and len(parts) == 4:
        mc = parts[:3]
        return ".".join(mc[:2]) if mc[2] == "0" else ".".join(mc)
    if len(parts) == 3:
        return f"1.{parts[0]}" if parts[1] == "0" else f"1.{parts[0]}.{parts[1]}"
    return None


class LoaderCatalog:
    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient()

    def game_versions(self, releases_only: bool = True) -> list[str]:
        m = self.http.get_json(MOJANG_MANIFEST, ttl=6 * 3600)
        return [v["id"] for v in m["versions"] if not releases_only or v["type"] == "release"]

    def java_for(self, minecraft: str) -> int | None:
        m = self.http.get_json(MOJANG_MANIFEST, ttl=6 * 3600)
        entry = next((v for v in m["versions"] if v["id"] == minecraft), None)
        if entry is None:
            return None
        detail = self.http.get_json(entry["url"], ttl=7 * 86400)
        jv = detail.get("javaVersion") or {}
        return int(jv["majorVersion"]) if "majorVersion" in jv else None

    def support(self, loader: str, minecraft: str) -> LoaderSupport:
        fn = {"fabric": self._fabric, "neoforge": self._neoforge, "forge": self._forge}.get(loader)
        if fn is None:
            return LoaderSupport(loader, minecraft, False, evidence=f"unknown loader {loader!r}")
        s = fn(minecraft)
        if s.supported:
            s.java = self.java_for(minecraft)
        return s

    def _fabric(self, mc: str) -> LoaderSupport:
        games = {g["version"] for g in self.http.get_json(f"{FABRIC_META}/versions/game", ttl=3600)}
        if mc not in games:
            return LoaderSupport("fabric", mc, False, evidence="meta.fabricmc.net/v2/versions/game lacks it")
        rows = self.http.get_json(f"{FABRIC_META}/versions/loader/{mc}", ttl=3600)
        versions = [r["loader"]["version"] for r in rows]
        stable = [r["loader"]["version"] for r in rows if r["loader"].get("stable")]
        return LoaderSupport(
            "fabric",
            mc,
            bool(versions),
            versions,
            stable[0] if stable else None,
            evidence=f"meta.fabricmc.net/v2/versions/loader/{mc}",
        )

    def _neoforge(self, mc: str) -> LoaderSupport:
        data = self.http.get_json(NEOFORGE_VERSIONS, ttl=3600)
        versions = [v for v in data.get("versions", []) if neoforge_to_minecraft(v) == mc]
        versions.sort(key=_vkey, reverse=True)
        stable = [v for v in versions if "beta" not in v and "alpha" not in v]
        return LoaderSupport(
            "neoforge",
            mc,
            bool(versions),
            versions,
            stable[0] if stable else None,
            evidence="maven.neoforged.net releases (version scheme mapping)",
        )

    def _forge(self, mc: str) -> LoaderSupport:
        promos = self.http.get_json(FORGE_PROMOS, ttl=3600).get("promos", {})
        xml = self.http._request("GET", FORGE_MAVEN_META, credential=None).text
        root = ET.fromstring(xml)  # noqa: S314 - allow-listed host, small document
        all_versions = [v.text or "" for v in root.iter("version")]
        versions = [v.split("-", 1)[1] for v in all_versions if v.startswith(f"{mc}-")]
        versions.sort(key=_vkey, reverse=True)
        rec = promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")
        return LoaderSupport(
            "forge",
            mc,
            bool(versions),
            versions,
            rec,
            evidence="maven.minecraftforge.net maven-metadata + promotions_slim.json",
        )
