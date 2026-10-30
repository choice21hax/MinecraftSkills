"""Modrinth API v2 adapter (https://docs.modrinth.com/api/). Public reads need no token."""

from __future__ import annotations

import json
from typing import Any

from mcat.models import Dependency, DepKind, FileRef, ModVersion, ProjectHit
from mcat.providers.http import HttpClient
from mcat.security.credentials import get_secret

API = "https://api.modrinth.com/v2"


def normalize_version(v: dict[str, Any], project: dict[str, Any] | None = None) -> ModVersion:
    files = sorted(v.get("files", []), key=lambda f: not f.get("primary", False))
    deps = []
    for d in v.get("dependencies", []):
        try:
            kind = DepKind(d.get("dependency_type", "required"))
        except ValueError:
            continue  # unknown dependency type: skipped but the raw data stays in provider cache
        deps.append(Dependency(kind=kind, project_id=d.get("project_id"), version_id=d.get("version_id")))
    project = project or {}
    lic = project.get("license")
    return ModVersion(
        provider="modrinth",
        project_id=str(v["project_id"]),
        version_id=str(v["id"]),
        slug=str(project.get("slug", v["project_id"])),
        name=str(project.get("title", v.get("name", ""))),
        version_number=str(v.get("version_number", "")),
        game_versions=[str(g) for g in v.get("game_versions", [])],
        loaders=[str(x) for x in v.get("loaders", [])],
        channel=str(v.get("version_type", "release")),
        published=str(v.get("date_published", "")),
        files=[
            FileRef(
                filename=str(f["filename"]),
                url=f.get("url"),
                hashes={k: str(h) for k, h in (f.get("hashes") or {}).items()},
                size=f.get("size"),
            )
            for f in files
        ],
        dependencies=deps,
        client_side=str(project.get("client_side", "unknown")),
        server_side=str(project.get("server_side", "unknown")),
        license=lic.get("id") if isinstance(lic, dict) else lic,
    )


class ModrinthProvider:
    name = "modrinth"

    def __init__(self, http: HttpClient | None = None, *, use_token: bool = False):
        self.http = http or HttpClient()
        self._credential: tuple[str, str, str] | None = None
        if use_token:
            token, _ = get_secret("modrinth")
            if token:
                self._credential = ("modrinth", "Authorization", token)
        self._projects: dict[str, dict[str, Any]] = {}

    def _get(self, path: str, params: dict[str, Any] | None = None, ttl: float = 3600) -> Any:
        return self.http.get_json(f"{API}{path}", params=params, credential=self._credential, ttl=ttl)

    def search(
        self,
        query: str,
        *,
        minecraft: str | None = None,
        loader: str | None = None,
        project_type: str = "mod",
        limit: int = 10,
    ) -> list[ProjectHit]:
        facets: list[list[str]] = [[f"project_type:{project_type}"]]
        if loader:
            facets.append([f"categories:{loader}"])
        if minecraft:
            facets.append([f"versions:{minecraft}"])
        data = self._get(
            "/search",
            {"query": query, "limit": min(max(limit, 1), 100), "facets": json.dumps(facets)},
            ttl=900,
        )
        return [
            ProjectHit(
                provider="modrinth",
                project_id=h["project_id"],
                slug=h["slug"],
                title=h["title"],
                description=h.get("description", ""),
                project_type=h.get("project_type", project_type),
                loaders=[c for c in h.get("categories", []) if c in ("fabric", "forge", "neoforge", "quilt")],
                game_versions=list(h.get("versions", [])),
                downloads=int(h.get("downloads", 0)),
                client_side=h.get("client_side", "unknown"),
                server_side=h.get("server_side", "unknown"),
                license=h.get("license"),
                url=f"https://modrinth.com/{h.get('project_type', 'mod')}/{h['slug']}",
            )
            for h in data.get("hits", [])
        ]

    def project(self, id_or_slug: str) -> dict[str, Any]:
        if id_or_slug not in self._projects:
            p = self._get(f"/project/{id_or_slug}")
            self._projects[p["id"]] = p
            self._projects[p["slug"]] = p
            self._projects[id_or_slug] = p
        return self._projects[id_or_slug]

    def versions(self, project: str, *, minecraft: str | None = None, loader: str | None = None) -> list[ModVersion]:
        params: dict[str, Any] = {}
        if loader:
            params["loaders"] = json.dumps([loader])
        if minecraft:
            params["game_versions"] = json.dumps([minecraft])
        p = self.project(project)
        raw = self._get(f"/project/{p['id']}/version", params, ttl=1800)
        return [normalize_version(v, p) for v in raw]

    def version(self, version_id: str) -> ModVersion:
        v = self._get(f"/version/{version_id}", ttl=86400)  # versions are immutable
        return normalize_version(v, self.project(v["project_id"]))

    def versions_by_hash(self, sha1_hashes: list[str]) -> dict[str, ModVersion]:
        if not sha1_hashes:
            return {}
        raw = self.http.get_json(
            f"{API}/version_files",
            method="POST",
            body={"hashes": sorted(set(sha1_hashes)), "algorithm": "sha1"},
            credential=self._credential,
            ttl=86400,
        )
        return {h: normalize_version(v, self.project(v["project_id"])) for h, v in raw.items()}

    def game_versions(self) -> list[dict[str, Any]]:
        return list(self._get("/tag/game_version", ttl=86400))
