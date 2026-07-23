"""Layered, deterministic visual validation contracts.

The PDF facade supplies measurements and findings.  This module deliberately
contains no PDF, image, or network code so the decision policy is cheap and
fixture-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Mapping


VisualOutcome = Literal["pass", "warn", "block", "arbitrate"]


@dataclass(frozen=True)
class VisualMeasurements:
    asset_id: int
    kind: str
    width: int
    height: int
    marker_present: bool
    caption_identity: bool
    bbox_valid: bool
    bbox_within_page: bool | None
    table_border_closed: bool | None
    numeric_values: int
    body_rows: int
    text_only: bool
    overlapping_objects: int = 0
    page_number: int = 0

    @property
    def dimensions_valid(self) -> bool:
        return self.width > 0 and self.height > 0

    @property
    def body_evidence_valid(self) -> bool:
        return self.numeric_values >= 4 or self.body_rows >= 2

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "width": self.width,
            "height": self.height,
            "marker_present": self.marker_present,
            "caption_identity": self.caption_identity,
            "bbox_valid": self.bbox_valid,
            "bbox_within_page": self.bbox_within_page,
            "table_border_closed": self.table_border_closed,
            "numeric_values": self.numeric_values,
            "body_rows": self.body_rows,
            "text_only": self.text_only,
            "overlapping_objects": self.overlapping_objects,
            "page_number": self.page_number,
        }


@dataclass(frozen=True)
class VisualLayerDecision:
    outcome: VisualOutcome
    confidence: float
    reason: str
    layers: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "confidence": self.confidence,
            "reason": self.reason,
            "layers": list(self.layers),
        }


def decide_visual_layers(
    measurements: VisualMeasurements,
    *,
    deterministic_errors: Iterable[Mapping[str, object]] = (),
    text_warnings: Iterable[Mapping[str, object]] = (),
) -> VisualLayerDecision:
    """Decide whether a candidate is clear, invalid, or needs arbitration."""

    errors = tuple(deterministic_errors)
    warnings = tuple(text_warnings)
    if errors:
        return VisualLayerDecision(
            "block",
            max(_confidence(item, 0.9) for item in errors),
            "deterministic check failed",
            ("deterministic",),
        )
    if not measurements.marker_present:
        return VisualLayerDecision("warn", 0.65, "asset marker is not present", ("deterministic",))
    if not measurements.caption_identity:
        return VisualLayerDecision("block", 0.9, "caption identity does not match object type", ("deterministic",))
    if measurements.overlapping_objects:
        return VisualLayerDecision("arbitrate", 0.72, "candidate overlaps another object", ("deterministic",))
    if warnings:
        return VisualLayerDecision("arbitrate", 0.72, "text/OCR consistency is inconclusive", ("deterministic", "text"))
    if not measurements.bbox_valid or measurements.bbox_within_page is False:
        return VisualLayerDecision("arbitrate", 0.68, "bbox validity is inconclusive", ("deterministic",))
    if measurements.kind == "table" and not measurements.body_evidence_valid:
        return VisualLayerDecision("arbitrate", 0.7, "table body evidence is incomplete", ("deterministic", "text"))
    if measurements.kind == "table" and measurements.table_border_closed is False:
        return VisualLayerDecision("arbitrate", 0.68, "table border closure is inconclusive", ("deterministic",))
    if measurements.kind == "figure" and measurements.text_only:
        return VisualLayerDecision("block", 0.9, "figure candidate is text-only", ("deterministic", "text"))
    if not measurements.dimensions_valid:
        return VisualLayerDecision("block", 0.99, "image dimensions are invalid", ("deterministic",))
    return VisualLayerDecision("pass", 0.92, "deterministic and text checks agree", ("deterministic", "text"))


def precision_recall(
    cases: Iterable[Mapping[str, object]],
    *,
    kind: str,
) -> dict[str, float | int]:
    """Compute local Guard precision/recall from fixture labels."""

    selected = [item for item in cases if str(item.get("kind", "")) == kind]
    true_positive = sum(1 for item in selected if item.get("expected_bad") and item.get("predicted_bad"))
    false_positive = sum(1 for item in selected if not item.get("expected_bad") and item.get("predicted_bad"))
    false_negative = sum(1 for item in selected if item.get("expected_bad") and not item.get("predicted_bad"))
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    return {
        "kind": kind,
        "count": len(selected),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
    }


def _confidence(item: Mapping[str, object], default: float) -> float:
    try:
        return max(0.0, min(1.0, float(item.get("confidence", default))))
    except (TypeError, ValueError):
        return default


__all__ = [
    "VisualLayerDecision",
    "VisualMeasurements",
    "decide_visual_layers",
    "precision_recall",
]
