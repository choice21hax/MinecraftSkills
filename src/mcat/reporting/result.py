"""The JSON result envelope.

Every command returns a :class:`Result`. Its JSON form is a stable contract (``schema_version``) that
agents parse, so fields are only ever added, never renamed or removed without a version bump.

``status`` separates "the command ran" from "the outcome is verified":

* ``success`` - operation completed and everything it claims is verified by the checks it ran.
* ``partial`` - operation ran, but some items are only proposed/unverified.
* ``blocked`` - operation cannot proceed or complete without input (credentials, toolchain, a
  required mod with no target version...).
* ``failed``  - the operation itself failed (bad input, I/O error, failed build).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from mcat.security.redact import redact_obj

SCHEMA_VERSION = "1.0"


class Status(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


EXIT_CODES: dict[Status, int] = {
    Status.SUCCESS: 0,
    Status.FAILED: 1,
    # 2 is reserved for argparse usage errors
    Status.PARTIAL: 3,
    Status.BLOCKED: 4,
}


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class VerificationState(StrEnum):
    """Per-item evidence level used by migration and validation reports."""

    VERIFIED = "verified"
    PROPOSED = "proposed"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class Evidence:
    source: str  # e.g. "crash-report:line 42", "modrinth:version/abc123", "file:mods/x.jar"
    detail: str = ""


@dataclass
class Finding:
    code: str
    message: str
    severity: Severity = Severity.INFO
    state: VerificationState | None = None
    confidence: str | None = None  # "confirmed" | "probable" | "possible"
    evidence: list[Evidence] = field(default_factory=list)
    fix: str | None = None
    subject: str | None = None


@dataclass
class Result:
    operation: str
    status: Status = Status.SUCCESS
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, finding: Finding) -> Finding:
        self.findings.append(finding)
        return finding

    def block(self, reason: str) -> None:
        self.blockers.append(reason)
        if self.status in (Status.SUCCESS, Status.PARTIAL):
            self.status = Status.BLOCKED

    def fail(self, reason: str) -> None:
        self.blockers.append(reason)
        self.status = Status.FAILED

    def degrade(self, warning: str | None = None) -> None:
        """Mark the result partial (ran, but not everything verified)."""
        if warning:
            self.warnings.append(warning)
        if self.status == Status.SUCCESS:
            self.status = Status.PARTIAL

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw = {"schema_version": SCHEMA_VERSION, **raw}
        return redact_obj(raw)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False, default=str)

    def to_text(self) -> str:
        lines = [f"[{self.status.value.upper()}] {self.operation}: {self.summary}".rstrip(": ")]
        for f in self.findings:
            tag = f.state.value if f.state else f.severity.value
            conf = f" ({f.confidence})" if f.confidence else ""
            lines.append(f"  - [{tag}] {f.code}{conf}: {f.message}")
            for ev in f.evidence[:3]:
                lines.append(f"      evidence: {ev.source} {ev.detail}".rstrip())
            if f.fix:
                lines.append(f"      fix: {f.fix}")
        for b in self.blockers:
            lines.append(f"  BLOCKER: {b}")
        for w in self.warnings:
            lines.append(f"  warning: {w}")
        for a in self.artifacts:
            lines.append(f"  artifact: {a}")
        return redact_obj("\n".join(lines))
