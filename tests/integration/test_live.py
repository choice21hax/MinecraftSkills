"""Opt-in integration tests against real services and toolchains.

uv run pytest -m network                       # Modrinth / loader metadata (no credentials)
uv run pytest -m network -k curseforge          # key from the OS keyring (or the CI env opt-in)
PATH=$PATH:/path/to/packwiz uv run pytest -m network -k packwiz   # real packwiz oracle
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


def _curseforge():
    from mcat.providers.curseforge import CurseForgeProvider, CurseForgeUnavailable

    try:
        return CurseForgeProvider()
    except CurseForgeUnavailable as e:
        pytest.skip(str(e))


# Targets below were chosen from real API results on 2026-10-30 (JEI: 190 NeoForge 26.2 files;
# create-mobile-packages file 7654463 fingerprint 2303265137; servercore disallows distribution).
@pytest.mark.network
def test_curseforge_search():
    cf = _curseforge()
    assert cf.test()
    hits = cf.search("jei", minecraft="26.2", loader="neoforge", limit=3)
    assert hits[0].slug == "jei" and hits[0].project_id == "238222"


@pytest.mark.network
def test_curseforge_versions_paginate_and_filter():
    cf = _curseforge()
    vs = cf.versions("238222", minecraft="26.2", loader="neoforge")
    assert len(vs) > 50  # more than one page
    assert all("26.2" in v.game_versions and "neoforge" in v.loaders for v in vs)
    assert len({v.version_id for v in vs}) == len(vs)
    required = {d.project_id for d in vs[0].dependencies if d.kind.value == "required"}
    assert required  # JEI 26.2 files declare a required library project


@pytest.mark.network
def test_curseforge_fingerprint_identifies_real_file(tmp_path):
    from mcat.providers.curseforge import fingerprint

    cf = _curseforge()
    raw = cf.files_batch(["7654463"])["7654463"]
    dest = cf.http.download(raw["downloadUrl"], tmp_path / raw["fileName"])
    fp = fingerprint(dest.read_bytes())
    assert fp == raw["fileFingerprint"] == 2303265137
    assert cf.match_fingerprints([fp])[fp].project_id == "1232978"


@pytest.mark.network
def test_curseforge_restricted_file_not_downloadable():
    cf = _curseforge()
    assert cf.mod("550579")["allowModDistribution"] is False
    v = cf.versions("550579", minecraft="1.20.1", loader="forge", limit=5)[0]
    assert v.primary_file is not None and not v.primary_file.download_allowed and v.primary_file.url is None


@pytest.mark.network
def test_curseforge_online_validation_of_real_manifest(tmp_path):
    import json

    from mcat.validation.validate import validate_path
    from tests.conftest import write_zip

    _curseforge()
    manifest = {
        "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.4.10", "primary": True}]},
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": "t",
        "version": "1",
        "files": [
            {"projectID": 1232978, "fileID": 7654463, "required": True},  # forge 1.20.1 file
            {"projectID": 238222, "fileID": 9352384, "required": True},  # JEI for NeoForge 26.2: wrong target
        ],
        "overrides": "overrides",
    }
    r = validate_path(write_zip(tmp_path / "p.zip", {"manifest.json": json.dumps(manifest)}), online=True)
    codes = {(f.code, f.subject) for f in r.findings}
    assert ("provider-incompatible", "mods/cf-238222-9352384.jar") in codes
    assert not any(s == "mods/cf-1232978-7654463.jar" and c == "provider-incompatible" for c, s in codes)
    assert r.status.value == "failed"


@pytest.mark.network
@pytest.mark.skipif(not shutil.which("packwiz"), reason="packwiz not on PATH")
def test_packwiz_refresh_is_a_noop_on_mcat_output(tmp_path):
    # real packwiz as an interoperability oracle: refreshing MCAT's pack must not change a byte
    import filecmp

    from mcat.pack.build import resolve_spec, write_packwiz
    from mcat.resolver.spec import parse_spec

    spec = parse_spec(
        {"name": "o", "version": "1", "minecraft": "26.2", "loader": "fabric", "mods": ["sodium", "lithium"]}, "d"
    )
    _, lock, _ = resolve_spec(spec)
    write_packwiz(lock, tmp_path / "a")
    shutil.copytree(tmp_path / "a", tmp_path / "b")
    subprocess.run(["packwiz", "refresh"], cwd=tmp_path / "b", check=True, capture_output=True, timeout=120)
    cmp = filecmp.dircmp(tmp_path / "a", tmp_path / "b")
    assert not cmp.diff_files and not cmp.left_only and not cmp.right_only
    assert not filecmp.dircmp(tmp_path / "a" / "mods", tmp_path / "b" / "mods").diff_files


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


_RUNTIME = pytest.mark.skipif(
    os.environ.get("MCAT_RUN_RUNTIME_TESTS") != "1",
    reason="starts real servers in a container and accepts the Minecraft EULA: set MCAT_RUN_RUNTIME_TESTS=1",
)


@pytest.mark.runtime
@_RUNTIME
@pytest.mark.parametrize("loader", ["fabric", "neoforge", "forge"])
def test_runtime_bare_server_reaches_ready(loader, tmp_path):
    from mcat.validation.runtime import RuntimeOptions, run_server_test

    r = run_server_test(
        RuntimeOptions(loader=loader, minecraft="26.2", accept_eula=True, workspace=tmp_path / "ws", cleanup=True)
    )
    assert r.status.value == "success", r.to_text()
    assert r.data["server"]["final_state"] == "stopped"


@pytest.mark.runtime
@_RUNTIME
def test_runtime_catches_broken_pack(tmp_path):
    from mcat.pack.build import resolve_spec, write_packwiz
    from mcat.resolver.spec import parse_spec
    from mcat.validation.runtime import RuntimeOptions, run_server_test

    spec = parse_spec(
        {"name": "b", "version": "1", "minecraft": "26.2", "loader": "fabric", "mods": ["appleskin"]}, "d"
    )
    _, lock, _ = resolve_spec(spec)
    lock["mods"] = [m for m in lock["mods"] if m["slug"] != "fabric-api"]  # deliberately broken
    write_packwiz(lock, tmp_path / "pack")
    r = run_server_test(
        RuntimeOptions(pack=tmp_path / "pack", accept_eula=True, workspace=tmp_path / "ws", cleanup=True)
    )
    assert r.status.value == "failed"
    assert any(f.code == "runtime-missing-dependency" and "fabric-api" in f.message for f in r.findings)
