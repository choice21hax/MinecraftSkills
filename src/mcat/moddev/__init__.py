"""Loader-specific mod project scaffolding and Gradle builds.

Each loader has its own template family. A template is only *verified* for a game version after a real
Gradle compilation of the generated project succeeded; ``VERIFIED_BUILDS`` records those runs. Other
versions in the same family are "unverified" and need ``--allow-unverified``. Versions with no template
family are reported unsupported rather than approximated.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

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
    applies: tuple[str, ...]  # game version prefixes the family targets (Mojang-named 26.x era)
    pins: dict[str, str] = field(default_factory=dict)  # plugin versions taken from official templates


# Plugin pins copied from the official templates (FabricMC/fabric-example-mod 26.2 branch,
# NeoForgeMDKs/MDK-26.2-ModDevGradle, Forge 26.2-65.1.5 MDK) on 2026-10-30.
FAMILIES = {
    "fabric": TemplateFamily("fabric", "mc26", ("26.",), {"loom_version": "1.19-SNAPSHOT"}),
    "neoforge": TemplateFamily("neoforge", "mc26", ("26.",), {"mdg_version": "2.0.148"}),
    "forge": TemplateFamily(
        "forge", "mc26", ("26.",), {"forgegradle_version": "[7.0.17,8)", "eventbus_validator_version": "7.0.5"}
    ),
}

# (loader, game version) -> evidence of a real successful `gradle build` of a generated project.
# Recorded by running `mcat mod init --with-examples` + `mcat mod build --allow-build` (Gradle 9.8.1,
# Temurin JDK 25.0.4.1, Arch Linux). Compilation only: no game launch was performed.
VERIFIED_BUILDS: dict[tuple[str, str], str] = {
    ("fabric", "26.2"): "2026-10-30 gradle build OK (loader 0.20.1, fabric-api 0.161.0+26.2, loom 1.19-SNAPSHOT)",
    ("neoforge", "26.2"): "2026-10-30 gradle build OK (neoforge 26.2.0.93, ModDevGradle 2.0.148)",
    ("forge", "26.2"): "2026-10-30 gradle build OK (forge 26.2-65.1.5, ForgeGradle 7.x)",
}


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
    fam = FAMILIES.get(loader)
    if fam and any(minecraft.startswith(p) for p in fam.applies):
        return fam
    return None


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
            + ", ".join(f"{f.loader} {'/'.join(f.applies)}x" for f in FAMILIES.values())
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


def run_build(project: Path, *, task: str = "build", timeout: int = 1800) -> BuildOutcome:
    gradle = gradle_command(project)
    if gradle is None:
        raise ScaffoldError("no Gradle wrapper or `gradle` on PATH")
    cmd = [*gradle, task, "--console=plain", "--stacktrace"]
    env = {k: val for k, val in os.environ.items() if not k.startswith("MCAT_") or k == "MCAT_CONTACT"}
    p = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    log = redact(p.stdout + "\n" + p.stderr)
    jars = sorted(str(j) for j in (project / "build" / "libs").glob("*.jar")) if p.returncode == 0 else []
    return BuildOutcome(
        p.returncode == 0,
        cmd,
        p.returncode,
        parse_build_errors(log) if p.returncode else [],
        jars,
        log[-6000:],
        " ".join(gradle),
    )
