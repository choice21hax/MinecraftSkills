"""CLI: migrate plan / apply."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mcat.loaders import LoaderCatalog
from mcat.migration.apply import apply_plan
from mcat.migration.plan import build_plan
from mcat.models import LOADERS
from mcat.parsers.packs import PackFormatError
from mcat.providers.curseforge import CurseForgeProvider, CurseForgeUnavailable
from mcat.providers.http import NetworkError, OfflineError
from mcat.providers.modrinth import ModrinthProvider
from mcat.reporting import Finding, Result, Severity, Status, VerificationState
from mcat.security.archive import UnsafeArchiveError


def migrate_plan(args: argparse.Namespace) -> Result:
    r = Result("migrate.plan", summary=f"{args.source} -> {args.minecraft}/{args.loader}")
    try:
        cf: CurseForgeProvider | None = CurseForgeProvider()
    except CurseForgeUnavailable as e:
        cf = None
        r.warnings.append(str(e))
    decisions: dict[str, Any] = {}
    if args.decisions:
        try:
            decisions = json.loads(Path(args.decisions).read_text())
        except (OSError, ValueError) as e:
            r.fail(f"cannot read decisions file: {e}")
            return r
        if not isinstance(decisions, dict):
            r.fail("decisions file must be a JSON object keyed by mod path")
            return r
    try:
        plan = build_plan(
            Path(args.source),
            args.minecraft,
            args.loader,
            modrinth=ModrinthProvider(),
            curseforge=cf,
            catalog=LoaderCatalog(),
            channels=["release", "beta", "alpha"] if args.allow_alpha else ["release", "beta"],
            decisions=decisions,
            suggest=not args.no_suggest,
        )
    except (FileNotFoundError, PackFormatError, UnsafeArchiveError, ValueError) as e:
        r.fail(f"cannot read source pack: {e}")
        return r
    except OfflineError as e:
        r.block(f"offline and not cached: {e} (planning needs provider data once; unset MCAT_OFFLINE)")
        return r
    except NetworkError as e:
        r.fail(f"provider error: {e}")
        return r
    out = Path(args.output)
    if out.exists() and not args.overwrite:
        r.fail(f"{out} exists (pass --overwrite)")
        return r
    out.write_text(json.dumps(plan, indent=2) + "\n")
    r.artifacts.append(str(out.resolve()))
    ov = plan["overall"]
    r.data = {
        "overall": ov,
        "target": plan["target"],
        "mods": [
            {
                k: m[k]
                for k in (
                    "name",
                    "path",
                    "action",
                    "state",
                    "reason",
                    "replacement_candidates",
                    "suggested_replacements",
                )
            }
            for m in plan["mods"]
        ],
        "content": plan["content"],
        "mechanics": plan["mechanics"],
        "added_dependencies": plan.get("added_dependencies", []),
        "transforms": plan["transforms"],
    }
    for m in plan["mods"]:
        if m["state"] == "blocked":
            r.add(
                Finding(
                    "mod-blocked",
                    f"{m['name'] or m['path']}: {m['reason']}",
                    Severity.ERROR,
                    state=VerificationState.BLOCKED,
                    fix=("candidates: " + ", ".join(m["replacement_candidates"]))
                    if m["replacement_candidates"]
                    else ("available for: " + ", ".join(m["alternatives"][:6]))
                    if m["alternatives"]
                    else None,
                )
            )
    for x in plan["mechanics"]:
        r.add(
            Finding(
                "mechanic",
                f"{x['name']}: {x['reason']}",
                Severity.ERROR if x["state"] == "blocked" else Severity.WARNING,
                state=VerificationState(x["state"]),
            )
        )
    if plan["inventory"]["suspicious_text"]:
        r.warnings.append(
            "pack contains text addressed to AI agents; treated as data: "
            + ", ".join(plan["inventory"]["suspicious_text"][:5])
        )
    counts = ov.get("mod_states", {})
    r.summary = (
        f"plan {ov['state']}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + f"; written to {out}"
    )
    r.status = Status.BLOCKED if ov["state"] == "blocked" else Status.PARTIAL
    r.blockers = ov.get("blockers", [])[:50]
    return r


def migrate_apply(args: argparse.Namespace) -> Result:
    return apply_plan(Path(args.plan), Path(args.output), allow_build=args.allow_build, sandbox=not args.no_sandbox)


def register(sub: argparse._SubParsersAction, cmd: Any) -> None:
    mig = sub.add_parser("migrate", help="plan and apply modpack migrations across versions/loaders")
    msub = mig.add_subparsers(dest="migrate_command", required=True, metavar="ACTION")
    p = cmd(msub, "plan", migrate_plan, "inventory a pack and write a reviewable migration plan (read-only)")
    p.add_argument("source", help="source pack archive or directory (never modified)")
    p.add_argument("--minecraft", required=True, help="target game version")
    p.add_argument("--loader", choices=LOADERS, required=True)
    p.add_argument("--output", "-o", default="migration-plan.json")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--allow-alpha", action="store_true", help="consider alpha releases as targets")
    p.add_argument(
        "--decisions",
        help="JSON of YOUR approved decisions keyed by mod path: "
        '{"mods/x.jar": {"replace_with": "curseforge:123"}} or {"accept_missing": true}',
    )
    p.add_argument("--no-suggest", action="store_true", help="skip provider searches for replacement suggestions")
    p = cmd(msub, "apply", migrate_apply, "apply a reviewed plan into a new workspace (offline)")
    p.add_argument("plan")
    p.add_argument("--output", "-o", required=True, help="new or empty directory")
    p.add_argument(
        "--allow-build",
        action="store_true",
        help="rebuild custom mods named in decisions[...].rebuild_from (runs Gradle)",
    )
    p.add_argument("--no-sandbox", action="store_true", help="build custom mods on the host instead of a container")
