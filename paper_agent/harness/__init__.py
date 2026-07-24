"""Workflow harness primitives for PaperAgent."""

from paper_agent.agents.contracts import PaperAgentRole
from paper_agent.harness.context import PaperContext, PaperWorkflowContext, ProgressCallback
from paper_agent.harness.executor import PaperWorkflow
from paper_agent.harness.checkpoints import CheckpointStore, CheckpointValidationError
from paper_agent.harness.errors import (
    NodeTimeoutError,
    NonRecoverableWorkflowError,
    PaperAgentHarnessError,
    RecoverableWorkflowError,
    VerificationBlockedError,
    WorkflowTimeoutError,
    classify_error,
)
from paper_agent.harness.node import HarnessNode, NodeResult, PaperWorkflowNode
from paper_agent.harness.repair import (
    RepairAction,
    RepairState,
    RepairStateMachine,
    RepairStep,
    RepairTransition,
)

__all__ = [
    "PaperAgentRole",
    "HarnessNode",
    "NodeResult",
    "PaperContext",
    "PaperWorkflow",
    "PaperWorkflowContext",
    "PaperWorkflowNode",
    "CheckpointStore",
    "CheckpointValidationError",
    "NodeTimeoutError",
    "NonRecoverableWorkflowError",
    "PaperAgentHarnessError",
    "RecoverableWorkflowError",
    "VerificationBlockedError",
    "WorkflowTimeoutError",
    "classify_error",
    "ProgressCallback",
    "RepairAction",
    "RepairState",
    "RepairStateMachine",
    "RepairStep",
    "RepairTransition",
]
