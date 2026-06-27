"""Shared schemas for the PaperAgent harness."""

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "AgentContract": "paper_agent.schemas.agent",
    "CodexConfig": "paper_agent.schemas.paper",
    "Claim": "paper_agent.schemas.evidence",
    "ClaimGrounding": "paper_agent.schemas.evidence",
    "Evidence": "paper_agent.schemas.evidence",
    "EvidenceMap": "paper_agent.schemas.evidence",
    "GroundingSection": "paper_agent.schemas.claim",
    "KnowledgeGraphEdge": "paper_agent.schemas.report",
    "KnowledgeGraphNode": "paper_agent.schemas.report",
    "NodeResult": "paper_agent.schemas.report",
    "PaperAsset": "paper_agent.schemas.asset",
    "TextLine": "paper_agent.schemas.asset",
    "VerificationResult": "paper_agent.schemas.report",
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(_LAZY_EXPORTS[name])
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = sorted(_LAZY_EXPORTS)
