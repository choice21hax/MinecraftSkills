"""In-memory provider/catalog fakes for offline tests."""

from __future__ import annotations

from mcat.loaders import LoaderSupport
from mcat.models import Dependency, DepKind, FileRef, ModVersion


def mv(
    project: str,
    vid: str,
    game: list[str],
    loaders: list[str],
    *,
    deps: list[tuple[str, str]] | None = None,
    channel: str = "release",
    published: str = "2026-01-01",
    slug: str | None = None,
    sha1: str | None = None,
    client: str = "required",
    server: str = "required",
) -> ModVersion:
    return ModVersion(
        provider="modrinth",
        project_id=project,
        version_id=vid,
        slug=slug or project,
        name=(slug or project).title(),
        version_number=vid,
        game_versions=game,
        loaders=loaders,
        channel=channel,
        published=published,
        files=[
            FileRef(
                f"{project}-{vid}.jar",
                f"https://cdn.modrinth.com/data/{project}/versions/{vid}/f.jar",
                {"sha1": sha1 or (vid * 40)[:40], "sha512": (vid * 128)[:128]},
                10,
            )
        ],
        dependencies=[Dependency(DepKind(k), project_id=p) for k, p in (deps or [])],
        client_side=client,
        server_side=server,
        license="MIT",
    )


class FakeModrinth:
    def __init__(self, versions: list[ModVersion]):
        self.by_project: dict[str, list[ModVersion]] = {}
        for v in versions:
            self.by_project.setdefault(v.project_id, []).append(v)
            self.by_project.setdefault(v.slug, self.by_project[v.project_id])

    def versions(self, project: str, **_: object) -> list[ModVersion]:
        return list(self.by_project.get(project, []))

    def version(self, vid: str) -> ModVersion:
        for vs in self.by_project.values():
            for v in vs:
                if v.version_id == vid:
                    return v
        raise KeyError(vid)

    def versions_by_hash(self, hashes: list[str]) -> dict[str, ModVersion]:
        out = {}
        for vs in self.by_project.values():
            for v in vs:
                for f in v.files:
                    if f.hashes.get("sha1") in hashes:
                        out[f.hashes["sha1"]] = v
        return out


class FakeCatalog:
    def __init__(self, supported: dict[tuple[str, str], str]):
        self.supported = supported

    def support(self, loader: str, minecraft: str) -> LoaderSupport:
        lv = self.supported.get((loader, minecraft))
        return LoaderSupport(
            loader, minecraft, lv is not None, [lv] if lv else [], lv, java=25 if lv else None, evidence="fake catalog"
        )
