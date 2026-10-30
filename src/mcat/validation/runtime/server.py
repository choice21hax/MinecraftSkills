"""`mcat test server` / `mcat test mod`: opt-in dedicated-server smoke tests in a sandbox.

Flow: EULA gate -> target + Java -> sandbox -> workspace -> verified installer -> install (network on)
-> pack/mod files (hash-verified, on the host) -> launch (network off) -> probe -> stop -> classify.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcat import __version__
from mcat.loaders import LoaderCatalog
from mcat.parsers.jarmeta import read_jar
from mcat.parsers.packs import PackInfo, parse_pack
from mcat.parsers.source import PackSource
from mcat.providers.http import HttpClient, NetworkError
from mcat.reporting import Evidence, Finding, Result, Severity, VerificationState
from mcat.security.archive import ensure_fresh_output
from mcat.validation.runtime.provision import (
    ProvisionError,
    fetch_installer,
    install_pack,
    launch_command,
    plan_server,
)
from mcat.validation.runtime.sandbox import (
    ContainerRunner,
    HostRunner,
    Limits,
    SandboxUnavailable,
    detect_engine,
)
from mcat.validation.runtime.state import ServerMonitor, ServerState

EULA_URL = "https://aka.ms/MinecraftEULA"
SERVER_PROPERTIES = {  # only written when the pack does not ship its own server.properties
    "online-mode": "false",  # no network inside the sandbox; nobody can join anyway
    "view-distance": "4",
    "simulation-distance": "4",
    "max-players": "1",
    "enable-rcon": "false",
    "enable-query": "false",
    "motd": "MCAT runtime smoke test",
}
KEEP_ON_CLEANUP = ("logs", "crash-reports", "server.properties", "eula.txt")


@dataclass
class RuntimeOptions:
    loader: str | None = None
    minecraft: str | None = None
    loader_version: str | None = None
    pack: Path | None = None
    mods: list[Path] = field(default_factory=list)
    accept_eula: bool = False
    timeout: int = 600
    install_timeout: int = 900
    limits: Limits = field(default_factory=Limits)
    workspace: Path | None = None
    cleanup: bool = False
    engine: str | None = None
    image: str | None = None
    allow_pull: bool = True
    allow_host: bool = False
    java: str | None = None


def default_workspace(loader: str, minecraft: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))) / "mcat" / "runtime"
    return base / f"{time.strftime('%Y%m%d-%H%M%S')}-{loader}-{minecraft}-{secrets.token_hex(3)}"


def _tail(path: Path, n: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _run_logged(argv: list[str], log: Path, timeout: int, cwd: Path | None) -> int | None:
    with open(log, "w") as fh:
        try:
            p = subprocess.run(
                argv,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                cwd=cwd,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
    return p.returncode


def _write_server_files(server: Path) -> None:
    (server / "eula.txt").write_text(
        f"# accepted by the user via `mcat test ... --accept-eula` ({EULA_URL})\neula=true\n"
    )
    props = server / "server.properties"
    if not props.exists():
        props.write_text("".join(f"{k}={v}\n" for k, v in SERVER_PROPERTIES.items()))


def _drive(
    argv: list[str], cwd: Path | None, console: Path, timeout: int, monitor: ServerMonitor, kill: Any
) -> int | None:
    """Run the server, probe it once 'Done' is logged, stop it, and return the exit code (None: killed)."""
    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    probe_sent = stop_sent = False
    stop_deadline: float | None = None
    with open(console, "w") as log:
        while True:
            now = time.monotonic()
            if now > deadline or (stop_deadline and now > stop_deadline):
                monitor.timed_out()
                kill()
                proc.kill()
                break
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                break
            log.write(line)
            state = monitor.feed(line)
            assert proc.stdin is not None
            try:
                if state is ServerState.DONE_LOGGED and not probe_sent:
                    monitor.nonce = f"mcat-probe-{secrets.token_hex(8)}"  # generated after 'Done'
                    proc.stdin.write(f"say {monitor.nonce}\n")
                    proc.stdin.flush()
                    probe_sent = True
                elif state is ServerState.READY and not stop_sent:
                    proc.stdin.write("stop\n")
                    proc.stdin.flush()
                    stop_sent = True
                    stop_deadline = time.monotonic() + 120
                elif state in (ServerState.CRASHED, ServerState.BLOCKED) and not stop_sent:
                    proc.stdin.write("stop\n")
                    proc.stdin.flush()
                    stop_sent = True
                    stop_deadline = time.monotonic() + 60
            except (BrokenPipeError, OSError):
                pass
    try:
        return proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        kill()
        proc.kill()
        return None


def run_server_test(opts: RuntimeOptions, *, operation: str = "test.server") -> Result:
    r = Result(operation)
    timings: dict[str, float] = {}
    t0 = time.monotonic()
    if not opts.accept_eula:
        r.block(
            "a Minecraft server only starts after the Minecraft EULA is accepted. Read "
            f"{EULA_URL} and re-run with --accept-eula if you agree; MCAT never accepts it for you"
        )
        return r

    # target: from the pack when given, otherwise from flags
    pack: PackInfo | None = None
    src: PackSource | None = None
    if opts.pack:
        try:
            src = PackSource(opts.pack)
            pack = parse_pack(src)
        except Exception as e:
            r.fail(f"cannot read pack: {e}")
            return r
        for attr in ("minecraft", "loader", "loader_version"):
            mine, theirs = getattr(opts, attr), getattr(pack, attr)
            if mine and theirs and mine != theirs:
                r.fail(f"--{attr.replace('_', '-')} {mine} disagrees with the pack's {theirs}")
                return r
            setattr(opts, attr, mine or theirs)
    if not (opts.loader and opts.minecraft):
        r.fail("give --loader and --minecraft (or a pack that declares them)")
        return r
    loader, mc = opts.loader, opts.minecraft
    catalog = LoaderCatalog()
    try:
        support = catalog.support(loader, mc)
    except NetworkError as e:
        r.fail(f"cannot read loader metadata: {e}")
        return r
    if not support.supported:
        r.block(f"{loader} does not support Minecraft {mc} ({support.evidence})")
        return r
    lv = opts.loader_version or support.recommended or (support.loader_versions[0] if support.loader_versions else None)
    if not lv or lv not in support.loader_versions:
        r.block(f"{loader} {lv} is not published for Minecraft {mc}")
        return r
    java_required = support.java
    r.data["target"] = {"loader": loader, "minecraft": mc, "loader_version": lv, "java_required": java_required}

    # sandbox selection: never silently fall back to the host
    runner: ContainerRunner | HostRunner
    if opts.allow_host:
        runner = HostRunner(opts.java or "java")
        r.data["sandbox"] = {"engine": "host", "isolation": "none (explicitly authorised with --allow-host-execution)"}
        r.warnings.append("running third-party server code directly on the host (explicitly authorised)")
    else:
        try:
            info = detect_engine(opts.engine)
        except SandboxUnavailable as e:
            r.block(
                f"no container sandbox available ({e}). MCAT does not run server jars on the host unless you "
                "explicitly authorise it with --allow-host-execution"
            )
            return r
        image = opts.image or f"eclipse-temurin:{java_required or 25}-jre"
        runner = ContainerRunner(info, image, opts.limits)
        r.warnings.extend(info.warnings)
        r.data["sandbox"] = {
            "engine": info.evidence,
            "image": image,
            "limits": opts.limits.__dict__,
            "network": "on for loader installation only; off while the server runs",
            "note": "containers reduce risk but are not a perfect security boundary",
        }

    ws = opts.workspace or default_workspace(loader, mc)
    try:
        ws = ensure_fresh_output(ws)
    except Exception as e:
        r.fail(f"workspace: {e}")
        return r
    server, logs = ws / "server", ws / "mcat-logs"
    for d in (server, logs, ws / "home"):
        d.mkdir(parents=True)
    r.data["workspace"] = str(ws)
    name = f"mcat-{secrets.token_hex(6)}"

    def kill() -> None:
        runner.kill(name)

    def host_cwd(rel: str) -> Path | None:
        return (ws / rel) if isinstance(runner, HostRunner) else None

    try:
        if isinstance(runner, ContainerRunner):
            try:
                digest, pulled = runner.ensure_image(allow_pull=opts.allow_pull)
            except SandboxUnavailable as e:
                r.block(str(e))
                return r
            r.data["sandbox"]["image_digest"] = digest
            if pulled:
                r.artifacts.append(f"pulled container image {digest}")
        major = runner.java_major(ws, name + "-java")
        r.data["java"] = {"major": major, "required": java_required}
        if java_required and (major is None or major < java_required):
            r.block(f"Java {java_required}+ required for Minecraft {mc}; the runtime has {major}")
            return r

        http = HttpClient()
        t = time.monotonic()
        plan = plan_server(loader, mc, lv, http)
        fetch_installer(plan, http, ws / "installer.jar")
        code = _run_logged(
            runner.argv(ws, plan.install_command, name=name + "-install", network=True, workdir="/work/server"),
            logs / "install.log",
            opts.install_timeout,
            host_cwd("server"),
        )
        timings["install_s"] = round(time.monotonic() - t, 1)
        r.data["provision"] = plan.to_dict()
        if code != 0:
            kill()
            r.fail(f"loader installer failed (exit {code}); last lines:\n{_tail(logs / 'install.log', 15)}")
            return r
        argv_launch = launch_command(loader, server, opts.limits.memory_mb)

        # pack + extra mods
        t = time.monotonic()
        if pack is not None and src is not None:
            cf = None
            if any(e.provider == "curseforge" for e in pack.entries):
                try:
                    from mcat.providers.curseforge import CurseForgeProvider

                    cf = CurseForgeProvider().files_batch
                except Exception as e:
                    r.warnings.append(str(e))
            inst = install_pack(src, pack, server, http, curseforge=cf)
            r.data["pack_install"] = inst.__dict__
            if inst.failed or inst.blocked:
                for item in inst.blocked:
                    r.add(
                        Finding(
                            "runtime-file-blocked",
                            f"{item['path']}: {item['reason']}",
                            Severity.ERROR,
                            state=VerificationState.BLOCKED,
                        )
                    )
                for item in inst.failed:
                    r.add(
                        Finding(
                            "runtime-file-failed",
                            f"{item['path']}: {item['reason']}",
                            Severity.ERROR,
                            state=VerificationState.FAILED,
                        )
                    )
                r.block("not all server files could be provisioned; the server was not started with an incomplete pack")
                return r
        expected_ids: set[str] = set()
        for jar in opts.mods:
            data = jar.read_bytes()
            mods, _ = read_jar(data)
            expected_ids |= {m.mod_id for m in mods}
            dest = server / "mods" / jar.name
            dest.parent.mkdir(exist_ok=True)
            with open(dest, "xb") as fh:
                fh.write(data)
        timings["files_s"] = round(time.monotonic() - t, 1)
        _write_server_files(server)

        # run (network off)
        monitor = ServerMonitor()
        t = time.monotonic()
        exit_code = _drive(
            runner.argv(ws, argv_launch, name=name, network=False, workdir="/work/server"),
            host_cwd("server"),
            logs / "console.log",
            opts.timeout,
            monitor,
            kill,
        )
        monitor.exited(exit_code)
        timings["server_s"] = round(time.monotonic() - t, 1)
        r.data["launch_command"] = argv_launch
        r.data["exit_code"] = exit_code
        r.data["server"] = monitor.to_dict()
        _classify(r, monitor, server, logs, expected_ids, loader)
    except (ProvisionError, NetworkError, OSError) as e:
        kill()
        r.fail(f"{type(e).__name__}: {e}")
    finally:
        if src is not None:
            src.close()
        timings["total_s"] = round(time.monotonic() - t0, 1)
        r.data["timings"] = timings
        final = (r.data.get("server") or {}).get("final_state", "not started")
        r.summary = r.summary or f"{loader} {mc} ({lv}){' + pack' if pack else ''}: server {final}"
        (ws / "report.json").write_text(
            json.dumps({"mcat": __version__, **r.to_dict()}, indent=2, sort_keys=True, default=str) + "\n"
        )
        r.artifacts.append(str(ws / "report.json"))
        if opts.cleanup:
            _cleanup(ws)
    return r


_FML_FOUND = re.compile(r"Found valid mod file \S+ with \{(?P<ids>[^}]*)\} mods")


def _mod_ids_in_log(text: str, ids: set[str]) -> set[str]:
    """Forge/NeoForge: ids FML itself reported discovering (debug.log); READY implies construction worked."""
    found = {i.strip() for m in _FML_FOUND.finditer(text) for i in m["ids"].split(",")}
    return ids & found


def _classify(r: Result, monitor: ServerMonitor, server: Path, logs: Path, expected: set[str], loader: str) -> None:
    from mcat.debug.analyze import analyze_text

    console = (logs / "console.log").read_text(errors="replace") if (logs / "console.log").exists() else ""
    debug_path = server / "logs" / "debug.log"
    debug_log = debug_path.read_text(errors="replace") if debug_path.is_file() else console
    crash_reports = sorted((server / "crash-reports").glob("*.txt")) if (server / "crash-reports").is_dir() else []
    state = monitor.state
    chain = [Evidence(f"{t.state.value} @ {t.at}s: {t.evidence}") for t in monitor.transitions]
    if state in (ServerState.STOPPED, ServerState.STOPPING) or (
        state is ServerState.READY or any(t.state is ServerState.READY for t in monitor.transitions)
    ):
        ready = any(t.state is ServerState.READY for t in monitor.transitions)
        if ready:
            r.add(
                Finding(
                    "server-ready",
                    f"dedicated server reached READY in {monitor.done_seconds}s (Done line + command round trip)",
                    Severity.INFO,
                    state=VerificationState.VERIFIED,
                    evidence=chain,
                )
            )
        if state is ServerState.STOPPED:
            r.add(
                Finding(
                    "server-clean-stop",
                    "server stopped cleanly (exit 0)",
                    Severity.INFO,
                    state=VerificationState.VERIFIED,
                )
            )
        else:
            r.degrade(f"server was ready but did not stop cleanly (final state {state.value})")
    elif state is ServerState.BLOCKED:
        r.block("server refused to start: EULA not accepted in eula.txt")
    else:
        r.fail(f"server did not become ready: {state.value}")
        r.add(
            Finding(
                "server-not-ready",
                f"final state {state.value}",
                Severity.ERROR,
                state=VerificationState.FAILED,
                evidence=chain,
            )
        )
    if expected:
        seen = expected & set(monitor.fabric_mods) if loader == "fabric" else _mod_ids_in_log(debug_log, expected)
        missing = expected - seen
        if missing:
            r.add(
                Finding(
                    "mod-not-loaded",
                    f"no evidence that {sorted(missing)} loaded",
                    Severity.ERROR,
                    state=VerificationState.FAILED,
                )
            )
        if seen:
            r.add(
                Finding(
                    "mod-loaded", f"loader reported {sorted(seen)}", Severity.INFO, state=VerificationState.VERIFIED
                )
            )
    # failure classification: reuse the minecraft-debug analyzer on everything the run produced
    text = console + "".join("\n" + p.read_text(errors="replace") for p in crash_reports[:3])
    if state is not ServerState.STOPPED:
        for f in analyze_text(text):
            f.code = f"runtime-{f.code}" if not f.code.startswith("runtime-") else f.code
            r.add(f)
    r.data["crash_reports"] = [str(p) for p in crash_reports]
    r.data["logs"] = {"console": str(logs / "console.log"), "install": str(logs / "install.log")}


def _cleanup(ws: Path) -> None:
    server = ws / "server"
    for child in list(server.iterdir()) if server.is_dir() else []:
        if child.name in KEEP_ON_CLEANUP:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    for extra in ("downloads", "installer.jar", "home"):
        p = ws / extra
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
