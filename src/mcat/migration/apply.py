"""Phases 3-5: transform content, build the target workspace, verify, and report honestly.

The source is never modified: it is re-read, its snapshot hash must match the plan, and every output goes
into a new directory. Apply is offline - it uses the lock captured in the plan.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcat.migration import rules
from mcat.migration.plan import namespace_guesses, snapshot_hash, summarize
from mcat.migration.transform import (
    Change,
    TransformError,
    make_id_mapper,
    migrate_data_path,
    quest_dependency_edges,
    transform_js,
    transform_json,
    transform_snbt,
)
from mcat.pack.build import write_packwiz
from mcat.parsers.inventory import VANILLA_NS, classify, extract_ids, instance_path
from mcat.parsers.packs import parse_pack
from mcat.parsers.source import PackSource
from mcat.reporting import Finding, Result, Severity, Status, VerificationState
from mcat.security.archive import ensure_fresh_output, normalize_member
from mcat.validation.validate import validate_path

TEXT_LIMIT = 16 * 1024**2


class PlanError(ValueError):
    pass


def load_plan(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise PlanError(f"cannot read plan: {e}") from e
    if plan.get("plan_version") != 1:
        raise PlanError("unsupported plan_version")
    for key in ("source", "target", "mods", "id_map", "decisions"):
        if key not in plan:
            raise PlanError(f"plan missing {key!r}")
    bad = {
        k: v
        for k, v in plan["id_map"].items()
        if not (isinstance(k, str) and isinstance(v, str) and ":" in k and ":" in v)
    }
    if bad:
        raise PlanError(f"id_map entries must be 'namespace:path' strings: {list(bad)[:5]}")
    return plan


def apply_plan(plan_path: Path, out: Path) -> Result:
    r = Result("migrate.apply", summary=str(out))
    try:
        plan = load_plan(plan_path)
    except PlanError as e:
        r.fail(str(e))
        return r
    source = Path(plan["source"]["path"])
    if not source.exists():
        r.fail(f"source pack not found: {source}")
        return r
    if snapshot_hash(source) != plan["source"]["sha256"]:
        r.fail("source pack changed since the plan was made; re-run `mcat migrate plan`")
        return r
    if plan.get("lock") is None:
        r.block("plan has no resolved target mod set (see resolution_problems); fix those and re-plan")
        return r
    out = ensure_fresh_output(out)
    src_mc, tgt_mc = plan["source"]["minecraft"], plan["target"]["minecraft"]
    id_map = {**plan.get("vanilla_id_map", {}), **plan["id_map"]}  # user mappings win
    mapper = make_id_mapper(id_map)
    flags = plan.get("transforms", {})
    changes: list[Change] = []
    diffs: list[str] = []
    content: dict[str, bytes] = {}
    preserved, failed_files, unmigrated = [], [], []
    quest_integrity: list[dict[str, Any]] = []
    unresolved: dict[str, list[str]] = {}  # id -> files still referencing a namespace whose mod is blocked
    blocked_ns = {ns for m in plan["mods"] if m["state"] == "blocked" for ns in m.get("affected_namespaces", [])}
    available_ns = {ns for m in plan["mods"] if m["state"] != "blocked" for ns in m.get("affected_namespaces", [])}
    available_ns |= (
        {ns for m in plan["lock"]["mods"] for ns in namespace_guesses(m["slug"])} if plan.get("lock") else set()
    )
    unknown_ns: dict[str, list[str]] = {}  # namespaces no target mod is known to provide (unverified)

    with PackSource(source) as src:
        pack = parse_pack(src)
        mods_by_path = {m["path"]: m for m in plan["mods"]}
        for name in src.names():
            ipath = instance_path(name, pack.override_roots)
            cat = classify(ipath)
            if cat == "pack_metadata" and ipath == name:
                continue
            if cat == "mods":
                m = mods_by_path.get(ipath)
                if m and m["state"] == "blocked" and name.endswith(".jar"):
                    dest = out / "unmigrated" / normalize_member(ipath)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(src.read(name, 1024**3))
                    unmigrated.append(ipath)
                continue  # target mods come from the lock, never from the old jars
            data = src.read(name, 1024**3)
            new_path, note = (
                migrate_data_path(ipath, src_mc, tgt_mc) if flags.get("datapack_folder_singular") else (ipath, None)
            )
            if note:
                changes.append(Change(ipath, "path", ipath, new_path, note))
            new_data = data
            if len(data) <= TEXT_LIMIT and ipath.endswith((".json", ".snbt", ".js")):
                text = data.decode("utf-8", errors="strict") if _is_utf8(data) else None
                if text is not None:
                    try:
                        new_text, ch = _transform(ipath, cat, text, mapper, src_mc, tgt_mc, flags)
                    except TransformError as e:
                        failed_files.append({"file": ipath, "error": str(e)})
                        new_text, ch = text, []
                    if cat == "ftbquests" and ipath.endswith(".snbt") and "/chapters/" in ipath and ch:
                        before, after = quest_dependency_edges(text), quest_dependency_edges(new_text)
                        quest_integrity.append({"file": ipath, "edges": len(before), "preserved": before == after})
                        if before != after:
                            failed_files.append({"file": ipath, "error": "quest dependency graph changed"})
                            new_text, ch = text, []
                    ids, _ = extract_ids(ipath, new_text)
                    for ident in ids:
                        ns = ident.split(":", 1)[0]
                        if ns in blocked_ns:
                            bucket = unresolved
                        elif ns not in available_ns and ns not in VANILLA_NS and not _local_namespace(ns, src.names()):
                            bucket = unknown_ns
                        else:
                            continue
                        files = bucket.setdefault(ident, [])
                        if new_path not in files and len(files) < 20:
                            files.append(new_path)
                    if ch:
                        changes.extend(ch)
                        diffs.append(
                            "".join(
                                difflib.unified_diff(
                                    text.splitlines(keepends=True),
                                    new_text.splitlines(keepends=True),
                                    f"a/{ipath}",
                                    f"b/{new_path}",
                                )
                            )
                        )
                        new_data = new_text.encode()
            else:
                preserved.append(ipath)
            if new_path in content:
                r.warnings.append(f"{new_path} provided by several override folders; first kept")
                continue
            content[new_path] = new_data

    notes = write_packwiz(plan["lock"], out / "pack", extra_files=content)
    (out / "changes.diff").write_text("\n".join(d for d in diffs if d))
    verify = validate_path(out / "pack")
    report = _report(plan, changes, failed_files, preserved, unmigrated, quest_integrity, verify, notes, unresolved)
    report["unknown_namespace_references"] = unknown_ns
    (out / "migration-report.json").write_text(json.dumps(report, indent=2) + "\n")
    r.artifacts += [str(out / "pack"), str(out / "migration-report.json"), str(out / "changes.diff")]
    if unmigrated:
        r.artifacts.append(str(out / "unmigrated"))
    r.data = {k: report[k] for k in ("overall", "counts", "mechanics", "quest_integrity", "failed_files")}
    r.data["unresolved_reference_count"] = len(unresolved)
    r.data["unknown_namespace_reference_count"] = len(unknown_ns)
    if unknown_ns:
        r.add(
            Finding(
                "unknown-namespace-references",
                f"{len(unknown_ns)} identifier(s) use namespaces no target mod is known to provide: "
                + ", ".join(sorted({i.split(":")[0] for i in unknown_ns})[:10]),
                Severity.WARNING,
                state=VerificationState.UNVERIFIED,
            )
        )
    if unresolved:
        r.add(
            Finding(
                "unresolved-references",
                f"{len(unresolved)} identifier(s) still reference blocked mods "
                "(see migration-report.json unresolved_references); map them in id_map or accept the loss",
                Severity.ERROR,
                state=VerificationState.BLOCKED,
            )
        )
    for m in report["mods"]:
        if m["state"] in ("blocked", "failed"):
            r.add(
                Finding(
                    "mod-" + m["state"],
                    f"{m['name'] or m['path']}: {m['reason']}",
                    Severity.ERROR,
                    state=VerificationState(m["state"]),
                )
            )
    for x in report["mechanics"]:
        r.add(
            Finding(
                "mechanic",
                f"{x['name']}: {x['reason']}",
                Severity.ERROR if x["state"] == "blocked" else Severity.WARNING,
                state=VerificationState(x["state"]),
            )
        )
    for f in failed_files:
        r.add(
            Finding(
                "transform-failed",
                f"{f['file']}: {f['error']} (copied unchanged)",
                Severity.WARNING,
                state=VerificationState.FAILED,
            )
        )
    r.findings.extend(v for v in verify.findings if v.severity in (Severity.ERROR, Severity.CRITICAL))
    overall = report["overall"]["state"]
    r.summary = f"migration {overall}: {len(changes)} change(s), {len(unmigrated)} unmigrated jar(s)"
    if overall == "blocked":
        r.status = Status.BLOCKED
        r.blockers = report["overall"]["blockers"]
    elif verify.status == Status.FAILED:
        r.status = Status.FAILED
    else:
        r.status = Status.PARTIAL  # never "success": gameplay and progression are not runtime-verified
    return r


def _local_namespace(ns: str, names: list[str]) -> bool:
    """True when the pack itself defines content in this namespace (kubejs/datapack assets or data)."""
    return any(f"/assets/{ns}/" in f"/{n}" or f"/data/{ns}/" in f"/{n}" for n in names)


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _transform(
    path: str, cat: str, text: str, mapper: Any, src_mc: str | None, tgt_mc: str, flags: dict[str, Any]
) -> tuple[str, list[Change]]:
    if path.endswith(".json"):
        return transform_json(text, path, mapper, source_mc=src_mc, target_mc=tgt_mc, recipe=cat == "recipe")
    if path.endswith(".snbt"):
        return transform_snbt(text, path, mapper)
    if path.endswith(".js") and cat == "kubejs_script":
        return transform_js(
            text, path, mapper, upgrade_legacy_events=bool(flags.get("kubejs_legacy_events")) and "onEvent(" in text
        )
    return text, []


def _report(
    plan: dict[str, Any],
    changes: list[Change],
    failed: list[dict[str, Any]],
    preserved: list[str],
    unmigrated: list[str],
    quests: list[dict[str, Any]],
    verify: Result,
    notes: list[str],
    unresolved: dict[str, list[str]],
) -> dict[str, Any]:
    mods = []
    for m in plan["mods"]:
        decision = plan["decisions"].get(m["key"], {})
        state = m["state"]
        reason = m["reason"]
        if state == "blocked" and decision.get("accept_missing"):
            reason += " (user accepted dropping this mod; its content references remain unresolved)"
        mods.append(
            {
                "path": m["path"],
                "name": m["name"],
                "action": m["action"],
                "state": state,
                "reason": reason,
                "target": m.get("target"),
                "progression_critical": m.get("progression_critical", False),
            }
        )
    mechanics = [dict(x) for x in plan.get("mechanics", [])]
    if quests and all(q["preserved"] for q in quests):
        mechanics.append(
            {
                "name": "Quest dependency graph",
                "state": "verified",
                "critical": True,
                "reason": f"{sum(q['edges'] for q in quests)} dependency edges identical after transform "
                "(static check; quest completion is not play-tested)",
            }
        )
    overall = summarize({**plan, "mechanics": plan.get("mechanics", [])})
    if verify.status == Status.FAILED:
        overall["blockers"] = [*overall["blockers"], "target pack failed structural validation"]
        overall["state"] = "failed"
    counts: dict[str, int] = {}
    for m in mods:
        counts[m["state"]] = counts.get(m["state"], 0) + 1
    return {
        "overall": {**overall, "complete": False},
        "counts": counts,
        "mods": mods,
        "mechanics": mechanics,
        "changes": [asdict(c) for c in changes],
        "quest_integrity": quests,
        "failed_files": failed,
        "preserved_unparsed": preserved[:500],
        "unmigrated_jars": unmigrated,
        "notes": [
            *notes,
            "vanilla id renames applied: " + ", ".join(f"{k}->{v}" for k, v in plan.get("vanilla_id_map", {}).items())
            if plan.get("vanilla_id_map")
            else "no vanilla id renames applied",
            "runtime verification (server/client launch, quest completion) has NOT been performed",
        ],
        "common_tag_mapping": "forge:* -> c:* tags are never mapped automatically; categories that usually "
        "map 1:1: " + ", ".join(sorted(rules.COMMON_TAG_CATEGORIES)) + " (add to id_map)",
        "validation": {"status": verify.status.value, "summary": verify.summary},
        "unresolved_references": unresolved,
    }
