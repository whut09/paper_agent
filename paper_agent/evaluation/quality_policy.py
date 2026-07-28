"""Risk-tiered delivery policy for report quality findings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Protocol


class FindingLike(Protocol):
    reason_code: str
    severity: str


class QualityDisposition(str, Enum):
    ACCEPT = "accept"
    WARN = "warn"
    AUTO_REPAIR = "auto_repair"
    BLOCK = "block"


AUTO_REPAIR_REASON_CODES = frozenset({
    "image_cropped",
    "page_overflow",
})

WARNING_REASON_CODES = frozenset({
    "caption_not_adjacent",
    "rendered_asset_count_mismatch",
    "renderer_failed",
    "renderer_timeout",
    "renderer_transport_failure",
    "renderer_unavailable",
})

BLOCK_REASON_CODES = frozenset({
    "empty_render",
    "image_too_small",
    "invalid_docx",
    "missing_critical_asset",
    "rendered_pdf_invalid",
    "unresolved_asset_marker",
})


@dataclass(frozen=True)
class QualityDecision:
    disposition: QualityDisposition
    repairable_reason_codes: tuple[str, ...] = ()
    blocking_reason_codes: tuple[str, ...] = ()
    warning_reason_codes: tuple[str, ...] = ()


def decide_quality(findings: Iterable[FindingLike]) -> QualityDecision:
    repairable: list[str] = []
    blocking: list[str] = []
    warnings: list[str] = []
    for finding in findings:
        reason_code = str(getattr(finding, "reason_code", "") or "")
        severity = str(getattr(finding, "severity", "") or "").lower()
        if reason_code in WARNING_REASON_CODES:
            warnings.append(reason_code)
        elif reason_code in AUTO_REPAIR_REASON_CODES and severity == "block":
            repairable.append(reason_code)
        elif reason_code in BLOCK_REASON_CODES or severity == "block":
            blocking.append(reason_code or "unknown_quality_failure")
        elif reason_code:
            warnings.append(reason_code)

    deduplicate = lambda values: tuple(dict.fromkeys(values))
    repairable_codes = deduplicate(repairable)
    blocking_codes = deduplicate(blocking)
    warning_codes = deduplicate(warnings)
    if blocking_codes:
        disposition = QualityDisposition.BLOCK
    elif repairable_codes:
        disposition = QualityDisposition.AUTO_REPAIR
    elif warning_codes:
        disposition = QualityDisposition.WARN
    else:
        disposition = QualityDisposition.ACCEPT
    return QualityDecision(disposition, repairable_codes, blocking_codes, warning_codes)


__all__ = [
    "AUTO_REPAIR_REASON_CODES",
    "BLOCK_REASON_CODES",
    "QualityDecision",
    "QualityDisposition",
    "WARNING_REASON_CODES",
    "decide_quality",
]
