"""Deterministic asset candidate generation and scoring.

This module is intentionally independent of OpenAI and PyMuPDF.  The PDF
facade can supply geometry from any detector while tests can use plain tuples.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from paper_agent.schemas.contracts import (
    AssetCandidate,
    AssetCandidatePool,
    BoundingBox,
    CandidateScore,
    CandidateStrategy,
    EvidenceBundle,
    caption_identity_score,
)


def _area(box: BoundingBox) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection(first: BoundingBox, second: BoundingBox) -> BoundingBox | None:
    result = (max(first[0], second[0]), max(first[1], second[1]), min(first[2], second[2]), min(first[3], second[3]))
    return result if result[2] > result[0] and result[3] > result[1] else None


def _contains(container: BoundingBox, item: BoundingBox, tolerance: float = 1.0) -> bool:
    return (
        container[0] <= item[0] + tolerance
        and container[1] <= item[1] + tolerance
        and container[2] >= item[2] - tolerance
        and container[3] >= item[3] - tolerance
    )


def _overlap_fraction(first: BoundingBox, second: BoundingBox) -> float:
    inter = _intersection(first, second)
    if inter is None or _area(first) <= 0:
        return 0.0
    return _area(inter) / _area(first)


def _numeric_coverage(text: str, object_type: str) -> float:
    if object_type.lower() != "table":
        return 1.0 if text.strip() else 0.5
    values = re.findall(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?%?(?![A-Za-z0-9])", text)
    rows = [line for line in text.splitlines() if line.strip()]
    if not values:
        return 0.15 if rows else 0.0
    return min(1.0, 0.35 + 0.1 * len(values) + 0.1 * max(0, len(rows) - 1))


def candidate_score(
    evidence: EvidenceBundle,
    bbox: BoundingBox,
    *,
    strategy: CandidateStrategy = CandidateStrategy.DETECTOR,
    border_closed: bool | None = None,
    object_bboxes: Iterable[BoundingBox] = (),
    page_width: float | None = None,
) -> CandidateScore:
    """Return a reproducible score without network or model calls."""

    source = evidence.source_bbox
    containment = 1.0 if _contains(bbox, source) else _overlap_fraction(source, bbox)
    border = 1.0 if border_closed is True else 0.45 if border_closed is None else 0.0
    purity = 1.0
    for other in object_bboxes:
        if other == source:
            continue
        overlap = _overlap_fraction(bbox, other)
        if overlap > 0.1:
            purity = max(0.0, purity - min(0.7, overlap))
    column = 1.0
    if page_width and page_width > 0:
        midpoint = (bbox[0] + bbox[2]) / 2
        source_midpoint = (source[0] + source[2]) / 2
        if (midpoint < page_width / 2) != (source_midpoint < page_width / 2):
            column = 0.0
    if strategy is CandidateStrategy.ADJACENT_SPLIT:
        purity = min(1.0, purity + 0.05)
    return CandidateScore(
        caption_identity=caption_identity_score(evidence.caption_text, evidence.object_type),
        bbox_containment=round(max(0.0, min(1.0, containment)), 6),
        border_completeness=border,
        numeric_cell_coverage=_numeric_coverage(evidence.table_or_formula_text, evidence.object_type),
        object_purity=round(purity, 6),
        page_column_overlap=column,
    )


def build_asset_candidate_pool(
    evidence: EvidenceBundle,
    candidates: Iterable[tuple[CandidateStrategy, BoundingBox, Path | None]],
    *,
    border_closed: dict[CandidateStrategy, bool | None] | None = None,
    object_bboxes: Iterable[BoundingBox] = (),
    page_width: float | None = None,
) -> AssetCandidatePool:
    """Build and retain every supplied candidate, including low-quality ones."""

    border_closed = border_closed or {}
    boxes = tuple(object_bboxes)
    result = []
    for strategy, bbox, image_path in candidates:
        score = candidate_score(
            evidence,
            bbox,
            strategy=strategy,
            border_closed=border_closed.get(strategy),
            object_bboxes=boxes,
            page_width=page_width,
        )
        diagnostics = (score.explanation,)
        result.append(AssetCandidate(evidence, strategy, bbox, image_path, score, diagnostics))
    if not result:
        raise ValueError("At least one asset candidate is required.")
    return AssetCandidatePool(evidence, tuple(result))


def candidate_bboxes_for_asset(
    bbox: BoundingBox,
    *,
    caption_bbox: BoundingBox | None = None,
    page_bbox: BoundingBox | None = None,
    adjacent_bboxes: Iterable[BoundingBox] = (),
) -> tuple[tuple[CandidateStrategy, BoundingBox], ...]:
    """Create the four standard geometry alternatives for a captured object."""

    text_bbox = bbox
    if caption_bbox is not None:
        text_bbox = (
            min(bbox[0], caption_bbox[0]),
            min(bbox[1], caption_bbox[1]),
            max(bbox[2], caption_bbox[2]),
            max(bbox[3], caption_bbox[3]),
        )
    candidates: list[tuple[CandidateStrategy, BoundingBox]] = [
        (CandidateStrategy.DETECTOR, bbox),
        (CandidateStrategy.TEXT_HEURISTIC, text_bbox),
        (CandidateStrategy.BORDER_ENCLOSED, bbox),
    ]
    split_candidates: list[BoundingBox] = []
    for adjacent in adjacent_bboxes:
        overlap = _intersection(bbox, adjacent)
        if overlap is None:
            continue
        choices = (
            (bbox[0], bbox[1], adjacent[0], bbox[3]),
            (adjacent[2], bbox[1], bbox[2], bbox[3]),
            (bbox[0], bbox[1], bbox[2], adjacent[1]),
            (bbox[0], adjacent[3], bbox[2], bbox[3]),
        )
        choices = tuple(choice for choice in choices if _area(choice) >= _area(bbox) * 0.2)
        if choices:
            split_candidates.append(max(choices, key=_area))
    candidates.append(
        (
            CandidateStrategy.ADJACENT_SPLIT,
            max(split_candidates, key=_area) if split_candidates else bbox,
        )
    )
    if page_bbox:
        candidates = [
            (strategy, (max(page_bbox[0], box[0]), max(page_bbox[1], box[1]), min(page_bbox[2], box[2]), min(page_bbox[3], box[3])))
            for strategy, box in candidates
        ]
    return tuple(candidates)


__all__ = ["build_asset_candidate_pool", "candidate_bboxes_for_asset", "candidate_score"]
