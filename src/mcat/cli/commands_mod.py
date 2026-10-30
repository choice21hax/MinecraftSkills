"""CLI: mod versions / init / build, and debug."""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcat import moddev
from mcat.loaders import LoaderCatalog
from mcat.models import LOADERS
from mcat.providers.http import NetworkError
from mcat.reporting import Evidence, Finding, Result, Severity, VerificationState
from mcat.security.archive import UnsafeArchiveError


def mod_versions(args: argparse.Namespace) -> Result:
    r = Result("mod.versions", summary=f"{args.loader} {args.minecraft}")
    try:
        s = LoaderCatalog().support(args.loader, args.minecraft)
    except NetworkError as e:
        r.fail(str(e))
        return r
    fam = moddev.family_for(args.loader, args.minecraft)
    r.data = {
        "loader": args.loader,
        "minecraft": args.minecraft,
        "upstream_supported": s.supported,
        "recommended_loader_version": s.recommended,
        "loader_versions": s.loader_versions[:10],
        "java": s.java,
        "evidence": s.evidence,
        "template": {
            "family": fam.name if fam else None,
            "verified": (args.loader, args.minecraft) in moddev.VERIFIED_BUILDS,
            "verification": moddev.VERIFIED_BUILDS.get((args.loader, args.minecraft)),
            "runtime_verified": moddev.RUNTIME_VERIFIED.get((args.loader, args.minecraft)),
        },
        "languages": ["java"],
    }
    if not s.supported:
        r.block(f"{args.loader} has no release for Minecraft {args.minecraft}")
    elif fam is None:
        r.block(f"upstream supports it, but MCAT has no template for {args.loader} {args.minecraft}")
    elif not r.data["template"]["verified"]:
        r.degrade("template family applies but has not been build-verified for this exact version")
    return r


def mod_init(args: argparse.Namespace) -> Result:
    r = Result("mod.init", summary=f"{args.loader} {args.minecraft}")
    if args.language != "java":
        r.block(f"{args.language} templates are not verified for any loader yet; use --language java")
        return r
    try:
        plan = moddev.plan_scaffold(args.loader, args.minecraft, args.mod_id, args.package, mod_name=args.name)
    except moddev.ScaffoldError as e:
        r.block(str(e))
        return r
    except NetworkError as e:
        r.fail(str(e))
        return r
    if not plan.verified and not args.allow_unverified:
        r.block(
            f"the {plan.family} template has not been build-verified for {args.loader} {args.minecraft}; "
            "re-run with --allow-unverified and then `mcat mod build` to verify it"
        )
        r.data = asdict(plan)
        return r
    try:
        files = moddev.write_project(plan, Path(args.output), examples=args.with_examples)
    except (moddev.ScaffoldError, UnsafeArchiveError) as e:
        r.fail(str(e))
        return r
    r.data = asdict(plan) | {"files": [str(f.relative_to(Path(args.output).resolve())) for f in files]}
    r.artifacts.append(str(Path(args.output).resolve()))
    r.add(
        Finding(
            "scaffold",
            "project generated; compilation not yet run",
            Severity.INFO,
            state=VerificationState.UNVERIFIED,
            evidence=[Evidence(e) for e in plan.evidence],
        )
    )
    r.degrade()
    r.summary = f"{args.loader} {args.minecraft} project at {args.output} (next: mcat mod build --allow-build)"
    return r


def mod_build(args: argparse.Namespace) -> Result:
    r = Result("mod.build", summary=args.project)
    project = Path(args.project).resolve()
    if not (project / "build.gradle").is_file() and not (project / "build.gradle.kts").is_file():
        r.fail(f"{project} is not a Gradle project")
        return r
    if not args.allow_build:
        r.block(
            "building runs the project's Gradle scripts and downloads the loader toolchain; "
            "re-run with --allow-build to authorise it"
        )
        return r
    try:
        out = moddev.run_build(project, task=args.task, timeout=args.timeout)
    except moddev.ScaffoldError as e:
        r.block(str(e))
        return r
    except subprocess.TimeoutExpired:
        r.fail(f"build exceeded {args.timeout}s")
        return r
    r.data = asdict(out)
    r.artifacts.extend(out.artifacts)
    if out.ok:
        r.add(
            Finding(
                "compiled",
                f"`{out.gradle} {args.task}` succeeded",
                Severity.INFO,
                state=VerificationState.VERIFIED,
                evidence=[Evidence("gradle", f"exit 0, {len(out.artifacts)} jar(s)")],
            )
        )
        r.warnings.append("compilation verified only: the mod has not been loaded in a game instance")
        r.summary = "build succeeded"
    else:
        for err in out.errors:
            loc = f"{err['file']}:{err['line']}" if err["file"] else "gradle"
            r.add(
                Finding("build-error", err["message"], Severity.ERROR, confidence="confirmed", evidence=[Evidence(loc)])
            )
        r.fail(f"build failed (exit {out.returncode})")
    return r


def debug_cmd(args: argparse.Namespace) -> Result:
    from mcat.debug.analyze import analyze_file, plan_repairs, quarantine
    from mcat.validation.validate import validate_path

    r = analyze_file(Path(args.log), pack=Path(args.pack) if args.pack else None)
    if not args.pack:
        if args.apply:
            r.block("--apply needs --pack <instance dir> so repairs stay inside a known directory")
        return r
    pack = Path(args.pack)
    if not pack.is_dir():
        r.block("--pack must be an instance directory (archives are never modified)")
        return r
    repairs = plan_repairs(pack)
    r.data["proposed_repairs"] = [
        {"file": f, "reason": why, "action": "move to mcat-quarantine/"} for f, why in repairs
    ]
    if args.apply and repairs:
        moved = quarantine(pack, [f for f, _ in repairs])
        r.data["applied_repairs"] = moved
        r.artifacts.append(str(pack / "mcat-quarantine"))
        v = validate_path(pack)
        r.data["revalidation"] = {
            "status": v.status.value,
            "summary": v.summary,
            "errors": [f.message for f in v.findings if f.severity.value == "error"],
        }
        r.warnings.append(
            "repairs moved files into mcat-quarantine/ (move them back to undo); relaunch the game "
            "and run `mcat debug` on the new log to confirm the original error is gone"
        )
    elif repairs:
        r.warnings.append(f"{len(repairs)} safe repair(s) proposed; re-run with --apply to quarantine them")
    return r


def register(sub: argparse._SubParsersAction, cmd: Any) -> None:
    mod = sub.add_parser("mod", help="scaffold and build Fabric/NeoForge/Forge mods")
    msub = mod.add_subparsers(dest="mod_command", required=True, metavar="ACTION")
    p = cmd(msub, "versions", mod_versions, "report loader/Java/template support for a game version")
    p.add_argument("--loader", choices=LOADERS, required=True)
    p.add_argument("--minecraft", required=True)

    p = cmd(msub, "init", mod_init, "scaffold a loader-specific mod project")
    p.add_argument("--loader", choices=LOADERS, required=True)
    p.add_argument("--minecraft", required=True)
    p.add_argument("--mod-id", required=True)
    p.add_argument("--package", required=True, help="Java package, e.g. com.example.mymod")
    p.add_argument("--name", help="display name")
    p.add_argument("--output", "-o", required=True, help="new or empty directory")
    p.add_argument("--language", choices=["java", "kotlin"], default="java")
    p.add_argument("--with-examples", action="store_true", help="include example block + item registration")
    p.add_argument(
        "--allow-unverified",
        action="store_true",
        help="scaffold even if this exact version's template was never build-verified",
    )

    p = cmd(msub, "build", mod_build, "run the Gradle build and parse compiler errors")
    p.add_argument("project")
    p.add_argument("--task", default="build")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--allow-build", action="store_true", help="authorise running Gradle (executes build scripts)")

    p = cmd(sub, "debug", debug_cmd, "analyze a crash report or latest.log")
    p.add_argument("log")
    p.add_argument("--pack", help="instance/pack directory to cross-check findings against")
    p.add_argument(
        "--apply",
        action="store_true",
        help="apply safe repairs (quarantine wrong-loader/duplicate jars) after authorization",
    )
