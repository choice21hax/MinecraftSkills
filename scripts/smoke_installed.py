"""Smoke-test an *installed* mcat (wheel or sdist) from outside the source tree.

Usage: python scripts/smoke_installed.py /path/to/venv/bin/mcat
Checks: version, packaged skills (discovered and valid), project-scope install/uninstall in an isolated
directory, JSON envelope shape and exit codes. Uses no network and no credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SKILLS = ("minecraft-modpack", "minecraft-migrate", "minecraft-moddev", "minecraft-debug")
ENVELOPE = {"schema_version", "operation", "status", "summary", "data", "artifacts", "findings", "blockers", "warnings"}


def run(exe: str, *args: str, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    p = subprocess.run([exe, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120, check=False)
    return p.returncode, p.stdout


def envelope(out: str) -> dict:
    data = json.loads(out)
    missing = ENVELOPE - set(data)
    assert not missing, f"envelope missing {missing}"
    assert data["schema_version"] == "1.0"
    return data


def main() -> int:
    exe = sys.argv[1]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home, proj = root / "home", root / "project"
        home.mkdir()
        proj.mkdir()
        (proj / ".git").mkdir()  # project root marker for Codex discovery
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "MCAT_CACHE_DIR": str(root / "cache"),
            "MCAT_OFFLINE": "1",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        }

        code, out = run(exe, "--version", cwd=root, env=env)
        assert code == 0 and out.startswith("mcat "), (code, out)

        code, out = run(exe, "skills", "validate", "--json", cwd=root, env=env)
        data = envelope(out)
        assert code == 0 and data["status"] == "success", out
        source = Path(data["data"]["source"])
        assert "site-packages" in source.parts or "_data" in source.parts, f"not the packaged copy: {source}"
        for name in SKILLS:
            assert (source / name / "SKILL.md").is_file(), name

        code, out = run(
            exe,
            "skills",
            "install",
            "--agents",
            "claude,codex",
            "--scope",
            "project",
            "--project",
            str(proj),
            "--json",
            cwd=root,
            env=env,
        )
        assert code == 0, out
        for base in (proj / ".claude" / "skills", proj / ".agents" / "skills"):
            for name in SKILLS:
                assert (base / name / "SKILL.md").is_file(), base / name
        assert not any(home.rglob("SKILL.md")), "project install touched HOME"

        code, out = run(exe, "skills", "status", "--project", str(proj), "--json", cwd=root, env=env)
        assert code == 0 and envelope(out)["status"] == "success", out

        code, out = run(
            exe,
            "skills",
            "uninstall",
            "--agents",
            "claude,codex",
            "--scope",
            "project",
            "--project",
            str(proj),
            "--json",
            cwd=root,
            env=env,
        )
        assert code == 0, out
        assert not any(proj.rglob("SKILL.md")), "uninstall left skills behind"

        # exit codes: failed=1, usage=2; blocked=4 (CurseForge without a key, no secure keyring)
        code, out = run(exe, "inspect", str(root / "missing.zip"), "--json", cwd=root, env=env)
        assert code == 1 and envelope(out)["status"] == "failed", (code, out)
        code, _ = run(exe, "no-such-command", cwd=root, env=env)
        assert code == 2, code
        code, out = run(exe, "auth", "test", "curseforge", "--json", cwd=root, env=env)
        assert code == 4 and envelope(out)["status"] == "blocked", (code, out)
    print("installed-package smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
