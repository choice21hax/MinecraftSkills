"""Phases 1-2: inventory the source pack and produce a reviewable migration plan (read-only).

Planning contacts providers for metadata only (no downloads, no code execution) and never writes to the
source. The plan is a JSON document the user/agent reviews; only ``decisions`` and ``id_map`` are meant to
be edited by hand. ``mcat migrate apply`` refuses a plan whose source snapshot hash no longer matches.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcat import __version__
from mcat.migration import adapters, rules
from mcat.models import ModVersion
from mcat.parsers.inventory import Inventory, build_inventory
from mcat.parsers.packs import PackEntry, PackInfo, parse_pack
from mcat.parsers.source import PackSource
from mcat.resolver.core import Request, Resolver, lock_dict
from mcat.resolver.unify import unify

PLAN_VERSION = 1
PROGRESSION_MODS = {
    "ftbquests": "FTB Quests progression",
    "kubejs": "KubeJS scripts and custom content",
    "crafttweaker": "CraftTweaker scripts",
    "gamestages": "Game Stages progression",
    "ftbteams": "FTB Teams (quest teams)",
    "ftblibrary": "FTB Library",
}
# Namespaces inferred from CurseForge slugs (ftb-quests-forge -> ftb_quests) vs the real mod ids.
SLUG_NAMESPACE_ALIASES = {"ftb_quests": "ftbquests", "ftb_teams": "ftbteams", "ftb_library": "ftblibrary"}


def snapshot_hash(path: Path) -> str:
    """Stable content hash of the source (archive bytes, or every file in a directory)."""
    h = hashlib.sha256()
    if path.is_file():
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
        return h.hexdigest()
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames.sort()
        for f in sorted(filenames):
            full = Path(dirpath, f)
            if full.is_symlink():
                continue
            h.update(full.relative_to(path).as_posix().encode())
            h.update(full.read_bytes())
    return h.hexdigest()


@dataclass
class ModPlan:
    key: str
    name: str
    path: str
    source_version: str | None
    mod_ids: list[str]
    provider: str | None
    project_id: str | None
    action: str  # retain | upgrade | replace | blocked | unknown
    state: str  # verified | proposed | unverified | blocked
    reason: str
    evidence: list[str] = field(default_factory=list)
    target: dict[str, Any] | None = None
    alternatives: list[str] = field(default_factory=list)
    replacement_candidates: list[str] = field(default_factory=list)  # curated rules (rules.py)
    # Machine guesses from provider search; never applied. A user approves one via decisions.replace_with.
    suggested_replacements: list[dict[str, Any]] = field(default_factory=list)
    affected_namespaces: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    progression_critical: bool = False


class Identifier:
    """Maps pack entries to provider projects using only hard evidence (ids in manifests, file hashes)."""

    def __init__(self, modrinth: Any, curseforge: Any | None):
        self.mr, self.cf = modrinth, curseforge

    def identify(self, entries: list[PackEntry]) -> dict[str, tuple[str, str, str]]:
        """entry path -> (provider, project_id, evidence)."""
        out: dict[str, tuple[str, str, str]] = {}
        need_hash = []
        for e in entries:
            if e.provider in ("modrinth", "curseforge") and e.project_id:
                out[e.path] = (e.provider, e.project_id, f"{e.provider} id in pack manifest")
            elif e.hashes.get("sha1"):
                need_hash.append(e)
        if need_hash and self.mr is not None:
            found = self.mr.versions_by_hash([e.hashes["sha1"] for e in need_hash])
            for e in need_hash:
                v = found.get(e.hashes["sha1"])
                if v:
                    out[e.path] = ("modrinth", v.project_id, f"sha1 match to modrinth:version/{v.version_id}")
        return out


def namespace_guesses(slug: str) -> list[str]:
    """Likely mod ids for a project slug, e.g. create-fabric -> [create, create_fabric]."""
    base = slug.lower()
    stems = {base}
    for suffix in ("-fabric", "-forge", "-neoforge", "-quilt", "-mod"):
        if base.endswith(suffix) and len(base) > len(suffix):
            stems.add(base[: -len(suffix)])
    # mod ids are usually the slug with "_" or with no separator (sophisticated-backpacks -> sophisticatedbackpacks)
    return sorted({s.replace("-", sep) for s in stems for sep in ("_", "")})


def _namespaces_of(entry: PackEntry) -> list[str]:
    return sorted({m.mod_id for m in entry.jar_mods} | {p for m in entry.jar_mods for p in m.provides})


def build_plan(
    source: Path,
    target_mc: str,
    target_loader: str,
    *,
    modrinth: Any,
    curseforge: Any | None,
    catalog: Any,
    channels: list[str] | None = None,
    decisions: dict[str, dict[str, Any]] | None = None,
    suggest: bool = True,
) -> dict[str, Any]:
    channels = channels or ["release", "beta"]
    decisions = validate_decisions(decisions or {})
    with PackSource(source) as src:
        pack = parse_pack(src)
        inv = build_inventory(src, pack)
    support = catalog.support(target_loader, target_mc)
    plan: dict[str, Any] = {
        "plan_version": PLAN_VERSION,
        "generated_by": f"mcat {__version__}",
        "source": {
            "path": str(source.resolve()),
            "sha256": snapshot_hash(source),
            "format": pack.format,
            "name": pack.name,
            "version": pack.version,
            "minecraft": pack.minecraft,
            "loader": pack.loader,
            "loader_version": pack.loader_version,
        },
        "target": {
            "minecraft": target_mc,
            "loader": target_loader,
            "loader_version": support.recommended or (support.loader_versions or [None])[0],
            "java": support.java,
            "supported": support.supported,
            "evidence": support.evidence,
        },
        "inventory": {
            "categories": inv.categories,
            "quests": {k: v for k, v in inv.to_dict()["quests"].items() if k != "quest_ids"},
            "suspicious_text": inv.suspicious_text,
            "parse_errors": inv.parse_errors[:50],
        },
        "mods": [],
        "content": [],
        "mechanics": [],
        "vanilla_id_map": {r.old: r.new for r in rules.applicable_renames(pack.minecraft, target_mc)},
        "vanilla_id_map_sources": {r.old: r.source for r in rules.applicable_renames(pack.minecraft, target_mc)},
        "id_map": {},
        "decisions": decisions,  # user-approved only (from --decisions); provenance "user"
        "transforms": _transform_flags(pack, target_mc, inv),
        "lock": None,
        "overall": {},
    }
    if not support.supported:
        plan["overall"] = {
            "state": "blocked",
            "blockers": [f"{target_loader} has no release for Minecraft {target_mc}"],
        }
        return plan
    if pack.minecraft is None:
        plan["inventory"]["warnings"] = ["source game version unknown; version-boundary transforms disabled"]

    ident = Identifier(modrinth, curseforge).identify(pack.entries)
    for e in pack.entries:
        repl = decisions.get(e.path, {}).get("replace_with")
        if repl:
            prov, _, proj = repl.partition(":")
            ident[e.path] = (prov, proj, f"user decision: replace with {repl}")

    def fetch(provider: str, project: str) -> list[ModVersion]:
        if provider == "modrinth":
            return modrinth.versions(project)
        if curseforge is None:
            return []
        if hasattr(curseforge, "target_versions"):
            return curseforge.target_versions(project, target_mc, target_loader)
        return curseforge.versions(project)

    resolver = Resolver(
        fetch,
        target_mc,
        target_loader,
        channels,
        fetch_version=lambda p, vid: modrinth.version(vid) if p == "modrinth" else None,
        dependency_check=_cross_loader_check(curseforge, target_loader),
    )
    _prefetch(resolver, curseforge, sorted(set(ident.values())), plan)
    requests: list[Request] = []
    for e in pack.entries:
        mp = _plan_mod(e, ident.get(e.path), resolver, curseforge, target_mc, target_loader, pack)
        plan["mods"].append(mp)
        if mp.action in ("retain", "upgrade", "replace") and mp.provider and mp.project_id:
            requests.append(Request(mp.provider, mp.project_id, mp.target["version_id"] if mp.target else None))
    # Full-graph check of the kept set: new required dependencies and conflicts.
    res = resolver.resolve(requests)
    identity = unify(res, modrinth.versions_by_hash) if res.ok and hasattr(modrinth, "versions_by_hash") else None
    if identity is not None:
        plan["provider_identity"] = {
            "method": "curseforge sha1 -> modrinth version_files",
            "checked": identity.checked,
            "merged": identity.merged,
            "conflicts": identity.conflicts,
            "error": identity.lookup_error,
            "fix": "pick one provider for a conflicting mod with a decisions entry "
            '{"<mod path>": {"replace_with": "<provider>:<id>"}}',
        }
    if res.ok:
        lock = lock_dict(
            res,
            target={k: plan["target"][k] for k in ("minecraft", "loader", "loader_version")},
            spec_digest=plan["source"]["sha256"],
            java=support.java,
            tool_version=__version__,
        )
        lock["pack"] = {"name": f"{pack.name or 'pack'} ({target_mc} {target_loader})", "version": pack.version or "1"}
        plan["lock"] = lock
        known = {(m.provider, m.project_id) for m in plan["mods"]}
        plan["added_dependencies"] = [
            {"slug": m["slug"], "version": m["version_number"], "reasons": m["reasons"]}
            for m in lock["mods"]
            if (m["provider"], m["project_id"]) not in known
        ]
    else:
        plan["resolution_problems"] = [
            {"kind": p.kind, "project": p.project, "message": p.message, "alternatives": p.alternatives}
            for p in res.problems
        ]
    if suggest:
        _suggest_replacements(plan["mods"], resolver, curseforge, modrinth, target_mc, target_loader, plan)
    _plan_content(plan, pack, inv)
    _plan_mechanics(plan, inv)
    plan["mods"] = [m.__dict__ for m in plan["mods"]]
    plan["overall"] = summarize(plan)
    return plan


PREFETCH_WORKERS = 8
DECISION_KEYS = {"accept_missing", "replace_with", "rebuild_from", "note"}


def validate_decisions(decisions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """User decisions keyed by mod path: {"accept_missing": true} or {"replace_with": "provider:project"}."""
    out: dict[str, dict[str, Any]] = {}
    for key, d in decisions.items():
        if not isinstance(d, dict) or not set(d) <= DECISION_KEYS:
            raise ValueError(f"decision for {key!r} must be an object with keys from {sorted(DECISION_KEYS)}")
        repl = d.get("replace_with")
        if repl is not None and not (
            isinstance(repl, str) and repl.partition(":")[0] in ("modrinth", "curseforge") and repl.partition(":")[2]
        ):
            raise ValueError(f"replace_with for {key!r} must be 'modrinth:<id>' or 'curseforge:<id>'")
        out[key] = {**d, "provenance": "user"}
    return out


_LOADER_WORDS = re.compile(r"\b(neo)?forge\b|\bfabric\b|\bquilt\b|\bport(ed)?\b|\breforged\b|[^a-z0-9]+")


_LOADER_QUERY_WORDS = re.compile(r"(?i)\b(neo)?forge\b|\bfabric\b|\bquilt\b|[()\[\]]")


def _norm_title(title: str) -> str:
    return _LOADER_WORDS.sub("", title.lower())


def _suggest_replacements(
    mods: list[ModPlan],
    resolver: Resolver,
    curseforge: Any | None,
    modrinth: Any,
    target_mc: str,
    target_loader: str,
    plan: dict[str, Any],
) -> None:
    """Search the providers for the target-loader edition of each blocked mod (e.g. a separate NeoForge
    project by the same author). Results are guesses: recorded with their basis, never applied."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    t = time.monotonic()
    blocked = [m for m in mods if m.state == "blocked" and m.provider and m.project_id]

    def one(m: ModPlan) -> list[dict[str, Any]]:
        known = resolver.all_versions(m.provider or "", m.project_id or "")
        title, authors = (known[0].slug.replace("-", " ") if known else m.name), set()
        if m.provider == "curseforge" and curseforge is not None:
            meta = curseforge.mod(m.project_id)
            title, authors = meta.get("name", title), {a.get("id") for a in meta.get("authors", [])}
        want = _norm_title(title)
        if not want:
            return []
        query = " ".join(_LOADER_QUERY_WORDS.sub(" ", title).split())
        found: list[dict[str, Any]] = []
        searches = [("modrinth", modrinth)] + ([("curseforge", curseforge)] if curseforge is not None else [])
        for prov, client in searches:
            try:
                hits = client.search(query, minecraft=target_mc, loader=target_loader, limit=10)
            except Exception:  # noqa: S112 - a failed suggestion search is not a planning error
                continue
            for h in hits:
                if (prov, h.project_id) == (m.provider, m.project_id):
                    continue
                basis = []
                if _norm_title(h.title) == want:
                    basis.append("same title ignoring loader words")
                if (
                    prov == "curseforge"
                    and authors
                    and authors & {a.get("id") for a in client.mod(h.project_id).get("authors", [])}
                ):
                    basis.append("same CurseForge author")
                if basis and (not h.loaders or target_loader in h.loaders):
                    found.append(
                        {
                            "replace_with": f"{prov}:{h.project_id}",
                            "slug": h.slug,
                            "title": h.title,
                            "basis": basis,
                            "approved": False,
                            "provenance": "search-guess",
                        }
                    )
        return found[:3]

    with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as pool:
        for m, sugg in zip(blocked, pool.map(one, blocked), strict=True):
            m.suggested_replacements = sugg
    plan.setdefault("timings", {})["suggestions_s"] = round(time.monotonic() - t, 2)


def _prefetch(
    resolver: Resolver, curseforge: Any | None, idents: list[tuple[str, str, str]], plan: dict[str, Any]
) -> None:
    """Fill the resolver's version cache with bounded concurrency (one request stream per project)."""
    import contextlib
    import time
    from concurrent.futures import ThreadPoolExecutor

    t = time.monotonic()
    projects = sorted({(p, proj) for p, proj, _ in idents})
    cf_ids = [proj for p, proj in projects if p == "curseforge"]
    if cf_ids and curseforge is not None and hasattr(curseforge, "mods_batch"):
        curseforge.mods_batch(cf_ids)  # one POST instead of one GET per project

    def one(key: tuple[str, str]) -> None:
        # failures are ignored here: the sequential pass below reports them per mod
        with contextlib.suppress(Exception):
            resolver.all_versions(*key)

    with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as pool:
        list(pool.map(one, projects))
    plan.setdefault("timings", {})["provider_prefetch_s"] = round(time.monotonic() - t, 2)
    plan["timings"]["projects"] = len(projects)


def _plan_mod(
    e: PackEntry,
    ident: tuple[str, str, str] | None,
    resolver: Resolver,
    curseforge: Any | None,
    target_mc: str,
    target_loader: str,
    pack: PackInfo,
) -> ModPlan:
    mod_ids = _namespaces_of(e)
    critical = bool(set(mod_ids) & set(PROGRESSION_MODS)) or any(
        k in (e.name or "").lower().replace(" ", "") for k in PROGRESSION_MODS
    )
    base: dict[str, Any] = dict(
        key=e.path,
        name=e.name,
        path=e.path,
        source_version=e.jar_mods[0].version if e.jar_mods else e.version_id,
        mod_ids=mod_ids,
        affected_namespaces=mod_ids,
        progression_critical=critical,
    )
    if ident is None:
        if e.provider == "curseforge" and curseforge is None:
            return ModPlan(
                **base,
                provider="curseforge",
                project_id=e.project_id,
                action="unknown",
                state="blocked",
                reason="CurseForge entry but no CurseForge API key configured",
            )
        return ModPlan(
            **base,
            provider=None,
            project_id=None,
            action="unknown",
            state="blocked",
            reason="not found on Modrinth by hash and no provider id in the manifest (custom or "
            "renamed mod); needs manual identification or a rebuild from source",
            gaps=["if source code is available, port it with the minecraft-moddev skill"],
        )
    provider, project, evidence = ident
    slug = next((v.slug for v in resolver.all_versions(provider, project)), project)
    if not mod_ids:  # no jar metadata offline: guess namespaces from the slug, and say so
        guesses = namespace_guesses(slug)
        base["affected_namespaces"] = guesses
        base["gaps"] = [f"namespaces {guesses} inferred from the project slug (unverified)"]
    cands = resolver.candidates(provider, project)
    if cands:

        def is_current(v: ModVersion) -> bool:
            return e.version_id == v.version_id or bool(
                e.hashes.get("sha1") and e.hashes.get("sha1") in {f.hashes.get("sha1") for f in v.files}
            )

        # Minimal change: keep the pack's own file when it already supports the target.
        v = next((c for c in cands if is_current(c)), cands[0])
        same = is_current(v)
        target = {
            "version_id": v.version_id,
            "version_number": v.version_number,
            "slug": v.slug,
            "game_versions": v.game_versions,
            "loaders": v.loaders,
            "channel": v.channel,
        }
        gaps = [
            *base.pop("gaps", []),
            *([] if same else ["config keys and content may differ between versions; review configs"]),
        ]
        return ModPlan(
            **base,
            provider=provider,
            project_id=project,
            action="replace" if evidence.startswith("user decision") else ("retain" if same else "upgrade"),
            state="verified" if same else "proposed",
            reason=(
                "same file already supports the target"
                if same
                else f"{v.slug} {v.version_number} lists {target_mc} / {target_loader}"
            ),
            evidence=[evidence, f"{provider}:version/{v.version_id}"],
            target=target,
            gaps=gaps,
        )
    alts = resolver.alternatives(provider, project)
    cands_x = rules.CROSS_LOADER_CANDIDATES.get((slug, target_loader), []) + rules.CROSS_LOADER_CANDIDATES.get(
        (slug, "any"), []
    )
    gaps = [
        *base.pop("gaps", []),
        "functionality and identifiers provided by this mod will be missing unless a "
        "replacement is chosen and its identifiers mapped in id_map",
    ]
    return ModPlan(
        **base,
        provider=provider,
        project_id=project,
        action="blocked",
        state="blocked",
        reason=_blocked_reason(resolver.all_versions(provider, project), slug, target_mc, target_loader),
        evidence=[evidence],
        alternatives=alts,
        replacement_candidates=[c for c in cands_x if c != slug],
        gaps=gaps,
    )


def _cross_loader_check(curseforge: Any | None, target_loader: str) -> Any:
    """A required CurseForge dependency whose project has no file for the target loader in any game
    version cannot be a real requirement of a file built for that loader (cross-loader metadata error)."""
    if curseforge is None or not hasattr(curseforge, "mod"):
        return None
    from mcat.providers.curseforge import CLASS_MODS, LOADER_TYPES

    want = LOADER_TYPES.get(target_loader)
    forge_family = {LOADER_TYPES["forge"], LOADER_TYPES["neoforge"]}
    fabric_family = {LOADER_TYPES["fabric"], LOADER_TYPES["quilt"]}
    other_family = fabric_family if want in forge_family else forge_family

    def check(provider: str, project: str) -> str | None:
        if provider != "curseforge" or want is None:
            return None
        try:
            meta = curseforge.mod(project)
        except Exception:  # unknown -> follow the dependency normally
            return None
        idx = meta.get("latestFilesIndexes") or []
        tagged = [i.get("modLoader") for i in idx if i.get("modLoader") is not None]
        # Only a different loader *family* counts: 1.20.1 NeoForge-compatible files are often tagged Forge.
        if int(meta.get("classId") or CLASS_MODS) != CLASS_MODS or not tagged or not set(tagged) <= other_family:
            return None
        return (
            f"declared required, but {meta.get('slug', project)} has no {target_loader} files on CurseForge "
            "(cross-loader metadata error, probable); not added - verify at runtime"
        )

    return check


def _blocked_reason(versions: list[ModVersion], slug: str, target_mc: str, target_loader: str) -> str:
    from mcat.resolver.core import loader_ok

    other = sorted({v.channel for v in versions if target_mc in v.game_versions and loader_ok(v, target_loader)})
    if other:  # files exist for the target but only on channels the plan excludes
        return (
            f"{slug} has only {'/'.join(other)} builds for {target_loader} {target_mc} "
            f"(excluded; re-plan with --allow-alpha to consider them)"
        )
    return f"{slug} has no {target_loader} release for Minecraft {target_mc}"


def _transform_flags(pack: PackInfo, target_mc: str, inv: Inventory) -> dict[str, Any]:
    src = pack.minecraft
    has_legacy_kjs = False  # decided per file at apply time; flag documents intent
    return {
        "vanilla_id_renames": bool(rules.applicable_renames(src, target_mc)),
        "datapack_folder_singular": rules.crosses(src, target_mc, "1.21"),
        "recipe_result_id": rules.crosses(src, target_mc, rules.RECIPE_RESULT_ID_SINCE),
        "recipe_ingredient_strings": rules.crosses(src, target_mc, rules.RECIPE_INGREDIENT_STRING_SINCE),
        "kubejs_legacy_events": inv.categories.get("kubejs_script", 0) > 0 or has_legacy_kjs,
        "common_tags_forge_to_c": "proposed-only" if rules.crosses(src, target_mc, rules.COMMON_TAG_SINCE) else False,
    }


def _plan_content(plan: dict[str, Any], pack: PackInfo, inv: Inventory) -> None:
    blocked_ns = {ns for m in plan["mods"] if m.state == "blocked" for ns in m.affected_namespaces}
    for cat, files in sorted(inv.files.items()):
        if cat in ("mods", "pack_metadata"):
            continue
        affected = sorted(
            {
                ns
                for f in files
                for ident, paths in inv.references.items()
                if f in paths
                for ns in [ident.split(":")[0]]
                if ns in blocked_ns
            }
        )
        adapter = adapters.for_category(cat)
        action, state = (adapter.action, adapter.state) if adapter else ("copy", "unverified")
        if affected:
            state = "blocked"
        plan["content"].append(
            {
                "category": cat,
                "files": len(files),
                "action": action,
                "state": state,
                "blocked_namespaces": affected,
                "adapter": adapter.name if adapter else None,
                "note": _content_note(cat),
            }
        )
    flags = plan["transforms"]
    present = {c["category"] for c in plan["content"]}
    plan["adapters"] = [
        a.describe(pack.minecraft, plan["target"]["minecraft"], flags)
        for a in adapters.ADAPTERS
        if present & set(a.categories)
    ]


def _content_note(cat: str) -> str:
    return {
        "config": "copied verbatim; option names may have changed in upgraded mod versions",
        "kubejs_script": "identifiers mapped via id_map; legacy onEvent() heads upgraded; API changes between "
        "KubeJS versions beyond that need manual review and runtime testing",
        "ftbquests": "quest ids and dependencies preserved byte-for-byte; item ids mapped via id_map only",
        "recipe": "vanilla recipe formats upgraded across documented boundaries; modded recipe types copied "
        "with id mapping only",
        "worldgen": "worldgen JSON formats change often; copied with id mapping only and must be tested",
    }.get(cat, "copied with id mapping")


def _plan_mechanics(plan: dict[str, Any], inv: Inventory) -> None:
    mods_by_ns = {ns: m for m in plan["mods"] for ns in m.affected_namespaces}
    for inferred, mod_id in SLUG_NAMESPACE_ALIASES.items():
        if inferred in mods_by_ns:
            mods_by_ns.setdefault(mod_id, mods_by_ns[inferred])
    for ns, label in PROGRESSION_MODS.items():
        m = mods_by_ns.get(ns)
        if m is None:
            continue
        state = "blocked" if m.state == "blocked" else "unverified"
        plan["mechanics"].append(
            {
                "name": label,
                "mod": ns,
                "state": state,
                "critical": True,
                "reason": m.reason
                if state == "blocked"
                else "mod available; progression must be play-tested after migration",
            }
        )
    if inv.quests.quests and "ftbquests" not in mods_by_ns:
        plan["mechanics"].append(
            {
                "name": "FTB Quests progression",
                "mod": "ftbquests",
                "state": "blocked",
                "critical": True,
                "reason": "quest files exist but no FTB Quests jar was identified",
            }
        )
    if inv.quests.dangling_dependencies:
        plan["mechanics"].append(
            {
                "name": "Quest dependency integrity",
                "mod": "ftbquests",
                "state": "unverified",
                "critical": False,
                "reason": f"{len(inv.quests.dangling_dependencies)} dependency ids do not match a "
                "quest (may reference chapters); preserved as-is",
            }
        )


def summarize(plan: dict[str, Any]) -> dict[str, Any]:
    mods = plan["mods"]
    blockers = [
        f"mod {m['name'] or m['path']}: {m['reason']}"
        for m in mods
        if m["state"] == "blocked" and not plan["decisions"].get(m["key"], {}).get("accept_missing")
    ]
    blockers += [f"mechanic {x['name']}: {x['reason']}" for x in plan["mechanics"] if x["state"] == "blocked"]
    blockers += [f"resolution: {p['message']}" for p in plan.get("resolution_problems", [])]
    counts: dict[str, int] = {}
    for m in mods:
        counts[m["state"]] = counts.get(m["state"], 0) + 1
    return {
        "state": "blocked" if blockers else "proposed",
        "blockers": blockers,
        "mod_states": counts,
        "complete": False,
        "note": "a plan is never 'complete': apply it, then verify builds and play-test progression",
    }
