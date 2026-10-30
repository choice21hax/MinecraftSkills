"""Cross-provider identity: collapse the same mod selected from Modrinth and CurseForge.

Identity is established only from authoritative evidence, never from similar names or slugs:

* a CurseForge file's sha1 (from the CurseForge API) looked up with Modrinth's ``version_files``
  endpoint maps it to the Modrinth version that has byte-identical content;
* project ids are compared only after that mapping.

The mapping is one-directional because CurseForge has no sha1 lookup (its fingerprint needs file bytes,
which MCAT does not download during resolution). Modrinth-only selections are therefore never matched
against CurseForge projects they were not hashed against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from mcat.models import ModVersion
from mcat.resolver.core import Problem, Resolution

HashLookup = Callable[[list[str]], dict[str, ModVersion]]  # sha1s -> Modrinth version with that file


@dataclass
class Unification:
    merged: list[dict[str, str]] = field(default_factory=list)  # identical files collapsed into one entry
    conflicts: list[dict[str, str]] = field(default_factory=list)  # same project, different files
    unmatched: list[str] = field(default_factory=list)  # CurseForge keys with no Modrinth hash match
    checked: int = 0
    lookup_error: str | None = None


def _sha1(v: ModVersion) -> str | None:
    f = v.primary_file
    return f.hashes.get("sha1") if f else None


def unify(res: Resolution, lookup: HashLookup, *, prefer: str = "modrinth") -> Unification:
    """Mutates ``res``: duplicate selections are merged (keeping ``prefer``), conflicts become problems."""
    out = Unification()
    cf_keys = {k: s for k, s in res.selected.items() if s.version.provider == "curseforge"}
    mr_by_project = {s.version.project_id: k for k, s in res.selected.items() if s.version.provider == "modrinth"}
    if not cf_keys or not mr_by_project:
        return out
    sha_by_key = {k: h for k, s in cf_keys.items() if (h := _sha1(s.version))}
    out.checked = len(sha_by_key)
    try:
        found = lookup(sorted(set(sha_by_key.values())))
    except Exception as e:  # network/offline: report, never guess
        out.lookup_error = f"{type(e).__name__}: {e}"
        return out
    for cf_key, sha in sorted(sha_by_key.items()):
        mv = found.get(sha)
        if mv is None:
            out.unmatched.append(cf_key)
            continue
        mr_key = mr_by_project.get(mv.project_id)
        if mr_key is None:
            continue  # the same file exists on Modrinth, but only one provider was selected: nothing to merge
        mr_sel = res.selected[mr_key]
        evidence = f"curseforge file sha1 {sha} == modrinth:version/{mv.version_id} (project {mv.project_id})"
        if _sha1(mr_sel.version) == sha:
            keep, drop = (mr_key, cf_key) if prefer == "modrinth" else (cf_key, mr_key)
            kept, dropped = res.selected[keep], res.selected.pop(drop)
            kept.reasons = sorted(set(kept.reasons) | set(dropped.reasons))
            kept.optional = kept.optional and dropped.optional
            kept.side_override = kept.side_override or dropped.side_override
            dv = dropped.version
            kept.equivalents.append(f"{dv.provider}:{dv.project_id}/{dv.version_id}")
            res.edges = sorted({(keep if a == drop else a, keep if b == drop else b, kind) for a, b, kind in res.edges})
            out.merged.append({"kept": keep, "dropped": drop, "evidence": evidence})
        else:
            msg = (
                f"{cf_key} and {mr_key} are the same project ({evidence}) but resolved to different files "
                f"({cf_key}: {cf_keys[cf_key].version.version_number}; {mr_key}: {mr_sel.version.version_number}). "
                "Request it from one provider or pin matching versions."
            )
            res.problems.append(Problem("duplicate-project", cf_key, msg, alternatives=[mr_key]))
            out.conflicts.append({"curseforge": cf_key, "modrinth": mr_key, "evidence": evidence})
    return out
