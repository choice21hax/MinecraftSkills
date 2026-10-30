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
        _check_online(pack, r)
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


def _check_online(pack: PackInfo, r: Result) -> None:
    from mcat.providers.http import NetworkError
    from mcat.providers.modrinth import ModrinthProvider

    mr = ModrinthProvider()
    for entry in pack.entries:
        if entry.provider != "modrinth" or not entry.version_id:
            if entry.provider == "curseforge":
                r.add(
                    Finding(
                        "provider-compat-unverified",
                        f"{entry.path}: CurseForge online check not implemented in validate; use `mcat resolve`",
                        Severity.INFO,
                        state=VerificationState.UNVERIFIED,
                    )
                )
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
