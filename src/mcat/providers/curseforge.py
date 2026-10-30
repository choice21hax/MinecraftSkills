"""CurseForge official API adapter (https://docs.curseforge.com/rest-api/).

Requires an API key in the OS keyring (``mcat auth set curseforge``). Without one, the provider is
reported as unavailable and MCAT keeps working in Modrinth-only mode. Files whose authors disabled
third-party distribution are never downloaded; they are surfaced as manual-download items.
"""

from __future__ import annotations

from typing import Any

from mcat.models import Dependency, DepKind, FileRef, ModVersion, ProjectHit
from mcat.providers.http import HttpClient, NetworkError
from mcat.security.credentials import get_secret

API = "https://api.curseforge.com"
GAME_ID = 432
CLASS_MODS = 6
CLASS_MODPACKS = 4471
LOADER_TYPES = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
_LOADER_NAMES = {"Forge": "forge", "Fabric": "fabric", "Quilt": "quilt", "NeoForge": "neoforge"}
_RELEASE = {1: "release", 2: "beta", 3: "alpha"}
_RELATION = {
    1: DepKind.EMBEDDED,
    2: DepKind.OPTIONAL,
    3: DepKind.REQUIRED,
    5: DepKind.INCOMPATIBLE,
    6: DepKind.EMBEDDED,  # "Include"
}  # 4 = Tool: not a runtime dependency


class CurseForgeUnavailable(Exception):
    """No API key configured; callers should degrade to Modrinth-only mode."""


def normalize_file(f: dict[str, Any], mod: dict[str, Any] | None = None) -> ModVersion:
    mod = mod or {}
    tags = [str(g) for g in f.get("gameVersions", [])]
    loaders = sorted({_LOADER_NAMES[t] for t in tags if t in _LOADER_NAMES})
    game_versions = [t for t in tags if t[:1].isdigit()]
    has_client, has_server = "Client" in tags, "Server" in tags
    client_side = server_side = "unknown"
    if has_client or has_server:  # CurseForge only sometimes tags environments
        client_side = "required" if has_client else "unsupported"
        server_side = "required" if has_server else "unsupported"
    hashes = {}
    for h in f.get("hashes", []):
        algo = {1: "sha1", 2: "md5"}.get(h.get("algo"))
        if algo:
            hashes[algo] = str(h["value"])
    hashes["cf_fingerprint"] = str(f.get("fileFingerprint", ""))
    allow = mod.get("allowModDistribution")
    download_allowed = bool(f.get("downloadUrl")) and allow is not False
    deps = [
        Dependency(kind=_RELATION[d["relationType"]], project_id=str(d["modId"]))
        for d in f.get("dependencies", [])
        if d.get("relationType") in _RELATION
    ]
    return ModVersion(
        provider="curseforge",
        project_id=str(f["modId"]),
        version_id=str(f["id"]),
        slug=str(mod.get("slug", f["modId"])),
        name=str(mod.get("name", f.get("displayName", ""))),
        version_number=str(f.get("displayName", f.get("fileName", ""))),
        game_versions=game_versions,
        loaders=loaders,
        channel=_RELEASE.get(int(f.get("releaseType", 1)), "release"),
        published=str(f.get("fileDate", "")),
        files=[
            FileRef(
                filename=str(f["fileName"]),
                url=f.get("downloadUrl") if download_allowed else None,
                hashes=hashes,
                size=f.get("fileLength"),
                download_allowed=download_allowed,
            )
        ],
        dependencies=deps,
        client_side=client_side,
        server_side=server_side,
        license=None,  # CurseForge exposes no SPDX license field
    )


class CurseForgeProvider:
    name = "curseforge"

    def __init__(self, http: HttpClient | None = None, api_key: str | None = None):
        if api_key is None:
            api_key, _ = get_secret("curseforge")
        if not api_key:
            raise CurseForgeUnavailable(
                "CurseForge API key not configured (run `mcat auth set curseforge`); using Modrinth only"
            )
        self.http = http or HttpClient()
        self._cred = ("curseforge", "x-api-key", api_key)
        self._mods: dict[str, dict[str, Any]] = {}

    def _get(self, path: str, params: dict[str, Any] | None = None, ttl: float = 3600) -> Any:
        return self.http.get_json(f"{API}{path}", params=params, credential=self._cred, ttl=ttl)

    def test(self) -> bool:
        data = self._get(f"/v1/games/{GAME_ID}", ttl=0)
        return data.get("data", {}).get("id") == GAME_ID

    def search(
        self,
        query: str,
        *,
        minecraft: str | None = None,
        loader: str | None = None,
        project_type: str = "mod",
        limit: int = 10,
    ) -> list[ProjectHit]:
        params: dict[str, Any] = {
            "gameId": GAME_ID,
            "classId": CLASS_MODPACKS if project_type == "modpack" else CLASS_MODS,
            "searchFilter": query,
            "pageSize": min(max(limit, 1), 50),
            "sortField": 2,  # popularity
            "sortOrder": "desc",
        }
        if minecraft:
            params["gameVersion"] = minecraft
        if loader:
            params["modLoaderType"] = LOADER_TYPES[loader]
        data = self._get("/v1/mods/search", params, ttl=900)
        hits = []
        for m in data.get("data", []):
            self._mods[str(m["id"])] = m
            idx = m.get("latestFilesIndexes", [])
            hits.append(
                ProjectHit(
                    provider="curseforge",
                    project_id=str(m["id"]),
                    slug=m.get("slug", ""),
                    title=m.get("name", ""),
                    description=m.get("summary", ""),
                    project_type=project_type,
                    loaders=sorted({k for k, v in LOADER_TYPES.items() for i in idx if i.get("modLoader") == v}),
                    game_versions=sorted({i["gameVersion"] for i in idx if "gameVersion" in i}),
                    downloads=int(m.get("downloadCount", 0)),
                    url=(m.get("links") or {}).get("websiteUrl"),
                )
            )
        return hits

    def mod(self, mod_id: str) -> dict[str, Any]:
        if mod_id not in self._mods:
            self._mods[mod_id] = self._get(f"/v1/mods/{mod_id}")["data"]
        return self._mods[mod_id]

    def versions(self, project: str, *, minecraft: str | None = None, loader: str | None = None) -> list[ModVersion]:
        params: dict[str, Any] = {"pageSize": 50}
        if minecraft:
            params["gameVersion"] = minecraft
        if loader:
            params["modLoaderType"] = LOADER_TYPES[loader]
        mod = self.mod(project)
        data = self._get(f"/v1/mods/{project}/files", params, ttl=1800)
        return [normalize_file(f, mod) for f in data.get("data", [])]

    def version(self, file_id: str, project: str) -> ModVersion:
        f = self._get(f"/v1/mods/{project}/files/{file_id}", ttl=86400)["data"]
        return normalize_file(f, self.mod(project))

    def download_url(self, project: str, file_id: str) -> str | None:
        """Official download URL, or None when the author disallows third-party distribution."""
        try:
            return self._get(f"/v1/mods/{project}/files/{file_id}/download-url", ttl=3600).get("data")
        except NetworkError:
            return None
