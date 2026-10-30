"""CLI: opt-in runtime verification (`mcat test server|mod|client|report`)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mcat.models import LOADERS
from mcat.reporting import Finding, Result, Severity, Status, VerificationState
from mcat.validation.runtime import RuntimeOptions, run_server_test
from mcat.validation.runtime.sandbox import Limits

CLIENT_MANUAL_CHECKS = [
    "launch the pack in your launcher with your own account and reach the title screen",
    "create a new single-player world and load into it",
    "open the inventory and a recipe viewer (if installed); check textures/models render",
    "join the server built from the same pack (`mcat test server` verifies the server side only)",
    "check shader/resource packs and keybind conflicts (Controls screen)",
]


def _options(args: argparse.Namespace) -> RuntimeOptions:
    return RuntimeOptions(
        loader=args.loader,
        minecraft=args.minecraft,
        loader_version=args.loader_version,
        pack=Path(args.pack) if getattr(args, "pack", None) else None,
        accept_eula=args.accept_eula,
        timeout=args.timeout,
        limits=Limits(memory_mb=args.memory, cpus=args.cpus, pids=args.pids),
        workspace=Path(args.workspace) if args.workspace else None,
        cleanup=args.cleanup,
        engine=args.engine,
        image=args.image,
        allow_pull=not args.no_pull,
        allow_host=args.allow_host_execution,
        java=args.java,
    )


def test_server(args: argparse.Namespace) -> Result:
    return run_server_test(_options(args))


def test_mod(args: argparse.Namespace) -> Result:
    opts = _options(args)
    opts.mods = [Path(j) for j in args.jar]
    for j in opts.mods:
        if not j.is_file():
            r = Result("test.mod")
            r.fail(f"no such jar: {j}")
            return r
    return run_server_test(opts, operation="test.mod")


def test_client(args: argparse.Namespace) -> Result:
    """Automated client launch is not offered: it would need a game account or a display server."""
    import shutil

    r = Result("test.client", summary="client checks")
    display = shutil.which("Xvfb") or shutil.which("xvfb-run")
    r.data["automated"] = {
        "launch": "not performed",
        "why": [
            "launching the official client needs the user's Microsoft account; MCAT never asks for or stores it",
            "no virtual display (Xvfb) is installed" if not display else "a virtual display exists, but see above",
        ],
    }
    r.data["manual_checks"] = CLIENT_MANUAL_CHECKS
    if args.pack:
        from mcat.validation.validate import validate_path

        v = validate_path(Path(args.pack))
        r.data["static_validation"] = v.status.value
        r.findings.extend(v.findings)
    r.add(
        Finding(
            "client-runtime-unverified",
            "client rendering, input and gameplay were not executed; use the manual checklist",
            Severity.INFO,
            state=VerificationState.UNVERIFIED,
        )
    )
    r.block("automated client launch unavailable; static checks only (see manual_checks)")
    return r


def test_report(args: argparse.Namespace) -> Result:
    """Re-read a runtime workspace and re-run the crash analyzer (feeds minecraft-debug)."""
    from mcat.debug.analyze import analyze_text

    ws = Path(args.workspace)
    r = Result("test.report", summary=str(ws))
    try:
        report: dict[str, Any] = json.loads((ws / "report.json").read_text())
    except (OSError, ValueError) as e:
        r.fail(f"no readable report.json in {ws}: {e}")
        return r
    r.data["run"] = {k: report.get(k) for k in ("operation", "status", "summary")}
    r.data["server"] = report.get("data", {}).get("server")
    r.data["timings"] = report.get("data", {}).get("timings")
    console = ws / "mcat-logs" / "console.log"
    if console.is_file():
        for f in analyze_text(console.read_text(errors="replace")):
            r.add(f)
        r.data["debug_next"] = f"mcat debug {console} --pack {ws / 'server'}"
    r.status = Status(report.get("status", "failed"))
    r.summary = f"{report.get('operation')}: {report.get('summary')}"
    return r


def _common(p: argparse.ArgumentParser, *, pack: bool = True) -> None:
    if pack:
        p.add_argument("pack", nargs="?", help="packwiz dir, .mrpack or CurseForge zip (optional)")
    p.add_argument("--loader", choices=LOADERS)
    p.add_argument("--minecraft")
    p.add_argument("--loader-version")
    p.add_argument("--accept-eula", action="store_true", help="you have read and accept https://aka.ms/MinecraftEULA")
    p.add_argument("--timeout", type=int, default=600, help="seconds for the server to start and stop")
    p.add_argument("--memory", type=int, default=4096, help="container memory limit in MiB")
    p.add_argument("--cpus", type=float, default=2.0)
    p.add_argument("--pids", type=int, default=1024, help="max processes/threads in the sandbox")
    p.add_argument("--workspace", help="new or empty directory (default: ~/.cache/mcat/runtime/<run>)")
    p.add_argument("--cleanup", action="store_true", help="delete server files after the run (keeps logs/report)")
    p.add_argument("--engine", choices=["podman", "docker"], help="container engine (default: podman, then docker)")
    p.add_argument("--image", help="container image with Java (default: eclipse-temurin:<java>-jre)")
    p.add_argument("--no-pull", action="store_true", help="never pull the container image")
    p.add_argument(
        "--allow-host-execution",
        action="store_true",
        help="DANGEROUS: run server code directly on this machine without a sandbox",
    )
    p.add_argument("--java", help="Java executable for --allow-host-execution")


def register(sub: argparse._SubParsersAction, cmd: Any) -> None:
    t = sub.add_parser("test", help="opt-in runtime verification in a sandbox (starts real servers)")
    tsub = t.add_subparsers(dest="test_command", required=True, metavar="ACTION")
    p = cmd(tsub, "server", test_server, "start a dedicated server (bare loader or a pack) and verify it")
    _common(p)
    p = cmd(tsub, "mod", test_mod, "load mod jar(s) on a dedicated server and verify they loaded")
    p.add_argument("jar", nargs="+")
    p.add_argument("--pack", help="pack providing dependencies (e.g. Fabric API)")
    _common(p, pack=False)
    p = cmd(tsub, "client", test_client, "client checks: static validation + manual checklist")
    p.add_argument("pack", nargs="?")
    p = cmd(tsub, "report", test_report, "summarise a runtime workspace and re-run crash analysis")
    p.add_argument("workspace")
