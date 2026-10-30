import json

from mcat.cli.main import main
from mcat.debug.analyze import analyze_text, plan_repairs
from tests.conftest import NEOFORGE_TOML, fabric_jar, make_jar

FABRIC_LOG = """[12:00:01] [main/INFO]: Loading Minecraft 26.2 with Fabric Loader 0.20.1
[12:00:02] [main/ERROR]: Incompatible mods found!
net.fabricmc.loader.impl.FormattedException: Mod resolution encountered an incompatible mod set!
A potential solution has been determined:
\t - Install fabric-api, any version.
Unmet dependency listing:
\t - Mod 'Mod Menu' (modmenu) 20.0.3 requires any version of fabric-api, which is missing!
\t - Mod 'Sodium Extra' (sodium-extra) 0.6.0 requires version 0.9.0 or later of mod 'Sodium' (sodium), but only the wrong version is present: 0.8.1!
"""

NEO_LOG = """[main/ERROR] [net.neoforged.fml.loading.ModSorter/LOADING]: Missing or unsupported mandatory dependencies:
\tMod ID: 'architectury', Requested by: 'rei', Expected range: '[13.0.0,)', Actual version: '[MISSING]'
\tMod ID: 'cloth_config', Requested by: 'rei', Expected range: '[15,)', Actual version: '14.0.1'
"""

JAVA_CRASH = """---- Minecraft Crash Report ----
Description: Initializing game

java.lang.UnsupportedClassVersionError: com/example/Mod has been compiled by a more recent version of the Java Runtime (class file version 69.0), this version of the Java Runtime only recognizes class file versions up to 65.0
"""

MIXIN_LOG = """[main/ERROR]: Mixin apply failed
org.spongepowered.asm.mixin.injection.throwables.InvalidInjectionException: Critical injection failure: @Inject annotation on onRender could not find any targets matching 'render' in net.minecraft.client.Gui. Using refmap fancyhud.refmap.json [PREINJECT Applicator Phase -> fancyhud.mixins.json:GuiMixin from mod fancyhud -> Prepare Injections]
java.lang.OutOfMemoryError: Java heap space
\tat com.example.thing.Foo.bar(Foo.java:10) ~[somemod-1.2.jar:?]
\tat com.example.thing.Foo.baz(Foo.java:11) ~[somemod-1.2.jar:?]
\tat net.minecraft.Main.main(Main.java:1) ~[minecraft-26.2.jar:?]
"""


def codes(text):
    return [(f.code, f.confidence) for f in analyze_text(text)]


def test_fabric_missing_and_wrong_version():
    fs = analyze_text(FABRIC_LOG)
    missing = next(f for f in fs if f.code == "missing-dependency")
    assert missing.subject == "fabric-api" and missing.confidence == "confirmed"
    wrong = next(f for f in fs if f.code == "dependency-version")
    assert wrong.subject == "sodium" and "0.8.1" in wrong.message
    assert fs[0].confidence == "confirmed"


def test_neoforge_dependencies():
    c = codes(NEO_LOG)
    assert ("missing-dependency", "confirmed") in c and ("dependency-version", "confirmed") in c


def test_java_version():
    f = next(f for f in analyze_text(JAVA_CRASH) if f.code == "java-too-old")
    assert "Java 25" in f.message and "Java 21" in f.message


def test_mixin_oom_and_stack_attribution_confidence_levels():
    fs = {f.code: f for f in analyze_text(MIXIN_LOG)}
    assert fs["mixin-failure"].confidence == "probable" and fs["mixin-failure"].subject == "fancyhud"
    assert fs["out-of-memory"].confidence == "confirmed"
    st = fs["stack-trace-mods"]
    assert st.confidence == "possible" and "somemod-1.2.jar" in st.message and "minecraft" not in st.message


def test_no_match_is_partial(tmp_path, capsys):
    p = tmp_path / "latest.log"
    p.write_text("[main/INFO]: all good\n")
    assert main(["debug", str(p), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


def test_secrets_redacted_from_debug_output(tmp_path, capsys):
    p = tmp_path / "latest.log"
    p.write_text(FABRIC_LOG + "\nx-api-key: $2a$10$" + "z" * 53 + "\n")
    main(["debug", str(p), "--json"])
    assert "zzzzzzzzzz" not in capsys.readouterr().out


def test_repairs_quarantine_and_revalidate(tmp_path, capsys):
    pack = tmp_path / "inst"
    (pack / "mods").mkdir(parents=True)
    (pack / "mmc-pack.json").write_text(
        json.dumps(
            {
                "components": [
                    {"uid": "net.minecraft", "version": "26.2"},
                    {"uid": "net.fabricmc.fabric-loader", "version": "0.20.1"},
                ]
            }
        )
    )
    (pack / "mods" / "a-old.jar").write_bytes(fabric_jar("a", "1.0"))
    (pack / "mods" / "a-new.jar").write_bytes(fabric_jar("a", "1.2"))
    (pack / "mods" / "neo.jar").write_bytes(make_jar(neoforge_toml=NEOFORGE_TOML))
    assert sorted(f for f, _ in plan_repairs(pack)) == ["mods/a-old.jar", "mods/neo.jar"]
    log = tmp_path / "latest.log"
    log.write_text(FABRIC_LOG)
    main(["debug", str(log), "--pack", str(pack), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert len(out["data"]["proposed_repairs"]) == 2 and (pack / "mods" / "neo.jar").exists()  # not applied
    main(["debug", str(log), "--pack", str(pack), "--apply", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert (pack / "mcat-quarantine" / "neo.jar").exists() and not (pack / "mods" / "a-old.jar").exists()
    assert out["data"]["revalidation"]["status"] in ("success", "partial")
