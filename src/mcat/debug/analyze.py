"""Crash report / latest.log analyzer.

Rules produce findings with an explicit confidence:
* ``confirmed`` - the log states the cause outright (loader dependency report, UnsupportedClassVersionError).
* ``probable``  - a strong, specific pattern (mixin failure naming a config/mod, duplicate-mod report).
* ``possible``  - circumstantial (a mod jar appears in the crashing stack trace).

Log content is untrusted data: it is parsed, never executed or followed as instructions.
"""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mcat.reporting import Evidence, Finding, Result, Severity, VerificationState
from mcat.security.redact import redact

MAX_LOG = 64 * 1024**2
CLASS_VERSION_TO_JAVA = {52: 8, 55: 11, 60: 16, 61: 17, 65: 21, 66: 22, 67: 23, 68: 24, 69: 25, 70: 26}


@dataclass
class Log:
    text: str
    lines: list[str]

    def line_of(self, pos: int) -> int:
        return self.text.count("\n", 0, pos) + 1


Rule = Callable[[Log], list[Finding]]
RULES: list[Rule] = []


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


def _ev(log: Log, m: re.Match[str], detail: str = "") -> Evidence:
    snippet = m.group(0).strip().splitlines()[0][:240]
    return Evidence(f"log:line {log.line_of(m.start())}", detail or snippet)


# ---- Fabric -----------------------------------------------------------------------------------------

_FABRIC_MISSING = re.compile(
    r"Mod '(?P<name>[^']+)' \((?P<id>[^)]+)\) (?P<ver>\S+) requires (?P<req>.+?) of (?:mod )?"
    r"(?:'(?P<depname>[^']+)' \((?P<dep>[^)]+)\)|(?P<bare>[a-z0-9_\-]+)), which is missing!"
)
_FABRIC_WRONG = re.compile(
    r"Mod '(?P<name>[^']+)' \((?P<id>[^)]+)\) (?P<ver>\S+) requires (?P<req>.+?) of (?:mod )?"
    r"(?:'(?P<depname>[^']+)' \((?P<dep>[^)]+)\)|(?P<bare>[a-z0-9_\-]+)), "
    r"but only the wrong version is present: (?P<have>[^!\s]+)!"
)
_FABRIC_BREAKS = re.compile(
    r"Mod '(?P<name>[^']+)' \((?P<id>[^)]+)\) (?P<ver>\S+) is incompatible with (?P<req>.+?) of (?:mod )?"
    r"(?:'(?P<depname>[^']+)' \((?P<dep>[^)]+)\)|(?P<bare>[a-z0-9_\-]+)), but (?:only )?.*?present"
)


@rule
def fabric_dependencies(log: Log) -> list[Finding]:
    out = []
    for m in _FABRIC_MISSING.finditer(log.text):
        dep = m["dep"] or m["bare"]
        out.append(
            Finding(
                "missing-dependency",
                f"{m['id']} {m['ver']} requires {m['req']} of {dep}, which is missing",
                Severity.ERROR,
                confidence="confirmed",
                subject=dep,
                evidence=[_ev(log, m)],
                fix=f"add {dep} ({m['req']}) for this game version; `mcat search {dep}` then resolve",
            )
        )
    for m in _FABRIC_WRONG.finditer(log.text):
        dep = m["dep"] or m["bare"]
        out.append(
            Finding(
                "dependency-version",
                f"{m['id']} requires {m['req']} of {dep}; found {m['have']}",
                Severity.ERROR,
                confidence="confirmed",
                subject=dep,
                evidence=[_ev(log, m)],
                fix=f"change {dep} to a version matching '{m['req']}', or change {m['id']} to a version "
                f"that accepts {dep} {m['have']}",
            )
        )
    for m in _FABRIC_BREAKS.finditer(log.text):
        dep = m["dep"] or m["bare"]
        out.append(
            Finding(
                "incompatible-mods",
                f"{m['id']} is incompatible with {dep} ({m['req']})",
                Severity.ERROR,
                confidence="confirmed",
                subject=dep,
                evidence=[_ev(log, m)],
                fix=f"remove one of {m['id']} / {dep}, or pick versions the authors mark compatible",
            )
        )
    return out


# ---- NeoForge / Forge -------------------------------------------------------------------------------

_FORGE_MISSING = re.compile(
    r"Mod ID: '(?P<dep>[^']+)', Requested by: '(?P<by>[^']+)', Expected range: '(?P<range>[^']*)', "
    r"Actual version: '(?P<have>[^']*)'"
)
_NEO_REQUIRES = re.compile(
    r"Mod (?P<by>[a-z0-9_\-]+) requires (?P<dep>[a-z0-9_\-]+) (?P<range>[^\n]+?)\s*\n\s*Currently, (?P=dep) is "
    r"(?P<have>not installed|[^\n]+)"
)


@rule
def forge_dependencies(log: Log) -> list[Finding]:
    out = []
    for m in _FORGE_MISSING.finditer(log.text):
        missing = m["have"] in ("[MISSING]", "MISSING", "")
        out.append(
            Finding(
                "missing-dependency" if missing else "dependency-version",
                f"{m['by']} requires {m['dep']} {m['range']}" + ("" if missing else f"; found {m['have']}"),
                Severity.ERROR,
                confidence="confirmed",
                subject=m["dep"],
                evidence=[_ev(log, m)],
                fix=f"install {m['dep']} in range {m['range']}",
            )
        )
    for m in _NEO_REQUIRES.finditer(log.text):
        missing = m["have"].strip() == "not installed"
        out.append(
            Finding(
                "missing-dependency" if missing else "dependency-version",
                f"{m['by']} requires {m['dep']} {m['range'].strip()}"
                + ("" if missing else f"; currently {m['have'].strip()}"),
                Severity.ERROR,
                confidence="confirmed",
                subject=m["dep"],
                evidence=[_ev(log, m)],
                fix=f"install {m['dep']} {m['range'].strip()}",
            )
        )
    return out


# ---- loader / Java / duplicates -----------------------------------------------------------------------

_CLASS_VERSION = re.compile(
    r"UnsupportedClassVersionError: (?P<cls>\S+) has been compiled by a more recent version of the Java Runtime "
    r"\(class file version (?P<need>\d+)\.\d+\), this version of the Java Runtime only recognizes class file "
    r"versions up to (?P<have>\d+)\.\d+"
)
_JAVA_REQ = re.compile(
    r"(?:requires|Requires) (?:version )?(?P<req>[^ ]+) of (?:mod )?'?java'?|"
    r"Minecraft \S+ requires Java (?P<req2>\d+)"
)


@rule
def java_version(log: Log) -> list[Finding]:
    out = []
    for m in _CLASS_VERSION.finditer(log.text):
        need, have = int(m["need"]), int(m["have"])
        jn, jh = CLASS_VERSION_TO_JAVA.get(need, need - 44), CLASS_VERSION_TO_JAVA.get(have, have - 44)
        out.append(
            Finding(
                "java-too-old",
                f"{m['cls']} needs Java {jn}+, but the game runs on Java {jh}",
                Severity.ERROR,
                confidence="confirmed",
                evidence=[_ev(log, m)],
                fix=f"launch with Java {jn} (set the instance's Java path / JAVA_HOME)",
            )
        )
    for m in _JAVA_REQ.finditer(log.text):
        out.append(
            Finding(
                "java-requirement",
                f"a mod or the game requires Java {m['req'] or m['req2']}",
                Severity.WARNING,
                confidence="probable",
                evidence=[_ev(log, m)],
            )
        )
    return out


_DUP = re.compile(r"(?:Duplicate mod|Found duplicate mods?)[:\s]+(?P<rest>[^\n]+(?:\n\s+[^\n]+){0,6})", re.I)
_DUP_NEO = re.compile(r"Mod ID: '(?P<id>[^']+)' from mod files: (?P<files>[^\n]+)")


@rule
def duplicates(log: Log) -> list[Finding]:
    out = []
    for m in _DUP_NEO.finditer(log.text):
        files = [f.strip() for f in m["files"].split(",")]
        out.append(
            Finding(
                "duplicate-mod",
                f"mod id {m['id']} is provided by several jars: {', '.join(files)}",
                Severity.ERROR,
                confidence="confirmed",
                subject=m["id"],
                evidence=[_ev(log, m)],
                fix="keep exactly one jar for this mod id (usually the newest compatible one)",
            )
        )
    if not out:
        for m in _DUP.finditer(log.text):
            out.append(
                Finding(
                    "duplicate-mod",
                    "loader reported duplicate mods: " + " ".join(m["rest"].split())[:300],
                    Severity.ERROR,
                    confidence="probable",
                    evidence=[_ev(log, m)],
                    fix="remove the extra copies so each mod id appears once",
                )
            )
    return out


_WRONG_LOADER = re.compile(
    r"(?P<file>[\w.\-+]+\.jar)[^\n]{0,120}?(?:is a Fabric mod|is a (?:Neo)?Forge mod|"
    r"is not a valid mod file|not a valid (?:Neo)?Forge mod)|"
    r"(?:Found|Detected) (?:a )?(?P<kind>Fabric|Forge|NeoForge|Quilt) mod[^\n]*?(?P<file2>[\w.\-+]+\.jar)",
    re.I,
)


@rule
def wrong_loader(log: Log) -> list[Finding]:
    return [
        Finding(
            "wrong-loader",
            f"{m['file'] or m['file2']} appears to be built for a different loader",
            Severity.ERROR,
            confidence="probable",
            subject=m["file"] or m["file2"],
            evidence=[_ev(log, m)],
            fix="replace it with the build for this pack's loader, or remove it",
        )
        for m in _WRONG_LOADER.finditer(log.text)
    ]


# ---- mixins -----------------------------------------------------------------------------------------

_MIXIN_APPLY = re.compile(
    r"Mixin \[(?P<cfg>[\w.\-]+\.json):(?P<mixin>[\w.$]+) from mod (?P<mod>[\w\-]+)\] from phase \[\w+\] in config "
    r"\[[^\]]+\] FAILED during (?P<phase>\w+)|"
    r"(?:Critical injection failure|InvalidInjectionException|InvalidMixinException)[^\n]*?"
    r"(?P<cfg2>[\w.\-]+\.mixins?\.json):(?P<mixin2>[\w.$]+)(?: from mod (?P<mod2>[\w\-]+))?"
)
_MIXIN_CFG_MISSING = re.compile(r"Mixin config (?P<cfg>[\w.\-]+\.json) does not exist")


@rule
def mixins(log: Log) -> list[Finding]:
    out = []
    seen = set()
    for m in _MIXIN_APPLY.finditer(log.text):
        cfg, mixin, mod = m["cfg"] or m["cfg2"], m["mixin"] or m["mixin2"], m["mod"] or m["mod2"]
        if (cfg, mixin) in seen:
            continue
        seen.add((cfg, mixin))
        owner = mod or cfg.split(".")[0]
        out.append(
            Finding(
                "mixin-failure",
                f"mixin {mixin} ({cfg}) from {owner} failed to apply",
                Severity.ERROR,
                confidence="probable",
                subject=owner,
                evidence=[_ev(log, m)],
                fix=f"{owner} is likely built for a different game/loader version or conflicts with "
                "another mod targeting the same code; update or remove it",
            )
        )
    for m in _MIXIN_CFG_MISSING.finditer(log.text):
        out.append(
            Finding(
                "mixin-config-missing",
                f"mixin config {m['cfg']} is missing",
                Severity.ERROR,
                confidence="probable",
                evidence=[_ev(log, m)],
                fix="a mod jar is corrupt or built for another loader; re-download it",
            )
        )
    return out


# ---- environment / misc -------------------------------------------------------------------------------

_SIMPLE: list[tuple[str, re.Pattern[str], str, str, str]] = [
    (
        "out-of-memory",
        re.compile(r"java\.lang\.OutOfMemoryError: (?P<kind>[^\n]+)"),
        "confirmed",
        "the JVM ran out of memory ({kind})",
        "raise -Xmx (e.g. 6-8 GB for large packs) or remove heavy mods",
    ),
    (
        "eula",
        re.compile(r"You need to agree to the EULA in order to run the server"),
        "confirmed",
        "server EULA not accepted",
        "set eula=true in eula.txt after reading https://aka.ms/MinecraftEULA",
    ),
    (
        "port-in-use",
        re.compile(r"FAILED TO BIND TO PORT|Address already in use"),
        "confirmed",
        "server port already in use",
        "stop the other server or change server-port in server.properties",
    ),
    (
        "server-watchdog",
        re.compile(r"A single server tick took [\d.]+ seconds"),
        "probable",
        "server watchdog killed a hung tick",
        "find the mod in the attached thread dump; raise max-tick-time only as a diagnostic",
    ),
    (
        "graphics-driver",
        re.compile(
            r"GLFW error 65542|Pixel format not accelerated|"
            r"WGL: The driver does not appear to support OpenGL"
        ),
        "probable",
        "OpenGL context could not be created",
        "update the GPU driver / select the discrete GPU",
    ),
    (
        "datapack-load-failure",
        re.compile(r"Failed to load datapacks, can't proceed with server load"),
        "confirmed",
        "datapacks failed to load",
        "look for the preceding 'Couldn't parse' / 'Failed to load' lines naming the file",
    ),
    (
        "registry-missing",
        re.compile(
            r"(?:Unknown|Missing) (?:registry|item|block) (?:key|entry|id)?[^\n]*?"
            r"(?P<id>[a-z0-9_.\-]+:[a-z0-9_./\-]+)"
        ),
        "probable",
        "reference to an unregistered id {id}",
        "the mod defining it is missing, or a script/datapack uses a renamed id",
    ),
    (
        "incompatible-mod-set",
        re.compile(r"Mod resolution encountered an incompatible mod set!"),
        "confirmed",
        "Fabric reported an incompatible mod set",
        "see the dependency findings that follow it",
    ),
]


@rule
def simple(log: Log) -> list[Finding]:
    out = []
    for code, pat, conf, msg, fix in _SIMPLE:
        m = pat.search(log.text)
        if m:
            groups = defaultdict(lambda: "?", {k: v for k, v in m.groupdict().items() if v})
            out.append(
                Finding(
                    code,
                    msg.format_map(groups),
                    Severity.ERROR if conf == "confirmed" else Severity.WARNING,
                    confidence=conf,
                    evidence=[_ev(log, m)],
                    fix=fix,
                )
            )
    return out


_NOCLASS = re.compile(r"(?:NoClassDefFoundError|ClassNotFoundException): (?P<cls>[\w/.$]+)")
_FRAME_JAR = re.compile(r"\bat [\w.$<>]+\([^)]*\) ~?\[(?P<jar>[^\]:]+\.jar)[:\]]")
_FRAME_MOD = re.compile(r"\bat [\w.$<>]+\([^)]*\) ~?\[(?P<mod>[a-z0-9_\-]+)(?:-[^\]]*)?[:\]]")
_DESCRIPTION = re.compile(r"^Description: (?P<d>.+)$", re.M)
_SUSPECTS = re.compile(r"Suspected Mods?:\s*(?P<s>(?:[^\n]+\n?)(?:\t[^\n]+\n?)*)")


@rule
def stack_attribution(log: Log) -> list[Finding]:
    out = []
    for m in _NOCLASS.finditer(log.text):
        cls = m["cls"].replace("/", ".")
        out.append(
            Finding(
                "missing-class",
                f"class {cls} could not be loaded",
                Severity.ERROR,
                confidence="probable",
                evidence=[_ev(log, m)],
                fix="a library/dependency is missing or a mod targets a different game version "
                "(check the mod whose package matches)",
            )
        )
        break
    s = _SUSPECTS.search(log.text)
    if s and "None" not in s["s"][:20]:
        out.append(
            Finding(
                "suspected-mods",
                "crash report names suspects: " + " ".join(s["s"].split())[:300],
                Severity.WARNING,
                confidence="possible",
                evidence=[_ev(log, s)],
            )
        )
    jars: dict[str, int] = {}
    for m in _FRAME_JAR.finditer(log.text[:200_000]):
        j = m["jar"].rsplit("/", 1)[-1]
        if not j.startswith(
            (
                "minecraft",
                "client-intermediary",
                "server-",
                "neoforge-",
                "forge-",
                "fabric-loader",
                "fmlloader",
                "java.base",
                "mixin",
                "modlauncher",
                "bootstraplauncher",
                "securejarhandler",
                "loader-",
                "DataFixerUpper",
                "brigadier",
                "netty",
                "guava",
            )
        ):
            jars[j] = jars.get(j, 0) + 1
    if jars:
        top = sorted(jars.items(), key=lambda kv: -kv[1])[:3]
        out.append(
            Finding(
                "stack-trace-mods",
                "mods present in the crashing stack: " + ", ".join(f"{j} ({n} frames)" for j, n in top),
                Severity.WARNING,
                confidence="possible",
                evidence=[Evidence("log:stack trace", "frame attribution")],
                fix="update or temporarily remove the top mod and retest; appearing in a stack trace "
                "does not prove it caused the crash",
            )
        )
    return out


def analyze_text(text: str) -> list[Finding]:
    log = Log(text, text.splitlines())
    findings: list[Finding] = []
    for r in RULES:
        findings.extend(r(log))
    order = {"confirmed": 0, "probable": 1, "possible": 2, None: 3}
    return sorted(findings, key=lambda f: order.get(f.confidence, 3))


def analyze_file(path: Path, *, pack: Path | None = None, apply: bool = False) -> Result:
    r = Result("debug", summary=str(path))
    try:
        if path.stat().st_size > MAX_LOG:
            r.fail(f"log larger than {MAX_LOG // 1024**2} MiB; pass the relevant section")
            return r
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        r.fail(f"cannot read log: {e.strerror}")
        return r
    kind = "crash-report" if "---- Minecraft Crash Report ----" in text else "log"
    d = _DESCRIPTION.search(text)
    r.data = {
        "kind": kind,
        "description": d["d"] if d else None,
        "loader": _detect_loader(text),
        "minecraft": _detect_mc(text),
    }
    for f in analyze_text(text):
        f.message = redact(f.message)
        r.add(f)
    if pack is not None:
        _cross_check(pack, r)
    if not r.findings:
        r.degrade("no known failure pattern matched; share the full crash report or the first ERROR/Exception lines")
    confirmed = [f for f in r.findings if f.confidence == "confirmed"]
    r.summary = (
        f"{kind}: {len(confirmed)} confirmed, "
        f"{sum(f.confidence == 'probable' for f in r.findings)} probable, "
        f"{sum(f.confidence == 'possible' for f in r.findings)} possible cause(s)"
    )
    if not confirmed and r.findings:
        r.degrade()
    return r


def _detect_loader(text: str) -> str | None:
    for marker, loader in (
        ("net.neoforged", "neoforge"),
        ("Loading Minecraft .* with Fabric Loader", "fabric"),
        ("fabricloader", "fabric"),
        ("net.minecraftforge", "forge"),
        ("quilt_loader", "quilt"),
    ):
        if re.search(marker, text):
            return loader
    return None


def _detect_mc(text: str) -> str | None:
    m = re.search(r"Minecraft Version: (\S+)|Loading Minecraft (\S+)|minecraft server version (\S+)", text)
    return next((g for g in m.groups() if g), None) if m else None


def _cross_check(pack: Path, r: Result) -> None:
    from mcat.parsers.packs import parse_pack
    from mcat.parsers.source import PackSource

    try:
        with PackSource(pack) as src:
            info = parse_pack(src)
    except Exception as e:
        r.warnings.append(f"could not read pack for cross-check: {type(e).__name__}: {e}")
        return
    present = {m.mod_id: e.path for e in info.entries for m in e.jar_mods}
    r.data["pack_mod_count"] = len(info.entries)
    for f in r.findings:
        if f.code == "missing-dependency" and f.subject:
            if f.subject in present:
                f.evidence.append(
                    Evidence(
                        f"file:{present[f.subject]}",
                        "jar IS present: it may be disabled, "
                        "the wrong loader, or failed to load - check earlier errors",
                    )
                )
                f.confidence = "probable"
            else:
                f.evidence.append(Evidence("pack", f"no jar provides mod id {f.subject}"))


def plan_repairs(pack: Path) -> list[tuple[str, str]]:
    """Safe, reversible repairs derivable from jar metadata alone: (jar path, reason).

    Only two classes qualify: jars built for another loader, and duplicate mod ids (the newest version is
    kept). Everything else needs a human decision or a resolver run.
    """
    from mcat.parsers.packs import parse_pack
    from mcat.parsers.source import PackSource
    from mcat.resolver.versions import Version

    with PackSource(pack) as src:
        info = parse_pack(src)
    repairs: list[tuple[str, str]] = []
    if info.loader:
        for e in info.entries:
            loaders = {m.loader for m in e.jar_mods}
            if (
                e.embedded
                and loaders
                and info.loader not in loaders
                and not (info.loader == "quilt" and "fabric" in loaders)
            ):
                repairs.append((e.path, f"built for {sorted(loaders)}, pack loader is {info.loader}"))
    by_id: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for e in info.entries:
        for m in e.jar_mods:
            by_id[m.mod_id].append((e.path, m.version))
    for mid, items in by_id.items():
        if len(items) > 1:
            keep = max(items, key=lambda it: Version(it[1]))
            repairs += [(p, f"duplicate of {mid}; keeping {keep[0]} ({keep[1]})") for p, _ in items if p != keep[0]]
    return sorted(set(repairs))


def quarantine(pack: Path, files: list[str]) -> list[str]:
    """Move jars to ``<pack>/mcat-quarantine/`` (reversible; never deletes). Returns moved paths."""
    qdir = pack / "mcat-quarantine"
    qdir.mkdir(exist_ok=True)
    moved = []
    for rel in files:
        src = (pack / rel).resolve()
        if not src.is_relative_to(pack.resolve()) or not src.is_file():
            continue
        dest = qdir / src.name
        if dest.exists():
            continue
        shutil.move(src, dest)
        moved.append(f"{rel} -> {dest.relative_to(pack)}")
    return moved


__all__ = ["VerificationState", "analyze_file", "analyze_text", "quarantine"]
