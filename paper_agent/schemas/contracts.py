"""Typed contracts for evidence, asset candidates, and workflow I/O.

The compatibility implementation still lives in ``paper_summary``.  These
types deliberately contain no extraction or model code so they can be adopted
incrementally without changing the legacy facade.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

BoundingBox = tuple[float, float, float, float]


class CandidateStrategy(str, Enum):
    DETECTOR = "detector_bbox"
    TEXT_HEURISTIC = "text_heuristic_bbox"
    BORDER_ENCLOSED = "border_enclosed_bbox"
    ADJACENT_SPLIT = "adjacent_object_split"


@dataclass(frozen=True)
class EvidenceBundle:
    """Immutable source evidence from one PDF page and one visual object."""

    page_number: int
    source_bbox: BoundingBox
    caption_text: str
    object_type: str
    table_or_formula_text: str = ""
    image_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["image_path"] = str(self.image_path) if self.image_path else ""
        return payload


@dataclass(frozen=True)
class CandidateScore:
    """Deterministic score components; every component is in the range [0, 1]."""

    caption_identity: float = 0.0
    bbox_containment: float = 0.0
    border_completeness: float = 0.0
    numeric_cell_coverage: float = 0.0
    object_purity: float = 0.0
    page_column_overlap: float = 0.0

    @property
    def total(self) -> float:
        return round(
            0.22 * self.caption_identity
            + 0.22 * self.bbox_containment
            + 0.16 * self.border_completeness
            + 0.16 * self.numeric_cell_coverage
            + 0.16 * self.object_purity
            + 0.08 * self.page_column_overlap,
            6,
        )

    @property
    def explanation(self) -> str:
        return (
            f"total={self.total:.3f}; caption={self.caption_identity:.3f}; "
            f"containment={self.bbox_containment:.3f}; border={self.border_completeness:.3f}; "
            f"numeric={self.numeric_cell_coverage:.3f}; purity={self.object_purity:.3f}; "
            f"column={self.page_column_overlap:.3f}"
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["total"] = self.total
        payload["explanation"] = self.explanation
        return payload


@dataclass(frozen=True)
class AssetCandidate:
    """One possible crop for an evidence bundle.

    Candidates are retained even when they lose selection.  This makes a bad
    crop inspectable and lets a later repair stage choose another geometry.
    """

    evidence: EvidenceBundle
    strategy: CandidateStrategy
    bbox: BoundingBox
    image_path: Path | None
    score: CandidateScore
    diagnostics: tuple[str, ...] = ()

    @property
    def quality(self) -> float:
        return self.score.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.evidence.page_number,
            "caption": self.evidence.caption_text,
            "object_type": self.evidence.object_type,
            "strategy": self.strategy.value,
            "bbox": list(self.bbox),
            "image_path": str(self.image_path) if self.image_path else "",
            "score": self.score.to_dict(),
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class AssetCandidatePool:
    evidence: EvidenceBundle
    candidates: tuple[AssetCandidate, ...]

    @property
    def selected(self) -> AssetCandidate:
        if not self.candidates:
            raise ValueError("An asset candidate pool cannot be empty.")
        order = {strategy: index for index, strategy in enumerate(CandidateStrategy)}
        return max(
            self.candidates,
            key=lambda item: (item.quality, -order[item.strategy]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence": self.evidence.to_dict(),
            "selected_strategy": self.selected.strategy.value,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class WorkflowNodeContract:
    """Runtime-visible contract for a workflow node and its sidecars."""

    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    sidecars: tuple[str, ...] = ()
    independently_resumable: bool = False
    context_reads: tuple[str, ...] = ()
    context_writes: tuple[str, ...] = ()


class WorkflowNodeLike(Protocol):
    name: str
    requires: list[str]
    produces: list[str]

    def run(self, context: Any) -> Any:
        ...


@dataclass(frozen=True)
class GuardResultContract:
    """Stable shape shared by local, model, and aggregate guards."""

    name: str
    status: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metrics: tuple[tuple[str, str], ...] = ()


_CAPTION_PATTERNS = {
    "table": re.compile(r"(?i)^(?:table|tab\.|表)\s*\d+"),
    "figure": re.compile(r"(?i)^(?:figure|fig\.?|图)\s*\d+"),
    "formula": re.compile(r"(?i)^(?:equation|eq\.?|公式)\s*\(?\d+\)?"),
}


def caption_identity_score(caption: str, object_type: str) -> float:
    pattern = _CAPTION_PATTERNS.get(object_type.lower())
    if not pattern:
        return 0.5 if caption.strip() else 0.0
    return 1.0 if pattern.search(caption.strip()) else 0.0


__all__ = [
    "AssetCandidate",
    "AssetCandidatePool",
    "BoundingBox",
    "CandidateScore",
    "CandidateStrategy",
    "EvidenceBundle",
    "GuardResultContract",
    "WorkflowNodeContract",
    "WorkflowNodeLike",
    "caption_identity_score",
]
