"""Server lifecycle state machine driven by console output plus MCAT's own probes.

Readiness is never inferred from an open port or from a single log line that any mod could print.
A server is READY only after this evidence chain, in order:

1. the vanilla ``Done (<n>s)! For help, type "help"`` line (dedicated server finished world load);
2. a round trip through the server's command loop: MCAT sends ``say <nonce>`` with a random nonce
   generated *after* step 1, and the server echoes it (only a live, ticking command loop can);
3. for a clean result, ``stop`` must end the process with exit code 0.

Log text is untrusted: it is only matched against fixed patterns, never interpreted.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import StrEnum


class ServerState(StrEnum):
    PROVISIONING = "provisioning"
    STARTING = "starting"
    LOADING_MODS = "loading_mods"
    LOADING_WORLD = "loading_world"
    DONE_LOGGED = "done_logged"  # step 1 only: not yet trusted
    READY = "ready"  # steps 1+2
    STOPPING = "stopping"
    STOPPED = "stopped"  # clean exit after READY
    CRASHED = "crashed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


TERMINAL = {ServerState.STOPPED, ServerState.CRASHED, ServerState.TIMEOUT, ServerState.BLOCKED}

_DONE = re.compile(r'Done \((?P<secs>[0-9.,]+)s\)! For help, type "help"')
_STARTING = re.compile(r"Starting minecraft server version (?P<version>\S+)")
_PREPARING = re.compile(r"Preparing (?:level|start region)")
_FABRIC_MODS = re.compile(r"Loading (?P<n>\d+) mods:")
_FABRIC_MOD_LINE = re.compile(r"^\s+(?:- |[|\\]-- )(?P<id>[a-z0-9_\-]+) (?P<version>\S+)")  # |--, \-- = jar-in-jar
_MODLOADER = re.compile(
    r"(?:ModLauncher|FML|NeoForge|FancyModLoader|net\.neoforged|net\.minecraftforge).*(?:running|starting|version)",
    re.I,
)
_EULA = re.compile(r"You need to agree to the EULA")
_CRASH = re.compile(
    r"---- Minecraft Crash Report ----|Failed to start the minecraft server|Exception in server tick loop"
    r"|Encountered an unexpected exception"
)
_STOPPING = re.compile(r"Stopping (?:the )?server")


@dataclass
class Transition:
    state: ServerState
    at: float  # seconds since start
    evidence: str


@dataclass
class ServerMonitor:
    nonce: str | None = None
    state: ServerState = ServerState.STARTING
    started: float = field(default_factory=time.monotonic)
    transitions: list[Transition] = field(default_factory=list)
    minecraft_version: str | None = None
    fabric_mods: dict[str, str] = field(default_factory=dict)
    done_seconds: float | None = None
    crash_lines: list[str] = field(default_factory=list)
    _in_fabric_list: bool = False

    def _go(self, state: ServerState, evidence: str) -> None:
        if state is self.state or self.state in TERMINAL:
            return
        self.state = state
        self.transitions.append(Transition(state, round(time.monotonic() - self.started, 3), evidence[:300]))

    def feed(self, line: str) -> ServerState:
        line = line.rstrip("\n")
        if self._in_fabric_list:
            m = _FABRIC_MOD_LINE.match(line)
            if m:
                self.fabric_mods[m["id"]] = m["version"]
                return self.state
            self._in_fabric_list = False
        if _EULA.search(line):
            self._go(ServerState.BLOCKED, line)
        elif _CRASH.search(line):
            self.crash_lines.append(line[:300])
            if self.state not in (ServerState.STOPPING,):
                self._go(ServerState.CRASHED, line)
        elif m := _FABRIC_MODS.search(line):
            self._in_fabric_list = True
            self._go(ServerState.LOADING_MODS, line)
        elif self.state is ServerState.STARTING and _MODLOADER.search(line):
            self._go(ServerState.LOADING_MODS, line)
        elif m := _STARTING.search(line):
            self.minecraft_version = m["version"]
            if self.state in (ServerState.STARTING, ServerState.LOADING_MODS):
                self._go(ServerState.LOADING_WORLD, line)
        elif _PREPARING.search(line) and self.state in (ServerState.STARTING, ServerState.LOADING_MODS):
            self._go(ServerState.LOADING_WORLD, line)
        elif m := _DONE.search(line):
            if self.state not in (ServerState.READY, ServerState.STOPPING):
                self.done_seconds = float(m["secs"].replace(",", "."))
                self._go(ServerState.DONE_LOGGED, line)
        elif self.nonce and self.nonce in line and self.state is ServerState.DONE_LOGGED:
            self._go(ServerState.READY, f"command round trip echoed nonce: {line}")
        elif _STOPPING.search(line) and self.state is ServerState.READY:
            self._go(ServerState.STOPPING, line)
        return self.state

    def exited(self, code: int | None) -> ServerState:
        if self.state is ServerState.STOPPING and code == 0:
            self._go(ServerState.STOPPED, "process exited with code 0 after stop")
        elif self.state not in TERMINAL:
            self._go(ServerState.CRASHED, f"process exited with code {code} in state {self.state.value}")
        return self.state

    def timed_out(self) -> None:
        self._go(ServerState.TIMEOUT, f"no terminal state within the time limit (last: {self.state.value})")

    def to_dict(self) -> dict[str, object]:
        return {
            "final_state": self.state.value,
            "minecraft_version_logged": self.minecraft_version,
            "done_seconds": self.done_seconds,
            "fabric_mods_loaded": dict(sorted(self.fabric_mods.items())),
            "crash_lines": self.crash_lines[:20],
            "transitions": [{"state": t.state.value, "at": t.at, "evidence": t.evidence} for t in self.transitions],
        }
