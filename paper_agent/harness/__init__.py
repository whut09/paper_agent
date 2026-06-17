"""Workflow harness primitives for PaperAgent."""

from paper_agent.harness.context import PaperContext, PaperWorkflowContext, ProgressCallback
from paper_agent.harness.node import HarnessNode, PaperWorkflowNode
from paper_agent.harness.result import NodeResult, VerificationResult
from paper_agent.harness.trace import PaperAgentRole
from paper_agent.harness.workflow import PaperWorkflow

__all__ = [
    "PaperAgentRole",
    "HarnessNode",
    "NodeResult",
    "PaperContext",
    "PaperWorkflow",
    "PaperWorkflowContext",
    "PaperWorkflowNode",
    "ProgressCallback",
    "VerificationResult",
]
