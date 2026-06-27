"""Local golden-case evaluation harness for PaperAgent."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    input_text: str
    assets: list[dict[str, Any]] = field(default_factory=list)
    expected_claims: list[str] = field(default_factory=list)
    forbidden_claims: list[str] = field(default_factory=list)
    expected_sections: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvaluationMetrics:
    grounded_claim_rate: float
    unsupported_core_claim_count: int
    coverage_score: float
    asset_reference_accuracy: float


@dataclass(frozen=True)
class EvaluationResult:
    case_name: str
    metrics: EvaluationMetrics


def load_cases(cases_dir: str | Path) -> list[EvaluationCase]:
    path = Path(cases_dir)
    if not path.exists():
        raise FileNotFoundError(f"Evaluation cases directory not found: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"Evaluation cases path is not a directory: {path}")
    cases: list[EvaluationCase] = []
    for case_path in sorted(path.glob("*.json")):
        payload = json.loads(case_path.read_text(encoding="utf-8"))
        cases.append(
            EvaluationCase(
                name=str(payload.get("name") or case_path.stem),
                input_text=str(payload.get("input_text") or ""),
                assets=list(payload.get("assets") or []),
                expected_claims=[str(item) for item in payload.get("expected_claims") or []],
                forbidden_claims=[str(item) for item in payload.get("forbidden_claims") or []],
                expected_sections=[str(item) for item in payload.get("expected_sections") or []],
            )
        )
    return cases


def evaluate_case(case: EvaluationCase) -> EvaluationResult:
    from paper_agent.paper_summary import _attach_claims_to_grounding_map, _build_grounding_map

    grounding_map = _build_grounding_map(case.input_text)
    expected_claims = [
        {"id": f"expected-{index}", "claim": text, "type": "claim", "core": True}
        for index, text in enumerate(case.expected_claims, 1)
    ]
    forbidden_claims = [
        {"id": f"forbidden-{index}", "claim": text, "type": "claim", "core": True}
        for index, text in enumerate(case.forbidden_claims, 1)
    ]
    grounded = _attach_claims_to_grounding_map(grounding_map, expected_claims + forbidden_claims)
    claims = grounded.get("claims", [])
    evidence_by_id = {item.get("id", ""): item for item in grounded.get("evidence", [])}
    expected_count = len(expected_claims)
    grounded_expected = [
        claim
        for claim in claims
        if str(claim.get("id", "")).startswith("expected-")
        and claim.get("evidence_ids")
        and _claim_supported_by_evidence(claim, evidence_by_id)
    ]
    unsupported_core = [
        claim
        for claim in claims
        if bool(claim.get("core", True))
        and (not claim.get("evidence_ids") or not _claim_supported_by_evidence(claim, evidence_by_id))
    ]
    metrics = EvaluationMetrics(
        grounded_claim_rate=_safe_rate(len(grounded_expected), expected_count),
        unsupported_core_claim_count=len(unsupported_core),
        coverage_score=_section_coverage_score(grounded, case.expected_sections),
        asset_reference_accuracy=_asset_reference_accuracy(case.input_text, case.assets),
    )
    return EvaluationResult(case.name, metrics)


def evaluate_cases(cases_dir: str | Path) -> dict[str, Any]:
    cases = load_cases(cases_dir)
    results = [evaluate_case(case) for case in cases]
    metrics = _aggregate_metrics([result.metrics for result in results])
    return {
        "case_count": len(results),
        "metrics": asdict(metrics),
        "cases": [
            {
                "name": result.case_name,
                "metrics": asdict(result.metrics),
            }
            for result in results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run PaperAgent golden-case evaluation.")
    parser.add_argument("--cases", default="evaluation/golden_cases", help="Directory containing *.json golden cases.")
    parsed = parser.parse_args(argv)
    print(json.dumps(evaluate_cases(parsed.cases), ensure_ascii=False, indent=2))
    return 0


def _aggregate_metrics(metrics: list[EvaluationMetrics]) -> EvaluationMetrics:
    if not metrics:
        return EvaluationMetrics(0.0, 0, 0.0, 0.0)
    return EvaluationMetrics(
        grounded_claim_rate=sum(item.grounded_claim_rate for item in metrics) / len(metrics),
        unsupported_core_claim_count=sum(item.unsupported_core_claim_count for item in metrics),
        coverage_score=sum(item.coverage_score for item in metrics) / len(metrics),
        asset_reference_accuracy=sum(item.asset_reference_accuracy for item in metrics) / len(metrics),
    )


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return numerator / denominator


def _section_coverage_score(grounding_map: dict[str, Any], expected_sections: list[str]) -> float:
    if not expected_sections:
        return 1.0
    aliases = {
        "introduction": "intro",
        "background": "intro",
        "intro": "intro",
        "method": "method",
        "methods": "method",
        "experiments": "experiments",
        "experiment": "experiments",
        "results": "experiments",
        "evaluation": "experiments",
    }
    matched = 0
    for expected in expected_sections:
        bucket = aliases.get(expected.strip().lower(), expected.strip().lower())
        if grounding_map.get(bucket):
            matched += 1
    return _safe_rate(matched, len(expected_sections))


def _asset_reference_accuracy(input_text: str, assets: list[dict[str, Any]]) -> float:
    if not assets:
        return 1.0
    matched = sum(1 for asset in assets if _asset_is_referenced(input_text, asset))
    return _safe_rate(matched, len(assets))


def _asset_is_referenced(input_text: str, asset: dict[str, Any]) -> bool:
    haystack = input_text.lower()
    caption = str(asset.get("caption") or asset.get("text") or "").strip()
    if caption and caption.lower() in haystack:
        return True
    kind = str(asset.get("kind") or "").lower()
    number = str(asset.get("number") or "").strip()
    if kind and number:
        patterns = {
            "figure": [f"figure {number}", f"fig. {number}", f"fig {number}", f"图{number}"],
            "table": [f"table {number}", f"tab. {number}", f"tab {number}", f"表{number}"],
            "formula": [f"equation {number}", f"formula {number}", f"公式{number}"],
        }
        if any(pattern in haystack for pattern in patterns.get(kind, [])):
            return True
    return False


def _claim_supported_by_evidence(claim: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> bool:
    claim_tokens = _content_tokens(str(claim.get("claim") or claim.get("text") or ""))
    if not claim_tokens:
        return False
    evidence_text = " ".join(
        str(evidence_by_id.get(evidence_id, {}).get("text", ""))
        for evidence_id in claim.get("evidence_ids", [])
    )
    evidence_tokens = _content_tokens(evidence_text)
    overlap = claim_tokens & evidence_tokens
    return len(overlap) >= min(2, len(claim_tokens))


def _content_tokens(text: str) -> set[str]:
    import re

    return {
        token
        for token in re.findall(r"[a-z][a-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", text.lower())
        if token not in {"the", "and", "for", "with", "that", "this"}
    }


__all__ = [
    "EvaluationCase",
    "EvaluationMetrics",
    "EvaluationResult",
    "evaluate_case",
    "evaluate_cases",
    "load_cases",
    "main",
]
