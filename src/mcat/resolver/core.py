"""Deterministic dependency resolver.

Given a target (game version + loader) and requested projects, selects exactly one provider version per
project such that:

* every selected version lists the exact target game version and loader (no "close enough" matches),
* every required dependency is selected (pinned version ids are honoured exactly),
* no selected version declares another selected project incompatible,
* excluded projects are never selected.

Search is depth-first with backtracking over candidates ordered newest-first (ties broken by version
id), so identical provider data always yields an identical lock. When unsatisfiable, the result explains
why and lists alternatives as evidence (other game versions / loaders that do exist) - it never silently
picks a broken version.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcat.models import DepKind, ModVersion
from mcat.resolver.versions import Version

LOADER_COMPAT = {"fabric": {"fabric"}, "neoforge": {"neoforge"}, "forge": {"forge"}, "quilt": {"quilt", "fabric"}}
MAX_STEPS = 20_000


@dataclass
class Problem:
    kind: str  # unavailable | conflict | excluded | pinned-mismatch | search-limit
    project: str
    message: str
    alternatives: list[str] = field(default_factory=list)


@dataclass
class Selection:
    version: ModVersion
    reasons: list[str]
    side_override: str | None = None
    optional: bool = False
    equivalents: list[str] = field(default_factory=list)  # same file from another provider (by hash)


@dataclass
class Resolution:
    selected: dict[str, Selection] = field(default_factory=dict)  # key = provider:project_id
    problems: list[Problem] = field(default_factory=list)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, kind)
    cycles: list[list[str]] = field(default_factory=list)
    optional_skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


VersionFetcher = Callable[[str, str], list[ModVersion]]  # (provider, project) -> versions (any target)


@dataclass
class Request:
    provider: str
    project: str
    version: str | None = None
    side: str | None = None
    optional: bool = False


def compatible(v: ModVersion, minecraft: str, loader: str, channels: list[str]) -> bool:
    return minecraft in v.game_versions and bool(set(v.loaders) & LOADER_COMPAT[loader]) and v.channel in channels


def _order(vs: list[ModVersion]) -> list[ModVersion]:
    return sorted(vs, key=lambda v: (v.published, v.version_id), reverse=True)


class Resolver:
    def __init__(
        self,
        fetch: VersionFetcher,
        minecraft: str,
        loader: str,
        channels: list[str],
        *,
        include_optional: bool = False,
        exclude: list[str] | None = None,
        fetch_version: Callable[[str, str], ModVersion | None] | None = None,
    ):
        self.fetch = fetch
        self.fetch_version = fetch_version
        self.minecraft, self.loader, self.channels = minecraft, loader, channels
        self.include_optional = include_optional
        self.exclude = set(exclude or [])
        self._cache: dict[tuple[str, str], list[ModVersion]] = {}
        self._steps = 0

    def all_versions(self, provider: str, project: str) -> list[ModVersion]:
        key = (provider, project)
        if key not in self._cache:
            self._cache[key] = self.fetch(provider, project)
        return self._cache[key]

    def candidates(self, provider: str, project: str, pin: str | None = None) -> list[ModVersion]:
        vs = [
            v for v in self.all_versions(provider, project) if compatible(v, self.minecraft, self.loader, self.channels)
        ]
        if pin:
            vs = [v for v in vs if pin in (v.version_id, v.version_number)]
        return _order(vs)

    def alternatives(self, provider: str, project: str) -> list[str]:
        out: set[tuple[str, str, str]] = set()
        for v in self.all_versions(provider, project):
            for gv in v.game_versions:
                out.add((gv, "+".join(sorted(v.loaders)), v.channel))
        ordered = sorted(out, key=lambda t: (Version(t[0]), t[1], t[2] == "release"), reverse=True)
        return ["/".join(t) for t in ordered[:15]]

    def _is_excluded(self, v: ModVersion | None, project: str) -> bool:
        names = {project}
        if v:
            names |= {v.project_id, v.slug}
        return bool(names & self.exclude)

    def resolve(self, requests: list[Request]) -> Resolution:
        res = Resolution()
        # Canonical keys: provider:project_id (slugs are normalised after the first fetch).
        pending: list[tuple[str, str, str | None, str, Request | None]] = [
            (r.provider, r.project, r.version, "requested", r) for r in requests
        ]
        ok = self._solve(pending, {}, res)
        if not ok and not res.problems:
            res.problems.append(Problem("conflict", "*", "no combination of versions satisfies all constraints"))
        res.cycles = _find_cycles(res.edges)
        return res

    def _solve(self, pending: list, chosen: dict[str, Selection], res: Resolution) -> bool:
        self._steps += 1
        if self._steps > MAX_STEPS:
            res.problems.append(Problem("search-limit", "*", f"gave up after {MAX_STEPS} resolution steps"))
            return False
        if not pending:
            res.selected = dict(sorted(chosen.items()))
            res.edges = sorted({e for s in chosen.values() for e in self._edges(s.version, chosen)})
            return True
        provider, project, pin, reason, req = pending[0]
        rest = pending[1:]
        existing = self._lookup(chosen, provider, project)
        if existing is not None:
            key, sel = existing
            if pin and pin not in (sel.version.version_id, sel.version.version_number):
                res.problems.append(
                    Problem(
                        "pinned-mismatch",
                        project,
                        f"{reason} requires version {pin}, but {sel.version.version_number} was selected",
                    )
                )
                return False
            updated = Selection(
                sel.version,
                sorted({*sel.reasons, reason}),
                sel.side_override,
                sel.optional and (req is None or req.optional) and reason == "requested",
            )
            return self._solve(rest, {**chosen, key: updated}, res)
        cands = self.candidates(provider, project, pin)
        if self._is_excluded(cands[0] if cands else None, project):
            res.problems.append(Problem("excluded", project, f"{project} is excluded but needed ({reason})"))
            return False
        if not cands:
            what = f"version {pin}" if pin else "version"
            res.problems.append(
                Problem(
                    "unavailable",
                    project,
                    f"{provider}:{project} has no {what} for Minecraft {self.minecraft} / {self.loader} "
                    f"(channels {','.join(self.channels)}); needed by {reason}",
                    self.alternatives(provider, project),
                )
            )
            return False
        local_problems: list[Problem] = []
        for cand in cands:
            clash = self._incompatibility(cand, chosen)
            if clash:
                local_problems.append(Problem("conflict", cand.slug, clash))
                continue
            key = f"{provider}:{cand.project_id}"
            sel = Selection(cand, [reason], req.side if req else None, bool(req and req.optional))
            nxt = dict(chosen)
            nxt[key] = sel
            deps = []
            for d in cand.dependencies:
                if d.kind == DepKind.REQUIRED or (d.kind == DepKind.OPTIONAL and self.include_optional):
                    if d.project_id is None and d.version_id is None:
                        continue
                    dep_project = d.project_id or self._project_of_version(provider, d.version_id)
                    if dep_project is None:
                        local_problems.append(
                            Problem("unavailable", cand.slug, f"dependency version {d.version_id} could not be found")
                        )
                        continue
                    deps.append((provider, dep_project, d.version_id, f"dependency of {cand.slug}", None))
                elif d.kind == DepKind.OPTIONAL and d.project_id:
                    res.optional_skipped.append(f"{cand.slug} -> {d.project_id}")
            snapshot = len(res.problems)
            if self._solve(deps + rest, nxt, res):
                return True
            local_problems.extend(res.problems[snapshot:])
            del res.problems[snapshot:]
        res.problems.extend(_dedupe(local_problems))
        return False

    def _lookup(self, chosen: dict[str, Selection], provider: str, project: str) -> tuple[str, Selection] | None:
        for key, sel in chosen.items():
            if key.startswith(provider + ":") and project in (sel.version.project_id, sel.version.slug):
                return key, sel
        return None

    def _project_of_version(self, provider: str, version_id: str | None) -> str | None:
        for (prov, _), vs in self._cache.items():
            if prov == provider:
                for v in vs:
                    if v.version_id == version_id:
                        return v.project_id
        if self.fetch_version and version_id:
            found = self.fetch_version(provider, version_id)
            return found.project_id if found else None
        return None

    def _incompatibility(self, cand: ModVersion, chosen: dict[str, Selection]) -> str | None:
        ids = {s.version.project_id: s for s in chosen.values()}
        for d in cand.dependencies:
            if d.kind == DepKind.INCOMPATIBLE and d.project_id in ids:
                return f"{cand.slug} {cand.version_number} is incompatible with {ids[d.project_id].version.slug}"
        for s in chosen.values():
            for d in s.version.dependencies:
                if d.kind == DepKind.INCOMPATIBLE and d.project_id == cand.project_id:
                    return f"{s.version.slug} declares {cand.slug} incompatible"
        return None

    def _edges(self, v: ModVersion, chosen: dict[str, Selection]) -> list[tuple[str, str, str]]:
        by_id = {s.version.project_id: s.version.slug for s in chosen.values()}
        return [
            (v.slug, by_id.get(d.project_id or "", d.project_id or d.version_id or "?"), d.kind.value)
            for d in v.dependencies
            if d.kind != DepKind.EMBEDDED
        ]


def _dedupe(problems: list[Problem]) -> list[Problem]:
    seen, out = set(), []
    for p in problems:
        if (p.kind, p.project, p.message) not in seen:
            seen.add((p.kind, p.project, p.message))
            out.append(p)
    return out


def _find_cycles(edges: list[tuple[str, str, str]]) -> list[list[str]]:
    graph: dict[str, list[str]] = {}
    for a, b, kind in edges:
        if kind == "required":
            graph.setdefault(a, []).append(b)
    cycles, state = [], {}

    def visit(n: str, stack: list[str]) -> None:
        state[n] = 1
        for m in graph.get(n, []):
            if state.get(m) == 1:
                cycles.append([*stack[stack.index(m) :], m])
            elif m not in state:
                visit(m, [*stack, m])
        state[n] = 2

    for n in sorted(graph):
        if n not in state:
            visit(n, [n])
    return cycles


def lock_dict(
    res: Resolution, *, target: dict[str, Any], spec_digest: str, java: int | None, tool_version: str
) -> dict[str, Any]:
    mods: list[dict[str, Any]] = []
    for key, sel in sorted(res.selected.items()):  # sorted: lockfiles must not depend on traversal order
        v = sel.version
        f = v.primary_file
        mods.append(
            {
                "key": key,
                "provider": v.provider,
                "project_id": v.project_id,
                "slug": v.slug,
                "name": v.name,
                "version_id": v.version_id,
                "version_number": v.version_number,
                "channel": v.channel,
                "game_versions": v.game_versions,
                "loaders": v.loaders,
                "side": sel.side_override or v.side.value,
                "optional": sel.optional,
                "license": v.license,
                "file": {
                    "filename": f.filename,
                    "url": f.url,
                    "hashes": f.hashes,
                    "size": f.size,
                    "download_allowed": f.download_allowed,
                }
                if f
                else None,
                "dependencies": [
                    {"kind": d.kind.value, "project_id": d.project_id, "version_id": d.version_id}
                    for d in v.dependencies
                ],
                "reasons": sorted(sel.reasons),
                "provenance": f"{v.provider}:version/{v.version_id}",
                "equivalents": sorted(sel.equivalents),
            }
        )
    restrictions = [
        {"key": m["key"], "file": m["file"]["filename"], "reason": "author disallows third-party distribution"}
        for m in mods
        if m["file"] and not m["file"]["download_allowed"]
    ]
    return {
        "lock_version": 1,
        "generated_by": f"mcat {tool_version}",
        "spec_sha256": spec_digest,
        "target": target,
        "java": java,
        "mods": mods,
        "restrictions": restrictions,
        "unlicensed_or_unknown": sorted(m["key"] for m in mods if not m["license"]),
        "graph": {"edges": sorted(list(e) for e in res.edges), "cycles": res.cycles},
    }
