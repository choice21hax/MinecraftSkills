"""The JSON envelope is a public contract: these keys must stay stable."""

import json

import pytest

from mcat.cli.main import main

ENVELOPE = {"schema_version", "operation", "status", "summary", "data", "artifacts", "findings", "blockers", "warnings"}


def run(capsys, *argv):
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_inspect_contract(capsys, fabric_instance):
    code, out = run(capsys, "inspect", str(fabric_instance))
    assert set(out) == ENVELOPE and out["schema_version"] == "1.0"
    assert out["status"] == "success" and code == 0
    for key in ("format", "minecraft", "loader", "entries", "inventory"):
        assert key in out["data"]
    codes = {f["code"] for f in out["findings"]}
    assert "untrusted-instructions" in codes and "unresolved-namespaces" in codes


def test_inspect_wrong_loader(capsys, tmp_path):
    from tests.conftest import NEOFORGE_TOML, make_jar, write_zip

    idx = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "1",
        "name": "x",
        "dependencies": {"minecraft": "26.2", "fabric-loader": "0.20.1"},
        "files": [],
    }
    p = write_zip(
        tmp_path / "x.mrpack",
        {"modrinth.index.json": json.dumps(idx), "overrides/mods/neo.jar": make_jar(neoforge_toml=NEOFORGE_TOML)},
    )
    _, out = run(capsys, "inspect", str(p))
    f = next(f for f in out["findings"] if f["code"] == "wrong-loader")
    assert f["confidence"] == "confirmed" and f["severity"] == "error"


def test_inspect_bad_archive_fails_safely(capsys, tmp_path):
    from tests.conftest import write_zip

    p = write_zip(tmp_path / "evil.zip", {"../../x": "y"})
    code, out = run(capsys, "inspect", str(p))
    assert code == 1 and out["status"] == "failed"
    assert any(f["code"] == "unsafe-archive" for f in out["findings"])


def test_inspect_missing_file(capsys, tmp_path):
    code, out = run(capsys, "inspect", str(tmp_path / "nope.zip"))
    assert code == 1 and out["blockers"]


def test_inspect_does_not_modify_source(capsys, mrpack):
    before = mrpack.read_bytes()
    run(capsys, "inspect", str(mrpack))
    assert mrpack.read_bytes() == before


def test_usage_error_exit_code():
    with pytest.raises(SystemExit) as e:
        main(["search"])
    assert e.value.code == 2


def test_search_curseforge_without_key_is_blocked(capsys, isolated_env, monkeypatch):
    from mcat.providers import curseforge

    monkeypatch.setattr(curseforge, "get_secret", lambda p: (None, "none"))
    code, out = run(capsys, "search", "jei", "--provider", "curseforge")
    assert code == 4 and out["status"] == "blocked" and "mcat auth set curseforge" in out["blockers"][0]
