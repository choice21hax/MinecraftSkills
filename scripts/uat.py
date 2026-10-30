"""Final user acceptance test: a new developer with nothing but a built wheel.

Usage:
  uv run python scripts/uat.py WHEEL OUTDIR [--build] [--accept-eula] [--codex]

Runs in a clean environment: temporary HOME, XDG dirs and uv tool dir; MCAT installed with
`uv tool install <wheel>`; the keyring is disabled (no credentials are copied in). No hosted backend.
--build        allow step 10 (sandboxed Gradle build; downloads the toolchain into the temp cache)
--accept-eula  you accept the Minecraft EULA for step 11 (local test server in a container)
--codex        step 3 also asks the real Codex CLI which skills it loads (no model call)
Steps not authorised are reported as skipped, never as passed. Report: OUTDIR/uat-report.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SKILLS = ("minecraft-modpack", "minecraft-migrate", "minecraft-moddev", "minecraft-debug")
steps: list[dict[str, Any]] = []


def record(n: int, name: str, state: str, detail: str, seconds: float) -> None:
    steps.append({"step": n, "name": name, "state": state, "detail": detail, "seconds": round(seconds, 1)})
    print(f"{n:>2}. [{state.upper():7}] {name}: {detail}", flush=True)


def main() -> int:
    args = sys.argv[1:]
    wheel, out = Path(args[0]).resolve(), Path(args[1]).resolve()
    allow_build, eula, codex = "--build" in args, "--accept-eula" in args, "--codex" in args
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} must be new or empty")
    home = out / "home"
    work = out / "work"
    for d in (home, work):
        d.mkdir(parents=True)
    env = {
        "PATH": f"{out / 'bin'}:{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "UV_TOOL_DIR": str(out / "tools"),
        "UV_TOOL_BIN_DIR": str(out / "bin"),
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv")),  # wheel deps only
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
    }
    if os.environ.get("DOCKER_HOST"):
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]

    def run(*cmd: str, cwd: Path = work, timeout: int = 900) -> tuple[int, str]:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout + p.stderr

    def mcat(*a: str, timeout: int = 900) -> tuple[int, dict[str, Any]]:
        code, text = run("mcat", *a, "--json", timeout=timeout)
        start = text.find("{")
        try:
            return code, json.loads(text[start:]) if start >= 0 else {}
        except ValueError:
            return code, {"status": "unparseable", "summary": text[-300:]}

    def step(n: int, name: str, fn: Any) -> Any:
        t = time.monotonic()
        try:
            state, detail, value = fn()
        except Exception as e:  # a crashing step is a failed step, with the reason
            state, detail, value = "failed", f"{type(e).__name__}: {e}", None
        record(n, name, state, detail, time.monotonic() - t)
        return value

    # 1. install from the built artifact
    def s1() -> tuple[str, str, None]:
        code, text = run("uv", "tool", "install", str(wheel))
        c2, ver = run("mcat", "--version")
        ok = code == 0 and c2 == 0 and shutil.which("mcat", path=env["PATH"]) == str(out / "bin" / "mcat")
        return ("passed" if ok else "failed", ver.strip() if ok else text[-300:], None)

    step(1, "install MCAT from the built wheel (uv tool install)", s1)

    # 2. install skills for both agents (user scope, in the temporary HOME)
    def s2() -> tuple[str, str, None]:
        code, d = mcat("skills", "install", "--agents", "claude,codex", "--scope", "user")
        linked = [p for b in (home / ".claude/skills", home / ".agents/skills") for p in b.glob("*/SKILL.md")]
        ok = code == 0 and len(linked) == 2 * len(SKILLS)
        return ("passed" if ok else "failed", f"{len(linked)} SKILL.md reachable ({d.get('status')})", None)

    step(2, "install Claude Code + Codex skills", s2)

    # 3. verify discovery
    def s3() -> tuple[str, str, None]:
        _code, d = mcat("skills", "status")
        rows = d.get("data", {}).get("entries", d.get("data", {}).get("skills", []))
        ok_rows = [r for r in rows if isinstance(r, dict) and r.get("state") in ("linked", "copied")]
        detail = f"mcat skills status: {len(ok_rows)} healthy entries"
        if codex and shutil.which("codex"):
            proj = work / "codex-proj"
            (proj / ".git").mkdir(parents=True)
            mcat("skills", "install", "--agents", "codex", "--scope", "project", "--project", str(proj))
            real_env = {**os.environ}  # Codex needs the real user's config; only reads skills, no model call
            p = subprocess.run(
                ["codex", "debug", "prompt-input", "hello"],
                cwd=proj,
                env=real_env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            seen = [s for s in SKILLS if s in p.stdout]
            detail += f"; codex debug prompt-input lists {len(seen)}/4"
            if len(seen) != 4:
                return ("failed", detail, None)
        elif codex:
            detail += "; codex CLI not installed"
        return ("passed" if len(ok_rows) >= 8 else "failed", detail, None)

    step(3, "verify skill discovery", s3)

    # 4. search Modrinth
    def s4() -> tuple[str, str, None]:
        code, d = mcat("search", "sodium", "--minecraft", "1.21.1", "--loader", "fabric")
        hits = d.get("data", {}).get("results") or d.get("data", {}).get("hits") or []
        slugs = [h.get("slug") for h in hits if isinstance(h, dict)]
        return ("passed" if code == 0 and "sodium" in slugs else "failed", f"{len(slugs)} hits, top: {slugs[:3]}", None)

    step(4, "search Modrinth", s4)

    # 5. CurseForge credentials (optional): a clean user has none; MCAT must say so, not crash or guess
    def s5() -> tuple[str, str, None]:
        code, _d = mcat("auth", "status")
        c2, t = mcat("auth", "test", "curseforge")
        ok = code in (0, 3, 4) and c2 == 4 and t.get("status") == "blocked"
        return (
            "passed" if ok else "failed",
            f"no key configured -> auth test blocked (exit {c2}); setting a key is interactive "
            "(`mcat auth set curseforge`) and was not automated",
            None,
        )

    step(5, "CurseForge credentials (optional path)", s5)

    # 6 + 7. create a minimal modpack and export it
    spec = work / "pack.yaml"
    spec.write_text('name: UAT\nminecraft: "1.21.1"\nloader: fabric\nmods: [lithium, modmenu, fabric-api]\n')

    def s6() -> tuple[str, str, None]:
        code, d = mcat("pack", "create", str(spec), "-o", str(work / "pack"), "--mrpack", str(work / "uat.mrpack"))
        lock = (
            json.loads((work / "pack" / "mcat.lock.json").read_text())
            if (work / "pack/mcat.lock.json").is_file()
            else {}
        )
        slugs = sorted(m["slug"] for m in lock.get("mods", []))
        ok = code == 0 and {"lithium", "modmenu", "fabric-api"} <= set(slugs)
        return ("passed" if ok else "failed", f"{d.get('summary')}; lock: {slugs}", None)

    step(6, "create a minimal modpack (Fabric 1.21.1)", s6)

    def s7() -> tuple[str, str, None]:
        import zipfile

        mr = work / "uat.mrpack"
        ok = mr.is_file() and "modrinth.index.json" in zipfile.ZipFile(mr).namelist()
        code, d = mcat("inspect", str(mr))
        return (
            "passed" if ok and code == 0 else "failed",
            f".mrpack {mr.stat().st_size if mr.exists() else 0} B; inspect: {d.get('summary')}",
            None,
        )

    step(7, "export the pack (.mrpack) and re-read it", s7)

    # 8. validate
    def s8() -> tuple[str, str, None]:
        code, d = mcat("validate", str(work / "pack"), "--online")
        errors = [f["code"] for f in d.get("findings", []) if f.get("severity") in ("error", "critical")]
        return ("passed" if code in (0, 3) and not errors else "failed", f"{d.get('status')}: {d.get('summary')}", None)

    step(8, "validate the pack (--online)", s8)

    # 9. scaffold a mod
    def s9() -> tuple[str, str, None]:
        code, d = mcat(
            "mod",
            "init",
            "--loader",
            "forge",
            "--minecraft",
            "1.20.1",
            "--mod-id",
            "uatmod",
            "--package",
            "com.example.uatmod",
            "-o",
            str(work / "uatmod"),
            "--with-examples",
        )
        ok = (
            code == 3
            and (work / "uatmod/build.gradle").is_file()
            and (work / "uatmod/THIRD_PARTY_NOTICES.md").is_file()
        )
        return ("passed" if ok else "failed", f"exit {code} ({d.get('status')}): {d.get('summary')}", None)

    step(9, "scaffold a mod (Forge 1.20.1)", s9)

    # 10. compile
    jar: list[Path] = []

    def s10() -> tuple[str, str, None]:
        if not allow_build:
            return ("skipped", "needs --build (runs Gradle in a container)", None)
        code, d = mcat("mod", "build", str(work / "uatmod"), "--allow-build", "--sandbox", timeout=3000)
        jar.extend(Path(a) for a in d.get("artifacts", []) if a.endswith(".jar") and "sources" not in a)
        return ("passed" if code == 0 and jar else "failed", f"{d.get('summary')}", None)

    step(10, "compile the mod (sandboxed Gradle)", s10)

    # 11. runtime test
    def s11() -> tuple[str, str, None]:
        if not eula:
            return ("skipped", "needs --accept-eula (the tester accepts the Minecraft EULA)", None)
        if not jar:
            return ("skipped", "no jar from step 10", None)
        code, d = mcat(
            "test",
            "mod",
            str(jar[0]),
            "--loader",
            "forge",
            "--minecraft",
            "1.20.1",
            "--accept-eula",
            "--memory",
            "3072",
            "--cleanup",
            timeout=1800,
        )
        loaded = [f for f in d.get("findings", []) if f.get("code") == "mod-loaded"]
        return ("passed" if code == 0 and loaded else "failed", f"{d.get('summary')}", None)

    step(11, "run a supported runtime test (mod on a Forge 1.20.1 server)", s11)

    # 12. deliberately broken pack
    def s12() -> tuple[str, str, None]:
        broken = work / "broken"
        shutil.copytree(work / "pack", broken)
        for f in (broken / "mods").glob("fabric-api*.pw.toml"):
            f.unlink()
        code, d = mcat("validate", str(broken))
        codes = {f["code"] for f in d.get("findings", [])}
        # observed on 1.0.0rc1: the lockfile entry and the packwiz index both point at the removed file
        ok = code == 1 and {"lock-entry-missing", "packwiz-missing-file"} <= codes
        return ("passed" if ok else "failed", f"exit {code}: {sorted(codes)[:6]}", None)

    step(12, "analyze a deliberately broken pack (fabric-api removed)", s12)

    # 13. migration plan
    def s13() -> tuple[str, str, None]:
        code, d = mcat(
            "migrate",
            "plan",
            str(work / "uat.mrpack"),
            "--minecraft",
            "1.21.1",
            "--loader",
            "neoforge",
            "-o",
            str(work / "plan.json"),
        )
        ok = (work / "plan.json").is_file() and code in (3, 4)
        return ("passed" if ok else "failed", f"exit {code}: {d.get('summary')}", None)

    step(13, "generate a migration plan (Fabric -> NeoForge 1.21.1)", s13)

    # 14. interpret the incomplete migration report
    def s14() -> tuple[str, str, None]:
        plan = json.loads((work / "plan.json").read_text())
        blocked = [m for m in plan["mods"] if m["state"] == "blocked"]
        every_reason = all(m["reason"] for m in blocked)
        code, _d = mcat("migrate", "apply", str(work / "plan.json"), "-o", str(work / "migrated"))
        bundle = all(
            (work / "migrated" / n).is_file() for n in ("ROLLBACK.md", "mappings.json", "verification-report.json")
        )
        report = json.loads((work / "migrated/migration-report.json").read_text()) if bundle else {}
        ok = (
            plan["overall"]["complete"] is False
            and every_reason
            and bundle
            and report.get("overall", {}).get("complete") is False
            and (code == 4) == bool(blocked)
        )
        summary = "; ".join(f"{m['name'] or m['path']}: {m['reason']}" for m in blocked[:3])
        return (
            "passed" if ok else "failed",
            f"{len(blocked)} blocked ({summary}); apply exit {code}; complete=false",
            None,
        )

    step(14, "interpret the incomplete migration report", s14)

    report = {
        "wheel": str(wheel),
        "steps": steps,
        "passed": sum(s["state"] == "passed" for s in steps),
        "skipped": sum(s["state"] == "skipped" for s in steps),
        "failed": sum(s["state"] == "failed" for s in steps),
        "environment": {"home": str(home), "keyring": "disabled", "hosted_backend": "none"},
        "agent_workflow": "a full Claude/Codex conversation was not run by this script (model usage); "
        "skill discovery is checked in step 3",
    }
    (out / "uat-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"passed {report['passed']}, skipped {report['skipped']}, failed {report['failed']}",
        f"-> {out / 'uat-report.json'}",
    )
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
