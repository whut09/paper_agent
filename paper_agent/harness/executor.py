"""DAG executor for the PaperAgent harness."""

from __future__ import annotations

from datetime import datetime, timezone

from paper_agent.harness.context import PaperWorkflowContext
from paper_agent.harness.node import NodeResult, PaperWorkflowNode


class PaperWorkflow:
    def __init__(self, nodes: list[PaperWorkflowNode]):
        self.nodes = {node.name: node for node in nodes}
        if len(self.nodes) != len(nodes):
            raise ValueError("PaperWorkflow node names must be unique.")
        for node in nodes:
            missing = [name for name in node.depends_on if name not in self.nodes]
            if missing:
                raise ValueError(f"Workflow node {node.name} depends on missing nodes: {missing}")

    @classmethod
    def default(cls) -> "PaperWorkflow":
        from paper_agent.paper_summary import (
            ExtractMethods,
            ExtractSections,
            GenerateReport,
            ParsePaper,
            PreparePaper,
            SummarizeContribution,
            VerifyClaims,
        )

        return cls(
            [
                PreparePaper(),
                ParsePaper(),
                ExtractSections(),
                SummarizeContribution(),
                ExtractMethods(),
                VerifyClaims(),
                GenerateReport(),
            ]
        )

    def run(self, context: PaperWorkflowContext) -> PaperWorkflowContext:
        from paper_agent.paper_summary import (
            _node_trace_entry,
            _normalize_node_result,
            _write_harness_sidecars,
        )

        pending = set(self.nodes)
        completed: set[str] = set()
        try:
            while pending:
                ready = sorted(name for name in pending if all(dep in completed for dep in self.nodes[name].depends_on))
                if not ready:
                    raise ValueError(f"Workflow has cyclic or unsatisfied dependencies: {sorted(pending)}")
                for name in ready:
                    context.check_cancelled()
                    node = self.nodes[name]
                    started_at = datetime.now(timezone.utc)
                    try:
                        node_result = node.run(context)
                    except Exception as exc:
                        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                        result = _normalize_node_result(
                            node,
                            NodeResult(status="failed", errors=[str(exc)]),
                            context,
                            elapsed,
                        )
                        context.node_results[node.name] = result
                        context.agent_trace.append(_node_trace_entry(context, node, result))
                        if context.output and context.paper_name and (context.verification or context.guard_results):
                            _write_harness_sidecars(context)
                        raise
                    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                    result = _normalize_node_result(node, node_result, context, elapsed)
                    context.node_results[node.name] = result
                    context.agent_trace.append(_node_trace_entry(context, node, result))
                    if "trace.json" in node.produces:
                        _write_harness_sidecars(context)
                    pending.remove(name)
                    completed.add(name)
            return context
        finally:
            context.close()


__all__ = ["PaperWorkflow"]
