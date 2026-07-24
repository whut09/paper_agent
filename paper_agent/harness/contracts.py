"""Audited workflow contracts for the compatibility workflow."""

from __future__ import annotations

import ast
import inspect
import textwrap

from paper_agent.schemas.contracts import WorkflowNodeContract, WorkflowNodeLike


NODE_CONTRACTS = {
    item.name: item
    for item in (
        WorkflowNodeContract(
            "PreparePaper",
            ("input_path", "output_dir"),
            ("output", "source_path", "pdf_path", "paper_name", "work_dir"),
            context_reads=("input_path", "output_dir"),
            context_writes=("legacy_asset_manifest", "legacy_summary", "output", "source_path", "pdf_path", "paper_name", "work_dir"),
        ),
        WorkflowNodeContract(
            "ParsePaper",
            ("pdf_path", "work_dir", "pages", "max_assets", "output", "paper_name"),
            ("paper_text", "assets", "asset_candidates", "asset_candidate_pools"),
            ("asset-candidates.json",),
            context_reads=("assets", "max_assets", "output", "pages", "paper_name", "pdf_path", "text", "work_dir"),
            context_writes=("asset_candidate_pools", "asset_candidates_path", "assets", "text"),
        ),
        WorkflowNodeContract(
            "ExtractSections",
            ("pdf_path", "pages", "paper_text", "assets", "paper_name"),
            (
                "paper_title",
                "abstract",
                "formulas",
                "grounding_map",
                "knowledge_graph",
                "correction_memories",
                "prompt_patches",
            ),
            context_reads=("correction_memories", "grounding_map", "pages", "paper_name", "paper_title", "pdf_path", "text"),
            context_writes=("abstract", "correction_memories", "formulas", "grounding_map", "knowledge_graph", "paper_title", "prompt_patches"),
        ),
        WorkflowNodeContract(
            "SummarizeContribution",
            (
                "paper_text",
                "assets",
                "abstract",
                "paper_title",
                "summary_language",
                "codex_envs",
                "correction_memories",
                "prompt_patches",
            ),
            ("config", "client", "chunk_notes", "partial_summaries"),
            ("chunk-notes.json", "partial-integrations.json"),
            independently_resumable=True,
            context_reads=("abstract", "assets", "client", "codex_envs", "config", "correction_memories", "output", "paper_name", "paper_title", "partial_summaries", "prompt_patches", "summary_language", "text"),
            context_writes=("chunk_notes", "client", "config"),
        ),
        WorkflowNodeContract(
            "ExtractMethods",
            (
                "chunk_notes",
                "partial_summaries",
                "assets",
                "abstract",
                "formulas",
                "paper_title",
                "summary_language",
                "correction_memories",
                "prompt_patches",
            ),
            ("draft_report",),
            context_reads=("abstract", "assets", "chunk_notes", "correction_memories", "formulas", "paper_title", "partial_summaries", "prompt_patches", "summary_language"),
            context_writes=("summary",),
        ),
        WorkflowNodeContract(
            "VerifyClaims",
            (
                "draft_report",
                "paper_text",
                "grounding_map",
                "abstract",
                "paper_title",
                "assets",
                "correction_memories",
                "prompt_patches",
            ),
            ("verification_report", "verified_report", "guard_results", "knowledge_graph"),
            ("verification.json", "grounding-map.json", "knowledge-graph.json"),
            context_reads=("abstract", "assets", "correction_memories", "grounding_map", "paper_title", "pdf_path", "prompt_patches", "repair_recheck_guards", "summary", "text"),
            context_writes=("guard_results", "knowledge_graph", "summary", "verification"),
        ),
        WorkflowNodeContract(
            "ReviseReport",
            (
                "verification_report",
                "draft_report",
                "assets",
                "revision_attempts",
                "repair_attempts",
                "repair_budget",
                "repair_cost_used",
            ),
            ("gate_decision", "verified_report", "repair_history"),
            ("verification-failed.md", "trace.json", "verification.json"),
            context_reads=("gate_history", "revision_attempts", "summary", "verification", "verification_failed_path"),
            context_writes=("gate_decision", "verification_failed_path"),
        ),
        WorkflowNodeContract(
            "GenerateReport",
            ("verified_report", "assets", "output", "source_path", "paper_name"),
            ("docx", "summary.md"),
            (
                "trace.json",
                "grounding-map.json",
                "grounding_map.json",
                "verification.json",
                "knowledge-graph.json",
                "knowledge_graph.json",
                "asset-candidates.json",
            ),
            context_reads=("assets", "docx_path", "output", "paper_name", "source_path", "summary", "summary_markdown_path"),
            context_writes=("asset_candidates_path", "docx_path", "grounding_map_path", "knowledge_graph_path", "summary", "summary_markdown_path", "trace_path", "verification_path"),
        ),
        WorkflowNodeContract(
            "RenderQA",
            ("docx", "assets", "output", "paper_name"),
            ("render_qa",),
            ("qa.json", "acceptance.json", "trace.json"),
            independently_resumable=True,
            context_reads=("assets", "docx_path", "output", "paper_name", "qa_path", "qa_result", "run_id", "work_dir"),
            context_writes=("current_reason_code", "download_ready", "qa_path", "qa_result"),
        ),
    )
}


def contract_for_node(node_or_name: WorkflowNodeLike | str) -> WorkflowNodeContract:
    name = node_or_name if isinstance(node_or_name, str) else node_or_name.name
    try:
        return NODE_CONTRACTS[name]
    except KeyError as exc:
        raise KeyError(f"No workflow I/O contract is registered for {name}.") from exc


def validate_declared_contract(node: WorkflowNodeLike) -> tuple[str, ...]:
    """Return declaration mismatches without executing a potentially costly node."""

    contract = contract_for_node(node)
    available_outputs = set(contract.outputs) | set(contract.sidecars)
    errors = []
    for name in node.requires:
        if name == "asset_manifest":
            name = "assets"
        if name not in contract.inputs:
            errors.append(f"{node.name} undeclared input: {name}")
    for name in node.produces:
        if name not in available_outputs and name not in {"assets", "asset_candidates"}:
            errors.append(f"{node.name} undeclared output: {name}")
    return tuple(errors)


def observed_context_accesses(node: WorkflowNodeLike) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Inspect direct context reads/writes in a node without running it."""

    tree = ast.parse(textwrap.dedent(inspect.getsource(node.run)))
    reads: set[str] = set()
    writes: set[str] = set()
    for item in ast.walk(tree):
        if not isinstance(item, ast.Attribute):
            continue
        if not isinstance(item.value, ast.Name) or item.value.id != "context":
            continue
        if item.attr in {"report", "check_cancelled"}:
            continue
        target = writes if isinstance(item.ctx, (ast.Store, ast.Del)) else reads
        target.add(item.attr)
    return tuple(sorted(reads)), tuple(sorted(writes))


def validate_observed_contract(node: WorkflowNodeLike) -> tuple[str, ...]:
    contract = contract_for_node(node)
    observed_reads, observed_writes = observed_context_accesses(node)
    errors = []
    if tuple(sorted(contract.context_reads)) != observed_reads:
        errors.append(
            f"{node.name} context reads differ: expected {contract.context_reads}, observed {observed_reads}"
        )
    if tuple(sorted(contract.context_writes)) != observed_writes:
        errors.append(
            f"{node.name} context writes differ: expected {contract.context_writes}, observed {observed_writes}"
        )
    return tuple(errors)


__all__ = [
    "NODE_CONTRACTS",
    "contract_for_node",
    "observed_context_accesses",
    "validate_declared_contract",
    "validate_observed_contract",
]
