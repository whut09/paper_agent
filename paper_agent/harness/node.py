"""Workflow node contracts for the PaperAgent harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from paper_agent.agents.contracts import AgentContract, PaperAgentRole

if TYPE_CHECKING:
    from paper_agent.harness.context import PaperWorkflowContext


@dataclass
class NodeResult:
    status: str = "success"
    outputs: dict = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


class HarnessNode(Protocol):
    name: str
    role: str
    requires: list[str]
    produces: list[str]

    def run(self, ctx: PaperWorkflowContext) -> NodeResult:
        ...


class PaperWorkflowNode:
    name = ""
    depends_on: tuple[str, ...] = ()
    agent_role = PaperAgentRole.EXTRACTOR
    agent_contract: AgentContract | None = None
    requires: list[str] = []
    produces: list[str] = []

    @property
    def role(self) -> str:
        return self.agent_role.value

    def run(self, context: PaperWorkflowContext) -> NodeResult | None:
        raise NotImplementedError


__all__ = ["HarnessNode", "NodeResult", "PaperWorkflowNode"]
