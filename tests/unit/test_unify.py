"""CurseForge pagination/fingerprints and cross-provider identity (offline)."""

import dataclasses
import json
import random

import httpx

from mcat.models import FileRef
from mcat.pack.build import resolve_spec
from mcat.providers import curseforge as cfmod
from mcat.providers.http import HttpClient
from mcat.resolver.spec import parse_spec
from tests.fakes import FakeCatalog, FakeModrinth, mv


def _murmur2_reference(data: bytes, seed: int = 1) -> int:
    # straightforward port of Austin Appleby's MurmurHash2, independent of the struct-based implementation
    data = bytes(b for b in data if b not in (9, 10, 13, 32))
    m, r, n = 0x5BD1E995, 24, len(data)
    h = (seed ^ n) & 0xFFFFFFFF
    i = 0
    while n - i >= 4:
        k = data[i] | data[i + 1] << 8 | data[i + 2] << 16 | data[i + 3] << 24
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
        i += 4
    left = n - i
    if left == 3:
        h ^= data[i + 2] << 16
    if left >= 2:
        h ^= data[i + 1] << 8
    if left >= 1:
        h ^= data[i]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def test_fingerprint_matches_reference_for_all_tail_lengths():
    rng = random.Random(7)
    for n in range(0, 70):
        data = bytes(rng.randrange(256) for _ in range(n))
        assert cfmod.fingerprint(data) == _murmur2_reference(data), n


def test_fingerprint_ignores_whitespace():
    assert cfmod.fingerprint(b"a b\tc\r\nd") == cfmod.fingerprint(b"abcd")


def _cf_file(fid, mod=10, game=("26.2", "NeoForge"), url=True):
    return {
        "id": fid,
        "modId": mod,
        "displayName": f"v{fid}",
        "fileName": f"m-{fid}.jar",
        "fileDate": f"2026-01-{fid % 28 + 1:02d}",
        "releaseType": 1,
        "gameVersions": list(game),
        "hashes": [{"value": f"{fid:040d}", "algo": 1}],
        "fileFingerprint": fid,
        "downloadUrl": f"https://edge.forgecdn.net/files/{fid}/m.jar" if url else None,
        "dependencies": [],
    }


def test_curseforge_versions_paginate(isolated_env):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/mods/10":
            return httpx.Response(200, json={"data": {"id": 10, "slug": "m", "name": "M"}})
        q = dict(req.url.params)
        calls.append(q)
        idx, size = int(q["index"]), int(q["pageSize"])
        page = [_cf_file(i) for i in range(idx, min(idx + size, 120))]
        return httpx.Response(200, json={"data": page, "pagination": {"index": idx, "totalCount": 120}})

    cf = cfmod.CurseForgeProvider(HttpClient(transport=httpx.MockTransport(handler), retries=0), api_key="k" * 20)
    vs = cf.versions("10", minecraft="26.2", loader="neoforge")
    assert len(vs) == 120 and [c["index"] for c in calls] == ["0", "50", "100"]
    assert calls[0]["gameVersion"] == "26.2" and calls[0]["modLoaderType"] == "6"
    assert len(cf.versions("10", limit=30)) == 30  # caller-imposed cap


def test_curseforge_fingerprint_match_and_files_batch(isolated_env):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        if req.url.path == "/v1/fingerprints/432":
            assert body == {"fingerprints": [5, 6]}
            return httpx.Response(200, json={"data": {"exactMatches": [{"id": 10, "file": _cf_file(5)}]}})
        if req.url.path == "/v1/mods":
            return httpx.Response(200, json={"data": [{"id": 10, "slug": "m", "name": "M"}]})
        if req.url.path == "/v1/mods/files":
            return httpx.Response(200, json={"data": [_cf_file(i, url=i != 8) for i in body["fileIds"]]})
        return httpx.Response(404)

    cf = cfmod.CurseForgeProvider(HttpClient(transport=httpx.MockTransport(handler), retries=0), api_key="k" * 20)
    m = cf.match_fingerprints([6, 5, 5])
    assert list(m) == [5] and m[5].slug == "m" and m[5].version_id == "5"
    v = cf.version("8")
    assert v.primary_file is not None and v.primary_file.download_allowed is False


def _cf(v, sha1, allowed=True):
    f = FileRef(
        f"{v.project_id}.jar", None if not allowed else "https://edge.forgecdn.net/x.jar", {"sha1": sha1}, 1, allowed
    )
    return dataclasses.replace(v, provider="curseforge", files=[f], license=None)


def _resolve(spec_mods, mr_versions, cf_versions, lookup):
    spec = parse_spec({"name": "p", "version": "1", "minecraft": "26.2", "loader": "fabric", "mods": spec_mods}, "d")
    fake = FakeModrinth(mr_versions)

    def fetch(provider, project):
        return (
            [v for v in cf_versions if v.project_id == project] if provider == "curseforge" else fake.versions(project)
        )

    return resolve_spec(spec, fetch=fetch, catalog=FakeCatalog({("fabric", "26.2"): "0.20.1"}), hash_lookup=lookup)


def test_identical_file_from_both_providers_is_merged_with_provenance():
    api_mr = mv("P7dR8mSH", "fa1", ["26.2"], ["fabric"], slug="fabric-api", sha1="a" * 40)
    api_cf = _cf(mv("306612", "9001", ["26.2"], ["fabric"], slug="fabric-api"), "a" * 40)
    res, lock, warnings = _resolve(
        ["fabric-api", {"id": "306612", "provider": "curseforge"}],
        [api_mr],
        [api_cf],
        lambda shas: {"a" * 40: api_mr},
    )
    assert res.ok and [m["key"] for m in lock["mods"]] == ["modrinth:P7dR8mSH"]
    assert lock["mods"][0]["equivalents"] == ["curseforge:306612/9001"]
    assert lock["provider_identity"]["merged"][0]["dropped"] == "curseforge:306612"


def test_same_name_different_hash_is_never_merged():
    a = mv("AAA", "a1", ["26.2"], ["fabric"], slug="jei", sha1="1" * 40)
    b = _cf(mv("238222", "77", ["26.2"], ["fabric"], slug="jei"), "2" * 40)
    res, lock, _ = _resolve(["AAA", {"id": "238222", "provider": "curseforge"}], [a], [b], lambda shas: {})
    assert res.ok and len(lock["mods"]) == 2
    assert lock["provider_identity"]["unmatched"] == ["curseforge:238222"]


def test_same_project_different_files_is_reported_not_guessed():
    mr_new = mv("AAA", "a2", ["26.2"], ["fabric"], slug="x", sha1="3" * 40, published="2026-02-01")
    mr_old = mv("AAA", "a1", ["26.2"], ["fabric"], slug="x", sha1="4" * 40)
    cf_old = _cf(mv("55", "66", ["26.2"], ["fabric"], slug="x"), "4" * 40)
    res, lock, _ = _resolve(
        ["AAA", {"id": "55", "provider": "curseforge"}], [mr_new, mr_old], [cf_old], lambda shas: {"4" * 40: mr_old}
    )
    assert not res.ok and res.problems[0].kind == "duplicate-project"
    assert "different files" in res.problems[0].message and lock["provider_identity"]["conflicts"]


def test_restricted_download_reported_and_lookup_failure_is_a_warning():
    a = mv("AAA", "a1", ["26.2"], ["fabric"], sha1="1" * 40)
    r = _cf(mv("55", "66", ["26.2"], ["fabric"], slug="restricted"), "2" * 40, allowed=False)

    def boom(shas):
        raise OSError("offline")

    res, lock, warnings = _resolve(["AAA", {"id": "55", "provider": "curseforge"}], [a], [r], boom)
    assert lock["restrictions"] == [
        {"key": "curseforge:55", "file": "55.jar", "reason": "author disallows third-party distribution"}
    ]
    assert lock["provider_identity"]["error"] and any("duplicate check could not run" in w for w in warnings)


class _FakeCF:
    """Stands in for CurseForgeProvider inside validate (no network, no key)."""

    files: dict = {}
    mods: dict = {}

    def __init__(self):
        pass

    def files_batch(self, ids):
        return {i: self.files[i] for i in ids if i in self.files}

    def mods_batch(self, ids):
        return {i: self.mods[i] for i in ids if i in self.mods}

    def match_fingerprints(self, fps):
        return {}


def _manifest_zip(tmp_path, files, extra=None):
    from tests.conftest import write_zip

    manifest = {
        "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.4.10", "primary": True}]},
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": "t",
        "version": "1",
        "files": [{"projectID": p, "fileID": f, "required": True} for p, f in files],
        "overrides": "overrides",
    }
    return write_zip(tmp_path / "p.zip", {"manifest.json": json.dumps(manifest), **(extra or {})})


def _raw(fid, mod, game, deps=()):
    f = _cf_file(fid, mod=mod, game=game)
    f["dependencies"] = [{"modId": d, "relationType": 3} for d in deps]
    return f


def test_curseforge_online_validation_findings(tmp_path, monkeypatch):
    from mcat.validation.validate import validate_path

    _FakeCF.files = {
        "1": _raw(1, 10, ("1.20.1", "Forge")),
        "2": _raw(2, 20, ("1.21.1", "NeoForge")),  # wrong target
        "3": _raw(3, 30, ("1.20.1", "Forge"), deps=(40, 50)),  # 40 missing; 50 has no forge files
        "4": _raw(4, 60, ("1.21.8",)),  # resource pack with other version tags
        "5": _raw(5, 99, ("1.20.1", "Forge")),  # belongs to another project
    }
    _FakeCF.mods = {
        "10": {"id": 10, "slug": "a", "classId": 6},
        "20": {"id": 20, "slug": "b", "classId": 6},
        "30": {"id": 30, "slug": "c", "classId": 6},
        "40": {
            "id": 40,
            "slug": "lib",
            "classId": 6,
            "latestFilesIndexes": [{"gameVersion": "1.20.1", "modLoader": 1}],
        },
        "50": {
            "id": 50,
            "slug": "fab",
            "classId": 6,
            "latestFilesIndexes": [{"gameVersion": "1.20.1", "modLoader": 4}],
        },
        "60": {"id": 60, "slug": "rp", "classId": 12},
        "99": {"id": 99, "slug": "z", "classId": 6},
    }
    monkeypatch.setattr(cfmod, "CurseForgeProvider", _FakeCF)
    r = validate_path(_manifest_zip(tmp_path, [(10, 1), (20, 2), (30, 3), (60, 4), (70, 5), (80, 6)]), online=True)
    got = {(f.code, f.subject): f for f in r.findings}
    assert ("provider-incompatible", "mods/cf-20-2.jar") in got
    miss = got[("missing-dependency", "mods/cf-30-3.jar")]
    assert miss.severity.value == "error" and "lib (40)" in miss.message
    assert got[("dependency-metadata-suspect", "mods/cf-30-3.jar")].state.value == "unverified"
    rp = got[("provider-metadata-mismatch", "mods/cf-60-4.jar")]
    assert rp.severity.value == "info" and "resource pack" in rp.message
    assert ("provider-identity-mismatch", "mods/cf-70-5.jar") in got
    assert got[("provider-lookup-failed", "mods/cf-80-6.jar")].state.value == "unverified"
    assert not any(c == "provider-incompatible" and s == "mods/cf-10-1.jar" for c, s in got)
    assert r.status.value == "failed"


def test_curseforge_online_without_key_is_unverified_not_passing(tmp_path, isolated_env, monkeypatch):
    from mcat.validation.validate import validate_path

    monkeypatch.setattr(cfmod, "get_secret", lambda name: (None, "none"))
    r = validate_path(_manifest_zip(tmp_path, [(10, 1)]), online=True)
    f = next(f for f in r.findings if f.code == "provider-compat-unverified")
    assert f.state.value == "unverified" and "not checked online" in f.message
    assert r.status.value == "partial"
