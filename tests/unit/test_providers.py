import json

import httpx
import pytest

from mcat.providers import curseforge as cfmod
from mcat.providers.http import AuthError, HttpClient, NetworkError, RateLimitedError, check_url
from mcat.providers.modrinth import ModrinthProvider, normalize_version

MR_PROJECT = {
    "id": "P1",
    "slug": "alpha",
    "title": "Alpha",
    "client_side": "required",
    "server_side": "unsupported",
    "license": {"id": "MIT"},
}
MR_VERSION = {
    "id": "V1",
    "project_id": "P1",
    "name": "Alpha 1.0",
    "version_number": "1.0.0",
    "game_versions": ["26.2"],
    "loaders": ["fabric"],
    "version_type": "release",
    "date_published": "2026-09-01T00:00:00Z",
    "files": [
        {
            "filename": "alpha-sources.jar",
            "url": "https://cdn.modrinth.com/s",
            "primary": False,
            "hashes": {"sha1": "0" * 40},
            "size": 1,
        },
        {
            "filename": "alpha.jar",
            "url": "https://cdn.modrinth.com/a",
            "primary": True,
            "hashes": {"sha1": "1" * 40, "sha512": "2" * 128},
            "size": 10,
        },
    ],
    "dependencies": [
        {"project_id": "P2", "version_id": None, "dependency_type": "required"},
        {"project_id": "P3", "dependency_type": "incompatible"},
        {"project_id": "P4", "dependency_type": "weird-new-type"},
    ],
}


def client(handler) -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(handler), retries=1, backoff=0)


def test_normalize_modrinth_version():
    v = normalize_version(MR_VERSION, MR_PROJECT)
    assert v.primary_file.filename == "alpha.jar"
    assert v.side.value == "client" and v.license == "MIT"
    assert [(d.kind.value, d.project_id) for d in v.dependencies] == [("required", "P2"), ("incompatible", "P3")]
    assert isinstance(v.game_versions[0], str)


def test_modrinth_search_builds_facets(isolated_env):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = req.url
        seen["ua"] = req.headers["user-agent"]
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "project_id": "P1",
                        "slug": "alpha",
                        "title": "Alpha",
                        "categories": ["fabric", "tech"],
                        "versions": ["26.2"],
                        "downloads": 5,
                        "project_type": "mod",
                    }
                ]
            },
        )

    hits = ModrinthProvider(client(handler)).search("alpha", minecraft="26.2", loader="fabric")
    facets = json.loads(seen["url"].params["facets"])
    assert ["categories:fabric"] in facets and ["versions:26.2"] in facets
    assert seen["ua"].startswith("mcat/")
    assert hits[0].loaders == ["fabric"]


def test_rate_limit_then_success(isolated_env):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset": "0"})
        return httpx.Response(200, json={"ok": True})

    assert client(handler).get_json("https://api.modrinth.com/v2/x", ttl=0) == {"ok": True}


def test_rate_limit_exhausted(isolated_env):
    with pytest.raises(RateLimitedError):
        client(lambda r: httpx.Response(429, headers={"retry-after": "120"})).get_json(
            "https://api.modrinth.com/v2/x", ttl=0
        )


def test_invalid_credentials(isolated_env):
    with pytest.raises(AuthError):
        client(lambda r: httpx.Response(403)).get_json("https://api.curseforge.com/v1/x", ttl=0)


def test_host_allowlist():
    with pytest.raises(NetworkError):
        check_url("https://evil.example.com/x")
    with pytest.raises(NetworkError):
        check_url("http://api.modrinth.com/v2")
    with pytest.raises(NetworkError):
        check_url("https://user:pw@api.modrinth.com/v2")


def test_credentials_not_forwarded_on_redirect(isolated_env):
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.url.host, req.headers.get("x-api-key")))
        if req.url.host == "api.curseforge.com":
            return httpx.Response(302, headers={"location": "https://edge.forgecdn.net/file"})
        return httpx.Response(200, json={})

    client(handler).get_json(
        "https://api.curseforge.com/v1/x", credential=("curseforge", "x-api-key", "SECRETKEY"), ttl=0
    )
    assert seen == [("api.curseforge.com", "SECRETKEY"), ("edge.forgecdn.net", None)]


def test_redirect_to_unlisted_host_blocked(isolated_env):
    h = lambda r: httpx.Response(302, headers={"location": "https://attacker.example/x"})  # noqa: E731
    with pytest.raises(NetworkError, match="allow-list"):
        client(h).get_json("https://api.modrinth.com/v2/x", ttl=0)


def test_credential_scope_is_per_host(isolated_env):
    seen = []

    def handler(req):
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, json={})

    client(handler).get_json("https://api.curseforge.com/v1/x", credential=("modrinth", "Authorization", "tok"), ttl=0)
    assert seen == [None]


def test_cache_and_offline(isolated_env, monkeypatch):
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(200, json={"v": len(n)})

    c = client(handler)
    assert c.get_json("https://api.modrinth.com/v2/c", ttl=100) == {"v": 1}
    assert c.get_json("https://api.modrinth.com/v2/c", ttl=100) == {"v": 1}
    monkeypatch.setenv("MCAT_OFFLINE", "1")
    from mcat.providers.http import OfflineError

    with pytest.raises(OfflineError):
        c.get_json("https://api.modrinth.com/v2/uncached", ttl=100)


CF_FILE = {
    "id": 4712345,
    "modId": 238222,
    "displayName": "JEI 1.20.1",
    "fileName": "jei.jar",
    "releaseType": 2,
    "fileDate": "2024-01-01",
    "fileLength": 5,
    "downloadUrl": None,
    "fileFingerprint": 123,
    "gameVersions": ["1.20.1", "Forge", "Client"],
    "hashes": [{"value": "a" * 40, "algo": 1}, {"value": "b" * 32, "algo": 2}],
    "dependencies": [{"modId": 1, "relationType": 3}, {"modId": 2, "relationType": 4}, {"modId": 3, "relationType": 5}],
}


def test_normalize_curseforge_file_restricted():
    v = cfmod.normalize_file(CF_FILE, {"slug": "jei", "name": "JEI", "allowModDistribution": False})
    assert v.loaders == ["forge"] and v.game_versions == ["1.20.1"] and v.channel == "beta"
    assert v.files[0].download_allowed is False and v.files[0].url is None
    assert v.side.value == "client"
    assert [(d.kind.value, d.project_id) for d in v.dependencies] == [("required", "1"), ("incompatible", "3")]


def test_curseforge_unavailable_without_key(isolated_env, monkeypatch):
    monkeypatch.setattr(cfmod, "get_secret", lambda p: (None, "none"))
    with pytest.raises(cfmod.CurseForgeUnavailable):
        cfmod.CurseForgeProvider()


def test_curseforge_sends_key_header(isolated_env):
    seen = {}

    def handler(req):
        seen["key"] = req.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 1,
                        "name": "JEI",
                        "slug": "jei",
                        "summary": "",
                        "downloadCount": 1,
                        "latestFilesIndexes": [{"gameVersion": "1.20.1", "modLoader": 1}],
                    }
                ]
            },
        )

    hits = cfmod.CurseForgeProvider(client(handler), api_key="k" * 20).search("jei", loader="forge")
    assert seen["key"] == "k" * 20 and hits[0].loaders == ["forge"]
