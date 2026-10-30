import json

import pytest

from mcat.parsers import jsscan, snbt
from mcat.parsers.inventory import build_inventory, classify
from mcat.parsers.jarmeta import read_jar
from mcat.parsers.packs import PackFormatError, detect_format, parse_pack
from mcat.parsers.source import PackSource
from tests.conftest import NEOFORGE_TOML, QUEST_CHAPTER, fabric_jar, make_jar, write_zip


def test_fabric_jar_metadata():
    mods, problems = read_jar(fabric_jar("alpha", "1.2.0", {"beta": ">=2"}, env="client", breaks={"gamma": "*"}))
    assert not problems
    m = mods[0]
    assert (m.loader, m.mod_id, m.version, m.environment) == ("fabric", "alpha", "1.2.0", "client")
    kinds = {(d.mod_id, d.kind) for d in m.dependencies}
    assert ("beta", "required") in kinds and ("gamma", "incompatible") in kinds


def test_neoforge_jar_uses_manifest_version():
    mods, _ = read_jar(make_jar(neoforge_toml=NEOFORGE_TOML))
    assert mods[0].loader == "neoforge" and mods[0].version == "2.0.1"
    assert mods[0].dependencies[0].kind == "required"


def test_forge_mods_toml_mandatory():
    toml = '[[mods]]\nmodId="f"\nversion="1"\n[[dependencies.f]]\nmodId="forge"\nmandatory=true\nversionRange="[47,)"\n'
    mods, _ = read_jar(make_jar(mods_toml=toml))
    assert mods[0].loader == "forge" and mods[0].dependencies[0].kind == "required"


def test_malformed_jar_metadata_reported_not_raised():
    mods, problems = read_jar(make_jar(extra={"fabric.mod.json": b"{not json"}))
    assert mods == [] and problems
    assert read_jar(b"garbage")[1]


def test_mrpack(mrpack):
    with PackSource(mrpack) as src:
        assert detect_format(src) == "mrpack"
        pack = parse_pack(src)
    assert (pack.minecraft, pack.loader, pack.loader_version) == ("26.2", "fabric", "0.20.1")
    sodium = next(e for e in pack.entries if e.path == "mods/sodium.jar")
    assert sodium.side == "client" and sodium.project_id == "AANobbMI" and sodium.version_id == "abcd1234"
    local = next(e for e in pack.entries if e.embedded)
    assert local.jar_mods[0].mod_id == "localmod"


def test_mrpack_traversal_path_rejected(tmp_path):
    idx = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "1",
        "name": "x",
        "dependencies": {"minecraft": "26.2"},
        "files": [{"path": "../../evil.jar", "hashes": {}, "downloads": []}],
    }
    p = write_zip(tmp_path / "evil.mrpack", {"modrinth.index.json": json.dumps(idx)})
    with PackSource(p) as src, pytest.raises(Exception, match="traversal"):
        parse_pack(src)


def test_curseforge(cf_zip):
    with PackSource(cf_zip) as src:
        pack = parse_pack(src)
    assert (pack.format, pack.minecraft, pack.loader, pack.loader_version) == (
        "curseforge",
        "1.20.1",
        "forge",
        "47.2.0",
    )
    assert pack.entries[0].project_id == "238222"


def test_packwiz(tmp_path):
    root = tmp_path / "pw"
    (root / "mods").mkdir(parents=True)
    (root / "pack.toml").write_text(
        'name = "PW"\nversion = "1"\npack-format = "packwiz:1.1.0"\n'
        '[index]\nfile = "index.toml"\nhash-format = "sha256"\nhash = "x"\n'
        '[versions]\nminecraft = "26.2"\nfabric = "0.20.1"\n'
    )
    (root / "index.toml").write_text(
        'hash-format = "sha256"\n[[files]]\nfile = "mods/sodium.pw.toml"\nhash = "y"\nmetafile = true\n'
    )
    (root / "mods" / "sodium.pw.toml").write_text(
        'name = "Sodium"\nfilename = "sodium.jar"\nside = "client"\n[download]\nurl = "https://cdn.modrinth.com/x"\n'
        'hash-format = "sha1"\nhash = "abc"\n[update.modrinth]\nmod-id = "AANobbMI"\nversion = "v1"\n'
    )
    with PackSource(root) as src:
        pack = parse_pack(src)
    assert pack.format == "packwiz" and pack.loader == "fabric"
    assert pack.entries[0].path == "mods/sodium.jar" and pack.entries[0].side == "client"


def test_unknown_format(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "a.txt").write_text("hi")
    with PackSource(tmp_path / "x") as src, pytest.raises(PackFormatError):
        parse_pack(src)


def test_instance_inventory(fabric_instance):
    with PackSource(fabric_instance) as src:
        pack = parse_pack(src)
        inv = build_inventory(src, pack)
    assert pack.loader is None  # mixed fabric + neoforge jars: refuse to guess
    assert inv.categories["ftbquests"] == 1 and inv.categories["kubejs_script"] == 1
    assert inv.categories["recipe"] == 1
    assert inv.quests.quests == 3 and inv.quests.dependency_edges == 3
    assert inv.quests.dangling_dependencies == ["7777777777777777"]
    assert "create:andesite_alloy" in inv.quests.item_refs
    assert "create" in inv.unresolved_namespaces and "mysterymod" in inv.unresolved_namespaces
    assert "commented" not in inv.referenced_namespaces  # comments are not code
    assert "README.md" in inv.suspicious_text


@pytest.mark.parametrize(
    "path,cat",
    [
        ("mods/a.jar", "mods"),
        ("config/ftbquests/quests/chapters/a.snbt", "ftbquests"),
        ("kubejs/server_scripts/a.js", "kubejs_script"),
        ("kubejs/data/x/recipes/a.json", "recipe"),
        ("datapacks/p/data/x/tags/item/a.json", "tag"),
        ("config/a.toml", "config"),
        ("scripts/a.zs", "crafttweaker"),
        ("resourcepacks/x.zip", "resourcepack"),
        ("kubejs/data/x/worldgen/biome/a.json", "worldgen"),
    ],
)
def test_classify(path, cat):
    assert classify(path) == cat


def test_snbt_parse_and_rewrite():
    data = snbt.parse(QUEST_CHAPTER)
    assert data["quests"][1]["dependencies"] == ["1111111111111111"]
    assert data["quests"][0]["tasks"][0]["count"] == 16
    new, changes = snbt.rewrite_strings(
        QUEST_CHAPTER, lambda s: "minecraft:short_grass" if s == "minecraft:grass" else None
    )
    assert len(changes) == 1 and 'item: "minecraft:short_grass"' in new
    assert new.replace("minecraft:short_grass", "minecraft:grass") == QUEST_CHAPTER  # nothing else touched


def test_snbt_errors():
    with pytest.raises(snbt.SnbtError):
        snbt.parse('{ a: "unterminated }')
    assert snbt.parse("[I; 1, 2, 3]") == [1, 2, 3]


def test_js_scan_skips_comments_regex_templates():
    from tests.conftest import KUBEJS_SERVER

    vals = [s.value for s in jsscan.scan_strings(KUBEJS_SERVER)]
    assert "create:andesite_alloy" in vals and "minecraft:iron_nugget" in vals
    assert "commented:out" not in vals
    assert not any("not_an_id" in v for v in vals)
    new, changes = jsscan.rewrite_strings(KUBEJS_SERVER, lambda s: "x:y" if s == "minecraft:andesite" else None)
    assert "'x:y'" in new and len(changes) == 1
