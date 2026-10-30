"""Provider-neutral data models. Version identifiers are always strings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

LOADERS = ("fabric", "neoforge", "forge")


class Side(StrEnum):
    CLIENT = "client"
    SERVER = "server"
    BOTH = "both"
    UNKNOWN = "unknown"


class DepKind(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    INCOMPATIBLE = "incompatible"
    EMBEDDED = "embedded"


@dataclass
class Dependency:
    kind: DepKind
    project_id: str | None = None  # provider project id
    version_id: str | None = None  # pinned provider version id, if the metadata pins one
    mod_id: str | None = None  # loader mod id (from jar metadata), when known
    constraint: str | None = None  # loader-native version range, when known


@dataclass
class FileRef:
    filename: str
    url: str | None
    hashes: dict[str, str] = field(default_factory=dict)  # algo -> hex
    size: int | None = None
    download_allowed: bool = True  # CurseForge may forbid third-party distribution


@dataclass
class ModVersion:
    """One immutable provider version of a project."""

    provider: str  # "modrinth" | "curseforge"
    project_id: str
    version_id: str
    slug: str
    name: str
    version_number: str
    game_versions: list[str]
    loaders: list[str]
    channel: str  # release | beta | alpha
    published: str
    files: list[FileRef]
    dependencies: list[Dependency] = field(default_factory=list)
    client_side: str = "unknown"  # required | optional | unsupported | unknown
    server_side: str = "unknown"
    license: str | None = None

    @property
    def primary_file(self) -> FileRef | None:
        return self.files[0] if self.files else None

    @property
    def side(self) -> Side:
        c, s = self.client_side, self.server_side
        if c == "unsupported" and s != "unsupported":
            return Side.SERVER
        if s == "unsupported" and c != "unsupported":
            return Side.CLIENT
        if "unknown" in (c, s):
            return Side.UNKNOWN
        return Side.BOTH

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d


@dataclass
class ProjectHit:
    provider: str
    project_id: str
    slug: str
    title: str
    description: str
    project_type: str
    loaders: list[str]
    game_versions: list[str]
    downloads: int
    client_side: str = "unknown"
    server_side: str = "unknown"
    license: str | None = None
    url: str | None = None


@dataclass
class Target:
    minecraft: str
    loader: str
    loader_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.minecraft, str):
            raise TypeError("minecraft version must be a string, never a number")
        if self.loader not in LOADERS:
            raise ValueError(f"unsupported loader {self.loader!r}; expected one of {', '.join(LOADERS)}")
