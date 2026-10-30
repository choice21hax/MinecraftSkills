"""CLI: resolve, pack create, validate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mcat.pack.build import (
    PackBuildError,
    packwiz_refresh,
    problems_as_dicts,
    resolve_spec,
    write_mrpack,
    write_packwiz,
)
from mcat.providers.curseforge import CurseForgeUnavailable
from mcat.providers.http import NetworkError
from mcat.reporting import Finding, Result, Severity, VerificationState
from mcat.resolver.spec import SpecError, load_spec
from mcat.security.archive import UnsafeArchiveError
from mcat.validation.validate import validate_path


def _resolve(spec_path: Path, r: Result) -> tuple[Any, dict[str, Any]] | None:
    try:
        spec = load_spec(spec_path)
        res, lock, warnings = resolve_spec(spec)
    except (SpecError, OSError) as e:
        r.fail(f"invalid spec: {e}")
        return None
    except CurseForgeUnavailable as e:
        r.block(str(e))
        return None
    except PackBuildError as e:
        r.block(str(e))
        return None
    except NetworkError as e:
        r.fail(f"provider error: {e}")
        return None
    r.warnings.extend(warnings)
    r.data["target"] = lock["target"]
    r.data["java"] = lock["java"]
    r.data["selected"] = [
        {k: m[k] for k in ("slug", "provider", "version_number", "version_id", "side", "reasons")} for m in lock["mods"]
    ]
    r.data["cycles"] = lock["graph"]["cycles"]
    r.data["optional_skipped"] = res.optional_skipped
    if not res.ok:
        r.data["problems"] = problems_as_dicts(res)
        for p in res.problems:
            r.add(
                Finding(
                    f"resolve-{p.kind}",
                    p.message,
                    Severity.ERROR,
                    state=VerificationState.BLOCKED,
                    subject=p.project,
                    fix=("alternatives that exist: " + ", ".join(p.alternatives[:8])) if p.alternatives else None,
                )
            )
        r.block("dependency graph is unsatisfiable for this target; see findings (no versions were guessed)")
        return None
    for m in lock["mods"]:
        if m["file"] and not m["file"].get("download_allowed", True):
            r.add(
                Finding(
                    "restricted-download",
                    f"{m['slug']}: CurseForge author disallows third-party distribution; manual download required",
                    Severity.WARNING,
                )
            )
    if lock["graph"]["cycles"]:
        r.warnings.append(f"dependency cycles present (allowed, but review): {lock['graph']['cycles']}")
    return res, lock


def resolve_cmd(args: argparse.Namespace) -> Result:
    r = Result("resolve", summary=args.spec)
    got = _resolve(Path(args.spec), r)
    if got is None:
        return r
    _, lock = got
    if args.output:
        out = Path(args.output)
        if out.exists() and not args.overwrite:
            r.fail(f"{out} exists (pass --overwrite to replace the lockfile)")
            return r
        out.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
        r.artifacts.append(str(out))
    else:
        r.data["lock"] = lock
    r.summary = f"resolved {len(lock['mods'])} mods for {lock['target']['minecraft']}/{lock['target']['loader']}"
    return r


def pack_create(args: argparse.Namespace) -> Result:
    r = Result("pack.create", summary=args.spec)
    got = _resolve(Path(args.spec), r)
    if got is None:
        return r
    _, lock = got
    try:
        notes = write_packwiz(lock, Path(args.output))
    except (PackBuildError, UnsafeArchiveError) as e:
        r.fail(str(e))
        return r
    r.artifacts.append(str(Path(args.output).resolve()))
    for n in notes:
        r.add(Finding("restricted-download", n, Severity.WARNING))
    ok, log = packwiz_refresh(Path(args.output))
    r.data["packwiz_refresh"] = {"ran": ok is not None, "ok": ok, "log": log}
    if ok is False:
        r.fail("packwiz refresh rejected the generated pack")
        return r
    if args.mrpack:
        try:
            excluded = write_mrpack(lock, Path(args.mrpack))
            r.artifacts.append(str(Path(args.mrpack).resolve()))
            for x in excluded:
                r.add(Finding("mrpack-excluded", x, Severity.WARNING))
                r.degrade()
        except (PackBuildError, FileExistsError) as e:
            r.fail(str(e))
            return r
    v = validate_path(Path(args.output))
    # provider compatibility was established by the resolver itself, so drop validate's offline caveat
    r.findings.extend(f for f in v.findings if f.code != "provider-compat-unverified")
    if v.status.value == "failed":
        r.fail("generated pack failed validation")
    r.summary = f"created {lock['pack']['name']} ({len(lock['mods'])} mods) at {args.output}"
    r.warnings.append("mods were resolved from provider metadata only; nothing was downloaded or launched")
    return r


def validate_cmd(args: argparse.Namespace) -> Result:
    return validate_path(Path(args.path), online=args.online, server=args.server)


def register(sub: argparse._SubParsersAction, cmd: Any) -> None:
    p = cmd(sub, "resolve", resolve_cmd, "resolve a pack spec into an exact, hashed lockfile")
    p.add_argument("spec", help="pack spec YAML")
    p.add_argument("--output", "-o", help="write the lockfile here (default: print in result data)")
    p.add_argument("--overwrite", action="store_true")

    pack = sub.add_parser("pack", help="create and export modpacks")
    psub = pack.add_subparsers(dest="pack_command", required=True, metavar="ACTION")
    p = cmd(psub, "create", pack_create, "resolve a spec and write a packwiz-format pack (+ optional .mrpack)")
    p.add_argument("spec")
    p.add_argument("--output", "-o", required=True, help="new or empty output directory")
    p.add_argument("--mrpack", help="also export a Modrinth .mrpack to this path")

    p = cmd(sub, "validate", validate_cmd, "validate a pack directory or archive")
    p.add_argument("path")
    p.add_argument("--online", action="store_true", help="cross-check entries against Modrinth metadata")
    p.add_argument("--server", action="store_true", help="validate as a dedicated-server distribution")
