"""Version comparison and loader-native constraint checking.

* Fabric: semver-ish predicates (``>=1.2.0``, ``<2``, ``~1.2``, ``^1.2``, ``1.2.x``, ``*``), space-separated
  terms are ANDed, list entries ORed (``a || b`` here).
* Forge/NeoForge: Maven version ranges (``[1.0,2.0)``, ``[26.2]``, ``[65,)``, ``1.0`` = soft requirement).

Versions are compared as strings split into numeric and textual parts; never as floats. Pre-release
suffixes (``-beta.1``) sort before the release. Unparseable constraints return ``None`` (unknown),
never ``True``.
"""

from __future__ import annotations

import re
from functools import total_ordering

_SPLIT = re.compile(r"[.\-+_]")


@total_ordering
class Version:
    def __init__(self, raw: str):
        self.raw = raw.strip()
        core = self.raw.partition("+")[0]  # build metadata is ignored for ordering
        main, _, pre = core.partition("-")
        self.main = tuple(_part(p) for p in main.split(".") if p != "")
        self.pre = tuple(_part(p) for p in _SPLIT.split(pre) if p) if pre else None

    def key(self, width: int) -> tuple:
        main = self.main + ((0, 0, ""),) * (width - len(self.main))
        # releases sort after pre-releases of the same core version
        return (main, (1,) if self.pre is None else (0, *self.pre))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        w = max(len(self.main), len(other.main))
        return self.key(w) == other.key(w)

    def __lt__(self, other: Version) -> bool:
        w = max(len(self.main), len(other.main))
        return self.key(w) < other.key(w)

    def __hash__(self) -> int:
        return hash(self.raw)

    def __repr__(self) -> str:
        return f"Version({self.raw!r})"


def _part(p: str) -> tuple[int, int, str]:
    return (0, int(p), "") if p.isdigit() else (1, 0, p)


def _fabric_term(term: str, v: Version) -> bool | None:
    term = term.strip()
    if term in ("", "*"):
        return True
    m = re.match(r"^(>=|<=|>|<|=|~|\^)?\s*(.+)$", term)
    if not m:
        return None
    op, ver = m.group(1) or "=", m.group(2)
    if ver.endswith((".x", ".X", ".*")) and op == "=":
        prefix = Version(ver[:-2]).main
        return v.main[: len(prefix)] == prefix
    target = Version(ver)
    if not target.main:
        return None
    if op == ">=":
        return v >= target
    if op == "<=":
        return v <= target
    if op == ">":
        return v > target
    if op == "<":
        return v < target
    if op == "=":
        return v == target
    if op == "~":  # same major.minor, >= target
        return v >= target and v.main[:2] == target.main[:2]
    if op == "^":  # same major, >= target
        return v >= target and v.main[:1] == target.main[:1]
    return None


def fabric_satisfies(constraint: str, version: str) -> bool | None:
    v = Version(version)
    any_unknown = False
    for alt in constraint.split("||"):
        results = [_fabric_term(t, v) for t in alt.split()] or [True]
        if None in results:
            any_unknown = True
            continue
        if all(results):
            return True
    return None if any_unknown else False


def maven_satisfies(constraint: str, version: str) -> bool | None:
    c = constraint.strip()
    if c in ("", "*"):
        return True
    v = Version(version)
    ranges = re.findall(r"[\[(][^\])]*[\])]", c)
    if not ranges:
        return v >= Version(c)  # soft requirement: treat as minimum
    for r in ranges:
        lo_inc, hi_inc = r[0] == "[", r[-1] == "]"
        body = r[1:-1]
        if "," not in body:
            if lo_inc and hi_inc and v == Version(body):
                return True
            continue
        lo, hi = (s.strip() for s in body.split(",", 1))
        ok = True
        if lo:
            ok &= v >= Version(lo) if lo_inc else v > Version(lo)
        if hi:
            ok &= v <= Version(hi) if hi_inc else v < Version(hi)
        if ok:
            return True
    return False


def satisfies(loader: str, constraint: str, version: str) -> bool | None:
    try:
        if loader in ("fabric", "quilt"):
            return fabric_satisfies(constraint, version)
        return maven_satisfies(constraint, version)
    except (ValueError, IndexError):
        return None
