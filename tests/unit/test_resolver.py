import json

import pytest

from mcat.pack.build import PackBuildError, resolve_spec, write_mrpack, write_packwiz
from mcat.resolver.core import Request, Resolver
from mcat.resolver.spec import SpecError, parse_spec
from mcat.resolver.versions import Version, fabric_satisfies, maven_satisfies
from mcat.validation.validate import validate_path
from tests.fakes import FakeCatalog, FakeModrinth, mv


def resolver(versions, mc="26.2", loader="fabric", **kw):
    fake = FakeModrinth(versions)
    return Resolver(lambda p, proj: fake.versions(proj), mc, loader, kw.pop("channels", ["release"]), **kw)


def test_versions_are_strings_not_floats():
    assert Version("1.20") > Version("1.2")  # float(1.20) == 1.2 would get this wrong
    assert Version("1.21.10") > Version("1.21.9")
    assert Version("26.1") > Version("1.21.11")
    assert Version("1.0.0-beta.2") < Version("1.0.0")
    assert Version("1.21") == Version("1.21.0")


@pytest.mark.parametrize(
    "c,v,ok",
    [
        (">=1.2.0", "1.10.0", True),
        ("<2", "2.0.0", False),
        ("~1.20.1", "1.20.4", True),
        ("~1.20.1", "1.21", False),
        ("^1.2", "1.9", True),
        ("^1.2", "2.0", False),
        ("1.20.x", "1.20.6", True),
        ("*", "anything", True),
        (">=0.15 <0.17", "0.16.2", True),
        (">=0.15 <0.17", "0.17.0", False),
        ("1.0 || 2.0", "2.0", True),
    ],
)
def test_fabric_predicates(c, v, ok):
    assert fabric_satisfies(c, v) is ok


@pytest.mark.parametrize(
    "c,v,ok",
    [
        ("[1.0,2.0)", "1.5", True),
        ("[1.0,2.0)", "2.0", False),
        ("[26.2]", "26.2", True),
        ("[26.2]", "26.2.1", False),
        ("[65,)", "65.1.5", True),
        ("(,47]", "48", False),
        ("1.5", "1.6", True),
    ],
)
def test_maven_ranges(c, v, ok):
    assert maven_satisfies(c, v) is ok


def test_resolves_transitive_and_newest_first():
    r = resolver(
        [
            mv("a", "a2", ["26.2"], ["fabric"], deps=[("required", "lib")], published="2026-03"),
            mv("a", "a1", ["26.2"], ["fabric"], published="2026-01"),
            mv("lib", "l1", ["26.2"], ["fabric"]),
        ]
    ).resolve([Request("modrinth", "a")])
    assert r.ok and sorted(s.version.version_id for s in r.selected.values()) == ["a2", "l1"]


def test_exact_version_match_only():
    r = resolver([mv("a", "a1", ["26.1.2"], ["fabric"])]).resolve([Request("modrinth", "a")])
    assert not r.ok and r.problems[0].kind == "unavailable"
    assert "26.1.2/fabric/release" in r.problems[0].alternatives


def test_wrong_loader_unavailable():
    r = resolver([mv("a", "a1", ["26.2"], ["neoforge"])]).resolve([Request("modrinth", "a")])
    assert not r.ok and "neoforge" in r.problems[0].alternatives[0]


def test_missing_dependency_explained():
    r = resolver([mv("a", "a1", ["26.2"], ["fabric"], deps=[("required", "gone")])]).resolve([Request("modrinth", "a")])
    assert not r.ok and any(p.project == "gone" and "dependency of a" in p.message for p in r.problems)


def test_incompatible_backtracks_to_older_version():
    r = resolver(
        [
            mv("a", "a2", ["26.2"], ["fabric"], deps=[("incompatible", "b")], published="2026-03"),
            mv("a", "a1", ["26.2"], ["fabric"], published="2026-01"),
            mv("b", "b1", ["26.2"], ["fabric"]),
        ]
    ).resolve([Request("modrinth", "b"), Request("modrinth", "a")])
    assert r.ok and r.selected["modrinth:a"].version.version_id == "a1"


def test_unsatisfiable_conflict():
    r = resolver(
        [
            mv("a", "a1", ["26.2"], ["fabric"], deps=[("incompatible", "b")]),
            mv("b", "b1", ["26.2"], ["fabric"]),
        ]
    ).resolve([Request("modrinth", "b"), Request("modrinth", "a")])
    assert not r.ok and r.problems[0].kind == "conflict"


def test_cycle_detected_and_terminates():
    r = resolver(
        [
            mv("a", "a1", ["26.2"], ["fabric"], deps=[("required", "b")]),
            mv("b", "b1", ["26.2"], ["fabric"], deps=[("required", "a")]),
        ]
    ).resolve([Request("modrinth", "a")])
    assert r.ok and r.cycles


def test_excluded_required_dependency():
    r = resolver(
        [mv("a", "a1", ["26.2"], ["fabric"], deps=[("required", "lib")]), mv("lib", "l1", ["26.2"], ["fabric"])],
        exclude=["lib"],
    ).resolve([Request("modrinth", "a")])
    assert not r.ok and r.problems[0].kind == "excluded"


def test_pinned_version_and_channel():
    vs = [
        mv("a", "a2", ["26.2"], ["fabric"], channel="beta", published="2026-03"),
        mv("a", "a1", ["26.2"], ["fabric"], published="2026-01"),
    ]
    assert resolver(vs).resolve([Request("modrinth", "a")]).selected["modrinth:a"].version.version_id == "a1"
    r = resolver(vs, channels=["release", "beta"]).resolve([Request("modrinth", "a", "a2")])
    assert r.selected["modrinth:a"].version.version_id == "a2"


def test_spec_rejects_float_versions():
    with pytest.raises(SpecError, match="quoted string"):
        parse_spec({"minecraft": 1.2, "loader": "fabric"})
    with pytest.raises(SpecError):
        parse_spec({"minecraft": "26.2", "loader": "quilt"})


def _spec():
    return parse_spec(
        {
            "name": "T",
            "minecraft": "26.2",
            "loader": "fabric",
            "mods": ["a", {"id": "c", "side": "client", "optional": True}],
        },
        "digest",
    )


def _fake():
    return FakeModrinth(
        [
            mv("a", "a1", ["26.2"], ["fabric"], deps=[("required", "lib")]),
            mv("lib", "l1", ["26.2"], ["fabric"]),
            mv("c", "c1", ["26.2"], ["fabric"], server="unsupported"),
        ]
    )


def test_lock_is_deterministic_and_complete():
    fake = _fake()
    cat = FakeCatalog({("fabric", "26.2"): "0.20.1"})
    _, lock1, _ = resolve_spec(_spec(), fetch=lambda p, x: fake.versions(x), catalog=cat)
    _, lock2, _ = resolve_spec(_spec(), fetch=lambda p, x: fake.versions(x), catalog=cat)
    assert json.dumps(lock1, sort_keys=True) == json.dumps(lock2, sort_keys=True)
    m = {x["slug"]: x for x in lock1["mods"]}
    assert m["lib"]["reasons"] == ["dependency of a"] and m["c"]["side"] == "client" and m["c"]["optional"]
    assert m["a"]["file"]["hashes"]["sha512"] and m["a"]["provenance"] == "modrinth:version/a1"
    assert lock1["target"]["loader_version"] == "0.20.1" and lock1["java"] == 25


def test_unsupported_target_blocked():
    with pytest.raises(PackBuildError, match="does not support"):
        resolve_spec(_spec(), fetch=lambda p, x: [], catalog=FakeCatalog({}))


def test_packwiz_and_mrpack_export_roundtrip(tmp_path):
    fake = _fake()
    _, lock, _ = resolve_spec(
        _spec(), fetch=lambda p, x: fake.versions(x), catalog=FakeCatalog({("fabric", "26.2"): "0.20.1"})
    )
    write_packwiz(lock, tmp_path / "pw")
    v = validate_path(tmp_path / "pw")
    assert v.status.value in ("success", "partial"), v.to_text()
    assert any(f.code == "packwiz-index" and f.state.value == "verified" for f in v.findings)
    excluded = write_mrpack(lock, tmp_path / "p.mrpack")
    assert excluded == []
    from mcat.parsers.packs import parse_pack
    from mcat.parsers.source import PackSource

    with PackSource(tmp_path / "p.mrpack") as src:
        pack = parse_pack(src)
    assert {e.version_id for e in pack.entries} == {"a1", "l1", "c1"}
    c = next(e for e in pack.entries if e.version_id == "c1")
    assert c.side == "client" and c.required is False


def test_validate_detects_tampered_packwiz(tmp_path):
    fake = _fake()
    _, lock, _ = resolve_spec(
        _spec(), fetch=lambda p, x: fake.versions(x), catalog=FakeCatalog({("fabric", "26.2"): "0.20.1"})
    )
    write_packwiz(lock, tmp_path / "pw")
    with open(tmp_path / "pw" / "mods" / "a.pw.toml", "a") as fh:
        fh.write("\n# tampered\n")
    v = validate_path(tmp_path / "pw")
    assert v.status.value == "failed" and any(f.code == "packwiz-hash-mismatch" for f in v.findings)


def test_validate_embedded_jar_dependencies(tmp_path):
    from tests.conftest import fabric_jar

    root = tmp_path / "inst"
    (root / "mods").mkdir(parents=True)
    (root / "mods" / "a.jar").write_bytes(fabric_jar("a", "1.0", {"b": ">=2.0", "missing": "*"}))
    (root / "mods" / "b.jar").write_bytes(fabric_jar("b", "1.5"))
    (root / "mods" / "b2.jar").write_bytes(fabric_jar("b", "1.6"))
    (root / "mods" / "c.jar").write_bytes(fabric_jar("c", "1.0", breaks={"a": "*"}))
    codes = {f.code for f in validate_path(root).findings}
    assert {"missing-dependency", "dependency-version", "duplicate-mod", "incompatible-mods"} <= codes
