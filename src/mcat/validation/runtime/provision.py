"""Provisioning: official loader installers and pack files, downloaded on the host by MCAT's
allow-listed HTTP client and verified before anything executes in the sandbox.

* Installers come only from the loaders' official mavens and are checked against the maven's
  published ``.sha256`` (same origin: protects against corruption/truncation, not a compromised maven).
* Mod files come only from allow-listed provider hosts and must match the pack's recorded hash.
  CurseForge files whose authors disallow distribution are never fetched (reported as blocked).
* Downloads run with bounded concurrency; nothing downloaded is executed on the host.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcat.parsers.packs import PackEntry, PackInfo
from mcat.parsers.source import PackSource
from mcat.providers.http import HttpClient, NetworkError
from mcat.security.archive import normalize_member, safe_join

FABRIC_META = "https://meta.fabricmc.net/v2/versions/installer"
NEOFORGE_INSTALLER = "https://maven.neoforged.net/releases/net/neoforged/neoforge/{v}/neoforge-{v}-installer.jar"
FORGE_INSTALLER = "https://maven.minecraftforge.net/net/minecraftforge/forge/{mc}-{v}/forge-{mc}-{v}-installer.jar"
DOWNLOAD_WORKERS = 6


class ProvisionError(Exception):
    pass


@dataclass
class ServerPlan:
    loader: str
    minecraft: str
    loader_version: str
    installer_url: str
    installer_sha256: str = ""
    install_command: list[str] = field(default_factory=list)  # run in /work/server, network on

    def to_dict(self) -> dict[str, Any]:
        return {
            "loader": self.loader,
            "minecraft": self.minecraft,
            "loader_version": self.loader_version,
            "installer_url": self.installer_url,
            "installer_sha256": self.installer_sha256,
            "install_command": self.install_command,
        }


def plan_server(loader: str, minecraft: str, loader_version: str, http: HttpClient) -> ServerPlan:
    if loader == "fabric":
        installers = http.get_json(FABRIC_META, ttl=3600)
        stable = next((i for i in installers if i.get("stable")), installers[0] if installers else None)
        if not stable:
            raise ProvisionError("Fabric meta lists no installer")
        cmd = ["java", "-jar", "/work/installer.jar", "server", "-mcversion", minecraft, "-loader", loader_version]
        cmd += ["-downloadMinecraft", "-dir", "/work/server"]
        return ServerPlan(loader, minecraft, loader_version, stable["url"], install_command=cmd)
    if loader == "neoforge":
        url = NEOFORGE_INSTALLER.format(v=loader_version)
    elif loader == "forge":
        url = FORGE_INSTALLER.format(mc=minecraft, v=loader_version)
    else:
        raise ProvisionError(f"unsupported loader {loader}")
    return ServerPlan(
        loader,
        minecraft,
        loader_version,
        url,
        install_command=["java", "-jar", "/work/installer.jar", "--installServer", "/work/server"],
    )


def fetch_installer(plan: ServerPlan, http: HttpClient, dest: Path) -> None:
    published = http.get_text(plan.installer_url + ".sha256").strip().split()[0].lower()
    http.download(plan.installer_url, dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != published:
        dest.unlink()
        raise ProvisionError(f"installer sha256 mismatch for {plan.installer_url}")
    plan.installer_sha256 = digest


def launch_command(loader: str, server: Path, memory_mb: int) -> list[str]:
    """Server launch argv (container paths), derived from the files the official installer produced."""
    heap = [f"-Xmx{max(memory_mb - 768, 512)}m", f"-Xms{min(1024, max(memory_mb - 768, 512))}m"]
    if loader == "fabric":
        if not (server / "fabric-server-launch.jar").is_file():
            raise ProvisionError("Fabric installer did not produce fabric-server-launch.jar")
        return ["java", *heap, "-jar", "fabric-server-launch.jar", "nogui"]
    group = "net/neoforged/neoforge" if loader == "neoforge" else "net/minecraftforge/forge"
    args = sorted((server / "libraries" / group).glob("*/unix_args.txt"))
    if len(args) != 1:
        raise ProvisionError(f"expected one {group}/*/unix_args.txt after install, found {len(args)}")
    rel = args[0].relative_to(server).as_posix()
    return ["java", *heap, f"@{rel}", "nogui"]


def _verify(data: bytes, hashes: dict[str, str]) -> str | None:
    for algo in ("sha512", "sha256", "sha1"):
        if algo in hashes:
            got = hashlib.new(algo, data).hexdigest()
            return None if got == hashes[algo].lower() else f"{algo} mismatch"
    return "no hash recorded; refusing unverifiable file"


@dataclass
class PackInstall:
    installed: list[str] = field(default_factory=list)
    skipped_client: list[str] = field(default_factory=list)
    skipped_optional: list[str] = field(default_factory=list)
    blocked: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    overrides: int = 0


def install_pack(
    src: PackSource,
    pack: PackInfo,
    server: Path,
    http: HttpClient,
    *,
    curseforge: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
) -> PackInstall:
    """Place a pack's server-side files into ``server`` (an existing, MCAT-created directory)."""
    out = PackInstall()
    cf_files: dict[str, dict[str, Any]] = {}
    cf_ids = [e.version_id for e in pack.entries if e.provider == "curseforge" and e.version_id]
    if cf_ids:
        if curseforge is None:
            for e in pack.entries:
                if e.provider == "curseforge":
                    out.blocked.append({"path": e.path, "reason": "CurseForge file needs an API key to download"})
        else:
            cf_files = curseforge(cf_ids)

    jobs: list[tuple[PackEntry, str, str, dict[str, str]]] = []  # entry, url, rel dest, hashes
    for e in pack.entries:
        if e.side == "client":
            out.skipped_client.append(e.path)
            continue
        if not e.required and not e.embedded:
            out.skipped_optional.append(e.path)
            continue
        if e.embedded:
            dest = safe_join(server, e.path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            roots = [f"{r}/" if r else "" for r in (pack.override_roots or [""])]
            name = next((r + e.path for r in roots if src.exists(r + e.path)), None)
            if name:
                with open(dest, "xb") as fh:
                    fh.write(src.read(name, 512 * 1024**2))
                out.installed.append(e.path)
            continue
        if e.provider == "curseforge":
            raw = cf_files.get(str(e.version_id))
            if raw is None:
                if curseforge is not None:
                    out.failed.append({"path": e.path, "reason": "file not returned by the CurseForge API"})
                continue
            if not raw.get("downloadUrl"):
                out.blocked.append({"path": e.path, "reason": "author disallows third-party downloads; fetch manually"})
                continue
            sha1 = next((h["value"] for h in raw.get("hashes", []) if h.get("algo") == 1), None)
            hashes = {"sha1": sha1} if sha1 else {}
            jobs.append((e, raw["downloadUrl"], f"mods/{normalize_member(raw['fileName'])}", hashes))
            continue
        if not e.downloads:
            out.failed.append({"path": e.path, "reason": "no download URL"})
            continue
        jobs.append((e, e.downloads[0], e.path, e.hashes))

    def fetch(job: tuple[PackEntry, str, str, dict[str, str]]) -> tuple[str, str | None]:
        _entry, url, rel, hashes = job
        dest = safe_join(server, rel)
        tmp = server.parent / "downloads" / hashlib.sha1(url.encode()).hexdigest()  # noqa: S324 - cache name only
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            if not tmp.exists():
                http.download(url, tmp)
            problem = _verify(tmp.read_bytes(), hashes)
            if problem:
                return rel, problem
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tmp, dest)
            return rel, None
        except (NetworkError, OSError) as err:
            return rel, str(err)

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        for rel, problem in pool.map(fetch, jobs):
            if problem:
                out.failed.append({"path": rel, "reason": problem})
            else:
                out.installed.append(rel)

    # overrides (configs, scripts, datapacks); client-only override roots are skipped
    roots = [r for r in pack.override_roots if r and "client" not in r] if pack.format != "packwiz" else [""]
    for root in roots:
        prefix = f"{root}/" if root else ""
        for n in src.names():
            if not n.startswith(prefix) or n.endswith("/"):
                continue
            rel = normalize_member(n[len(prefix) :])
            if pack.format == "packwiz" and (
                rel in ("pack.toml", "index.toml", "mcat.lock.json", ".packwizignore") or rel.endswith(".pw.toml")
            ):
                continue
            if rel.startswith("mods/") and rel.endswith(".jar"):
                continue  # embedded jars handled above
            dest = safe_join(server, rel)
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "xb") as fh:
                fh.write(src.read(n))
            out.overrides += 1
    return out
