"""Isolation backends for running third-party Minecraft code (installers, servers, mods).

Preference: rootless Podman, then Docker. Running on the host is never chosen automatically; it needs
an explicit ``allow_host`` authorisation from the user. Containers reduce blast radius (filesystem,
network, resources, privileges) but are **not** a perfect security boundary: kernel or runtime bugs can
allow escapes, and membership in the ``docker`` group is root-equivalent on the host.

Per run, the container gets:
* only the run's workspace, mounted at ``/work`` (no home directory, no credentials, no host paths);
* ``--network none`` unless the step is explicitly a provisioning step (installers need maven/Mojang);
* memory, CPU, process-count and ``/tmp`` size limits; a read-only root filesystem;
* all Linux capabilities dropped, ``no-new-privileges``, the invoking user's uid/gid;
* an explicit environment (``HOME=/work/home``, nothing inherited).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SandboxUnavailable(Exception):
    pass


@dataclass
class Limits:
    memory_mb: int = 4096
    cpus: float = 2.0
    pids: int = 1024
    tmp_mb: int = 512


@dataclass
class EngineInfo:
    engine: str  # podman | docker | host
    rootless: bool
    version: str
    evidence: str
    warnings: list[str]


def _probe(exe: str) -> tuple[bool, str, str]:
    try:
        p = subprocess.run(
            [exe, "info", "--format", "{{json .}}"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "", f"{exe} info failed: {e}"
    if p.returncode != 0:
        return False, "", f"{exe} info failed: {p.stderr.strip()[:200]}"
    return True, p.stdout, ""


def detect_engine(prefer: str | None = None) -> EngineInfo:
    """Find a usable container engine. Raises SandboxUnavailable if none works."""
    order = [prefer] if prefer else ["podman", "docker"]
    notes = []
    for engine in order:
        exe = shutil.which(engine)
        if not exe:
            notes.append(f"{engine}: not installed")
            continue
        ok, out, err = _probe(exe)
        if not ok:
            notes.append(err)
            continue
        low = out.lower()
        if engine == "podman":
            rootless = '"rootless":true' in low.replace(" ", "")
        else:
            rootless = "rootless" in low and "name=rootless" in low
        version = ""
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            version = subprocess.run(
                [exe, "version", "--format", "{{.Server.Version}}" if engine == "docker" else "{{.Version}}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            ).stdout.strip()
        warnings = []
        if not rootless:
            warnings.append(
                f"{engine} daemon runs as root; access to it is root-equivalent and a container escape would "
                "reach root. Prefer rootless Podman for untrusted packs."
            )
        return EngineInfo(
            engine, rootless, version, f"{engine} {version} ({'rootless' if rootless else 'rootful'})", warnings
        )
    raise SandboxUnavailable("; ".join(notes) or "no container engine")


@dataclass
class ContainerRunner:
    info: EngineInfo
    image: str
    limits: Limits

    @property
    def exe(self) -> str:
        return shutil.which(self.info.engine) or self.info.engine

    def ensure_image(self, *, allow_pull: bool = True) -> tuple[str, bool]:
        """Return (image digest or id, pulled?). Pulling fetches the image from its registry."""
        p = subprocess.run(
            [self.exe, "image", "inspect", self.image, "--format", "{{index .RepoDigests 0}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        pulled = False
        if p.returncode != 0:
            if not allow_pull:
                raise SandboxUnavailable(f"image {self.image} not present and pulling is not allowed")
            q = subprocess.run([self.exe, "pull", "-q", self.image], capture_output=True, text=True, timeout=900)
            if q.returncode != 0:
                raise SandboxUnavailable(f"cannot pull {self.image}: {q.stderr.strip()[:300]}")
            pulled = True
            p = subprocess.run(
                [self.exe, "image", "inspect", self.image, "--format", "{{index .RepoDigests 0}}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        return p.stdout.strip() or self.image, pulled

    def argv(
        self,
        workspace: Path,
        command: list[str],
        *,
        name: str,
        network: bool,
        workdir: str = "/work",
        extra_volumes: dict[Path, str] | None = None,  # host dir -> container path (MCAT-owned caches only)
        extra_env: dict[str, str] | None = None,
    ) -> list[str]:
        lim = self.limits
        args = [
            self.exe,
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--network",
            "bridge" if network else "none",
            "--memory",
            f"{lim.memory_mb}m",
            "--memory-swap",
            f"{lim.memory_mb}m",
            "--cpus",
            str(lim.cpus),
            "--pids-limit",
            str(lim.pids),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={lim.tmp_mb}m",  # noqa: S108 - container-private tmpfs
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "HOME=/work/home",
            "--env",
            "LANG=C.UTF-8",
            "--volume",
            f"{workspace.resolve()}:/work:rw",
            "--workdir",
            workdir,
        ]
        for host, target in (extra_volumes or {}).items():
            args += ["--volume", f"{host.resolve()}:{target}:rw"]
        for k, val in (extra_env or {}).items():
            args += ["--env", f"{k}={val}"]
        if self.info.engine == "podman" and self.info.rootless:
            args += ["--userns", "keep-id"]
        return [*args, self.image, *command]

    def kill(self, name: str) -> None:
        subprocess.run([self.exe, "rm", "-f", name], capture_output=True, timeout=60, check=False)

    def java_major(self, workspace: Path, name: str) -> int | None:
        p = subprocess.run(
            self.argv(workspace, ["java", "-version"], name=name, network=False),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return parse_java_major(p.stderr + p.stdout)


def parse_java_major(text: str) -> int | None:
    import re

    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


@dataclass
class HostRunner:
    """Runs on the host. Only constructed after explicit user authorisation (``--allow-host-execution``)."""

    java: str

    def argv(
        self,
        workspace: Path,
        command: list[str],
        *,
        name: str,
        network: bool,
        workdir: str = "/work",
        extra_volumes: dict[Path, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> list[str]:
        if command and command[0] == "java":
            command = [self.java, *command[1:]]
        return [c.replace("/work", str(workspace.resolve())) for c in command]

    def kill(self, name: str) -> None:  # processes are killed via their Popen handle
        return None

    def java_major(self, workspace: Path, name: str) -> int | None:
        p = subprocess.run([self.java, "-version"], capture_output=True, text=True, timeout=60, check=False)
        return parse_java_major(p.stderr + p.stdout)
