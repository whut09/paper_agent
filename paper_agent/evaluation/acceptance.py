"""Migration acceptance records and representative-paper suite runner."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from paper_agent.schemas.acceptance import (
    AcceptanceBlocker,
    AcceptanceMetrics,
    ManifestComparison,
    MigrationAcceptanceResult,
    SectionCoverageComparison,
)
from paper_agent.schemas.findings import default_actions, finding_from_legacy, findings_from_verification_payload


REQUIRED_REPORT_SECTIONS = (
    "核心信息",
    "摘要",
    "背景与问题",
    "创新点",
    "一句话总结",
    "方法主线",
    "关键结果",
    "深度分析",
    "局限",
    "总结",
)

MIGRATION_PHASES = (
    (1, "asset_candidates", "4fdc995", "evaluation/migration_golden/01-asset-candidates.json"),
    (2, "typed_findings", "a50a30e", "evaluation/migration_golden/02-typed-findings.json"),
    (3, "repair_state_machine", "1c0530d", "evaluation/migration_golden/03-repair-state-machine.json"),
    (4, "checkpoints", "f37756d", "evaluation/migration_golden/04-checkpoints.json"),
    (5, "render_qa", "95e8eba", "evaluation/migration_golden/05-render-qa.json"),
)

_ACTION_OVERRIDES = {
    "caption_not_adjacent": ("move_caption_next_to_asset", "regenerate_docx"),
    "duplicate_repair_signature": ("select_alternate_candidate", "stop_repeating_identical_geometry"),
    "invalid_docx": ("regenerate_docx", "inspect_generate_report_trace"),
    "empty_render": ("regenerate_docx", "inspect_renderer_output"),
    "image_cropped": ("resize_asset_to_content_area", "regenerate_docx"),
    "image_too_small": ("recapture_asset_at_higher_resolution", "regenerate_docx"),
    "missing_critical_asset": ("capture_missing_asset", "rewrite_asset_reference"),
    "qa_not_recorded": ("rerun_with_render_qa",),
    "renderer_failed": ("repair_or_install_docx_renderer", "rerun_render_qa"),
    "renderer_timeout": ("increase_render_qa_timeout", "rerun_render_qa"),
    "renderer_unavailable": ("install_libreoffice_or_enable_word_com", "rerun_render_qa"),
    "rendered_pdf_invalid": ("retry_docx_renderer", "inspect_rendered_pdf"),
    "page_overflow": ("reduce_asset_size_or_repaginate", "regenerate_docx"),
    "unresolved_asset_marker": ("rewrite_asset_marker", "regenerate_docx"),
    "workflow_incomplete": ("resume_from_last_checkpoint", "inspect_trace_json"),
    "workflow_stage_failed": ("retry_failed_stage", "inspect_trace_json"),
}

_OBSERVATIONAL_ACTIONS = {
    "score_candidates",
    "visual_arbitration",
    "retry_verifier",
    "use_deterministic_checks",
}


def migration_phase_records() -> list[dict[str, Any]]:
    regression_tests = {
        "asset_candidates": "test/test_asset_candidates.py",
        "typed_findings": "test/test_findings.py",
        "repair_state_machine": "test/test_repair_state_machine.py",
        "checkpoints": "test/test_workflow_checkpoints.py",
        "render_qa": "test/test_render_qa.py",
    }
    return [
        {
            "order": order,
            "name": name,
            "commit": commit,
            "golden_fixture": fixture,
            "regression_test": regression_tests[name],
            "full_test_command": "python -m pytest -q",
            "facade": "paper_agent.harness.workflow.summarize_paper",
        }
        for order, name, commit, fixture in MIGRATION_PHASES
    ]


def suggested_actions(reason_code: str) -> tuple[str, ...]:
    actions = _ACTION_OVERRIDES.get(reason_code) or default_actions(reason_code)
    return tuple(action for action in actions if action) or ("inspect_finding",)


def _normalized_caption(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _manifest_key(item: dict[str, Any]) -> str:
    return "|".join(
        (
            str(item.get("kind") or item.get("object_type") or "").lower(),
            str(int(item.get("page_number") or item.get("page") or 0)),
            _normalized_caption(item.get("caption")),
        )
    )


def legacy_manifest_from_assets(assets: Iterable[Any]) -> list[dict[str, Any]]:
    manifest = []
    for asset_id, asset in enumerate(assets, 1):
        rect = getattr(asset, "rect", None)
        bbox = [round(float(value), 3) for value in rect] if rect is not None else []
        manifest.append(
            {
                "asset_id": asset_id,
                "kind": str(getattr(asset, "kind", "")),
                "page_number": int(getattr(asset, "page_number", 0) or 0),
                "caption": str(getattr(asset, "caption", "")),
                "bbox": bbox,
                "image_path": str(getattr(asset, "path", "")),
            }
        )
    return manifest


def selected_manifest_from_pools(pools: Iterable[Any]) -> list[dict[str, Any]]:
    manifest = []
    for asset_id, pool in enumerate(pools, 1):
        selected = pool.selected
        manifest.append(
            {
                "asset_id": asset_id,
                "kind": str(selected.evidence.object_type),
                "page_number": int(selected.evidence.page_number),
                "caption": str(selected.evidence.caption_text),
                "bbox": [round(float(value), 3) for value in selected.bbox],
                "image_path": str(selected.image_path) if selected.image_path else "",
                "strategy": selected.strategy.value,
                "score": selected.score.total,
            }
        )
    return manifest


def selected_manifest_from_sidecar(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("selected_manifest"), list):
        return [dict(item) for item in payload["selected_manifest"] if isinstance(item, dict)]
    result = []
    for asset_id, pool in enumerate(payload.get("pools") or [], 1):
        if not isinstance(pool, dict):
            continue
        evidence = pool.get("evidence") if isinstance(pool.get("evidence"), dict) else {}
        selected_strategy = str(pool.get("selected_strategy") or "")
        candidates = [item for item in pool.get("candidates") or [] if isinstance(item, dict)]
        selected = next((item for item in candidates if item.get("strategy") == selected_strategy), candidates[0] if candidates else {})
        result.append(
            {
                "asset_id": asset_id,
                "kind": str(evidence.get("object_type") or selected.get("object_type") or ""),
                "page_number": int(evidence.get("page_number") or selected.get("page_number") or 0),
                "caption": str(evidence.get("caption_text") or selected.get("caption") or ""),
                "bbox": list(selected.get("bbox") or evidence.get("source_bbox") or []),
                "image_path": str(selected.get("image_path") or ""),
                "strategy": selected_strategy,
            }
        )
    return result


def compare_asset_manifests(
    legacy: Iterable[dict[str, Any]],
    current: Iterable[dict[str, Any]],
    *,
    legacy_available: bool = True,
) -> ManifestComparison:
    old = list(legacy)
    new = list(current)
    old_by_key = {_manifest_key(item): item for item in old}
    new_by_key = {_manifest_key(item): item for item in new}
    common = sorted(old_by_key.keys() & new_by_key.keys())
    changed = []
    for key in common:
        old_bbox = tuple(round(float(value), 2) for value in old_by_key[key].get("bbox") or ())
        new_bbox = tuple(round(float(value), 2) for value in new_by_key[key].get("bbox") or ())
        if old_bbox and new_bbox and old_bbox != new_bbox:
            changed.append(key)
    return ManifestComparison(
        legacy_count=len(old),
        current_count=len(new),
        matched_count=len(common),
        added=tuple(sorted(new_by_key.keys() - old_by_key.keys())),
        removed=tuple(sorted(old_by_key.keys() - new_by_key.keys())),
        changed=tuple(changed),
        legacy_available=legacy_available,
    )


def report_section_coverage(summary: str, legacy_summary: str = "") -> SectionCoverageComparison:
    headings = tuple(dict.fromkeys(re.findall(r"(?m)^##\s+([^\r\n#]+)", summary or "")))
    legacy_headings = tuple(dict.fromkeys(re.findall(r"(?m)^##\s+([^\r\n#]+)", legacy_summary or "")))

    def present(required: str, values: tuple[str, ...]) -> bool:
        return any(required == value.strip() or required in value for value in values)

    missing = tuple(required for required in REQUIRED_REPORT_SECTIONS if not present(required, headings))
    score = (len(REQUIRED_REPORT_SECTIONS) - len(missing)) / len(REQUIRED_REPORT_SECTIONS)
    legacy_score = None
    if legacy_summary:
        legacy_missing = sum(not present(required, legacy_headings) for required in REQUIRED_REPORT_SECTIONS)
        legacy_score = (len(REQUIRED_REPORT_SECTIONS) - legacy_missing) / len(REQUIRED_REPORT_SECTIONS)
    return SectionCoverageComparison(
        REQUIRED_REPORT_SECTIONS,
        headings,
        missing,
        round(score, 6),
        legacy_headings,
        round(legacy_score, 6) if legacy_score is not None else None,
    )


def ineffective_repair_count(repair_history: Iterable[dict[str, Any]]) -> int:
    count = 0
    for attempt in repair_history:
        transitions = attempt.get("transitions") if isinstance(attempt, dict) else []
        for transition in transitions or []:
            if not isinstance(transition, dict):
                continue
            unchanged = transition.get("before_signature") == transition.get("after_signature")
            if unchanged and str(transition.get("action") or "") not in _OBSERVATIONAL_ACTIONS:
                count += 1
    return count


def effective_repair_count(repair_history: Iterable[dict[str, Any]]) -> int:
    count = 0
    for attempt in repair_history:
        transitions = attempt.get("transitions") if isinstance(attempt, dict) else []
        count += sum(bool(item.get("changed")) for item in transitions or [] if isinstance(item, dict))
    return count


def _blocker(reason_code: str, message: str, *, stage: str = "acceptance", asset_id: int | None = None, actions: Iterable[str] = ()) -> AcceptanceBlocker:
    resolved = tuple(str(item) for item in actions if str(item)) or suggested_actions(reason_code)
    return AcceptanceBlocker(reason_code, message or reason_code, resolved, stage, asset_id)


def _verification_blockers(verification: Any) -> list[AcceptanceBlocker]:
    if verification is None:
        return []
    result = []
    for finding in getattr(verification, "findings", ()) or ():
        if str(getattr(finding, "severity", "")) != "error":
            continue
        result.append(
            _blocker(
                str(getattr(finding, "reason_code", "legacy_error")),
                str(getattr(finding, "human_message", "")),
                stage=str(getattr(finding, "stage", "verifier")),
                asset_id=getattr(finding, "asset_id", None),
                actions=getattr(finding, "suggested_actions", ()),
            )
        )
    if result:
        return result
    for failure in getattr(verification, "hard_failures", ()) or ():
        finding = finding_from_legacy(failure, stage="verifier", severity="error", confidence=0.95)
        result.append(_blocker(finding.reason_code, finding.human_message, stage=finding.stage, asset_id=finding.asset_id, actions=finding.suggested_actions))
    return result


def build_acceptance_result(context: Any, *, model_call_count: int = 0) -> MigrationAcceptanceResult:
    legacy_manifest = list(getattr(context, "legacy_asset_manifest", ()) or ())
    current_manifest = selected_manifest_from_pools(getattr(context, "asset_candidate_pools", ()) or ())
    if not legacy_manifest:
        legacy_manifest = legacy_manifest_from_assets(getattr(context, "assets", ()) or ())
    manifest = compare_asset_manifests(legacy_manifest, current_manifest, legacy_available=bool(legacy_manifest))
    coverage = report_section_coverage(str(getattr(context, "summary", "") or ""), str(getattr(context, "legacy_summary", "") or ""))
    verification = getattr(context, "verification", None)
    qa_result = getattr(context, "qa_result", None)
    repair_history = list(getattr(context, "repair_history", ()) or ())
    blockers = _verification_blockers(verification)
    ineffective = ineffective_repair_count(repair_history)
    if ineffective:
        blockers.append(_blocker("duplicate_repair_signature", f"{ineffective} repair action(s) reused the same geometry/content signature", stage="repair"))
    if qa_result is not None:
        for finding in getattr(qa_result, "findings", ()) or ():
            severity = str(getattr(finding, "severity", ""))
            if severity != "block" and str(getattr(qa_result, "status", "")) != "warning":
                continue
            blockers.append(
                _blocker(
                    str(getattr(finding, "reason_code", "render_qa_failed")),
                    str(getattr(finding, "message", "")),
                    stage="render_qa",
                    asset_id=getattr(finding, "asset_id", None),
                    actions=getattr(finding, "suggested_actions", ()),
                )
            )
    failed_nodes = [item for item in getattr(context, "agent_trace", ()) or () if item.get("status") == "failed"]
    if failed_nodes and not blockers:
        node = failed_nodes[-1]
        blockers.append(_blocker("workflow_stage_failed", "; ".join(node.get("errors") or ()) or f"{node.get('node')} failed", stage=str(node.get("node") or "workflow")))

    qa_status = str(getattr(qa_result, "status", "pending") or "pending")
    if blockers:
        status = "blocked"
    elif qa_status == "pass":
        status = "passed"
    elif qa_status == "warning":
        status = "blocked"
        blockers.append(_blocker("renderer_failed", "RenderQA did not complete certification.", stage="render_qa"))
    else:
        status = "running"
    warnings = []
    if verification is not None:
        warnings.extend(str(item.get("reason") or item.get("message") or "") for item in getattr(verification, "soft_warnings", ()) or ())
    if qa_result is not None:
        warnings.extend(str(item.message) for item in getattr(qa_result, "findings", ()) if str(getattr(item, "severity", "")) == "warning")
    elapsed = 0.0
    started = getattr(context, "workflow_started_at", None)
    if started:
        elapsed = max(0.0, time.monotonic() - float(started))
    hard_failure_count = len(getattr(verification, "hard_failures", ()) or ()) if verification is not None else 0
    guard_warning_count = sum(len(getattr(item, "warnings", ()) or ()) for item in getattr(context, "guard_results", ()) or ())
    metrics = AcceptanceMetrics(
        elapsed_seconds=round(elapsed, 4),
        model_call_count=int(model_call_count),
        model_call_count_source="instrumented_api_attempts",
        repair_count=effective_repair_count(repair_history),
        ineffective_repair_count=ineffective,
        hard_failure_count=hard_failure_count,
        warning_count=len([item for item in warnings if item]) + guard_warning_count,
        final_qa=qa_status,
    )
    return MigrationAcceptanceResult(
        status,
        str(getattr(context, "paper_name", "")),
        str(getattr(context, "source_path", "") or getattr(context, "input_path", "")),
        metrics,
        manifest,
        coverage,
        blockers,
        [item for item in warnings if item],
        migration_phase_records(),
        {
            "verification_legacy_fields_retained": ["hard_failures", "soft_warnings", "patch_suggestions", "errors"],
            "asset_candidate_legacy_fields_retained": ["run_id", "paper_name", "pools"],
            "compatibility_window": "one migration cycle",
        },
        getattr(context, "qa_path", None),
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def write_acceptance_sidecar(path: Path, result: MigrationAcceptanceResult) -> Path:
    return _atomic_write_json(path, result.to_dict())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def audit_existing_run(artifacts_dir: str | Path, paper_name: str, source_path: str = "") -> MigrationAcceptanceResult:
    root = Path(artifacts_dir)
    trace_path = root / f"{paper_name}-trace.json"
    verification_path = root / f"{paper_name}-verification.json"
    summary_path = root / f"{paper_name}-summary.md"
    qa_path = root / f"{paper_name}-qa.json"
    docx_path = root / f"{paper_name}-summary.docx"
    candidate_path = root / f"{paper_name}-asset-candidates.json"
    trace = _read_json(trace_path)
    verification_payload = _read_json(verification_path)
    nested_verification = verification_payload.get("verification") if isinstance(verification_payload.get("verification"), dict) else verification_payload
    findings = findings_from_verification_payload(nested_verification)
    verification = SimpleNamespace(
        findings=findings,
        hard_failures=list(nested_verification.get("hard_failures") or []),
        soft_warnings=list(nested_verification.get("soft_warnings") or []),
    )
    qa_payload = _read_json(qa_path)
    qa_findings = [
        SimpleNamespace(
            reason_code=str(item.get("reason_code") or "render_qa_failed"),
            severity=str(item.get("severity") or "warning"),
            message=str(item.get("message") or ""),
            asset_id=item.get("asset_id"),
            suggested_actions=tuple(item.get("suggested_actions") or ()),
        )
        for item in qa_payload.get("findings") or []
        if isinstance(item, dict)
    ]
    qa_result = SimpleNamespace(status=str(qa_payload.get("status") or "pending"), findings=qa_findings) if qa_payload else None
    candidate_payload = _read_json(candidate_path)
    selected_manifest = selected_manifest_from_sidecar(candidate_payload)
    summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    context = SimpleNamespace(
        paper_name=paper_name,
        source_path=source_path or str(trace.get("source_path") or ""),
        input_path=source_path,
        summary=summary,
        legacy_summary="",
        legacy_asset_manifest=selected_manifest,
        asset_candidate_pools=[],
        assets=[],
        verification=verification,
        qa_result=qa_result,
        qa_path=qa_path if qa_path.exists() else None,
        repair_history=list(trace.get("repair_history") or verification_payload.get("repair_history") or []),
        agent_trace=list(trace.get("nodes") or []),
        guard_results=[],
        workflow_started_at=None,
    )
    recorded_model_calls = trace.get("model_call_count")
    estimated_model_calls = sum(bool(item.get("llm_required")) for item in trace.get("nodes") or [])
    result = build_acceptance_result(
        context,
        model_call_count=int(recorded_model_calls) if recorded_model_calls is not None else estimated_model_calls,
    )
    result.metrics = AcceptanceMetrics(
        elapsed_seconds=round(
            sum(float((item.get("metrics") or {}).get("elapsed_seconds") or 0.0) for item in trace.get("nodes") or []),
            4,
        ),
        model_call_count=result.metrics.model_call_count,
        model_call_count_source="instrumented_api_attempts" if recorded_model_calls is not None else "legacy_llm_node_estimate",
        repair_count=result.metrics.repair_count,
        ineffective_repair_count=result.metrics.ineffective_repair_count,
        hard_failure_count=result.metrics.hard_failure_count,
        warning_count=result.metrics.warning_count,
        final_qa=result.metrics.final_qa,
    )
    result.manifest_comparison = compare_asset_manifests(selected_manifest, selected_manifest, legacy_available=bool(candidate_payload))
    if not qa_payload:
        result.blockers.append(_blocker("qa_not_recorded", "This historical run predates RenderQA and cannot be certified."))
        result.status = "blocked"
    elif qa_result and qa_result.status == "warning":
        for finding in qa_findings or [SimpleNamespace(reason_code="renderer_failed", message="RenderQA warning", suggested_actions=())]:
            if not any(item.reason_code == finding.reason_code for item in result.blockers):
                result.blockers.append(_blocker(finding.reason_code, finding.message, stage="render_qa", actions=finding.suggested_actions))
        result.status = "blocked"
    if not docx_path.exists() and not result.blockers:
        result.blockers.append(_blocker("workflow_incomplete", "No generated DOCX is available for this run."))
        result.status = "blocked"
    return result


def run_acceptance_suite(
    suite_path: str | Path,
    artifacts_dir: str | Path,
    output_path: str | Path,
    *,
    execute: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    suite = _read_json(Path(suite_path))
    papers = [item for item in suite.get("papers") or [] if isinstance(item, dict)]
    if limit is not None:
        papers = papers[: max(0, limit)]
    results = []
    for paper in papers:
        source = str(paper.get("source") or "")
        paper_name = str(paper.get("paper_name") or Path(source).stem)
        if execute:
            from paper_agent.config import ConfigManager
            from paper_agent.harness.workflow import summarize_paper

            try:
                summarize_paper(
                    source,
                    artifacts_dir,
                    codex_envs={
                        "CODEX_BASE_URL": str(ConfigManager.get("CODEX_BASE_URL", "")),
                        "CODEX_API_KEY": str(ConfigManager.get("CODEX_API_KEY", "")),
                        "CODEX_MODEL": str(ConfigManager.get("CODEX_MODEL", "")),
                        "CODEX_USE_PROXY": str(ConfigManager.get("CODEX_USE_PROXY", "")),
                        "CODEX_PROXY": str(ConfigManager.get("CODEX_PROXY", "")),
                    },
                )
            except RuntimeError:
                # A typed blocked report is an expected acceptance outcome.
                pass
            result_payload = audit_existing_run(artifacts_dir, paper_name, source).to_dict()
        else:
            result_payload = audit_existing_run(artifacts_dir, paper_name, source).to_dict()
        results.append(result_payload)
        if result_payload.get("status") == "blocked":
            blocked_path = Path(output_path).parent / f"{paper_name}-acceptance-blocked.json"
            _atomic_write_json(blocked_path, result_payload)
    report = {
        "schema_version": 1,
        "suite": str(suite.get("name") or Path(suite_path).stem),
        "paper_count": len(results),
        "passed_count": sum(item.get("status") == "passed" for item in results),
        "blocked_count": sum(item.get("status") == "blocked" for item in results),
        "warning_count": sum(item.get("status") == "warning" for item in results),
        "meets_exit_criteria": len(results) >= 10 and all(bool(item.get("meets_exit_criteria")) for item in results),
        "papers": results,
    }
    _atomic_write_json(Path(output_path), report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit or execute the PaperAgent migration acceptance suite.")
    parser.add_argument("--suite", default="evaluation/representative_papers.json")
    parser.add_argument("--artifacts", default="paper_agent_files")
    parser.add_argument("--output", default="paper_agent_files/migration-acceptance.json")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    report = run_acceptance_suite(args.suite, args.artifacts, args.output, execute=args.execute, limit=args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["papers"] and all(item.get("meets_exit_criteria") for item in report["papers"]) else 2


__all__ = [
    "MIGRATION_PHASES",
    "REQUIRED_REPORT_SECTIONS",
    "audit_existing_run",
    "build_acceptance_result",
    "compare_asset_manifests",
    "effective_repair_count",
    "ineffective_repair_count",
    "legacy_manifest_from_assets",
    "main",
    "migration_phase_records",
    "report_section_coverage",
    "run_acceptance_suite",
    "selected_manifest_from_pools",
    "selected_manifest_from_sidecar",
    "suggested_actions",
    "write_acceptance_sidecar",
]
