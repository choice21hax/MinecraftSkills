"""Install the canonical skills into Claude Code and Codex discovery locations.

Discovery locations (verified against the official docs, 2026-10):
* Claude Code: project ``.claude/skills/<name>/SKILL.md``; personal ``~/.claude/skills/<name>``.
* Codex:       repo ``.agents/skills/<name>`` (scanned from CWD up to repo root); user ``~/.agents/skills``.
Both follow symlinked skill directories, so symlinks to the single canonical source are the default;
``--copy`` creates managed copies (marked with ``.mcat-managed.json``) for filesystems without symlinks.

An entry is only ever replaced or removed when MCAT can prove it manages it (symlink into the canonical
source, or a copy carrying the marker). Anything else is an unrelated skill and is left alone unless the
user explicitly passes ``--force``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path

import yaml

SKILL_NAMES = ("minecraft-modpack", "minecraft-migrate", "minecraft-moddev", "minecraft-debug")
AGENT_DIRS = {"claude": ".claude/skills", "codex": ".agents/skills"}
MARKER = ".mcat-managed.json"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class SkillInstallError(Exception):
    pass


def canonical_root() -> Path:
    """Locate the canonical skills/ directory (repo checkout or packaged data)."""
    env = os.environ.get("MCAT_SKILLS_DIR")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "skills"
        if (cand / SKILL_NAMES[0] / "SKILL.md").is_file():
            return cand
    packaged = Path(str(resources.files("mcat") / "_data" / "skills"))
    if (packaged / SKILL_NAMES[0] / "SKILL.md").is_file():
        return packaged
    raise SkillInstallError("canonical skills directory not found (set MCAT_SKILLS_DIR)")


def validate_skill(path: Path) -> list[str]:
    """Check a skill directory against the Agent Skills specification. Returns problems."""
    problems = []
    md = path / "SKILL.md"
    if not md.is_file():
        return [f"{path.name}: SKILL.md missing"]
    text = md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return [f"{path.name}: SKILL.md lacks YAML frontmatter"]
    try:
        _, fm, body = text.split("---\n", 2)
        meta = yaml.safe_load(fm) or {}
    except (ValueError, yaml.YAMLError) as e:
        return [f"{path.name}: invalid frontmatter: {e}"]
    name, desc = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not NAME_RE.match(name) or len(name) > 64:
        problems.append(f"{path.name}: invalid name {name!r}")
    elif name != path.name:
        problems.append(f"{path.name}: name {name!r} does not match directory")
    if not isinstance(desc, str) or not (1 <= len(desc) <= 1024):
        problems.append(f"{path.name}: description must be 1-1024 characters")
    allowed = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    extra = set(meta) - allowed
    if extra:
        problems.append(f"{path.name}: non-portable frontmatter keys {sorted(extra)}")
    if len(body.splitlines()) > 500:
        problems.append(f"{path.name}: SKILL.md body over 500 lines")
    for ref in re.findall(r"\]\((references/[^)]+)\)", body):
        if not (path / ref).is_file():
            problems.append(f"{path.name}: broken reference {ref}")
    return problems


def _tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file() and p.name != MARKER):
        h.update(f.relative_to(path).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()


@dataclass
class EntryStatus:
    agent: str
    scope: str
    skill: str
    path: str
    state: str  # missing | linked | copied | stale-copy | broken-link | conflict | invalid
    detail: str = ""


def target_base(agent: str, scope: str, project: Path) -> Path:
    if agent not in AGENT_DIRS:
        raise SkillInstallError(f"unknown agent {agent!r}; expected claude or codex")
    base = Path.home() if scope == "user" else project
    return base / AGENT_DIRS[agent]


def inspect_entry(entry: Path, source: Path) -> tuple[str, str]:
    if entry.is_symlink():
        raw = os.readlink(entry)
        resolved = (entry.parent / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
        if resolved == source.resolve():
            return ("linked", str(resolved)) if resolved.exists() else ("broken-link", str(resolved))
        if not resolved.exists():
            return "conflict", f"foreign broken symlink -> {raw}"
        return "conflict", f"symlink to unrelated location {resolved}"
    if not entry.exists():
        return "missing", ""
    marker = entry / MARKER
    if entry.is_dir() and marker.is_file():
        try:
            info = json.loads(marker.read_text())
        except ValueError:
            return "conflict", "unreadable MCAT marker"
        if info.get("hash") == _tree_hash(source):
            return "copied", "managed copy, up to date"
        return "stale-copy", "managed copy, canonical source changed"
    return "conflict", "existing skill not managed by MCAT"


def status(agents: list[str], scopes: list[str], project: Path) -> list[EntryStatus]:
    root = canonical_root()
    out = []
    for agent in agents:
        for scope in scopes:
            base = target_base(agent, scope, project)
            for name in SKILL_NAMES:
                entry = base / name
                state, detail = inspect_entry(entry, root / name)
                if state in ("linked", "copied"):
                    problems = validate_skill(entry)
                    if problems:
                        state, detail = "invalid", "; ".join(problems)
                out.append(EntryStatus(agent, scope, name, str(entry), state, detail))
    return out


def install(
    agents: list[str], scope: str, project: Path, *, copy: bool = False, force: bool = False
) -> list[EntryStatus]:
    root = canonical_root()
    for name in SKILL_NAMES:
        problems = validate_skill(root / name)
        if problems:
            raise SkillInstallError("canonical skill invalid: " + "; ".join(problems))
    results = []
    for agent in agents:
        base = target_base(agent, scope, project)
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SkillInstallError(f"cannot create {base}: {e.strerror}") from e
        if not base.is_dir() or (base.is_symlink() and not base.resolve().is_dir()):
            raise SkillInstallError(f"invalid installation path {base}")
        for name in SKILL_NAMES:
            src, entry = root / name, base / name
            state, detail = inspect_entry(entry, src)
            if state == "conflict" and not force:
                results.append(
                    EntryStatus(
                        agent,
                        scope,
                        name,
                        str(entry),
                        "conflict",
                        f"{detail}; left untouched (re-run with --force to replace)",
                    )
                )
                continue
            want = "copied" if copy else "linked"
            if state == want:
                results.append(EntryStatus(agent, scope, name, str(entry), state, "already installed"))
                continue
            try:
                _remove(entry)
                if copy:
                    shutil.copytree(src, entry, symlinks=False)
                    (entry / MARKER).write_text(json.dumps({"source": str(src), "hash": _tree_hash(src)}))
                else:
                    entry.symlink_to(src, target_is_directory=True)
            except OSError as e:
                results.append(EntryStatus(agent, scope, name, str(entry), "failed", e.strerror or str(e)))
                continue
            action = (
                "replaced unrelated entry (--force)"
                if state == "conflict"
                else ("updated" if state in ("stale-copy", "broken-link", "linked", "copied") else "installed")
            )
            results.append(EntryStatus(agent, scope, name, str(entry), want, action))
    return results


def uninstall(agents: list[str], scope: str, project: Path) -> list[EntryStatus]:
    root = canonical_root()
    results = []
    for agent in agents:
        base = target_base(agent, scope, project)
        for name in SKILL_NAMES:
            entry = base / name
            state, detail = inspect_entry(entry, root / name)
            if state in ("linked", "broken-link", "copied", "stale-copy"):
                try:
                    _remove(entry)
                    results.append(EntryStatus(agent, scope, name, str(entry), "removed"))
                except OSError as e:
                    results.append(EntryStatus(agent, scope, name, str(entry), "failed", e.strerror or str(e)))
            elif state == "conflict":
                results.append(
                    EntryStatus(
                        agent, scope, name, str(entry), "conflict", f"{detail}; not managed by MCAT, left untouched"
                    )
                )
            else:
                results.append(EntryStatus(agent, scope, name, str(entry), "missing"))
    return results


def _remove(entry: Path) -> None:
    if entry.is_symlink() or entry.is_file():
        entry.unlink()
    elif entry.is_dir():
        shutil.rmtree(entry)


def as_dicts(rows: list[EntryStatus]) -> list[dict[str, str]]:
    return [asdict(r) for r in rows]
