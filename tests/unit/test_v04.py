"""v0.4: content-type-aware compatibility, resolver pins/advisory/suspect deps, migration decisions,
suggestions, output bundle, adapters, custom rebuild gate, compatibility registry, build cache lock."""

import json
import threading
import time
from dataclasses import replace

import pytest

from mcat import moddev
from mcat.migration import adapters
from mcat.migration.apply import apply_plan
from mcat.migration.plan import build_plan, namespace_guesses, validate_decisions
from mcat.models import ProjectHit
from mcat.resolver.core import Request, Resolver, compatible
from tests.fakes import FakeCatalog, FakeModrinth, mv
from tests.unit.test_migration import providers, source_pack  # noqa: F401 - fixtures


def _resolver(versions, **kw):
    fake = FakeModrinth(versions)
    return Resolver(lambda _prov, proj: fake.versions(proj), "1.21.1", "neoforge", ["release", "beta"], **kw)


# ---- compatibility --------------------------------------------------------------------------------


def test_resource_pack_needs_no_loader_but_needs_game_version():
    rp = replace(mv("rp", "r1", ["1.21.1"], []), project_type="resourcepack")
    assert compatible(rp, "1.21.1", "neoforge", ["release"])
    assert not compatible(rp, "1.20.1", "neoforge", ["release"])
    mod = mv("m", "m1", ["1.21.1"], [])
    assert not compatible(mod, "1.21.1", "neoforge", ["release"])  # a mod with no loader tag is not a match
    mr_rp = mv("rp2", "r2", ["1.21.1"], ["minecraft"])  # Modrinth tags packs with "minecraft"
    assert compatible(mr_rp, "1.21.1", "fabric", ["release"])


# ---- resolver --------------------------------------------------------------------------------------


def test_pin_applies_when_project_is_first_reached_as_a_dependency():
    r = _resolver(
        [
            mv("a", "a1", ["1.21.1"], ["neoforge"], deps=[("required", "lib")]),
            mv("lib", "lib_new", ["1.21.1"], ["neoforge"], published="2026-02-01"),
            mv("lib", "lib_old", ["1.21.1"], ["neoforge"], published="2026-01-01"),
        ]
    )
    res = r.resolve([Request("modrinth", "a"), Request("modrinth", "lib", "lib_old")])
    assert res.ok, res.problems
    assert res.selected["modrinth:lib"].version.version_id == "lib_old"


def test_resource_pack_requirements_are_advisory():
    rp = replace(mv("rp", "r1", ["1.21.1"], [], deps=[("required", "sodium")]), project_type="resourcepack")
    res = _resolver([rp]).resolve([Request("modrinth", "rp")])
    assert res.ok and "modrinth:sodium" not in res.selected
    assert any("advisory" in s for s in res.optional_skipped)


def test_suspect_dependency_is_recorded_not_followed():
    vs = [mv("addon", "ad1", ["1.21.1"], ["neoforge"], deps=[("required", "create-fabric")])]
    check = lambda prov, proj: "fabric-only project" if proj == "create-fabric" else None  # noqa: E731
    res = _resolver(vs, dependency_check=check).resolve([Request("modrinth", "addon")])
    assert res.ok
    assert res.suspect_dependencies == [
        {"from": "modrinth:addon", "to": "modrinth:create-fabric", "reason": "fabric-only project"}
    ]


# ---- planning ---------------------------------------------------------------------------------------


class SearchableModrinth(FakeModrinth):
    def __init__(self, versions, hits):
        super().__init__(versions)
        self.hits = hits

    def search(self, query, **_):
        return [h for h in self.hits if query.lower().split()[0] in h.title.lower()]


def _hit(pid, title, loaders):
    return ProjectHit("modrinth", pid, pid, title, "", "mod", loaders, ["26.2"], 1)


def test_suggestions_are_guesses_and_decisions_are_user_approved(source_pack, providers):  # noqa: F811
    fake, cat = providers
    fake = SearchableModrinth(
        [
            *{id(v): v for vs in fake.by_project.values() for v in vs}.values(),
            mv("create-port", "cp1", ["26.2"], ["fabric"]),
        ],
        [_hit("create-port", "Create (Fabric)", ["fabric"])],
    )
    plan = build_plan(source_pack, "26.2", "fabric", modrinth=fake, curseforge=None, catalog=cat)
    create = next(m for m in plan["mods"] if m["path"] == "mods/create.jar")
    assert create["state"] == "blocked"
    sugg = create["suggested_replacements"]
    assert sugg and sugg[0]["replace_with"] == "modrinth:create-port" and sugg[0]["approved"] is False
    assert plan["decisions"] == {}  # a suggestion never becomes a decision on its own

    decided = build_plan(
        source_pack,
        "26.2",
        "fabric",
        modrinth=fake,
        curseforge=None,
        catalog=cat,
        decisions={"mods/create.jar": {"replace_with": "modrinth:create-port"}},
    )
    create = next(m for m in decided["mods"] if m["path"] == "mods/create.jar")
    assert create["action"] == "replace" and create["evidence"][0].startswith("user decision")
    assert decided["decisions"]["mods/create.jar"]["provenance"] == "user"
    assert any(m["key"] == "modrinth:create-port" for m in decided["lock"]["mods"])


@pytest.mark.parametrize(
    "bad",
    [{"x": {"replace_with": "github:foo"}}, {"x": {"replace_with": "modrinth:"}}, {"x": {"delete": True}}, {"x": 1}],
)
def test_bad_decisions_rejected(bad):
    with pytest.raises(ValueError):
        validate_decisions(bad)


def test_namespace_guesses_include_unseparated_ids():
    assert namespace_guesses("sophisticated-backpacks") == ["sophisticated_backpacks", "sophisticatedbackpacks"]
    assert "create" in namespace_guesses("create-fabric")


def test_alpha_only_target_is_explained(source_pack, providers):  # noqa: F811
    fake, cat = providers
    fake.by_project["create"].append(mv("create", "create_a", ["26.2"], ["fabric"], slug="create", channel="alpha"))
    plan = build_plan(source_pack, "26.2", "fabric", modrinth=fake, curseforge=None, catalog=cat)
    create = next(m for m in plan["mods"] if m["path"] == "mods/create.jar")
    assert (
        create["state"] == "blocked" and "only alpha builds" in create["reason"] and "--allow-alpha" in create["reason"]
    )


def test_current_file_retained_when_it_supports_the_target(tmp_path):
    from tests.conftest import write_zip

    idx = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "1",
        "name": "Same",
        "dependencies": {"minecraft": "1.21.1", "neoforge": "21.1.1"},
        "files": [
            {
                "path": "mods/a.jar",
                "hashes": {"sha1": ("a_old" * 40)[:40]},
                "env": {"client": "required", "server": "required"},
                "downloads": ["https://cdn.modrinth.com/data/a/versions/a_old/a.jar"],
                "fileSize": 1,
            }
        ],
    }
    pack = write_zip(tmp_path / "p.mrpack", {"modrinth.index.json": json.dumps(idx)})
    fake = FakeModrinth(
        [
            mv("a", "a_old", ["1.21.1"], ["neoforge"], published="2026-01-01"),
            mv("a", "a_new", ["1.21.1"], ["neoforge"], published="2026-03-01"),
        ]
    )
    plan = build_plan(
        pack, "1.21.1", "neoforge", modrinth=fake, curseforge=None, catalog=FakeCatalog({("neoforge", "1.21.1"): "x"})
    )
    m = plan["mods"][0]
    assert (m["action"], m["state"], m["target"]["version_id"]) == ("retain", "verified", "a_old")


def test_plan_lists_adapters_with_active_rules(source_pack, providers):  # noqa: F811
    fake, cat = providers
    plan = build_plan(source_pack, "26.2", "fabric", modrinth=fake, curseforge=None, catalog=cat)
    names = {a["adapter"]: a for a in plan["adapters"]}
    assert {"ftb-quests", "kubejs", "config-keys"} <= set(names)
    assert any("short_grass" in rule for rule in names["ftb-quests"]["active_rules"])
    assert any("loot_tables -> loot_table" in rule for rule in names["recipes"]["active_rules"])
    assert all(c.get("adapter") for c in plan["content"] if adapters.for_category(c["category"]))


# ---- apply outputs ----------------------------------------------------------------------------------


def test_apply_writes_full_bundle_and_separates_mappings(source_pack, providers, tmp_path):  # noqa: F811
    fake, cat = providers
    before = source_pack.read_bytes()
    plan = build_plan(source_pack, "26.2", "fabric", modrinth=fake, curseforge=None, catalog=cat)
    plan["id_map"] = {"create:andesite_alloy": "newmod:alloy"}
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    out = tmp_path / "out"
    apply_plan(tmp_path / "plan.json", out)
    for name in (
        "inventory.json",
        "plan.json",
        "mappings.json",
        "unresolved-references.json",
        "verification-report.json",
        "transformation-log.jsonl",
        "ROLLBACK.md",
        "changes.diff",
        "pack/mcat.lock.json",
    ):
        assert (out / name).is_file(), name
    mappings = json.loads((out / "mappings.json").read_text())
    assert mappings["user_approved"]["id_map"] == {"create:andesite_alloy": "newmod:alloy"}
    assert "minecraft:grass" in mappings["automatic_rules"]["vanilla_id_map"]
    log = [json.loads(line) for line in (out / "transformation-log.jsonl").read_text().splitlines()]
    origins = {e["origin"] for e in log}
    assert "user id_map" in origins and any(o.startswith("vanilla rename rule") for o in origins)
    verification = json.loads((out / "verification-report.json").read_text())
    assert verification["runtime"]["state"] == "unverified"
    assert "delete" in (out / "ROLLBACK.md").read_text()
    assert source_pack.read_bytes() == before


def test_rebuild_requires_allow_build_and_matching_target(source_pack, providers, tmp_path):  # noqa: F811
    fake, cat = providers
    proj = tmp_path / "custom-src"
    proj.mkdir()
    (proj / moddev.PROJECT_META).write_text(json.dumps({"loader": "fabric", "minecraft": "26.2"}))
    other = tmp_path / "other-src"
    other.mkdir()
    (other / moddev.PROJECT_META).write_text(json.dumps({"loader": "forge", "minecraft": "1.20.1"}))
    plan = build_plan(
        source_pack,
        "26.2",
        "fabric",
        modrinth=fake,
        curseforge=None,
        catalog=cat,
        decisions={
            "mods/custom.jar": {"rebuild_from": str(proj)},
            "mods/good.jar": {"rebuild_from": str(other)},
        },
    )
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    r = apply_plan(tmp_path / "plan.json", tmp_path / "out")  # no allow_build: nothing is executed
    rebuilt = {x["mod"]: x for x in r.data["rebuilt_custom_mods"]}
    assert "--allow-build" in rebuilt["mods/custom.jar"]["reason"]
    assert "plan targets fabric 26.2" in rebuilt["mods/good.jar"]["reason"]
    assert all(x["state"] == "blocked" for x in rebuilt.values())


# ---- registry and build lock ---------------------------------------------------------------------------


def test_registry_entries_have_provenance_and_drive_verified_views():
    reg = moddev.load_registry()
    for e in reg["entries"]:
        assert e["build"]["date"] and e["source"] and e["loader_version"]
        assert moddev.family_for(e["loader"], e["minecraft"]).name == e["family"]
    assert ("forge", "1.20.1") in moddev.VERIFIED_BUILDS and ("neoforge", "1.21.1") in moddev.RUNTIME_VERIFIED
    assert ("neoforge", "1.20.1") not in moddev.VERIFIED_BUILDS


def test_build_cache_lock_serialises(tmp_path):
    lock = tmp_path / "cache.lock"
    events: list[str] = []

    def worker(tag: str) -> None:
        with moddev._exclusive(lock):
            events.append(f"{tag}+")
            time.sleep(0.05)
            events.append(f"{tag}-")

    ts = [threading.Thread(target=worker, args=(t,)) for t in "ab"]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert events in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])


def test_validator_downgrades_recorded_suspect_dependency(tmp_path):
    from mcat.reporting import Result
    from mcat.validation.validate import _check_lock_closure

    lock = {
        "mods": [
            {
                "key": "curseforge:1",
                "provider": "curseforge",
                "project_id": "1",
                "slug": "addon",
                "version_id": "9",
                "dependencies": [{"kind": "required", "project_id": "2"}],
            }
        ],
        "suspect_dependencies": [{"from": "curseforge:1", "to": "curseforge:2", "reason": "fabric-only"}],
    }
    r = Result("t")
    _check_lock_closure(lock, r)
    codes = {f.code for f in r.findings}
    assert "dependency-metadata-suspect" in codes and "lock-dependency-missing" not in codes
