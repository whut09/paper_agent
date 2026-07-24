"""Shared schemas for the PaperAgent harness."""

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "AcceptanceBlocker": "paper_agent.schemas.acceptance",
    "AcceptanceMetrics": "paper_agent.schemas.acceptance",
    "AgentContract": "paper_agent.schemas.agent",
    "CodexConfig": "paper_agent.schemas.paper",
    "Claim": "paper_agent.schemas.evidence",
    "ClaimGrounding": "paper_agent.schemas.evidence",
    "AssetCandidate": "paper_agent.schemas.contracts",
    "AssetCandidatePool": "paper_agent.schemas.contracts",
    "BoundingBox": "paper_agent.schemas.contracts",
    "CandidateScore": "paper_agent.schemas.contracts",
    "CandidateStrategy": "paper_agent.schemas.contracts",
    "Evidence": "paper_agent.schemas.evidence",
    "EvidenceBundle": "paper_agent.schemas.contracts",
    "EvidenceMap": "paper_agent.schemas.evidence",
    "Finding": "paper_agent.schemas.findings",
    "FindingReasonCode": "paper_agent.schemas.findings",
    "FindingSeverity": "paper_agent.schemas.findings",
    "FindingStage": "paper_agent.schemas.findings",
    "migrate_verification_payload": "paper_agent.schemas.findings",
    "GuardResultContract": "paper_agent.schemas.contracts",
    "ManifestComparison": "paper_agent.schemas.acceptance",
    "MigrationAcceptanceResult": "paper_agent.schemas.acceptance",
    "RenderAssetMeasurement": "paper_agent.schemas.qa",
    "RenderPageMeasurement": "paper_agent.schemas.qa",
    "RenderQAFinding": "paper_agent.schemas.qa",
    "RenderQAResult": "paper_agent.schemas.qa",
    "SummaryRunResult": "paper_agent.schemas.qa",
    "SectionCoverageComparison": "paper_agent.schemas.acceptance",
    "GroundingSection": "paper_agent.schemas.claim",
    "KnowledgeGraphEdge": "paper_agent.schemas.report",
    "KnowledgeGraphNode": "paper_agent.schemas.report",
    "NodeResult": "paper_agent.schemas.report",
    "PaperAsset": "paper_agent.schemas.asset",
    "TextLine": "paper_agent.schemas.asset",
    "VerificationResult": "paper_agent.schemas.report",
    "WorkflowNodeContract": "paper_agent.schemas.contracts",
    "WorkflowNodeLike": "paper_agent.schemas.contracts",
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(_LAZY_EXPORTS[name])
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = sorted(_LAZY_EXPORTS)
