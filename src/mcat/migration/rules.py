"""Versioned migration knowledge. Every entry names the version boundary and its source.

Only changes documented in official changelogs / mod migration guides belong here. Anything inferred
must go through the plan's user-reviewed ``id_map`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcat.resolver.versions import Version


@dataclass(frozen=True)
class IdRename:
    old: str
    new: str
    since: str  # first game version using the new id
    source: str


VANILLA_RENAMES = [
    IdRename("minecraft:grass", "minecraft:short_grass", "1.20.3", "Minecraft 1.20.3 changelog (23w40a)"),
    IdRename("minecraft:scute", "minecraft:turtle_scute", "1.20.5", "Minecraft 1.20.5 changelog (24w03a)"),
    IdRename("minecraft:chain", "minecraft:iron_chain", "1.21.9", "Minecraft 1.21.9 changelog (copper chains)"),
]


@dataclass(frozen=True)
class FolderRename:
    old: str
    new: str
    since: str
    source: str


# Datapack registry folders became singular in 1.21 (24w21a).
DATAPACK_FOLDER_RENAMES = [
    FolderRename(old, new, "1.21", "Minecraft 1.21 changelog (24w21a): singular registry folder names")
    for old, new in [
        ("recipes", "recipe"),
        ("loot_tables", "loot_table"),
        ("advancements", "advancement"),
        ("functions", "function"),
        ("predicates", "predicate"),
        ("item_modifiers", "item_modifier"),
        ("structures", "structure"),
        ("tags/items", "tags/item"),
        ("tags/blocks", "tags/block"),
        ("tags/fluids", "tags/fluid"),
        ("tags/entity_types", "tags/entity_type"),
        ("tags/game_events", "tags/game_event"),
        ("tags/functions", "tags/function"),
    ]
]

# Recipe JSON format boundaries for vanilla recipe types.
RECIPE_RESULT_ID_SINCE = "1.20.5"  # result {"item": x} -> {"id": x} (24w10a item components)
RECIPE_INGREDIENT_STRING_SINCE = "1.21.2"  # {"item": x} -> "x", {"tag": t} -> "#t" (24w33a)
VANILLA_RECIPE_TYPES = {
    "minecraft:crafting_shaped",
    "minecraft:crafting_shapeless",
    "minecraft:smelting",
    "minecraft:blasting",
    "minecraft:smoking",
    "minecraft:campfire_cooking",
    "minecraft:stonecutting",
    "minecraft:smithing_transform",
}

# Conventional tags: Forge used forge:*, NeoForge (1.20.5+/1.21) and Fabric (1.21) converged on c:* .
# Only these top-level categories are mapped, and every mapping stays "proposed" until verified in-game.
COMMON_TAG_SINCE = "1.21"
COMMON_TAG_CATEGORIES = {
    "ingots",
    "nuggets",
    "gems",
    "dusts",
    "ores",
    "storage_blocks",
    "raw_materials",
    "rods",
    "glass_blocks",
    "dyes",
    "chests",
    "seeds",
    "crops",
    "stones",
    "cobblestones",
}

# KubeJS 5 (legacy onEvent) -> KubeJS 6+ event groups, from the KubeJS 6 migration guide.
KUBEJS_LEGACY_EVENTS = {
    "recipes": "ServerEvents.recipes",
    "item.tags": "ServerEvents.tags('item')",
    "block.tags": "ServerEvents.tags('block')",
    "fluid.tags": "ServerEvents.tags('fluid')",
    "item.registry": "StartupEvents.registry('item')",
    "block.registry": "StartupEvents.registry('block')",
    "jei.hide.items": "JEIEvents.hideItems",
    "jei.add.items": "JEIEvents.addItems",
    "item.right_click": "ItemEvents.rightClicked",
    "player.logged_in": "PlayerEvents.loggedIn",
}

# Mods that only exist for one loader family and well-known cross-loader counterparts. These are
# *candidates* to discuss with the user, never automatic substitutions.
CROSS_LOADER_CANDIDATES = {
    ("fabric-api", "neoforge"): ["forgified-fabric-api"],
    ("forge-config-api-port", "neoforge"): [],
    ("cloth-config", "any"): ["cloth-config"],
    ("architectury-api", "any"): ["architectury-api"],
}


def crosses(source_mc: str | None, target_mc: str, boundary: str) -> bool:
    """True when migrating from before ``boundary`` to at/after it."""
    if source_mc is None:
        return False
    return Version(source_mc) < Version(boundary) <= Version(target_mc)


def applicable_renames(source_mc: str | None, target_mc: str) -> list[IdRename]:
    return [r for r in VANILLA_RENAMES if crosses(source_mc, target_mc, r.since)]
