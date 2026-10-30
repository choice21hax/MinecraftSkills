"""`mcat doctor`: diagnose the environment MCAT depends on. Read-only: installs nothing, changes nothing.

Each check yields a row {check, state, detail, fix} with state ok | warn | missing | error | skipped, plus
findings for anything that limits a workflow. Network checks run only when not offline.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcat import __version__, agentskills
from mcat.reporting import Finding, Result, Severity
from mcat.security import credentials as cred

# Java each supported game line needs (Mojang version manifest: javaVersion.majorVersion).
JAVA_NEEDS = {"1.20.1": 17, "1.21.1": 21, "26.x": 25}


def _run(cmd: list[str], timeout: int = 20) -> str | None:
    exe = shutil.which(cmd[0]) if not os.path.isabs(cmd[0]) else cmd[0]
    if not exe:
        return None
    try:
        p = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (p.stdout + p.stderr).strip()


def _java_major(text: str) -> int | None:
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


def find_javas() -> list[dict[str, Any]]:
    """java executables on PATH, in JAVA_HOME and in the usual JVM directories (no installs, no changes)."""
    candidates: list[Path] = []
    if shutil.which("java"):
        candidates.append(Path(shutil.which("java") or "java"))
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "bin" / "java")
    for base in ("/usr/lib/jvm", "/usr/java", "/Library/Java/JavaVirtualMachines", os.path.expanduser("~/.jdks")):
        b = Path(base)
        if b.is_dir():
            candidates += sorted(b.glob("*/bin/java")) + sorted(b.glob("*/Contents/Home/bin/java"))
    seen, out = set(), []
    for c in candidates:
        try:
            real = c.resolve()
        except OSError:
            continue
        if real in seen or not real.is_file():
            continue
        seen.add(real)
        text = _run([str(real), "-version"]) or ""
        out.append({"path": str(c), "major": _java_major(text), "on_path": str(c) == shutil.which("java")})
    return out


def run_doctor(*, offline: bool = False, project: Path | None = None) -> Result:
    r = Result("doctor", summary=f"mcat {__version__}")
    rows: list[dict[str, str]] = []

    def row(check: str, state: str, detail: str, fix: str = "") -> None:
        rows.append({"check": check, "state": state, "detail": detail, "fix": fix})

    # Python
    py = sys.version_info
    row("python", "ok" if py >= (3, 12) else "error", f"{py.major}.{py.minor}.{py.micro} at {sys.executable}")
    if py < (3, 12):
        r.fail("Python 3.12+ required")

    # Java: installations and which game lines they can build/run on the host
    javas = find_javas()
    majors = sorted({j["major"] for j in javas if j["major"]})
    r.data["java_installations"] = javas
    if javas:
        row("java", "ok", f"{len(javas)} installation(s): majors {majors}")
    else:
        row("java", "missing", "no JDK found", "install a JDK (e.g. Temurin) or use --sandbox builds/tests")
    compat = {}
    for line, need in JAVA_NEEDS.items():
        compat[line] = {"needs": need, "host_has": need in majors}
    r.data["java_compatibility"] = compat
    missing = [line for line, c in compat.items() if not c["host_has"]]
    if missing:
        r.add(
            Finding(
                "java-version-missing",
                f"no host JDK for {', '.join(missing)} (needs {', '.join(str(JAVA_NEEDS[m]) for m in missing)}); "
                "sandboxed builds/tests use container images instead",
                Severity.INFO,
            )
        )

    # Build tools
    gradle = _run(["gradle", "--version", "--quiet"]) if shutil.which("gradle") else None
    row(
        "gradle",
        "ok" if gradle else "missing",
        next((ln for ln in gradle.splitlines() if ln.startswith("Gradle")), "present") if gradle else "not on PATH",
        "" if gradle else "optional: projects use their Gradle wrapper, or `mod build --sandbox`",
    )
    row(
        "packwiz",
        "ok" if shutil.which("packwiz") else "missing",
        shutil.which("packwiz") or "not on PATH (optional; MCAT writes packwiz format natively)",
        "" if shutil.which("packwiz") else "optional: go install github.com/packwiz/packwiz@latest",
    )
    for tool in ("git", "uv"):
        v = _run([tool, "--version"])
        row(tool, "ok" if v else "missing", (v or "not on PATH").splitlines()[0])

    # Credentials (never the secret)
    st = cred.backend_status()
    row(
        "credential-backend",
        "ok" if st.secure else "warn",
        st.detail,
        "" if st.secure else (cred.LINUX_SETUP_GUIDE.splitlines()[0] if sys.platform.startswith("linux") else ""),
    )
    if not st.secure:
        r.add(Finding("keyring-insecure", f"No secure keyring backend: {st.detail}", Severity.WARNING))
        r.degrade()
    creds = cred.credential_status()
    r.data["credentials"] = creds
    cf_ok = bool(creds.get("curseforge", {}).get("configured"))
    row(
        "curseforge-credential",
        "ok" if cf_ok else "missing",
        f"configured ({creds['curseforge']['source']})" if cf_ok else "not configured (optional)",
        "" if cf_ok else "mcat auth set curseforge",
    )

    # Connectivity
    if offline or os.environ.get("MCAT_OFFLINE") == "1":
        row("modrinth", "skipped", "offline")
        row("curseforge-api", "skipped", "offline")
    else:
        from mcat.providers.curseforge import CurseForgeProvider, CurseForgeUnavailable
        from mcat.providers.http import NetworkError
        from mcat.providers.modrinth import ModrinthProvider

        try:
            hits = ModrinthProvider().search("sodium", limit=1)
            row("modrinth", "ok", f"search answered ({len(hits)} hit)")
        except NetworkError as e:
            row("modrinth", "error", f"{type(e).__name__}: {e}", "check network/proxy; MCAT_OFFLINE=1 for offline use")
            r.degrade()
        if cf_ok:
            try:
                ok = CurseForgeProvider().test()
                row("curseforge-api", "ok" if ok else "error", "key accepted" if ok else "unexpected answer")
            except (NetworkError, CurseForgeUnavailable) as e:
                row("curseforge-api", "error", f"{type(e).__name__}", "mcat auth test curseforge")
        else:
            row("curseforge-api", "skipped", "no key")

    # Skills: source package and discovery locations for both agents
    try:
        root = agentskills.canonical_root()
        problems = [p for n in agentskills.SKILL_NAMES for p in agentskills.validate_skill(root / n)]
        row("skills-source", "error" if problems else "ok", str(root))
        for p in problems:
            r.add(Finding("skill-invalid", p, Severity.ERROR))
        if problems:
            r.degrade()
        entries = agentskills.status(["claude", "codex"], ["user", "project"], project or Path.cwd())
        installed: dict[str, int] = {}
        for ent in entries:
            if ent.state in ("linked", "copied"):
                installed[f"{ent.agent}/{ent.scope}"] = installed.get(f"{ent.agent}/{ent.scope}", 0) + 1
        bad = [f"{x.agent}/{x.scope}/{x.skill}: {x.state}" for x in entries if x.state in ("broken-link", "invalid")]
        r.data["skills_installed"] = installed
        complete = [k for k, n in installed.items() if n == len(agentskills.SKILL_NAMES)]
        row(
            "skills-discovery",
            "error" if bad else ("ok" if complete else "missing"),
            f"all four installed for {complete}" if complete else "not installed for Claude Code or Codex",
            "; ".join(bad) if bad else ("" if complete else "mcat skills install --agents claude,codex --scope user"),
        )
    except agentskills.SkillInstallError as e:
        r.fail(str(e))

    # Writable locations
    cache = Path(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))) / "mcat"
    for name, d in (("cache-dir", cache), ("workspace", project or Path.cwd())):
        try:
            d.mkdir(parents=True, exist_ok=True) if name == "cache-dir" else None
            with tempfile.NamedTemporaryFile(dir=d, prefix=".mcat-doctor-"):
                pass
            row(name, "ok", f"{d} writable")
        except OSError as e:
            row(name, "error", f"{d}: {e.strerror}")
            r.degrade()

    # Runtime sandbox
    from mcat.validation.runtime.sandbox import SandboxUnavailable, detect_engine

    try:
        info = detect_engine()
        row(
            "sandbox",
            "ok" if info.rootless else "warn",
            f"{info.engine} {info.version} ({'rootless' if info.rootless else 'rootful'})",
            "" if info.rootless else "rootless Podman is preferred: docker group membership is root-equivalent",
        )
    except SandboxUnavailable as e:
        row("sandbox", "missing", str(e), "install Podman (preferred) or Docker to use `mcat test` and `--sandbox`")

    # Optional system pieces for documented features
    display = shutil.which("Xvfb") or shutil.which("xvfb-run")
    row("virtual-display", "ok" if display else "missing", display or "no Xvfb (client tests are manual anyway)")

    r.data["checks"] = rows
    r.data["python"] = f"{py.major}.{py.minor}.{py.micro}"
    r.data["note"] = "doctor only reports; it never installs packages or changes settings"
    level = {"ok": Severity.INFO, "skipped": Severity.INFO, "warn": Severity.WARNING, "missing": Severity.WARNING}
    for x in rows:
        r.add(
            Finding(
                f"{x['check']}",
                f"{x['state']}: {x['detail']}",
                level.get(x["state"], Severity.ERROR),
                fix=x["fix"] or None,
            )
        )
    return r
