"""DAG executor for the PaperAgent harness.

The executor owns workflow state.  GUI callers only provide a context and may
disconnect without losing completed node outputs.
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from threading import Event

from paper_agent.harness.checkpoints import (
    CheckpointStore,
    CheckpointValidationError,
    context_state,
    identity_for_context,
    node_key,
    restore_context,
)
from paper_agent.harness.context import PaperWorkflowContext
from paper_agent.harness.errors import NodeTimeoutError, WorkflowTimeoutError, classify_error, is_recoverable_error
from paper_agent.harness.node import NodeResult, PaperWorkflowNode


def _float_setting(values: dict[str, str], *names: str, default: float = 0.0) -> float:
    for name in names:
        raw = values.get(name)
        if raw is None:
            raw = os.environ.get(name)
        if raw is None:
            try:
                from paper_agent.config import ConfigManager

                raw = ConfigManager.get(name)
            except (OSError, ValueError):
                raw = None
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                continue
    return default


def _node_timeout(node: PaperWorkflowNode, context: PaperWorkflowContext) -> float:
    if node.timeout_seconds is not None:
        return max(0.0, float(node.timeout_seconds))
    return _float_setting(
        context.codex_envs,
        "PAPER_AGENT_NODE_TIMEOUT_SECONDS",
        "WORKFLOW_NODE_TIMEOUT_SECONDS",
        default=0.0,
    )


def _workflow_timeout(context: PaperWorkflowContext) -> float:
    return _float_setting(
        context.codex_envs,
        "PAPER_AGENT_WORKFLOW_TIMEOUT_SECONDS",
        "WORKFLOW_TIMEOUT_SECONDS",
        default=3600.0,
    )


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
            ReviseReport,
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
                ReviseReport(),
                GenerateReport(),
            ]
        )

    def _checkpoint_store(self, context: PaperWorkflowContext) -> CheckpointStore | None:
        input_path = str(getattr(context, "input_path", ""))
        output_dir = context.output_dir
        # Small unit-test contexts often use a fictitious paper.pdf and should
        # remain side-effect free. Production inputs always exist by this point.
        if not os.path.isfile(input_path) and not context.checkpoint_root:
            return None
        root = context.checkpoint_root or output_dir
        try:
            store = CheckpointStore(root, identity_for_context(context))
        except OSError:
            return None
        context.checkpoint_root = store.root
        return store

    def _restore_ready_nodes(
        self,
        context: PaperWorkflowContext,
        store: CheckpointStore | None,
        pending: set[str],
        completed: set[str],
    ) -> None:
        if store is None:
            return
        # Restore in dependency order. A bad checkpoint invalidates that node
        # and all descendants, while unrelated valid nodes remain usable.
        changed = True
        while changed:
            changed = False
            for name in sorted(pending):
                node = self.nodes[name]
                if any(dep not in completed for dep in node.depends_on):
                    continue
                dependencies = {dep: context.checkpoint_keys[dep] for dep in node.depends_on if dep in context.checkpoint_keys}
                key = node_key(context, name, dependencies, tuple(node.requires))
                context.checkpoint_keys[name] = key
                try:
                    loaded = store.load(name, key)
                except CheckpointValidationError:
                    context.invalidated_nodes.add(name)
                    continue
                if loaded is None:
                    continue
                state, result = loaded
                restore_context(context, state)
                if not isinstance(result, NodeResult):
                    result = NodeResult()
                context.node_results[name] = result
                context.restored_nodes.add(name)
                context.agent_trace.append(
                    {
                        "run_id": context.run_id,
                        "agent": node.agent_role.value,
                        "contract": node.agent_contract.name if node.agent_contract else "",
                        "llm_required": bool(node.agent_contract and node.agent_contract.llm_required),
                        "node": name,
                        "input_keys": list(node.requires),
                        "output_keys": list(node.produces),
                        "status": "restored",
                        "errors": [],
                        "warnings": [],
                        "metrics": {"checkpoint_restored": True},
                        "artifacts": list(result.artifacts),
                    }
                )
                pending.remove(name)
                completed.add(name)
                changed = True

    def _execute_node(self, node: PaperWorkflowNode, context: PaperWorkflowContext) -> NodeResult | None:
        attempts = max(1, int(getattr(node, "max_attempts", 1)))
        timeout = _node_timeout(node, context)
        last_error: BaseException | None = None
        for attempt in range(attempts):
            context.node_attempts[node.name] = attempt + 1
            context.check_cancelled(node.name)
            context.current_node = node.name
            context.node_cancellation_events[node.name] = Event()
            remaining = (
                context.workflow_timeout_seconds - (time.monotonic() - context.workflow_started_at)
                if context.workflow_timeout_seconds and context.workflow_started_at
                else 0.0
            )
            if context.workflow_timeout_seconds and remaining <= 0:
                raise WorkflowTimeoutError("workflow exceeded its total time budget")
            effective_timeout = timeout
            if remaining:
                effective_timeout = min(timeout, remaining) if timeout else remaining
            if effective_timeout:
                context.node_deadlines[node.name] = time.monotonic() + effective_timeout
            worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"paper-node-{node.name}")
            future = worker.submit(node.run, context)
            try:
                result = future.result(timeout=effective_timeout or None)
                worker.shutdown(wait=True, cancel_futures=True)
                return result
            except FuturesTimeoutError as exc:
                context.cancel_node(node.name)
                future.cancel()
                worker.shutdown(wait=False, cancel_futures=True)
                last_error = NodeTimeoutError(f"node {node.name} timed out after {effective_timeout:.1f}s")
            except BaseException as exc:
                worker.shutdown(wait=False, cancel_futures=True)
                last_error = exc
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                context.node_deadlines.pop(node.name, None)
                context.node_cancellation_events.pop(node.name, None)
            if last_error is None or not is_recoverable_error(last_error) or attempt + 1 >= attempts:
                raise last_error
            remaining = context.workflow_timeout_seconds - (time.monotonic() - context.workflow_started_at) if context.workflow_timeout_seconds and context.workflow_started_at else None
            delay = max(0.0, float(getattr(node, "retry_base_delay", 0.25))) * (2**attempt)
            if remaining is not None and remaining <= delay:
                raise last_error
            time.sleep(delay)
        raise last_error or RuntimeError(f"node {node.name} failed")

    def run(self, context: PaperWorkflowContext) -> PaperWorkflowContext:
        from paper_agent.paper_summary import _node_trace_entry, _normalize_node_result, _write_harness_sidecars

        pending = set(self.nodes)
        completed: set[str] = set()
        context.workflow_started_at = time.monotonic()
        context.workflow_timeout_seconds = _workflow_timeout(context)
        store = self._checkpoint_store(context)
        try:
            self._restore_ready_nodes(context, store, pending, completed)
            while pending:
                context.check_cancelled()
                if context.workflow_timeout_seconds and time.monotonic() - context.workflow_started_at >= context.workflow_timeout_seconds:
                    raise WorkflowTimeoutError("workflow exceeded its total time budget")
                ready = sorted(name for name in pending if all(dep in completed for dep in self.nodes[name].depends_on))
                if not ready:
                    raise ValueError(f"Workflow has cyclic or unsatisfied dependencies: {sorted(pending)}")
                for name in ready:
                    context.check_cancelled()
                    node = self.nodes[name]
                    dependencies = {dep: context.checkpoint_keys[dep] for dep in node.depends_on if dep in context.checkpoint_keys}
                    key = node_key(context, name, dependencies, tuple(node.requires))
                    context.checkpoint_keys[name] = key
                    started_at = datetime.now(timezone.utc)
                    try:
                        node_result = self._execute_node(node, context)
                    except BaseException as exc:
                        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                        result = _normalize_node_result(node, NodeResult(status="failed", errors=[str(exc)]), context, elapsed)
                        result.metrics["error_class"] = classify_error(exc)
                        result.metrics["retry_count"] = max(0, context.node_attempts.get(node.name, 1) - 1)
                        context.node_results[node.name] = result
                        context.agent_trace.append(_node_trace_entry(context, node, result))
                        if context.output and context.paper_name and (context.verification or context.guard_results):
                            _write_harness_sidecars(context)
                        raise
                    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                    result = _normalize_node_result(node, node_result, context, elapsed)
                    result.metrics.setdefault("retry_count", max(0, context.node_attempts.get(node.name, 1) - 1))
                    context.node_results[node.name] = result
                    context.agent_trace.append(_node_trace_entry(context, node, result))
                    if store is not None and result.status != "failed":
                        store.save(node.name, key, context_state(context), result)
                    if "trace.json" in node.produces:
                        _write_harness_sidecars(context)
                    pending.remove(name)
                    if name == "ReviseReport" and context.gate_decision == "revise":
                        completed.discard("VerifyClaims")
                        pending.update({"VerifyClaims", "ReviseReport"})
                    elif name == "ReviseReport" and context.gate_decision == "block":
                        _write_harness_sidecars(context)
                        pending.clear()
                        completed.add(name)
                        break
                    else:
                        completed.add(name)
            return context
        finally:
            context.close()


__all__ = ["PaperWorkflow"]
