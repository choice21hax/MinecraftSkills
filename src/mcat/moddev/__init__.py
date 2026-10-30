"""Loader-specific mod project scaffolding and Gradle builds.

Each loader has its own template family. A template is only *verified* for a game version after a real
Gradle compilation of the generated project succeeded; ``VERIFIED_BUILDS`` records those runs. Other
versions in the same family are "unverified" and need ``--allow-unverified``. Versions with no template
family are reported unsupported rather than approximated.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from mcat.loaders import LoaderCatalog
from mcat.providers.modrinth import ModrinthProvider
from mcat.security.archive import ensure_fresh_output
from mcat.security.redact import redact

MOD_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PACKAGE_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$")


@dataclass(frozen=True)
class TemplateFamily:
    loader: str
    name: str  # directory under templates/<loader>/
    applies: tuple[str, ...]  # exact game versions, or prefixes ending in "." (e.g. "26.")
    pins: dict[str, str] = field(default_factory=dict)  # plugin versions taken from official templates
    gradle: str = ""  # Gradle version of the official template's wrapper (used for sandboxed builds)
    per_version: dict[str, dict[str, str]] = field(default_factory=dict)  # version-specific variables
    source: str = ""  # the official template the pins were taken from
    build_jdk: int = 0  # minimum JDK to *run* the toolchain (0 = the game's Java); javac still targets the game

    def matches(self, minecraft: str) -> bool:
        return any(minecraft.startswith(a) if a.endswith(".") else minecraft == a for a in self.applies)


# Pins copied from the official templates on 2026-10-30. Older versions use separate families because
# their toolchains differ (Loom remap + Mojang mappings, ForgeGradle 6/7, ModDevGradle on 1.21.1) and
# their registry APIs differ (no Properties#setId before 1.21.2).
FAMILY_LIST: tuple[TemplateFamily, ...] = (
    TemplateFamily(
        "fabric",
        "mc26",
        ("26.",),
        {"loom_version": "1.19-SNAPSHOT"},
        "9.8.2",
        source="FabricMC/fabric-example-mod (26.2 branch)",
    ),
    TemplateFamily(
        "fabric",
        "remap",
        ("1.20.1", "1.21.1"),
        {"loom_version": "1.19-SNAPSHOT"},
        "9.8.2",
        per_version={
            "1.20.1": {"resource_location": "new ResourceLocation(MOD_ID, path)"},
            "1.21.1": {"resource_location": "ResourceLocation.fromNamespaceAndPath(MOD_ID, path)"},
        },
        source="FabricMC/fabric-example-mod branches 1.20 / 1.21 (fabric-loom-remap, official Mojang mappings)",
        build_jdk=25,  # Loom 1.19 requires JVM 25 (observed: "Dependency requires at least JVM runtime version 25")
    ),
    TemplateFamily(
        "neoforge", "mc26", ("26.",), {"mdg_version": "2.0.148"}, "9.8.2", source="NeoForgeMDKs/MDK-26.2-ModDevGradle"
    ),
    TemplateFamily(
        "neoforge",
        "mdg1211",
        ("1.21.1",),
        {"mdg_version": "2.0.148"},
        "9.2.1",
        source="NeoForgeMDKs/MDK-1.21.1-ModDevGradle",
    ),
    TemplateFamily(
        "forge",
        "mc26",
        ("26.",),
        {"forgegradle_version": "[7.0.17,8)", "eventbus_validator_version": "7.0.5"},
        "9.8.2",
        source="Forge 26.2-65.1.5 MDK",
    ),
    TemplateFamily(
        "forge",
        "fg7",
        ("1.21.1",),
        {"forgegradle_version": "[7.0.3,8)", "pack_format": "34"},
        "9.3.1",
        source="Forge 1.21.1-52.1.20 MDK",
    ),
    TemplateFamily(
        "forge",
        "fg6",
        ("1.20.1",),
        {"forgegradle_version": "[6.0,6.2)", "pack_format": "15"},
        "8.8",
        source="Forge 1.20.1-47.4.28 MDK",
    ),
)
FAMILIES = {f"{f.loader}/{f.name}": f for f in FAMILY_LIST}


def load_registry() -> dict[str, Any]:
    """The compatibility registry (compatibility.json): combinations MCAT actually built and ran, with dates
    and toolchains. Absence means unverified; nothing here is inferred from upstream metadata."""
    data: dict[str, Any] = json.loads(resources.files("mcat.moddev").joinpath("compatibility.json").read_text())
    return data


def _evidence(kind: str) -> dict[tuple[str, str], str]:
    out = {}
    for e in load_registry()["entries"]:
        rec = e.get(kind)
        if rec:
            detail = rec.get("toolchain") or f"{rec.get('image')}: {rec.get('evidence')}"
            out[(e["loader"], e["minecraft"])] = (
                f"{rec['date']} {kind} OK ({detail}; {e['loader']} {e['loader_version']})"
            )
    return out


# (loader, game version) -> evidence of a real successful `gradle build` of a generated project.
VERIFIED_BUILDS: dict[tuple[str, str], str] = _evidence("build")
# Combinations whose generated mod was also loaded on a real dedicated server (`mcat test mod`).
RUNTIME_VERIFIED: dict[tuple[str, str], str] = _evidence("runtime")


PROJECT_META = ".mcat-project.json"


class ScaffoldError(Exception):
    pass


def templates_root() -> Path:
    env = os.environ.get("MCAT_TEMPLATES_DIR")
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "templates" / "fabric").is_dir():
            return parent / "templates"
    return Path(str(resources.files("mcat") / "_data" / "templates"))


def family_for(loader: str, minecraft: str) -> TemplateFamily | None:
    return next((f for f in FAMILY_LIST if f.loader == loader and f.matches(minecraft)), None)


def _next_minor(mc: str) -> str:
    parts = mc.split(".")
    return f"{parts[0]}.{int(parts[1]) + 1}" if len(parts) >= 2 and parts[1].isdigit() else mc


@dataclass
class ScaffoldPlan:
    loader: str
    minecraft: str
    family: str
    verified: bool
    variables: dict[str, str]
    evidence: list[str]


def plan_scaffold(
    loader: str,
    minecraft: str,
    mod_id: str,
    package: str,
    *,
    mod_name: str | None = None,
    catalog: LoaderCatalog | None = None,
    modrinth: ModrinthProvider | None = None,
    license_: str = "All Rights Reserved",
) -> ScaffoldPlan:
    if not MOD_ID_RE.match(mod_id):
        raise ScaffoldError("mod id must match [a-z][a-z0-9_]{1,63}")
    if not PACKAGE_RE.match(package):
        raise ScaffoldError("package must be a dotted lowercase Java package, e.g. com.example.mymod")
    fam = family_for(loader, minecraft)
    if fam is None:
        raise ScaffoldError(
            f"no MCAT template for {loader} on Minecraft {minecraft}; templates exist for "
            + ", ".join(
                f"{f.loader} {'/'.join(a + 'x' if a.endswith('.') else a for a in f.applies)}" for f in FAMILY_LIST
            )
        )
    catalog = catalog or LoaderCatalog()
    support = catalog.support(loader, minecraft)
    if not support.supported:
        raise ScaffoldError(f"{loader} does not publish a release for Minecraft {minecraft} ({support.evidence})")
    if not support.java:
        raise ScaffoldError(f"could not determine the Java version for Minecraft {minecraft}")
    main_class = "".join(p.capitalize() for p in mod_id.split("_")) or "ExampleMod"
    v = {
        "mod_id": mod_id,
        "mod_name": mod_name or mod_id.replace("_", " ").title(),
        "package": package,
        "main_class": main_class,
        "minecraft_version": minecraft,
        "java_version": str(support.java),
        "mod_version": "0.1.0",
        "group": package,
        "license": license_,
        "minecraft_next": _next_minor(minecraft),
        **fam.pins,
        **fam.per_version.get(minecraft, {}),
    }
    evidence = [support.evidence, f"java {support.java} from Mojang version manifest"]
    loader_version = support.recommended or support.loader_versions[0]
    if loader == "fabric":
        v["loader_version"] = loader_version
        mr = modrinth or ModrinthProvider()
        api = [x for x in mr.versions("fabric-api", minecraft=minecraft, loader="fabric") if x.channel == "release"]
        api = sorted(api, key=lambda x: x.published, reverse=True) or sorted(
            mr.versions("fabric-api", minecraft=minecraft, loader="fabric"), key=lambda x: x.published, reverse=True
        )
        if not api:
            raise ScaffoldError(f"no Fabric API build published for Minecraft {minecraft}")
        v["fabric_api_version"] = api[0].version_number
        evidence.append(f"fabric-api {api[0].version_number} from modrinth:version/{api[0].version_id}")
    elif loader == "neoforge":
        v["neo_version"] = loader_version
    elif loader == "forge":
        v["forge_version"] = loader_version
        v["forge_major"] = loader_version.split(".")[0]
    return ScaffoldPlan(loader, minecraft, fam.name, (loader, minecraft) in VERIFIED_BUILDS, v, evidence)


def _render(text: str, v: dict[str, str], examples: bool) -> str:
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        tag = line.strip()
        if tag == "//#if examples":
            skipping = not examples
            continue
        if tag == "//#endif":
            skipping = False
            continue
        if not skipping:
            out.append(line)
    text = "".join(out)
    for k, val in v.items():
        text = text.replace("{{" + k + "}}", val)
    leftover = re.findall(r"\{\{[a-z_]+\}\}", text)
    if leftover:
        raise ScaffoldError(f"template variables not provided: {sorted(set(leftover))}")
    return text


def write_project(plan: ScaffoldPlan, out: Path, *, examples: bool = False) -> list[Path]:
    src = templates_root() / plan.loader / plan.family
    if not src.is_dir():
        raise ScaffoldError(f"template directory missing: {src}")
    out = ensure_fresh_output(out)
    v = plan.variables
    written = []
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src).as_posix()
        rel = (
            rel.replace("__package__", v["package"].replace(".", "/"))
            .replace("__Main__", v["main_class"])
            .replace("{{mod_id}}", v["mod_id"])
        )
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render(f.read_text(encoding="utf-8"), v, examples), encoding="utf-8")
        written.append(target)
    fam = FAMILIES[f"{plan.loader}/{plan.family}"]
    meta = {
        "loader": plan.loader,
        "minecraft": plan.minecraft,
        "family": plan.family,
        "java": v["java_version"],
        "gradle": fam.gradle,
        "build_jdk": str(max(fam.build_jdk, int(v["java_version"]))),
        "template_source": fam.source,
    }
    (out / PROJECT_META).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    written.append(out / PROJECT_META)
    notices = templates_root() / "THIRD_PARTY_NOTICES.md"  # upstream template licenses travel with the code
    if notices.is_file():
        (out / "THIRD_PARTY_NOTICES.md").write_text(notices.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(out / "THIRD_PARTY_NOTICES.md")
    return written


# ---- builds ----------------------------------------------------------------------------------------

_JAVAC_ERR = re.compile(r"^(?P<file>[^\s:][^:]*\.(?:java|kt)):(?P<line>\d+): error: (?P<msg>.+)$", re.M)
_GRADLE_WHAT = re.compile(r"\* What went wrong:\n(?P<msg>(?:.+\n){1,6})", re.M)


@dataclass
class BuildOutcome:
    ok: bool
    command: list[str]
    returncode: int
    errors: list[dict[str, str]]
    artifacts: list[str]
    log_tail: str
    gradle: str


def gradle_command(project: Path) -> list[str] | None:
    wrapper = project / "gradlew"
    if wrapper.is_file() and (project / "gradle" / "wrapper" / "gradle-wrapper.jar").is_file():
        return [str(wrapper)]
    exe = shutil.which("gradle")
    return [exe] if exe else None


def parse_build_errors(log: str) -> list[dict[str, str]]:
    errors = [{"file": m["file"], "line": m["line"], "message": m["msg"].strip()} for m in _JAVAC_ERR.finditer(log)]
    for m in _GRADLE_WHAT.finditer(log):
        errors.append({"file": "", "line": "", "message": " ".join(m["msg"].split())[:500]})
    return errors[:50]


def build_image(project: Path) -> str:
    """Official Gradle image matching the template's wrapper version and the target's Java."""
    try:
        meta = json.loads((project / PROJECT_META).read_text())
    except (OSError, ValueError) as e:
        raise ScaffoldError(
            f"{PROJECT_META} missing or unreadable; sandboxed builds need an mcat-generated project"
        ) from e
    if not meta.get("gradle") or not meta.get("java"):
        raise ScaffoldError(f"{PROJECT_META} lacks gradle/java")
    # ModDevGradle/ForgeGradle 7 on Gradle 9 need a JDK >= 21 to run, Loom 1.19 needs 25 (javac --release
    # still targets the game's Java); ForgeGradle 6 on Gradle 8.8 runs on the game's JDK 17.
    java = int(meta.get("build_jdk") or meta["java"])
    if int(meta["gradle"].split(".")[0]) >= 9:
        java = max(java, 21)
    return f"gradle:{meta['gradle']}-jdk{java}"


@contextlib.contextmanager
def _exclusive(lock: Path | None) -> Iterator[None]:
    """Serialise sandboxed builds sharing one Gradle cache: Gradle's own cross-process lock handshake
    cannot reach a build in another container (separate network namespace), so two at once corrupt it."""
    if lock is None:
        yield
        return
    try:
        import fcntl
    except ImportError:  # Windows: no container builds share a cache there in practice
        yield
        return
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def run_build(
    project: Path, *, task: str = "build", timeout: int = 1800, sandbox: bool = False, engine: str | None = None
) -> BuildOutcome:
    """Run Gradle. ``sandbox=True`` runs it in an official Gradle container (network on for dependency
    downloads; only the project and a per-image Gradle cache mounted; no host credentials or env)."""
    if sandbox:
        from mcat.validation.runtime.sandbox import ContainerRunner, Limits, detect_engine

        image = build_image(project)
        runner = ContainerRunner(detect_engine(engine), image, Limits(memory_mb=6144, cpus=4, pids=4096, tmp_mb=2048))
        runner.ensure_image()
        cache = (
            Path(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")))
            / "mcat"
            / "gradle"
            / image.replace(":", "-")
        )
        cache.mkdir(parents=True, exist_ok=True)
        gradle = ["gradle"]
        cmd = runner.argv(
            project,
            [*gradle, task, "--console=plain", "--stacktrace", "--no-daemon"],
            name=f"mcat-build-{os.getpid()}",
            network=True,
            extra_volumes={cache: "/gradle-home"},
            extra_env={"GRADLE_USER_HOME": "/gradle-home"},
        )
        env = None
        lock: Path | None = cache.parent / f"{cache.name}.lock"
    else:
        maybe = gradle_command(project)
        if maybe is None:
            raise ScaffoldError("no Gradle wrapper or `gradle` on PATH")
        gradle = maybe
        cmd = [*gradle, task, "--console=plain", "--stacktrace"]
        env = {k: val for k, val in os.environ.items() if not k.startswith("MCAT_") or k == "MCAT_CONTACT"}
        lock = None
    try:
        with _exclusive(lock):
            p = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired as e:
        raise ScaffoldError(f"build timed out after {timeout}s") from e
    log = redact(p.stdout + "\n" + p.stderr)
    jars = sorted(str(j) for j in (project / "build" / "libs").glob("*.jar")) if p.returncode == 0 else []
    return BuildOutcome(
        p.returncode == 0,
        cmd,
        p.returncode,
        parse_build_errors(log) if p.returncode else [],
        jars,
        log[-6000:],
        f"{image} (sandboxed)" if sandbox else " ".join(gradle),
    )
