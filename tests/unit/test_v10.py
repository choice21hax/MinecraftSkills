"""v1.0 RC: doctor diagnostics, template notices, version consistency."""

import tomllib
from pathlib import Path

import pytest

from mcat import __version__, doctor, moddev
from mcat.validation.runtime import sandbox

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {
    "python",
    "java",
    "gradle",
    "packwiz",
    "credential-backend",
    "curseforge-credential",
    "modrinth",
    "curseforge-api",
    "skills-source",
    "skills-discovery",
    "cache-dir",
    "workspace",
    "sandbox",
}


@pytest.mark.parametrize(
    ("text", "major"),
    [
        ('openjdk version "25.0.4" 2026-07-15', 25),
        ('java version "1.8.0_402"', 8),
        ('openjdk version "17" 2021-09-14', 17),
        ("garbage", None),
    ],
)
def test_java_version_parsing(text, major):
    assert doctor._java_major(text) == major


def test_doctor_offline_reports_every_check_and_never_crashes(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "find_javas", lambda: [{"path": "/x/java", "major": 21, "on_path": True}])

    def no_engine(prefer=None):
        raise sandbox.SandboxUnavailable("podman: not installed; docker: not installed")

    monkeypatch.setattr(sandbox, "detect_engine", no_engine)
    r = doctor.run_doctor(offline=True, project=tmp_path)
    rows = {c["check"]: c for c in r.data["checks"]}
    assert set(rows) >= REQUIRED
    assert rows["modrinth"]["state"] == "skipped" and rows["sandbox"]["state"] == "missing"
    assert "Podman" in rows["sandbox"]["fix"]
    compat = r.data["java_compatibility"]
    assert compat["1.21.1"]["host_has"] is True and compat["26.x"]["host_has"] is False
    assert any(f.code == "java-version-missing" for f in r.findings)
    assert "never installs" in r.data["note"]


def test_generated_project_carries_template_notices(tmp_path):
    from tests.fakes import FakeCatalog

    cat = FakeCatalog({("neoforge", "1.21.1"): "21.1.265"})
    plan = moddev.plan_scaffold("neoforge", "1.21.1", "noticetest", "com.example.noticetest", catalog=cat)
    moddev.write_project(plan, tmp_path / "p", examples=False)
    text = (tmp_path / "p" / "THIRD_PARTY_NOTICES.md").read_text()
    assert "NeoForged project" in text and "CC0-1.0" in text


def test_version_is_consistent_and_semver():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == __version__
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## {__version__} " in changelog
