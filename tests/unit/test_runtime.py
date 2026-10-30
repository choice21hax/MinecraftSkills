"""Runtime verification logic that does not need a container (state machine, sandbox policy, provisioning)."""

import hashlib
import json

import pytest

from mcat.parsers.packs import PackEntry, PackInfo
from mcat.parsers.source import PackSource
from mcat.validation.runtime import provision, sandbox, server
from mcat.validation.runtime.state import ServerMonitor, ServerState

FABRIC_LOG = """[18:23:10] [main/INFO]: Loading 3 mods:
\t- fabric-api 0.161.0+26.2
\t   |-- fabric-api-base 2.0.4
\t   \\-- fabric-command-api-v2 3.1.0
\t- minecraft 26.2
[18:23:15] [Server thread/INFO]: Starting minecraft server version 26.2
[18:23:24] [Server thread/INFO]: Done (8.814s)! For help, type "help"
"""


def _feed(m, text):
    for line in text.splitlines():
        m.feed(line)


def test_ready_requires_nonce_round_trip_after_done():
    m = ServerMonitor()
    _feed(m, FABRIC_LOG)
    assert m.state is ServerState.DONE_LOGGED  # 'Done' alone is not readiness
    assert set(m.fabric_mods) == {"fabric-api", "fabric-api-base", "fabric-command-api-v2", "minecraft"}
    m.feed("[Server thread/INFO]: [Not Secure] [Server] mcat-probe-guess")  # a mod echoing text before the probe
    assert m.state is ServerState.DONE_LOGGED
    m.nonce = "mcat-probe-1234abcd"
    m.feed("[Server thread/INFO]: [Not Secure] [Server] mcat-probe-1234abcd")
    assert m.state is ServerState.READY
    m.feed("[Server thread/INFO]: Stopping the server")
    assert m.exited(0) is ServerState.STOPPED
    assert [t.state for t in m.transitions][-3:] == [ServerState.READY, ServerState.STOPPING, ServerState.STOPPED]


def test_done_line_before_world_load_states_and_crash():
    m = ServerMonitor()
    m.feed("[main/INFO]: Loading 1 mods:")
    m.feed("---- Minecraft Crash Report ----")
    assert m.state is ServerState.CRASHED
    m.feed('Done (1.0s)! For help, type "help"')  # terminal states are sticky
    assert m.state is ServerState.CRASHED


def test_eula_refusal_is_blocked_and_exit_without_stop_is_crash():
    m = ServerMonitor()
    m.feed("[main/INFO]: You need to agree to the EULA in order to run the server.")
    assert m.state is ServerState.BLOCKED
    m2 = ServerMonitor()
    _feed(m2, FABRIC_LOG)
    assert m2.exited(1) is ServerState.CRASHED
    m3 = ServerMonitor()
    m3.timed_out()
    assert m3.state is ServerState.TIMEOUT


def test_container_argv_is_locked_down():
    info = sandbox.EngineInfo("docker", False, "29", "docker 29 (rootful)", [])
    r = sandbox.ContainerRunner(info, "eclipse-temurin:25-jre", sandbox.Limits(memory_mb=2048, cpus=1.5, pids=256))
    argv = r.argv(__import__("pathlib").Path("/tmp/ws"), ["java", "-version"], name="n", network=False)
    joined = " ".join(argv)
    for flag in (
        "--network none",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--read-only",
        "--memory 2048m",
        "--memory-swap 2048m",
        "--cpus 1.5",
        "--pids-limit 256",
        "--rm",
    ):
        assert flag in joined, flag
    assert "/tmp/ws:/work:rw" in joined and "HOME=/work/home" in joined
    assert sum(a == "--volume" for a in argv) == 1  # nothing but the workspace is mounted
    assert "--env-file" not in argv and not any("KEY" in a or "TOKEN" in a for a in argv)
    assert "--network bridge" in " ".join(r.argv(__import__("pathlib").Path("/tmp/ws"), ["x"], name="n", network=True))


def test_parse_java_major():
    assert sandbox.parse_java_major('openjdk version "25.0.5" 2026-10-20 LTS') == 25
    assert sandbox.parse_java_major('java version "1.8.0_402"') == 8
    assert sandbox.parse_java_major("garbage") is None


def test_eula_gate_comes_first(isolated_env):
    r = server.run_server_test(server.RuntimeOptions(loader="fabric", minecraft="26.2"))
    assert r.status.value == "blocked" and "--accept-eula" in r.blockers[0]


def test_no_sandbox_never_falls_back_to_host(isolated_env, monkeypatch):
    from mcat.loaders import LoaderSupport

    monkeypatch.setattr(
        server.LoaderCatalog,
        "support",
        lambda self, loader, mc: LoaderSupport(loader, mc, True, ["0.20.1"], "0.20.1", 25, "fake"),
    )

    def none(prefer=None):
        raise sandbox.SandboxUnavailable("podman: not installed; docker: not installed")

    monkeypatch.setattr(server, "detect_engine", none)
    ran = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: ran.append(a))
    r = server.run_server_test(server.RuntimeOptions(loader="fabric", minecraft="26.2", accept_eula=True))
    assert r.status.value == "blocked" and "--allow-host-execution" in r.blockers[0]
    assert ran == []


def test_launch_command_from_installer_output(tmp_path):
    (tmp_path / "libraries/net/neoforged/neoforge/26.2.0.93").mkdir(parents=True)
    (tmp_path / "libraries/net/neoforged/neoforge/26.2.0.93/unix_args.txt").write_text("x")
    cmd = provision.launch_command("neoforge", tmp_path, 4096)
    assert cmd[-2:] == ["@libraries/net/neoforged/neoforge/26.2.0.93/unix_args.txt", "nogui"]
    with pytest.raises(provision.ProvisionError):
        provision.launch_command("fabric", tmp_path, 4096)


def test_verify_requires_a_matching_hash():
    data = b"jar"
    assert provision._verify(data, {"sha1": hashlib.sha1(data).hexdigest()}) is None
    assert provision._verify(data, {"sha512": "0" * 128}) == "sha512 mismatch"
    assert "refusing" in provision._verify(data, {})


def test_install_pack_skips_client_and_blocks_restricted_curseforge(tmp_path):
    pack = PackInfo(
        format="curseforge", name="p", version="1", minecraft="1.20.1", loader="forge", loader_version="47.4.10"
    )
    pack.entries = [
        PackEntry("mods/a.jar", "a", provider="curseforge", project_id="1", version_id="11"),
        PackEntry("mods/c.jar", "c", provider="modrinth", side="client", downloads=["https://cdn.modrinth.com/x"]),
    ]
    (tmp_path / "src").mkdir()
    (tmp_path / "srv").mkdir()

    class NoHttp:
        def download(self, *a, **k):
            raise AssertionError("nothing should be downloaded")

    files = {"11": {"id": 11, "modId": 1, "fileName": "a.jar", "downloadUrl": None, "hashes": []}}
    with PackSource(tmp_path / "src") as src:
        out = provision.install_pack(src, pack, tmp_path / "srv", NoHttp(), curseforge=lambda ids: files)
    assert out.skipped_client == ["mods/c.jar"]
    assert out.blocked and "disallows" in out.blocked[0]["reason"] and not out.installed


def test_report_rereads_workspace(tmp_path):
    from argparse import Namespace

    from mcat.cli.commands_test import test_report

    (tmp_path / "mcat-logs").mkdir()
    (tmp_path / "mcat-logs/console.log").write_text(
        "Exception in thread main java.lang.OutOfMemoryError: Java heap space\n"
    )
    (tmp_path / "report.json").write_text(json.dumps({"operation": "test.server", "status": "failed", "summary": "x"}))
    r = test_report(Namespace(workspace=str(tmp_path)))
    assert r.status.value == "failed" and r.data["debug_next"].startswith("mcat debug ")
    assert r.findings  # the debug analyzer classified the log


def test_offline_validate_catches_missing_lock_dependency(tmp_path):
    from mcat.pack.build import resolve_spec, write_packwiz
    from mcat.resolver.spec import parse_spec
    from mcat.validation.validate import validate_path
    from tests.fakes import FakeCatalog, FakeModrinth, mv

    fake = FakeModrinth(
        [mv("a", "a1", ["26.2"], ["fabric"], deps=[("required", "lib")]), mv("lib", "l1", ["26.2"], ["fabric"])]
    )
    spec = parse_spec({"name": "p", "version": "1", "minecraft": "26.2", "loader": "fabric", "mods": ["a"]}, "d")
    _, lock, _ = resolve_spec(
        spec, fetch=lambda p, x: fake.versions(x), catalog=FakeCatalog({("fabric", "26.2"): "0.20.1"})
    )
    lock["mods"] = [m for m in lock["mods"] if m["slug"] != "lib"]  # hand-edited, broken
    write_packwiz(lock, tmp_path / "pw")
    v = validate_path(tmp_path / "pw")
    assert v.status.value == "failed" and any(f.code == "lock-dependency-missing" for f in v.findings)
