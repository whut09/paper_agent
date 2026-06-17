"""Workflow harness primitives for PaperAgent."""

from paper_agent.harness.context import PaperWorkflowContext, ProgressCallback
from paper_agent.harness.node import PaperWorkflowNode
from paper_agent.harness.result import VerificationResult
from paper_agent.harness.trace import PaperAgentRole
from paper_agent.harness.workflow import PaperWorkflow

__all__ = [
    "PaperAgentRole",
    "PaperWorkflow",
    "PaperWorkflowContext",
    "PaperWorkflowNode",
    "ProgressCallback",
    "VerificationResult",
]

