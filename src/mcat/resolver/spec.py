"""Pack spec (YAML) loading and validation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from mcat.models import LOADERS


class SpecError(ValueError):
    pass


@dataclass
class ModRequest:
    id: str
    provider: str = "modrinth"
    version: str | None = None  # version_number or provider version id
    side: str | None = None
    optional: bool = False


@dataclass
class PackSpec:
    name: str
    version: str
    minecraft: str
    loader: str
    loader_version: str = "latest"
    channels: list[str] = field(default_factory=lambda: ["release"])
    include_optional: bool = False
    mods: list[ModRequest] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    digest: str = ""


def load_spec(path: Path) -> PackSpec:
    raw_bytes = path.read_bytes()
    try:
        raw = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as e:
        raise SpecError(f"invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise SpecError("spec must be a mapping")
    return parse_spec(raw, hashlib.sha256(raw_bytes).hexdigest())


def parse_spec(raw: dict, digest: str = "") -> PackSpec:
    for key in ("minecraft", "loader"):
        if key not in raw:
            raise SpecError(f"missing required key {key!r}")
    mc = raw["minecraft"]
    if not isinstance(mc, str):
        # YAML turns 1.20 into a float and silently loses information (1.20 -> 1.2)
        raise SpecError(f"minecraft version must be a quoted string, got {type(mc).__name__} {mc!r}")
    if raw["loader"] not in LOADERS:
        raise SpecError(f"loader must be one of {', '.join(LOADERS)}")
    channels = [str(c) for c in raw.get("channels", ["release"])]
    bad = set(channels) - {"release", "beta", "alpha"}
    if bad:
        raise SpecError(f"unknown channels {sorted(bad)}")
    mods = []
    for m in raw.get("mods", []) or []:
        if isinstance(m, str):
            mods.append(ModRequest(id=m))
        elif isinstance(m, dict) and "id" in m:
            prov = str(m.get("provider", "modrinth"))
            if prov not in ("modrinth", "curseforge"):
                raise SpecError(f"unknown provider {prov!r} for {m['id']}")
            side = m.get("side")
            if side not in (None, "client", "server", "both"):
                raise SpecError(f"invalid side {side!r} for {m['id']}")
            ver = m.get("version")
            if ver is not None and not isinstance(ver, str):
                raise SpecError(f"version for {m['id']} must be a quoted string")
            mods.append(
                ModRequest(
                    id=str(m["id"]), provider=prov, version=ver, side=side, optional=bool(m.get("optional", False))
                )
            )
        else:
            raise SpecError(f"invalid mod entry {m!r}")
    lv = raw.get("loader_version", "latest")
    if not isinstance(lv, str):
        raise SpecError("loader_version must be a quoted string")
    return PackSpec(
        name=str(raw.get("name", "Unnamed Pack")),
        version=str(raw.get("version", "1.0.0")),
        minecraft=mc,
        loader=raw["loader"],
        loader_version=lv,
        channels=channels,
        include_optional=bool(raw.get("include_optional", False)),
        mods=mods,
        exclude=[str(x) for x in raw.get("exclude", []) or []],
        digest=digest,
    )
