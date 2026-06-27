"""Gate policy decisions for the PaperAgent harness."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

DEFAULT_MAX_ASSETS = 13
DEFAULT_FIGURE_ASSET_LIMIT = 5
DEFAULT_TABLE_ASSET_LIMIT = 4
DEFAULT_FORMULA_ASSET_LIMIT = 4


class GateDecision(str, Enum):
    PASS = "pass"
    WARN = "warn"
    REVISE = "revise"
    BLOCK = "block"


@dataclass(frozen=True)
class GatePolicy:
    max_revision_attempts: int = 2

    def decide(self, verification: Any, revision_attempts: int = 0) -> GateDecision:
        return verification_to_gate_decision(
            verification,
            revision_attempts=revision_attempts,
            max_revision_attempts=self.max_revision_attempts,
        )


def verification_to_gate_decision(
    verification: Any,
    *,
    revision_attempts: int = 0,
    max_revision_attempts: int = 2,
) -> GateDecision:
    if verification is None:
        return GateDecision.BLOCK
    hard_failures = list(getattr(verification, "hard_failures", []) or [])
    soft_warnings = list(getattr(verification, "soft_warnings", []) or [])
    errors = list(getattr(verification, "errors", []) or [])
    if hard_failures:
        if revision_attempts < max_revision_attempts:
            return GateDecision.REVISE
        return GateDecision.BLOCK
    if soft_warnings or errors:
        return GateDecision.WARN
    if bool(getattr(verification, "passed", False)):
        return GateDecision.PASS
    return GateDecision.WARN


def _verification_should_block_report(result: Any) -> bool:
    return verification_to_gate_decision(
        result,
        revision_attempts=GatePolicy().max_revision_attempts,
    ) == GateDecision.BLOCK


__all__ = [
    "DEFAULT_FIGURE_ASSET_LIMIT",
    "DEFAULT_FORMULA_ASSET_LIMIT",
    "DEFAULT_MAX_ASSETS",
    "DEFAULT_TABLE_ASSET_LIMIT",
    "GateDecision",
    "GatePolicy",
    "_verification_should_block_report",
    "verification_to_gate_decision",
]
