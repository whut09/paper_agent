"""Deterministic evidence and asset candidate utilities."""

from paper_agent.assets.candidates import (
    build_asset_candidate_pool,
    candidate_score,
    candidate_bboxes_for_asset,
)

__all__ = [
    "build_asset_candidate_pool",
    "candidate_bboxes_for_asset",
    "candidate_score",
]
