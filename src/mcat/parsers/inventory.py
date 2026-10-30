"""Phase-1 inventory: classify pack content and index identifier references.

Everything here is static analysis of untrusted files: nothing is executed, and text found in the pack
(READMEs, quest descriptions, scripts) is treated as data. Text that looks like instructions aimed at
an AI agent is flagged so skills never act on it.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from mcat.parsers import jsscan, snbt
from mcat.parsers.packs import PackInfo
from mcat.parsers.source import PackSource
from mcat.security.archive import UnsafeArchiveError

ID_RE = re.compile(r"^#?([a-z0-9_.\-]+):([a-z0-9_.\-/]+)$")
VANILLA_NS = {"minecraft", "c", "forge", "neoforge", "fabric", "brigadier"}
MAX_TEXT = 8 * 1024**2
_INJECTION = re.compile(
    r"(?i)(ignore (all )?(previous|prior) instructions|you are (an? )?(ai|assistant|agent)\b|"
    r"system prompt|run the following command|curl\s+[^\s]+\s*\|\s*(sh|bash))"
)

CATEGORIES = [
    "mods",
    "config",
    "defaultconfigs",
    "kubejs_script",
    "kubejs_data",
    "kubejs_assets",
    "crafttweaker",
    "datapack",
    "recipe",
    "tag",
    "loot_table",
    "worldgen",
    "advancement",
    "ftbquests",
    "resourcepack",
    "shaderpack",
    "pack_metadata",
    "other",
]


@dataclass
class QuestGraph:
    chapters: int = 0
    quests: int = 0
    tasks: int = 0
    rewards: int = 0
    dependency_edges: int = 0
    dangling_dependencies: list[str] = field(default_factory=list)
    quest_ids: list[str] = field(default_factory=list)
    item_refs: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


@dataclass
class Inventory:
    categories: dict[str, int] = field(default_factory=dict)
    files: dict[str, list[str]] = field(default_factory=dict)
    referenced_namespaces: dict[str, int] = field(default_factory=dict)
    references: dict[str, list[str]] = field(default_factory=dict)  # identifier -> files
    provided_mod_ids: list[str] = field(default_factory=list)
    unresolved_namespaces: list[str] = field(default_factory=list)
    quests: QuestGraph = field(default_factory=QuestGraph)
    suspicious_text: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    symlinks_skipped: list[str] = field(default_factory=list)

    def to_dict(self, max_files: int = 50) -> dict[str, Any]:
        d = asdict(self)
        d["files"] = {k: v[:max_files] for k, v in self.files.items()}
        d["references"] = {k: v[:5] for k, v in sorted(self.references.items())[:500]}
        d["quests"]["quest_ids"] = d["quests"]["quest_ids"][:200]
        d["quests"]["item_refs"] = sorted(set(d["quests"]["item_refs"]))[:500]
        return d


def instance_path(name: str, roots: list[str]) -> str:
    for r in sorted(roots, key=len, reverse=True):
        if r and name.startswith(r + "/"):
            return name[len(r) + 1 :]
    return name


def classify(path: str) -> str:
    p = path.lower()
    parts = p.split("/")
    if parts[0] == "mods" and p.endswith((".jar", ".pw.toml")):
        return "mods"
    if p.startswith("config/ftbquests/") and p.endswith(".snbt"):
        return "ftbquests"
    if parts[0] == "kubejs":
        if len(parts) > 1 and parts[1].endswith("_scripts") and p.endswith((".js", ".ts")):
            return "kubejs_script"
        if len(parts) > 1 and parts[1] == "data":
            return _data_kind(parts[2:]) or "kubejs_data"
        if len(parts) > 1 and parts[1] == "assets":
            return "kubejs_assets"
    if parts[0] == "scripts" and p.endswith(".zs"):
        return "crafttweaker"
    if parts[0] == "config":
        return "config"
    if parts[0] == "defaultconfigs":
        return "defaultconfigs"
    if parts[0] in ("resourcepacks",):
        return "resourcepack"
    if parts[0] in ("shaderpacks",):
        return "shaderpack"
    if "data" in parts:
        i = parts.index("data")
        kind = _data_kind(parts[i + 1 :])
        if kind:
            return kind
        if parts[0] in ("datapacks", "global_packs", "openloader", "paxi", "kubejs"):
            return "datapack"
    if parts[0] in ("datapacks", "global_packs", "openloader"):
        return "datapack"
    if p in (
        "modrinth.index.json",
        "manifest.json",
        "pack.toml",
        "index.toml",
        "mmc-pack.json",
        "minecraftinstance.json",
        "instance.cfg",
    ):
        return "pack_metadata"
    return "other"


def _data_kind(rest: list[str]) -> str | None:
    """rest = [namespace, folder, ...] under a data/ directory."""
    if len(rest) < 2:
        return None
    folder = rest[1]
    if folder in ("recipes", "recipe"):
        return "recipe"
    if folder in ("tags",):
        return "tag"
    if folder in ("loot_tables", "loot_table"):
        return "loot_table"
    if folder == "worldgen" or folder == "dimension" or folder == "dimension_type":
        return "worldgen"
    if folder in ("advancements", "advancement"):
        return "advancement"
    return None


def _json_ids(obj: Any, out: list[str]) -> None:
    if isinstance(obj, str):
        if ID_RE.match(obj):
            out.append(obj.lstrip("#"))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and ID_RE.match(k) and ":" in k:
                out.append(k.lstrip("#"))
            _json_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _json_ids(v, out)


def extract_ids(path: str, text: str) -> tuple[list[str], str | None]:
    """Return (identifiers, parse_error) for a supported structured file."""
    ids: list[str] = []
    try:
        if path.endswith((".json", ".mcmeta")):
            _json_ids(json.loads(text), ids)
        elif path.endswith(".snbt"):
            _json_ids(snbt.parse(text), ids)
        elif path.endswith((".js", ".ts")):
            ids = [s.value.lstrip("#") for s in jsscan.scan_strings(text) if ID_RE.match(s.value)]
        elif path.endswith(".toml"):
            _json_ids(tomllib.loads(text), ids)
    except (ValueError, snbt.SnbtError, tomllib.TOMLDecodeError) as e:
        return ids, f"{path}: {type(e).__name__}: {str(e)[:200]}"
    return ids, None


def _quest_graph(src: PackSource, names: list[str], graph: QuestGraph) -> None:
    all_deps: list[tuple[str, str]] = []
    for n in names:
        if "/chapters/" not in n or not n.endswith(".snbt"):
            continue
        try:
            data = snbt.parse(src.read_text(n, MAX_TEXT))
        except (snbt.SnbtError, UnsafeArchiveError) as e:
            graph.parse_errors.append(f"{n}: {e}")
            continue
        if not isinstance(data, dict):
            continue
        graph.chapters += 1
        for q in data.get("quests", []) or []:
            if not isinstance(q, dict):
                continue
            graph.quests += 1
            qid = str(q.get("id", ""))
            graph.quest_ids.append(qid)
            for d in q.get("dependencies", []) or []:
                all_deps.append((qid, str(d)))
            for t in q.get("tasks", []) or []:
                graph.tasks += 1
                item = t.get("item") if isinstance(t, dict) else None
                if isinstance(item, dict):
                    item = item.get("id")
                if isinstance(item, str):
                    graph.item_refs.append(item)
            graph.rewards += len(q.get("rewards", []) or [])
    known = set(graph.quest_ids)
    graph.dependency_edges = len(all_deps)
    # dependencies may also point to chapters or other objects; only flag ids absent from every quest
    graph.dangling_dependencies = sorted({d for _, d in all_deps if d not in known})


def build_inventory(src: PackSource, pack: PackInfo) -> Inventory:
    inv = Inventory(symlinks_skipped=list(src.symlinks))
    files: dict[str, list[str]] = defaultdict(list)
    ns_counter: Counter[str] = Counter()
    refs: dict[str, list[str]] = defaultdict(list)
    quest_files: list[str] = []
    for name in src.names():
        ipath = instance_path(name, pack.override_roots)
        cat = classify(ipath)
        files[cat].append(ipath)
        if cat == "ftbquests":
            quest_files.append(name)
        if cat in ("mods", "resourcepack", "shaderpack", "other") or src.size(name) > MAX_TEXT:
            is_doc = name.lower().endswith((".md", ".txt")) and src.size(name) < MAX_TEXT
            if is_doc and _INJECTION.search(src.read_text(name, MAX_TEXT)):
                inv.suspicious_text.append(ipath)
            continue
        if not name.endswith((".json", ".snbt", ".js", ".ts", ".toml", ".mcmeta")):
            continue
        text = src.read_text(name, MAX_TEXT)
        if _INJECTION.search(text):
            inv.suspicious_text.append(ipath)
        if cat in ("config", "defaultconfigs") and not name.endswith((".json", ".snbt")):
            continue  # TOML configs are mostly options, not identifier carriers; skip for signal quality
        ids, err = extract_ids(name, text)
        if err:
            inv.parse_errors.append(err)
        for ident in ids:
            ns = ident.split(":", 1)[0]
            ns_counter[ns] += 1
            if len(refs[ident]) < 20 and ipath not in refs[ident]:
                refs[ident].append(ipath)
    for e in pack.entries:
        if e.embedded or e.path.startswith("mods/"):
            files.setdefault("mods", [])
            if e.path not in files["mods"]:
                files["mods"].append(e.path)
    _quest_graph(src, quest_files, inv.quests)
    provided = {m.mod_id for e in pack.entries for m in e.jar_mods}
    provided |= {p for e in pack.entries for m in e.jar_mods for p in m.provides}
    inv.files = {k: sorted(v) for k, v in files.items()}
    inv.categories = {k: len(v) for k, v in sorted(inv.files.items())}
    inv.referenced_namespaces = dict(ns_counter.most_common())
    inv.references = dict(refs)
    inv.provided_mod_ids = sorted(provided)
    # Only meaningful when every mod jar is embedded (otherwise mod ids are unknown offline).
    if pack.entries and all(e.embedded for e in pack.entries):
        local_ns = set()  # namespaces whose assets the pack ships itself (custom KubeJS content)
        for p in inv.files.get("kubejs_assets", []):
            parts = p.split("/")
            if len(parts) > 3:
                local_ns.add(parts[2])
        inv.unresolved_namespaces = sorted(
            ns for ns in ns_counter if ns not in provided and ns not in VANILLA_NS and ns not in local_ns
        )
    return inv
