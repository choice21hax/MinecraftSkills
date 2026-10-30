"""Structured, stable result envelopes shared by every CLI command."""

from mcat.reporting.result import (
    EXIT_CODES,
    Evidence,
    Finding,
    Result,
    Severity,
    Status,
    VerificationState,
)

__all__ = ["EXIT_CODES", "Evidence", "Finding", "Result", "Severity", "Status", "VerificationState"]
