"""Format-aware content transformations used by ``mcat migrate apply``.

Each transform takes the original text and returns (new_text, changes). Changes are reviewable
(location, before, after, rule). Only exact identifier matches are rewritten; structure the code does not
understand is left byte-for-byte untouched.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcat.migration import rules
from mcat.parsers import jsscan, snbt


@dataclass
class Change:
    file: str
    location: str
    before: str
    after: str
    rule: str


class TransformError(ValueError):
    pass


def make_id_mapper(id_map: dict[str, str]) -> Callable[[str], str | None]:
    """Map ``ns:path`` and ``#ns:path`` exactly; anything else is untouched."""

    def fn(value: str) -> str | None:
        if value in id_map:
            return id_map[value]
        if value.startswith("#") and value[1:] in id_map:
            return "#" + id_map[value[1:]]
        return None

    return fn


# ---- JSON --------------------------------------------------------------------------------------------


def _walk_json(obj: Any, mapper: Callable[[str], str | None], path: str, out: list[tuple[str, str, str]]) -> Any:
    if isinstance(obj, str):
        new = mapper(obj)
        if new is not None and new != obj:
            out.append((path, obj, new))
            return new
        return obj
    if isinstance(obj, list):
        return [_walk_json(v, mapper, f"{path}/{i}", out) for i, v in enumerate(obj)]
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            nk = k
            if ":" in k:  # identifiers used as keys (e.g. tag/loot maps)
                mapped = mapper(k)
                if mapped is not None and mapped != k:
                    out.append((f"{path}/{k}(key)", k, mapped))
                    nk = mapped
            result[nk] = _walk_json(v, mapper, f"{path}/{k}", out)
        return result
    return obj


def _upgrade_recipe(
    data: dict[str, Any], source_mc: str | None, target_mc: str, file: str, changes: list[Change]
) -> dict[str, Any]:
    if data.get("type") not in rules.VANILLA_RECIPE_TYPES:
        return data
    if rules.crosses(source_mc, target_mc, rules.RECIPE_RESULT_ID_SINCE):
        res = data.get("result")
        if isinstance(res, dict) and "item" in res and "id" not in res:
            res["id"] = res.pop("item")
            changes.append(Change(file, "/result", '{"item": ...}', '{"id": ...}', "recipe-result-id@1.20.5"))
    if rules.crosses(source_mc, target_mc, rules.RECIPE_INGREDIENT_STRING_SINCE):

        def simplify(ing: Any, loc: str) -> Any:
            if isinstance(ing, dict) and set(ing) == {"item"}:
                changes.append(Change(file, loc, json.dumps(ing), json.dumps(ing["item"]), "ingredient-string@1.21.2"))
                return ing["item"]
            if isinstance(ing, dict) and set(ing) == {"tag"}:
                changes.append(
                    Change(file, loc, json.dumps(ing), json.dumps("#" + ing["tag"]), "ingredient-string@1.21.2")
                )
                return "#" + ing["tag"]
            if isinstance(ing, list):
                return [simplify(x, f"{loc}/{i}") for i, x in enumerate(ing)]
            return ing

        if "ingredients" in data:
            data["ingredients"] = [simplify(x, f"/ingredients/{i}") for i, x in enumerate(data["ingredients"])]
        if isinstance(data.get("key"), dict):
            data["key"] = {k: simplify(v, f"/key/{k}") for k, v in data["key"].items()}
        for field in ("ingredient", "base", "addition", "template"):
            if field in data:
                data[field] = simplify(data[field], f"/{field}")
    return data


def transform_json(
    text: str, file: str, mapper: Callable[[str], str | None], *, source_mc: str | None, target_mc: str, recipe: bool
) -> tuple[str, list[Change]]:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise TransformError(f"{file}: invalid JSON: {e}") from e
    hits: list[tuple[str, str, str]] = []
    new = _walk_json(data, mapper, "", hits)
    changes = [Change(file, loc, a, b, "id-map") for loc, a, b in hits]
    if recipe and isinstance(new, dict):
        new = _upgrade_recipe(new, source_mc, target_mc, file, changes)
    if not changes:
        return text, []
    indent = 2 if "\n" in text.strip() else None
    return json.dumps(new, indent=indent, ensure_ascii=False) + ("\n" if text.endswith("\n") else ""), changes


# ---- SNBT (FTB Quests) -------------------------------------------------------------------------------


def transform_snbt(text: str, file: str, mapper: Callable[[str], str | None]) -> tuple[str, list[Change]]:
    try:
        snbt.parse(text)
    except snbt.SnbtError as e:
        raise TransformError(f"{file}: unparseable SNBT: {e}") from e
    new, raw = snbt.rewrite_strings(text, mapper)
    return new, [Change(file, f"offset {off}", a, b, "id-map") for off, a, b in raw]


def quest_dependency_edges(text: str) -> set[tuple[str, str]]:
    data = snbt.parse(text)
    edges = set()
    if isinstance(data, dict):
        for q in data.get("quests", []) or []:
            if isinstance(q, dict):
                for d in q.get("dependencies", []) or []:
                    edges.add((str(q.get("id")), str(d)))
    return edges


# ---- KubeJS -----------------------------------------------------------------------------------------


def transform_js(
    text: str, file: str, mapper: Callable[[str], str | None], *, upgrade_legacy_events: bool
) -> tuple[str, list[Change]]:
    new, raw = jsscan.rewrite_strings(text, mapper)
    changes = [Change(file, f"line {line}", a, b, "id-map") for line, a, b in raw]
    if upgrade_legacy_events:
        new, ev = _upgrade_kubejs_events(new, file)
        changes += ev
    return new, changes


def _upgrade_kubejs_events(text: str, file: str) -> tuple[str, list[Change]]:
    """Rewrite ``onEvent('<known>', fn)`` heads to KubeJS 6 group calls. Unknown events are untouched."""
    out, last, changes = [], 0, []
    for s in jsscan.scan_strings(text):
        head_start = text.rfind("onEvent(", max(0, s.start - 20), s.start)
        if head_start < 0 or text[head_start + len("onEvent(") : s.start].strip():
            continue
        target = rules.KUBEJS_LEGACY_EVENTS.get(s.value)
        if target is None:
            continue
        after = text[s.end :]
        comma = after.find(",")
        if comma < 0 or after[:comma].strip():
            continue
        # ServerEvents.recipes -> "ServerEvents.recipes(fn"; ServerEvents.tags('item') -> "...tags('item', fn"
        head = f"{target[:-1]}, " if target.endswith(")") else f"{target}("
        out.append(text[last:head_start])
        out.append(head)
        last = s.end + comma + 1
        changes.append(
            Change(file, f"line {s.line}", f"onEvent('{s.value}', ...)", head.strip(), "kubejs-legacy-event")
        )
    out.append(text[last:])
    return "".join(out), changes


# ---- datapack paths ---------------------------------------------------------------------------------


def migrate_data_path(path: str, source_mc: str | None, target_mc: str) -> tuple[str, str | None]:
    """Rename registry folders inside ``.../data/<ns>/<folder>/...`` when crossing 1.21."""
    parts = path.split("/")
    if "data" not in parts:
        return path, None
    i = parts.index("data")
    if len(parts) < i + 4:
        return path, None
    sub = "/".join(parts[i + 2 :])
    for fr in rules.DATAPACK_FOLDER_RENAMES:
        if rules.crosses(source_mc, target_mc, fr.since) and (sub.startswith(fr.old + "/")):
            new_sub = fr.new + sub[len(fr.old) :]
            return "/".join([*parts[: i + 2], new_sub]), f"folder {fr.old} -> {fr.new} ({fr.source})"
    return path, None
