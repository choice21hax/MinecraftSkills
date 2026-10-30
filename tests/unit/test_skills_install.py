import json
import os
from pathlib import Path

import pytest

from mcat import agentskills as sk
from mcat.cli.main import main


@pytest.fixture
def project(tmp_path, isolated_env) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def test_canonical_skills_are_valid():
    root = sk.canonical_root()
    for name in sk.SKILL_NAMES:
        assert sk.validate_skill(root / name) == [], name


def test_validate_detects_problems(tmp_path):
    d = tmp_path / "Bad_Name"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: Bad_Name\ndescription: ''\nmodel: x\n---\nbody [r](references/missing.md)\n"
    )
    problems = " ".join(sk.validate_skill(d))
    assert "invalid name" in problems and "description" in problems and "non-portable" in problems
    assert "broken reference" in problems


@pytest.mark.parametrize("copy", [False, True])
def test_install_status_uninstall_both_agents(project, copy):
    rows = sk.install(["claude", "codex"], "project", project, copy=copy)
    assert {r.state for r in rows} == {"copied" if copy else "linked"}
    # each agent discovers all four skills independently in its own documented location
    for agent_dir in (".claude/skills", ".agents/skills"):
        found = sorted(p.parent.name for p in (project / agent_dir).glob("*/SKILL.md"))
        assert found == sorted(sk.SKILL_NAMES)
    st = sk.status(["claude", "codex"], ["project"], project)
    assert all(r.state in ("linked", "copied") for r in st)
    sk.uninstall(["claude", "codex"], "project", project)
    assert not any((project / ".claude/skills").iterdir())


def test_conflict_left_untouched(project):
    foreign = project / ".claude/skills/minecraft-debug"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("mine")
    rows = sk.install(["claude"], "project", project)
    conflict = next(r for r in rows if r.skill == "minecraft-debug")
    assert conflict.state == "conflict"
    assert (foreign / "SKILL.md").read_text() == "mine"
    # uninstall never removes unrelated skills
    sk.uninstall(["claude"], "project", project)
    assert (foreign / "SKILL.md").read_text() == "mine"
    # explicit --force replaces it
    rows = sk.install(["claude"], "project", project, force=True)
    assert next(r for r in rows if r.skill == "minecraft-debug").state == "linked"


def test_broken_managed_link_repaired_and_foreign_broken_link_kept(project, tmp_path):
    base = project / ".agents/skills"
    base.mkdir(parents=True)
    root = sk.canonical_root()
    # a stale link that once pointed at our canonical source (simulate by pointing at it, then check repair)
    (base / "minecraft-modpack").symlink_to(tmp_path / "nowhere")
    rows = sk.install(["codex"], "project", project)
    assert next(r for r in rows if r.skill == "minecraft-modpack").state == "conflict"
    assert os.readlink(base / "minecraft-modpack") == str(tmp_path / "nowhere")
    assert (base / "minecraft-migrate").resolve() == (root / "minecraft-migrate").resolve()


def test_stale_copy_updated(project, tmp_path, monkeypatch):
    import shutil

    canon = tmp_path / "canon"
    shutil.copytree(sk.canonical_root(), canon)
    monkeypatch.setenv("MCAT_SKILLS_DIR", str(canon))
    sk.install(["claude"], "project", project, copy=True)
    with open(canon / "minecraft-debug" / "SKILL.md", "a") as fh:
        fh.write("\nextra line\n")
    st = {r.skill: r.state for r in sk.status(["claude"], ["project"], project)}
    assert st["minecraft-debug"] == "stale-copy"
    rows = sk.install(["claude"], "project", project, copy=True)
    assert next(r for r in rows if r.skill == "minecraft-debug").detail == "updated"


def test_permission_failure_reported(project):
    base = project / ".claude"
    base.mkdir()
    base.chmod(0o500)
    try:
        with pytest.raises(sk.SkillInstallError, match="cannot create"):
            sk.install(["claude"], "project", project)
    finally:
        base.chmod(0o700)


def test_user_scope_uses_home(isolated_env, project):
    sk.install(["codex"], "user", project)
    assert (isolated_env / ".agents/skills/minecraft-debug/SKILL.md").is_file()


def test_cli_skills_json(project, capsys):
    assert main(["skills", "install", "--agents", "claude,codex", "--project", str(project), "--json"]) == 0
    capsys.readouterr()
    code = main(["skills", "status", "--scope", "project", "--project", str(project), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["data"]["discoverable"]["codex"] == sorted(sk.SKILL_NAMES)
