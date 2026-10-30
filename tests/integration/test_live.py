"""Opt-in integration tests against real services and toolchains.

uv run pytest -m network                       # Modrinth / loader metadata (no credentials)
MCAT_ALLOW_ENV_CREDENTIALS=1 MCAT_CURSEFORGE_API_KEY=... uv run pytest -m network -k curseforge
MCAT_RUN_BUILD_TESTS=1 uv run pytest -m build  # real Gradle builds (downloads toolchains, minutes)
uv run pytest -m network -k codex              # Codex skill discovery via `codex debug prompt-input`
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from mcat import agentskills, moddev
from mcat.loaders import LoaderCatalog
from mcat.providers.modrinth import ModrinthProvider


@pytest.mark.network
def test_modrinth_search_and_hash_lookup():
    mr = ModrinthProvider()
    hits = mr.search("sodium", minecraft="26.2", loader="fabric", limit=5)
    assert any(h.slug == "sodium" for h in hits)
    v = mr.versions("sodium", minecraft="26.2", loader="fabric")[0]
    sha1 = v.primary_file.hashes["sha1"]
    assert mr.versions_by_hash([sha1])[sha1].version_id == v.version_id


@pytest.mark.network
@pytest.mark.parametrize("loader", ["fabric", "neoforge", "forge"])
def test_loader_support_26_2(loader):
    s = LoaderCatalog().support(loader, "26.2")
    assert s.supported and s.java == 25 and s.recommended


@pytest.mark.network
def test_nonexistent_version_unsupported():
    assert not LoaderCatalog().support("fabric", "1.99.99").supported


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("MCAT_CURSEFORGE_API_KEY"), reason="no CurseForge key in environment")
def test_curseforge_search():
    from mcat.providers.curseforge import CurseForgeProvider

    cf = CurseForgeProvider()
    assert cf.test()
    assert cf.search("jei", loader="neoforge", limit=3)


@pytest.mark.network
@pytest.mark.skipif(not shutil.which("codex"), reason="codex CLI not installed")
def test_codex_discovers_skills(tmp_path):
    # `codex debug prompt-input` renders the model-visible prompt offline (no model call, no quota)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    agentskills.install(["codex"], "project", tmp_path)
    out = subprocess.run(
        ["codex", "debug", "prompt-input", "hi"], cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False
    )
    for name in agentskills.SKILL_NAMES:
        assert f"{name}:" in out.stdout


@pytest.mark.build
@pytest.mark.skipif(os.environ.get("MCAT_RUN_BUILD_TESTS") != "1", reason="set MCAT_RUN_BUILD_TESTS=1")
@pytest.mark.parametrize("loader", ["fabric", "neoforge", "forge"])
def test_scaffold_compiles(loader, tmp_path):
    plan = moddev.plan_scaffold(loader, "26.2", "mcattest", "com.example.mcattest")
    moddev.write_project(plan, tmp_path / loader, examples=True)
    outcome = moddev.run_build(tmp_path / loader, timeout=1800)
    assert outcome.ok, outcome.log_tail
    assert outcome.artifacts
