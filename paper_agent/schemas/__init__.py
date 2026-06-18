"""Shared schemas for the PaperAgent harness."""

from paper_agent.schemas.agent import AgentContract
from paper_agent.schemas.asset import PaperAsset, TextLine
from paper_agent.schemas.claim import GroundingSection
from paper_agent.schemas.paper import CodexConfig
from paper_agent.schemas.report import KnowledgeGraphEdge, KnowledgeGraphNode, NodeResult, VerificationResult

__all__ = [
    "CodexConfig",
    "AgentContract",
    "GroundingSection",
    "KnowledgeGraphEdge",
    "KnowledgeGraphNode",
    "NodeResult",
    "PaperAsset",
    "TextLine",
    "VerificationResult",
]
