"""Regression corpus: plan + apply every synthetic pack in tests/corpus against fake providers."""

import hashlib
import json
from pathlib import Path

import pytest

from mcat.migration.apply import apply_plan
from mcat.migration.plan import build_plan
from tests.fakes import FakeCatalog, FakeModrinth, mv

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
CASES = sorted(p.name for p in CORPUS.iterdir() if (p / "case.json").is_file())
BUNDLE = (
    "inventory.json",
    "plan.json",
    "mappings.json",
    "unresolved-references.json",
    "verification-report.json",
    "transformation-log.jsonl",
    "ROLLBACK.md",
    "changes.diff",
    "migration-report.json",
    "pack/mcat.lock.json",
)


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


@pytest.mark.parametrize("name", CASES)
def test_corpus_case(name, tmp_path):
    case = json.loads((CORPUS / name / "case.json").read_text())
    src = CORPUS / name / "pack"
    before = _tree_digest(src)
    mc, loader = case["target"]
    fake = FakeModrinth([mv(p, v, g, ls) for p, v, g, ls in case["versions"]])
    plan = build_plan(src, mc, loader, modrinth=fake, curseforge=None, catalog=FakeCatalog({(loader, mc): "x"}))
    expect = case["expect"]
    if "mod_states" in expect:
        assert plan["overall"]["mod_states"] == expect["mod_states"]
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    out = tmp_path / "out"
    r = apply_plan(tmp_path / "plan.json", out)

    assert r.status.value == expect["status"], r.summary
    assert _tree_digest(src) == before  # the source pack is never modified
    for rel in BUNDLE:
        assert (out / rel).is_file(), rel
    unresolved = json.loads((out / "unresolved-references.json").read_text())["blocked_mod_references"]
    assert sorted(unresolved) == sorted(expect["unresolved"])
    for rel, data in expect.get("files", {}).items():
        actual = json.loads((out / rel).read_text())
        assert {k: actual.get(k) for k in data} == data, rel
    for rel, needles in expect.get("contains", {}).items():
        text = (out / rel).read_text()
        assert all(n in text for n in needles), (rel, [n for n in needles if n not in text])
    for rel in expect.get("exists", []):
        assert (out / rel).is_file(), rel
    report = json.loads((out / "migration-report.json").read_text())
    assert all(q["preserved"] for q in report["quest_integrity"])
    if expect.get("quest_graph_preserved"):
        assert report["quest_integrity"], "expected a quest file to be checked"
    assert report["overall"]["complete"] is False  # never claims gameplay was verified
