"""Workflow context for the PaperAgent harness."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from paper_agent.harness.node import NodeResult


ProgressCallback = Callable[[float, str], None]


def _default_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class PaperWorkflowContext:
    input_path: str
    output_dir: str | Path
    pages: list[int] | None
    summary_language: str
    codex_envs: dict[str, str]
    max_assets: int
    progress: ProgressCallback | None = None
    cancellation_event: asyncio.Event | None = None
    run_id: str = field(default_factory=_default_run_id)
    output: Path | None = None
    source_path: Path | None = None
    pdf_path: Path | None = None
    paper_name: str = ""
    work_dir: Path | None = None
    text: str = ""
    assets: list[Any] = field(default_factory=list)
    asset_candidate_pools: list[Any] = field(default_factory=list)
    paper_title: str = ""
    abstract: str = ""
    formulas: list[str] = field(default_factory=list)
    grounding_map: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    knowledge_graph: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    correction_memories: list[Any] = field(default_factory=list)
    prompt_patches: list[Any] = field(default_factory=list)
    config: Any | None = None
    client: Any | None = None
    chunk_notes: list[str] = field(default_factory=list)
    partial_summaries: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    verification: Any | None = None
    guard_results: list[Any] = field(default_factory=list)
    gate_decision: str = ""
    gate_history: list[dict[str, Any]] = field(default_factory=list)
    revision_attempts: int = 0
    repair_attempts: dict[str, int] = field(default_factory=dict)
    repair_history: list[dict[str, Any]] = field(default_factory=list)
    repair_cost_used: float = 0.0
    repair_budget: float = 8.0
    repair_max_actions_per_asset: int = 2
    repair_recheck_guards: set[str] = field(default_factory=set)
    agent_trace: list[dict[str, object]] = field(default_factory=list)
    node_results: dict[str, NodeResult] = field(default_factory=dict)
    docx_path: Path | None = None
    summary_markdown_path: Path | None = None
    verification_failed_path: Path | None = None
    trace_path: Path | None = None
    grounding_map_path: Path | None = None
    verification_path: Path | None = None
    knowledge_graph_path: Path | None = None
    asset_candidates_path: Path | None = None
    acceptance_path: Path | None = None
    acceptance_result: Any | None = None
    legacy_asset_manifest: list[dict[str, Any]] = field(default_factory=list)
    legacy_summary: str = ""
    qa_result: Any | None = None
    qa_path: Path | None = None
    download_ready: bool = False
    current_stage: str = ""
    current_progress: float = 0.0
    progress_message: str = ""
    current_reason_code: str = ""
    model_call_count: int = 0
    checkpoint_root: Path | None = None
    checkpoint_keys: dict[str, str] = field(default_factory=dict)
    restored_nodes: set[str] = field(default_factory=set)
    invalidated_nodes: set[str] = field(default_factory=set)
    workflow_timeout_seconds: float = 0.0
    workflow_started_at: float | None = None
    node_cancellation_events: dict[str, threading.Event] = field(default_factory=dict)
    node_deadlines: dict[str, float] = field(default_factory=dict)
    node_attempts: dict[str, int] = field(default_factory=dict)
    prompt_version: str = ""
    code_version: str = ""
    _checkpoint_required_inputs: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)

    def report(self, value: float, desc: str) -> None:
        self.current_progress = max(0.0, min(float(value), 1.0))
        self.progress_message = desc
        if self.progress:
            self.progress(value, desc)

    def check_cancelled(self, node_name: str | None = None) -> None:
        if self.cancellation_event and self.cancellation_event.is_set():
            raise asyncio.CancelledError
        current = node_name or getattr(self, "current_node", "")
        if current:
            event = self.node_cancellation_events.get(current)
            if event and event.is_set():
                raise asyncio.CancelledError
            deadline = self.node_deadlines.get(current)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"workflow node {current} exceeded its timeout")
        if self.workflow_timeout_seconds and self.workflow_started_at:
            if time.monotonic() - self.workflow_started_at >= self.workflow_timeout_seconds:
                raise TimeoutError("workflow exceeded its total time budget")

    def cancel_node(self, node_name: str) -> None:
        event = self.node_cancellation_events.get(node_name)
        if event:
            event.set()

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


PaperContext = PaperWorkflowContext


__all__ = ["PaperContext", "PaperWorkflowContext", "ProgressCallback"]
