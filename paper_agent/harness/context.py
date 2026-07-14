"""Workflow context for the PaperAgent harness."""

from __future__ import annotations

import asyncio
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
    agent_trace: list[dict[str, object]] = field(default_factory=list)
    node_results: dict[str, NodeResult] = field(default_factory=dict)
    docx_path: Path | None = None
    summary_markdown_path: Path | None = None
    verification_failed_path: Path | None = None
    trace_path: Path | None = None
    grounding_map_path: Path | None = None
    verification_path: Path | None = None
    knowledge_graph_path: Path | None = None

    def report(self, value: float, desc: str) -> None:
        if self.progress:
            self.progress(value, desc)

    def check_cancelled(self) -> None:
        if self.cancellation_event and self.cancellation_event.is_set():
            raise asyncio.CancelledError

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


PaperContext = PaperWorkflowContext


__all__ = ["PaperContext", "PaperWorkflowContext", "ProgressCallback"]
