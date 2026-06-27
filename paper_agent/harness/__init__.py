"""Workflow harness primitives for PaperAgent."""

from paper_agent.agents.contracts import PaperAgentRole
from paper_agent.harness.context import PaperContext, PaperWorkflowContext, ProgressCallback
from paper_agent.harness.executor import PaperWorkflow
from paper_agent.harness.node import HarnessNode, NodeResult, PaperWorkflowNode

__all__ = [
    "PaperAgentRole",
    "HarnessNode",
    "NodeResult",
    "PaperContext",
    "PaperWorkflow",
    "PaperWorkflowContext",
    "PaperWorkflowNode",
    "ProgressCallback",
]
