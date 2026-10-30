"""Semantic migration adapters: one per kind of pack content.

Each adapter states what it transforms automatically (with the documented rule and source), what it only
preserves, and what it cannot do. The plan lists the adapters that apply to a pack and the rules active
for its source -> target versions, so a reviewer sees the exact scope before anything is written.
Adapters never invent identifiers: renames come from `rules.py` (cited changelogs) or the user's id_map.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcat.migration import rules


@dataclass(frozen=True)
class Adapter:
    name: str
    categories: tuple[str, ...]
    action: str  # transform | copy
    state: str  # best state the adapter can reach without a runtime test: proposed | unverified
    preserves: str
    cannot: tuple[str, ...]
    rules: Callable[[str | None, str, dict[str, Any]], list[str]] = field(default=lambda s, t, f: [])

    def describe(self, source_mc: str | None, target_mc: str, flags: dict[str, Any]) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "categories": list(self.categories),
            "action": self.action,
            "best_state": self.state,
            "active_rules": self.rules(source_mc, target_mc, flags),
            "preserves": self.preserves,
            "cannot": list(self.cannot),
        }


def _id_rules(src: str | None, tgt: str, flags: dict[str, Any]) -> list[str]:
    out = ["user id_map (only mappings you approve)"]
    out += [f"{r.old} -> {r.new} ({r.source})" for r in rules.applicable_renames(src, tgt)]
    return out


def _datapack_rules(src: str | None, tgt: str, flags: dict[str, Any]) -> list[str]:
    out = _id_rules(src, tgt, flags)
    if flags.get("datapack_folder_singular"):
        out += [f"folder {r.old} -> {r.new} ({r.source})" for r in rules.DATAPACK_FOLDER_RENAMES]
    return out


def _recipe_rules(src: str | None, tgt: str, flags: dict[str, Any]) -> list[str]:
    out = _datapack_rules(src, tgt, flags)
    if flags.get("recipe_result_id"):
        out.append('vanilla recipe result {"item": x} -> {"id": x} (1.20.5, 24w10a item components)')
    if flags.get("recipe_ingredient_strings"):
        out.append('vanilla ingredients {"item": x} -> "x", {"tag": t} -> "#t" (1.21.2, 24w33a)')
    return out


def _kubejs_rules(src: str | None, tgt: str, flags: dict[str, Any]) -> list[str]:
    out = _id_rules(src, tgt, flags)
    if flags.get("kubejs_legacy_events"):
        out.append("legacy onEvent('x', ...) heads -> KubeJS 6+ event groups (when unambiguous)")
    return out


ADAPTERS: tuple[Adapter, ...] = (
    Adapter(
        "ftb-quests",
        ("ftbquests",),
        "transform",
        "unverified",
        "quest/chapter ids, dependency graph (checked identical after transform), rewards, text",
        ("task or reward types of mods that are blocked", "progression balance: needs a play-test"),
        _id_rules,
    ),
    Adapter(
        "kubejs",
        ("kubejs_script",),
        "transform",
        "unverified",
        "script logic byte-for-byte except mapped identifiers and upgraded event heads",
        ("KubeJS API changes beyond event heads", "addon APIs (kubejs-create etc.) between major versions"),
        _kubejs_rules,
    ),
    Adapter(
        "datapack-schema",
        ("tag", "loot_table", "advancement", "datapack", "kubejs_data"),
        "transform",
        "proposed",
        "file contents except mapped identifiers; registry folders renamed across 1.21",
        ("component/predicate syntax changes not listed in rules.py",),
        _datapack_rules,
    ),
    Adapter(
        "recipes",
        ("recipe",),
        "transform",
        "proposed",
        "modded recipe types copied with identifier mapping only",
        ("modded recipe serializers that changed format (e.g. Create processing recipes across majors)",),
        _recipe_rules,
    ),
    Adapter(
        "worldgen",
        ("worldgen",),
        "transform",
        "unverified",
        "structure/feature/biome JSON with identifier mapping only",
        ("worldgen codec changes between versions: must be tested in a fresh world",),
        _datapack_rules,
    ),
    Adapter(
        "config-keys",
        ("config", "defaultconfigs"),
        "copy",
        "unverified",
        "every file verbatim (no option is renamed without an upstream source)",
        ("renamed or removed options in upgraded mod versions: mods usually regenerate defaults",),
    ),
    Adapter(
        "resource-packs",
        ("resourcepack", "kubejs_assets", "shaderpack"),
        "copy",
        "unverified",
        "textures, models, lang files verbatim",
        ("pack.mcmeta pack_format is not rewritten", "model/texture paths renamed by mod updates"),
    ),
)
BY_CATEGORY = {c: a for a in ADAPTERS for c in a.categories}


def for_category(category: str) -> Adapter | None:
    return BY_CATEGORY.get(category)
