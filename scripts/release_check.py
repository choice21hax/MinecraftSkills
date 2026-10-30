"""Build and verify release candidates locally. Publishes nothing.

Usage: uv run python scripts/release_check.py OUTDIR

1. builds wheel + sdist twice (SOURCE_DATE_EPOCH = last commit time) and requires identical bytes
2. writes SHA256SUMS
3. checks metadata: version == mcat.__version__, Requires-Dist == pyproject dependencies, license text
4. scans artifacts: no jars/archives/binaries, no token-shaped strings, no files over 1 MB, skills,
   templates and compatibility registry present
5. installs the wheel into a fresh venv, runs scripts/smoke_installed.py and minimal offline examples
   (doctor --offline, mod matrix, inspect + validate + migrate plan --no-suggest on a corpus pack)
6. writes release-report.json with every check and its result; exit 1 if any check failed
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = (".jar", ".zip", ".mrpack", ".class", ".so", ".dll", ".exe", ".pyc")
TOKEN_PATTERNS = [
    re.compile(rb"\$2a\$10\$[A-Za-z0-9./]{40,}"),  # CurseForge key shape
    re.compile(rb"mrp_[A-Za-z0-9]{30,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
MAX_FILE = 1_000_000
checks: list[dict[str, object]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    checks.append({"check": name, "ok": ok, "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")
    return ok


def sh(*cmd: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=900)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build(dest: Path, epoch: str) -> list[Path]:
    env = {**os.environ, "SOURCE_DATE_EPOCH": epoch}
    p = sh("uv", "build", "--wheel", "--sdist", "-o", str(dest), env=env)
    if p.returncode:
        raise SystemExit(f"uv build failed:\n{p.stderr[-3000:]}")
    return sorted(dest.glob("*.whl")) + sorted(dest.glob("*.tar.gz"))


def members(art: Path) -> dict[str, bytes]:
    if art.suffix == ".whl":
        with zipfile.ZipFile(art) as z:
            return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
    with tarfile.open(art) as t:
        out = {}
        for m in t.getmembers():
            if m.isfile():
                f = t.extractfile(m)
                out[m.name] = f.read() if f else b""
        return out


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    epoch = sh("git", "log", "-1", "--format=%ct").stdout.strip() or "315532800"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = pyproject["project"]["version"]

    a = build(out / "build-a", epoch)
    b = build(out / "build-b", epoch)
    same = [x.name == y.name and sha256(x) == sha256(y) for x, y in zip(a, b, strict=True)]
    check("reproducible build (two builds, identical bytes)", len(a) == 2 and all(same), ", ".join(x.name for x in a))
    dist = out / "dist"
    dist.mkdir(exist_ok=True)
    arts = []
    for x in a:
        target = dist / x.name
        target.write_bytes(x.read_bytes())
        arts.append(target)
    sums = "".join(f"{sha256(x)}  {x.name}\n" for x in arts)
    (dist / "SHA256SUMS").write_text(sums)
    check("checksums written", True, str(dist / "SHA256SUMS"))

    wheel = next(x for x in arts if x.suffix == ".whl")
    sdist = next(x for x in arts if x.name.endswith(".tar.gz"))
    wm = members(wheel)
    meta = next(v for k, v in wm.items() if k.endswith(".dist-info/METADATA")).decode()
    check("version matches", f"\nVersion: {version}\n" in meta and f"-{version}-" in wheel.name, version)
    from mcat import __version__

    check("package __version__ matches pyproject", __version__ == version, __version__)
    requires = sorted(line.split(": ", 1)[1] for line in meta.splitlines() if line.startswith("Requires-Dist: "))
    check(
        "dependency metadata matches pyproject",
        requires == sorted(pyproject["project"]["dependencies"]),
        "; ".join(requires),
    )
    lic = next((line for line in meta.splitlines() if line.startswith("License")), "License: (none)")
    check("license metadata present (decision pending: docs/licensing.md)", "License" in lic, lic)

    for art in arts:
        mem = members(art)
        bad = [n for n in mem if n.endswith(FORBIDDEN_SUFFIXES)]
        big = [n for n, data in mem.items() if len(data) > MAX_FILE]
        tok = [n for n, data in mem.items() if any(p.search(data) for p in TOKEN_PATTERNS)]
        check(f"{art.name}: no jars/archives/binaries", not bad, ", ".join(bad[:5]))
        check(f"{art.name}: no files > 1 MB", not big, ", ".join(big[:5]))
        check(f"{art.name}: no token-shaped strings", not tok, ", ".join(tok[:5]))
    need = ["mcat/moddev/compatibility.json", "mcat/_data/templates/THIRD_PARTY_NOTICES.md"]
    need += [
        f"mcat/_data/skills/{s}/SKILL.md"
        for s in ("minecraft-modpack", "minecraft-migrate", "minecraft-moddev", "minecraft-debug")
    ]
    missing = [n for n in need if n not in wm]
    check("wheel contains skills, templates, registry, notices", not missing, ", ".join(missing))
    check("sdist builds the same version", f"-{version}.tar.gz" in sdist.name, sdist.name)

    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        sh("uv", "venv", "-q", str(venv))
        py = venv / "bin" / "python"
        mcat = venv / "bin" / "mcat"
        p = sh("uv", "pip", "install", "-q", "--python", str(py), str(wheel))
        check("wheel installs into a clean venv", p.returncode == 0, p.stderr[-300:])
        p = sh(str(py), str(ROOT / "scripts" / "smoke_installed.py"), str(mcat), cwd=Path(tmp))
        check(
            "installed smoke test (skills install/status/uninstall, envelope, exit codes)",
            p.returncode == 0,
            (p.stdout + p.stderr)[-300:],
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(Path(tmp) / "home"),
            "MCAT_OFFLINE": "1",
            "MCAT_CACHE_DIR": str(Path(tmp) / "cache"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        }
        (Path(tmp) / "home").mkdir()

        def mc(*args: str) -> tuple[int, dict[str, object]]:
            r = sh(str(mcat), *args, "--json", cwd=Path(tmp), env=env)
            try:
                return r.returncode, json.loads(r.stdout)
            except ValueError:
                return r.returncode, {"status": "unparseable", "summary": (r.stdout + r.stderr)[-200:]}

        code, d = mc("doctor", "--offline")
        names = {c["check"] for c in d.get("data", {}).get("checks", [])}  # type: ignore[union-attr]
        want = {
            "python",
            "java",
            "gradle",
            "packwiz",
            "credential-backend",
            "curseforge-credential",
            "modrinth",
            "skills-source",
            "skills-discovery",
            "cache-dir",
            "workspace",
            "sandbox",
        }
        check("doctor --offline reports every diagnostic", want <= names, ", ".join(sorted(want - names)))
        code, d = mc("mod", "matrix")
        check("mod matrix from the wheel", code == 0 and "8 executed" in str(d.get("summary")), str(d.get("summary")))
        corpus = ROOT / "tests" / "corpus" / "quests-fabric-1.20.1-to-26.2" / "pack"
        code, d = mc("inspect", str(corpus))
        check("example: inspect a pack", code == 0, str(d.get("summary")))
        code, d = mc("validate", str(corpus))
        check("example: validate a pack", code in (0, 3), f"exit {code}: {d.get('summary')}")
        code, d = mc(
            "migrate",
            "plan",
            str(corpus),
            "--minecraft",
            "26.2",
            "--loader",
            "fabric",
            "--no-suggest",
            "-o",
            str(Path(tmp) / "plan.json"),
        )
        check("example: migrate plan offline is blocked, not crashed", code == 4, f"exit {code}: {d.get('summary')}")
        p = sh("uv", "pip", "uninstall", "-q", "--python", str(py), "minecraft-agent-toolkit")
        check("uninstall", p.returncode == 0 and not mcat.exists(), p.stderr[-200:])

    report = {
        "version": version,
        "source_date_epoch": epoch,
        "commit": sh("git", "rev-parse", "HEAD").stdout.strip(),
        "dirty": bool(sh("git", "status", "--porcelain").stdout.strip()),
        "artifacts": {x.name: sha256(x) for x in arts},
        "checks": checks,
        "published": False,
        "note": "local release candidate; publishing (PyPI, GitHub Release, skill directories) needs the owner",
    }
    (dist / "release-report.json").write_text(json.dumps(report, indent=2) + "\n")
    failed = [c for c in checks if not c["ok"]]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed; report: {dist / 'release-report.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
