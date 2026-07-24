"""Evaluation utilities for PaperAgent.

Imports are lazy because the legacy guard facade depends on the compatibility
module ``paper_summary``.  Keeping it lazy lets deterministic validators be
used by ``paper_summary`` without creating an import cycle.
"""

__all__ = [
    "GUARD_SPECS",
    "GuardResult",
    "GuardSpec",
    "evaluate_case",
    "evaluate_cases",
    "load_cases",
    "_parse_verification_result",
    "_verification_should_block_report",
    "VisualLayerDecision",
    "VisualMeasurements",
    "decide_visual_layers",
    "precision_recall",
    "audit_existing_run",
    "run_acceptance_suite",
]


def __getattr__(name: str):
    if name in {"GUARD_SPECS", "GuardResult", "GuardSpec"}:
        from paper_agent.evaluation.guards import GUARD_SPECS, GuardResult, GuardSpec

        return {"GUARD_SPECS": GUARD_SPECS, "GuardResult": GuardResult, "GuardSpec": GuardSpec}[name]
    if name in {"_parse_verification_result", "_verification_should_block_report"}:
        from paper_agent.evaluation.validators import (
            _parse_verification_result,
            _verification_should_block_report,
        )

        return {
            "_parse_verification_result": _parse_verification_result,
            "_verification_should_block_report": _verification_should_block_report,
        }[name]
    if name in {"evaluate_case", "evaluate_cases", "load_cases"}:
        from paper_agent.evaluation.runner import evaluate_case, evaluate_cases, load_cases

        return {"evaluate_case": evaluate_case, "evaluate_cases": evaluate_cases, "load_cases": load_cases}[name]
    if name in {"VisualLayerDecision", "VisualMeasurements", "decide_visual_layers", "precision_recall"}:
        from paper_agent.evaluation.visual_validation import (
            VisualLayerDecision,
            VisualMeasurements,
            decide_visual_layers,
            precision_recall,
        )

        return {
            "VisualLayerDecision": VisualLayerDecision,
            "VisualMeasurements": VisualMeasurements,
            "decide_visual_layers": decide_visual_layers,
            "precision_recall": precision_recall,
        }[name]
    if name in {"audit_existing_run", "run_acceptance_suite"}:
        from paper_agent.evaluation.acceptance import audit_existing_run, run_acceptance_suite

        return {"audit_existing_run": audit_existing_run, "run_acceptance_suite": run_acceptance_suite}[name]
    raise AttributeError(name)
