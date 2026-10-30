"""Structural, metadata and dependency validation of a pack (offline by default).

Checks:
* pack metadata: game version, loader and loader version present
* packwiz index hashes match file contents; lockfile agrees with packwiz metadata
* every entry has a hash and provenance
* embedded JARs: correct loader, required dependencies present and version-compatible, no declared
  incompatibilities, no duplicate mod ids, game version constraint satisfied
* side sanity: client-only mods are flagged for server distributions
* (--online) every provider entry's version actually lists the target game version and loader
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

from mcat.parsers.packs import PackFormatError, PackInfo, parse_pack
from mcat.parsers.source import PackSource
from mcat.reporting import Evidence, Finding, Result, Severity, Status, VerificationState
from mcat.resolver.versions import satisfies
from mcat.security.archive import UnsafeArchiveError

BUILTIN_IDS = {
    "fabric": {"minecraft", "java", "fabricloader", "fabric-loader"},
    "quilt": {"minecraft", "java", "quilt_loader", "fabricloader"},
    "neoforge": {"minecraft", "neoforge", "java"},
    "forge": {"minecraft", "forge", "java"},
}


def _check_packwiz_index(src: PackSource, r: Result) -> None:
    try:
        pack = tomllib.loads(src.read_text("pack.toml"))
        idx_name = pack.get("index", {}).get("file", "index.toml")
        idx_raw = src.read(idx_name)
    except (KeyError, tomllib.TOMLDecodeError) as e:
        r.add(Finding("packwiz-index", f"cannot read packwiz index: {e}", Severity.ERROR))
        return
    algo = pack.get("index", {}).get("hash-format", "sha256")
    want = pack.get("index", {}).get("hash")
    if want and algo in hashlib.algorithms_available and hashlib.new(algo, idx_raw).hexdigest() != want:
        r.add(
            Finding(
                "packwiz-index-hash",
                "index.toml hash does not match pack.toml",
                Severity.ERROR,
                state=VerificationState.FAILED,
            )
        )
    idx = tomllib.loads(idx_raw.decode())
    ialgo = idx.get("hash-format", "sha256")
    bad = 0
    for f in idx.get("files", []):
        try:
            data = src.read(f["file"])
        except KeyError:
            r.add(Finding("packwiz-missing-file", f"indexed file missing: {f['file']}", Severity.ERROR))
            continue
        algo_f = f.get("hash-format", ialgo)
        if algo_f in hashlib.algorithms_available and hashlib.new(algo_f, data).hexdigest() != f.get("hash"):
            bad += 1
            r.add(
                Finding(
                    "packwiz-hash-mismatch",
                    f"{f['file']} content does not match index hash",
                    Severity.ERROR,
                    state=VerificationState.FAILED,
                )
            )
    if not bad:
        r.add(
            Finding("packwiz-index", "packwiz index hashes verified", Severity.INFO, state=VerificationState.VERIFIED)
        )


def _check_lock(src: PackSource, pack: PackInfo, r: Result) -> None:
    if not src.exists("mcat.lock.json"):
        return
    lock = json.loads(src.read_text("mcat.lock.json"))
    t = lock.get("target", {})
    if (t.get("minecraft"), t.get("loader")) != (pack.minecraft, pack.loader):
        r.add(
            Finding(
                "lock-target-mismatch",
                f"lock targets {t.get('minecraft')}/{t.get('loader')} but pack declares {pack.minecraft}/{pack.loader}",
                Severity.ERROR,
            )
        )
    by_version = {e.version_id: e for e in pack.entries if e.version_id}
    for m in lock.get("mods", []):
        e = by_version.get(m["version_id"])
        if e is None:
            r.add(
                Finding(
                    "lock-entry-missing", f"{m['slug']} {m['version_number']} in lock but not in pack", Severity.ERROR
                )
            )
            continue
        for algo, h in e.hashes.items():
            if m["file"]["hashes"].get(algo) not in (None, h):
                r.add(
                    Finding(
                        "lock-hash-mismatch",
                        f"{m['slug']}: {algo} differs between lock and pack",
                        Severity.ERROR,
                        state=VerificationState.FAILED,
                    )
                )
    _check_lock_closure(lock, r)


def _check_lock_closure(lock: dict[str, Any], r: Result) -> None:
    """Dependency closure recorded at resolve time: catches hand-edited packs before any launch."""
    present = {(m["provider"], m["project_id"]) for m in lock.get("mods", [])}
    present |= {
        (eq.split(":", 1)[0], eq.split(":", 1)[1].split("/", 1)[0])
        for m in lock.get("mods", [])
        for eq in m.get("equivalents", [])
    }
    suspect = {(x["from"], x["to"]): x["reason"] for x in lock.get("suspect_dependencies", [])}
    for m in lock.get("mods", []):
        for d in m.get("dependencies", []):
            if d.get("kind") == "required" and d.get("project_id") and (m["provider"], d["project_id"]) not in present:
                why = suspect.get((m["key"], f"{m['provider']}:{d['project_id']}"))
                if why:  # recorded at resolve time with its evidence; still needs a runtime check
                    r.add(
                        Finding(
                            "dependency-metadata-suspect",
                            f"{m['slug']}: {why}",
                            Severity.WARNING,
                            confidence="probable",
                            state=VerificationState.UNVERIFIED,
                            subject=m["slug"],
                            evidence=[Evidence(f"{m['provider']}:version/{m['version_id']}")],
                        )
                    )
                    continue
                r.add(
                    Finding(
                        "lock-dependency-missing",
                        f"{m['slug']} requires {m['provider']} project {d['project_id']}, which is not in the pack",
                        Severity.ERROR,
                        confidence="confirmed",
                        state=VerificationState.FAILED,
                        subject=m["slug"],
                        evidence=[Evidence(f"{m['provider']}:version/{m['version_id']}")],
                    )
                )


def _check_jars(pack: PackInfo, r: Result) -> None:
    jar_entries = [e for e in pack.entries if e.jar_mods]
    if not jar_entries:
        return
    loader = pack.loader
    provided: dict[str, str] = {}
    owners: dict[str, list[str]] = defaultdict(list)
    for e in jar_entries:
        for m in e.jar_mods:
            owners[m.mod_id].append(e.path)
            provided[m.mod_id] = m.version
            for p in m.provides:
                provided.setdefault(p, m.version)
    for mid, paths in owners.items():
        if len(paths) > 1:
            r.add(
                Finding(
                    "duplicate-mod",
                    f"mod id {mid!r} provided by {len(paths)} jars: {', '.join(paths)}",
                    Severity.ERROR,
                    confidence="confirmed",
                    subject=mid,
                )
            )
    if pack.minecraft:
        provided.setdefault("minecraft", pack.minecraft)
    if pack.loader_version and loader:
        for lid in {"fabric": ["fabricloader"], "neoforge": ["neoforge"], "forge": ["forge"]}.get(loader, []):
            provided.setdefault(lid, pack.loader_version)
    for e in jar_entries:
        for m in e.jar_mods:
            if loader and m.loader != loader and not (loader == "quilt" and m.loader == "fabric"):
                r.add(
                    Finding(
                        "wrong-loader",
                        f"{e.path}: built for {m.loader}, pack uses {loader}",
                        Severity.ERROR,
                        confidence="confirmed",
                        subject=e.path,
                        evidence=[Evidence(f"file:{e.path}", "jar metadata")],
                    )
                )
                continue
            for d in m.dependencies:
                if d.mod_id in ("java",):
                    continue
                have = provided.get(d.mod_id)
                if d.kind == "required":
                    if have is None:
                        if d.mod_id in BUILTIN_IDS.get(m.loader, set()):
                            continue  # loader/game version unknown: cannot check
                        r.add(
                            Finding(
                                "missing-dependency",
                                f"{m.mod_id} requires {d.mod_id} {d.constraint}",
                                Severity.ERROR,
                                confidence="confirmed",
                                subject=m.mod_id,
                                evidence=[Evidence(f"file:{e.path}", "declared dependency")],
                                fix=f"add {d.mod_id} matching {d.constraint}",
                            )
                        )
                        continue
                    ok = satisfies(m.loader, d.constraint, have)
                    if ok is False:
                        r.add(
                            Finding(
                                "dependency-version",
                                f"{m.mod_id} requires {d.mod_id} {d.constraint}, found {have}",
                                Severity.ERROR,
                                confidence="confirmed",
                                subject=m.mod_id,
                                evidence=[Evidence(f"file:{e.path}", "declared dependency")],
                            )
                        )
                    elif ok is None:
                        r.add(
                            Finding(
                                "dependency-unchecked",
                                f"{m.mod_id}: could not evaluate constraint {d.mod_id} {d.constraint!r} against {have}",
                                Severity.INFO,
                                state=VerificationState.UNVERIFIED,
                            )
                        )
                elif d.kind == "incompatible" and have is not None:
                    if satisfies(m.loader, d.constraint, have) is not False:
                        r.add(
                            Finding(
                                "incompatible-mods",
                                f"{m.mod_id} declares {d.mod_id} {d.constraint} incompatible (found {have})",
                                Severity.ERROR,
                                confidence="confirmed",
                                subject=m.mod_id,
                            )
                        )


def validate_path(path: Path, *, online: bool = False, server: bool = False) -> Result:
    r = Result("validate", summary=str(path))
    try:
        with PackSource(path) as src:
            pack = parse_pack(src)
            if pack.format == "packwiz":
                _check_packwiz_index(src, r)
            _check_lock(src, pack, r)
            fps = _embedded_fingerprints(src, pack) if online else {}
    except (FileNotFoundError, PackFormatError, UnsafeArchiveError, ValueError) as e:
        r.fail(f"cannot read pack: {e}")
        return r
    r.data = {
        "format": pack.format,
        "minecraft": pack.minecraft,
        "loader": pack.loader,
        "loader_version": pack.loader_version,
        "entries": len(pack.entries),
    }
    for key, val in (("minecraft", pack.minecraft), ("loader", pack.loader), ("loader_version", pack.loader_version)):
        if not val:
            r.add(Finding("metadata-missing", f"pack does not declare {key}", Severity.ERROR))
    for p in pack.problems:
        r.add(Finding("pack-problem", p, Severity.WARNING))
    for entry in pack.entries:
        if not entry.hashes and not entry.embedded and entry.provider != "curseforge":
            r.add(
                Finding(
                    "no-hash", f"{entry.path} has no hash; integrity unverifiable", Severity.WARNING, subject=entry.path
                )
            )
        if server and entry.side == "client":
            r.add(
                Finding(
                    "client-only-on-server",
                    f"{entry.path} is client-only and must not be in a server pack",
                    Severity.WARNING,
                    subject=entry.path,
                )
            )
    _check_jars(pack, r)
    if online:
        _check_online(pack, r, fps)
    else:
        unverified = [e for e in pack.entries if not e.embedded]
        if unverified:
            r.add(
                Finding(
                    "provider-compat-unverified",
                    f"{len(unverified)} provider entries were not checked against provider metadata (use --online)",
                    Severity.INFO,
                    state=VerificationState.UNVERIFIED,
                )
            )
    errors = [f for f in r.findings if f.severity in (Severity.ERROR, Severity.CRITICAL)]
    if errors:
        r.status = Status.FAILED
        r.summary = f"{len(errors)} error(s)"
    elif any(f.state == VerificationState.UNVERIFIED for f in r.findings):
        r.degrade()
        r.summary = "structurally valid; some checks unverified"
    else:
        r.summary = "valid"
    r.warnings.append("validation does not launch the game; gameplay, rendering and worldgen are unverified")
    return r


def _embedded_fingerprints(src: PackSource, pack: PackInfo) -> dict[int, str]:
    """CurseForge fingerprints of jars physically present in the pack (fingerprint -> path)."""
    from mcat.providers.curseforge import fingerprint

    out: dict[int, str] = {}
    roots = [f"{root}/" if root else "" for root in (pack.override_roots or [""])]
    for e in pack.entries:
        if not (e.embedded and e.path.endswith(".jar")):
            continue
        name = next((rt + e.path for rt in roots if src.exists(rt + e.path)), None)
        if name is None:
            continue
        try:
            out[fingerprint(src.read(name, 512 * 1024**2))] = e.path
        except (KeyError, OSError, UnsafeArchiveError):
            continue
    return out


def _check_online(pack: PackInfo, r: Result, embedded_fps: dict[int, str] | None = None) -> None:
    _check_online_modrinth(pack, r)
    _check_online_curseforge(pack, r, embedded_fps or {})


_CF_CLASSES = {6: "mod", 12: "resource pack", 6552: "shader pack", 6945: "data pack", 4546: "customization"}


def _cf_has_target_files(mod: dict[str, Any] | None, minecraft: str | None, loader_type: int | None) -> bool | None:
    """Whether CurseForge's ``latestFilesIndexes`` lists any file for the target (None: unknown)."""
    if not mod or not minecraft:
        return None
    idx = mod.get("latestFilesIndexes") or []
    return any(i.get("gameVersion") == minecraft and i.get("modLoader") in (loader_type, None) for i in idx)


def _check_online_curseforge(pack: PackInfo, r: Result, embedded_fps: dict[int, str]) -> None:
    """Identity, target, hash, dependency and distribution checks against the official CurseForge API.

    CurseForge game-version/loader tags and file dependency lists are author-entered metadata, so
    disagreements are reported as probable (not confirmed) problems. Entries that could not be looked
    up are reported as unverified, never as passing.
    """
    from mcat.models import DepKind
    from mcat.providers.curseforge import (
        LOADER_TYPES,
        CurseForgeProvider,
        CurseForgeUnavailable,
        normalize_file,
    )
    from mcat.providers.http import NetworkError
    from mcat.resolver.core import LOADER_COMPAT

    entries = [e for e in pack.entries if e.provider == "curseforge" and e.version_id and e.project_id]
    if not entries:
        return

    def unverified(reason: str) -> None:
        r.add(
            Finding(
                "provider-compat-unverified",
                f"{len(entries)} CurseForge entries not checked online: {reason}",
                Severity.WARNING,
                state=VerificationState.UNVERIFIED,
            )
        )

    try:
        cf = CurseForgeProvider()
        files = cf.files_batch([e.version_id for e in entries if e.version_id])
        embedded = cf.match_fingerprints(sorted(embedded_fps)) if embedded_fps else {}
        mods = cf.mods_batch(sorted({str(f["modId"]) for f in files.values()}))
    except CurseForgeUnavailable as err:
        unverified(str(err))
        return
    except NetworkError as err:
        unverified(f"lookup failed ({err})")
        return
    in_pack = {e.project_id for e in entries} | {v.project_id for v in embedded.values()}
    for fp, v in sorted(embedded.items()):
        r.add(
            Finding(
                "embedded-identified",
                f"{embedded_fps[fp]} is CurseForge project {v.slug} ({v.project_id}), file {v.version_id}",
                Severity.INFO,
                state=VerificationState.VERIFIED,
                evidence=[Evidence(f"curseforge fingerprint {fp}")],
                subject=embedded_fps[fp],
            )
        )
    identified = {embedded_fps[fp] for fp in embedded}
    unidentified = sorted(
        e.path for e in pack.entries if e.provider != "curseforge" and e.path not in identified
    )  # entries that might provide a dependency under another identity
    maybe = f" (it may be provided by an entry not identified on CurseForge: {', '.join(unidentified[:5])})"
    loader_type = LOADER_TYPES.get(pack.loader or "")
    dep_targets = cf.mods_batch(
        sorted(
            {
                str(d["modId"])
                for f in files.values()
                for d in f.get("dependencies", [])
                if d.get("relationType") == 3 and str(d["modId"]) not in in_pack
            }
        )
    )
    verified = 0
    for e in entries:
        raw = files.get(str(e.version_id))
        if raw is None:
            r.add(
                Finding(
                    "provider-lookup-failed",
                    f"{e.path}: CurseForge file {e.version_id} not returned by the API",
                    Severity.WARNING,
                    state=VerificationState.UNVERIFIED,
                    subject=e.path,
                )
            )
            continue
        ev = [Evidence(f"curseforge:file/{e.version_id}")]
        mod = mods.get(str(raw["modId"]))
        kind = _CF_CLASSES.get(int((mod or {}).get("classId") or 6), "other")
        label = e.path if kind == "mod" else f"{e.path} ({kind} {raw.get('fileName', '')})"
        problems = 0

        def report(code: str, msg: str, sev: Severity, *, _l: str = label, _e: Any = e, **kw: Any) -> None:
            nonlocal problems
            problems += 1
            r.add(
                Finding(
                    code,
                    f"{_l}: {msg}",
                    sev,
                    evidence=[Evidence(f"curseforge:file/{_e.version_id}")],
                    subject=_e.path,
                    **kw,
                )
            )

        if str(raw["modId"]) != str(e.project_id):
            report(
                "provider-identity-mismatch",
                f"file {e.version_id} belongs to project {raw['modId']}, not {e.project_id}",
                Severity.ERROR,
                state=VerificationState.FAILED,
            )
        v = normalize_file(raw, mod)
        f = v.primary_file
        theirs = f.hashes if f else {}
        for algo, value in e.hashes.items():
            if algo in theirs and theirs[algo] != value:
                report(
                    "hash-mismatch",
                    f"{algo} does not match CurseForge's file {e.version_id}",
                    Severity.ERROR,
                    state=VerificationState.FAILED,
                )
        if kind != "mod":
            if pack.minecraft and v.game_versions and pack.minecraft not in v.game_versions:
                report(
                    "provider-metadata-mismatch",
                    f"{kind} is tagged {sorted(v.game_versions)}, not {pack.minecraft}; it may still load "
                    "(resource/data pack formats are checked at runtime)",
                    Severity.INFO,
                    state=VerificationState.UNVERIFIED,
                )
        else:
            if pack.minecraft and pack.minecraft not in v.game_versions:
                report(
                    "provider-incompatible",
                    f"CurseForge file {v.version_number} is tagged {v.game_versions}, not {pack.minecraft}",
                    Severity.ERROR,
                    confidence="probable",
                )
            if pack.loader and v.loaders and not set(v.loaders) & LOADER_COMPAT.get(pack.loader, {pack.loader}):
                report(
                    "provider-metadata-mismatch",
                    f"CurseForge tags this file {v.loaders}, not {pack.loader}; the jar may still support "
                    f"{pack.loader} (check its metadata or test at runtime)",
                    Severity.WARNING,
                    confidence="probable",
                    state=VerificationState.UNVERIFIED,
                )
            elif pack.loader and not v.loaders:
                report(
                    "provider-loader-untagged",
                    "CurseForge file carries no loader tag; loader compatibility unverified",
                    Severity.INFO,
                    state=VerificationState.UNVERIFIED,
                )
            for d in v.dependencies:
                if d.kind is not DepKind.REQUIRED or d.project_id in in_pack:
                    continue
                target = dep_targets.get(d.project_id or "")
                name = f"{target.get('slug')} ({d.project_id})" if target else d.project_id
                satisfiable = _cf_has_target_files(target, pack.minecraft, loader_type)
                if satisfiable is False:
                    report(
                        "dependency-metadata-suspect",
                        f"declares required CurseForge project {name}, which has no {pack.minecraft}/{pack.loader} "
                        "files; likely a cross-loader metadata error (the jar's own metadata decides)",
                        Severity.WARNING,
                        confidence="possible",
                        state=VerificationState.UNVERIFIED,
                    )
                else:
                    report(
                        "missing-dependency",
                        f"requires CurseForge project {name}, which is not in the pack"
                        + (maybe if unidentified else ""),
                        Severity.WARNING if unidentified else Severity.ERROR,
                        confidence="probable",
                        state=VerificationState.UNVERIFIED if unidentified else VerificationState.FAILED,
                    )
        if f and not f.download_allowed:
            r.add(
                Finding(
                    "manual-download",
                    f"{label}: author disallows third-party distribution; download manually through CurseForge",
                    Severity.INFO,
                    evidence=ev,
                    subject=e.path,
                )
            )
        verified += problems == 0
    r.add(
        Finding(
            "curseforge-online",
            f"{verified}/{len(entries)} CurseForge entries confirmed for {pack.minecraft}/{pack.loader} "
            "(identity, target tags, hashes, required dependencies)",
            Severity.INFO,
            state=VerificationState.VERIFIED if verified == len(entries) else VerificationState.UNVERIFIED,
        )
    )


def _check_online_modrinth(pack: PackInfo, r: Result) -> None:
    from mcat.providers.http import NetworkError
    from mcat.providers.modrinth import ModrinthProvider

    mr = ModrinthProvider()
    for entry in pack.entries:
        if entry.provider != "modrinth" or not entry.version_id:
            continue
        try:
            v = mr.version(entry.version_id)
        except NetworkError as err:
            r.add(
                Finding(
                    "provider-lookup-failed",
                    f"{entry.path}: {err}",
                    Severity.WARNING,
                    state=VerificationState.UNVERIFIED,
                )
            )
            continue
        if pack.minecraft not in v.game_versions or (pack.loader and pack.loader not in v.loaders):
            r.add(
                Finding(
                    "provider-incompatible",
                    f"{entry.path}: Modrinth version {v.version_number} lists "
                    f"{v.game_versions}/{v.loaders}, not {pack.minecraft}/{pack.loader}",
                    Severity.ERROR,
                    confidence="confirmed",
                    evidence=[Evidence(f"modrinth:version/{v.version_id}")],
                )
            )
        sha = entry.hashes.get("sha512") or entry.hashes.get("sha1")
        if sha and sha not in {h for f in v.files for h in f.hashes.values()}:
            r.add(
                Finding(
                    "hash-mismatch",
                    f"{entry.path}: hash does not match Modrinth's file for this version",
                    Severity.ERROR,
                    state=VerificationState.FAILED,
                )
            )
