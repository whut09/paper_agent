import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paper_agent.evaluation.acceptance import (
    MIGRATION_PHASES,
    build_acceptance_result,
    compare_asset_manifests,
    effective_repair_count,
    ineffective_repair_count,
    report_section_coverage,
    run_acceptance_suite,
    selected_manifest_from_sidecar,
    suggested_actions,
)
from paper_agent.schemas.findings import findings_from_verification_payload


FIXTURE_ROOT = Path("evaluation/migration_golden")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_every_migration_phase_has_a_golden_fixture_and_ordered_commit():
    assert [item[1] for item in MIGRATION_PHASES] == [
        "asset_candidates",
        "typed_findings",
        "repair_state_machine",
        "checkpoints",
        "render_qa",
    ]
    for _order, phase, commit, fixture in MIGRATION_PHASES:
        payload = json.loads(Path(fixture).read_text(encoding="utf-8"))
        assert payload["phase"] == phase
        assert len(commit) == 7


def test_asset_manifest_golden_compares_identity_and_geometry():
    payload = _fixture("01-asset-candidates.json")
    result = compare_asset_manifests(payload["legacy_manifest"], payload["current_manifest"])

    assert result.matched_count == payload["expected"]["matched_count"]
    assert len(result.changed) == payload["expected"]["changed_count"]
    assert result.match_rate == 1.0


def test_old_asset_candidate_sidecar_remains_readable_for_one_cycle():
    old_payload = {
        "run_id": "legacy",
        "paper_name": "paper",
        "pools": [
            {
                "evidence": {"page_number": 2, "caption_text": "Table 1. Results", "object_type": "table"},
                "selected_strategy": "detector_bbox",
                "candidates": [
                    {
                        "page_number": 2,
                        "caption": "Table 1. Results",
                        "object_type": "table",
                        "strategy": "detector_bbox",
                        "bbox": [1, 2, 3, 4],
                    }
                ],
            }
        ],
    }

    manifest = selected_manifest_from_sidecar(old_payload)

    assert manifest[0]["kind"] == "table"
    assert manifest[0]["caption"] == "Table 1. Results"


def test_typed_finding_golden_migrates_legacy_blocker_with_action():
    payload = _fixture("02-typed-findings.json")
    findings = findings_from_verification_payload(payload["legacy_verification"])

    assert findings[0].reason_code == payload["expected"]["reason_code"]
    assert payload["expected"]["suggested_action"] in findings[0].suggested_actions


def test_repair_golden_does_not_count_identical_signature_as_progress():
    payload = _fixture("03-repair-state-machine.json")
    history = payload["repair_history"]

    assert effective_repair_count(history) == payload["expected"]["effective_repair_count"]
    assert ineffective_repair_count(history) == payload["expected"]["ineffective_repair_count"]


def test_checkpoint_golden_retains_legacy_trace_fields():
    payload = _fixture("04-checkpoints.json")
    legacy = payload["legacy_trace"]
    current = payload["current_trace"]

    for field in payload["expected"]["legacy_fields_retained"]:
        assert field in legacy
        assert field in current


def test_render_qa_golden_has_executable_block_action():
    payload = _fixture("05-render-qa.json")
    finding = payload["qa"]["findings"][0]

    assert payload["expected"]["suggested_action"] in suggested_actions(finding["reason_code"])


def test_acceptance_result_blocks_duplicate_signature_and_reports_coverage():
    all_sections = "\n\n".join(f"## {name}\n内容" for name in (
        "核心信息", "摘要", "背景与问题", "创新点", "一句话总结",
        "方法主线", "关键结果", "深度分析", "局限", "总结",
    ))
    history = _fixture("03-repair-state-machine.json")["repair_history"]
    context = SimpleNamespace(
        paper_name="paper",
        source_path="paper.pdf",
        summary=all_sections,
        legacy_summary=all_sections,
        legacy_asset_manifest=[],
        assets=[],
        asset_candidate_pools=[],
        verification=None,
        qa_result=SimpleNamespace(status="pass", findings=[]),
        qa_path=Path("paper-qa.json"),
        repair_history=history,
        agent_trace=[],
        guard_results=[],
        workflow_started_at=None,
    )

    result = build_acceptance_result(context, model_call_count=7)

    assert result.status == "blocked"
    assert result.metrics.model_call_count == 7
    assert result.metrics.repair_count == 1
    assert result.metrics.ineffective_repair_count == 1
    assert result.section_coverage.score == 1.0
    assert result.blockers[0].reason_code == "duplicate_repair_signature"
    assert result.blockers[0].suggested_actions


def test_report_section_coverage_compares_legacy_and_current():
    result = report_section_coverage("## 摘要\nA\n## 总结\nB", "## 摘要\nA")

    assert result.score > result.legacy_score
    assert "创新点" in result.missing_sections


def test_representative_suite_accepts_pass_or_actionable_block_for_ten_papers(tmp_path):
    papers = []
    for index in range(10):
        name = f"paper-{index}"
        papers.append({"paper_name": name, "source": f"{name}.pdf"})
        (tmp_path / f"{name}-summary.docx").write_bytes(b"docx")
        (tmp_path / f"{name}-summary.md").write_text("## 摘要\n内容\n## 总结\n内容", encoding="utf-8")
        (tmp_path / f"{name}-trace.json").write_text(json.dumps({"nodes": []}), encoding="utf-8")
        (tmp_path / f"{name}-verification.json").write_text(
            json.dumps({"verification": {"passed": True, "hard_failures": [], "soft_warnings": []}}),
            encoding="utf-8",
        )
        qa = {"status": "pass", "findings": []}
        if index % 2:
            qa = {
                "status": "block",
                "findings": [{"reason_code": "image_cropped", "severity": "block", "message": "cropped"}],
            }
        (tmp_path / f"{name}-qa.json").write_text(json.dumps(qa), encoding="utf-8")
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps({"name": "ten", "papers": papers}), encoding="utf-8")

    report = run_acceptance_suite(suite_path, tmp_path, tmp_path / "acceptance.json")

    assert report["paper_count"] == 10
    assert report["passed_count"] == 5
    assert report["blocked_count"] == 5
    assert report["meets_exit_criteria"] is True
    for paper in report["papers"]:
        if paper["status"] == "blocked":
            assert all(item["reason_code"] and item["suggested_actions"] for item in paper["blockers"])


def test_acceptance_blocker_rejects_missing_next_action():
    from paper_agent.schemas.acceptance import AcceptanceBlocker

    with pytest.raises(ValueError):
        AcceptanceBlocker("image_cropped", "cropped", ())


def test_render_qa_warning_is_actionable_block_for_migration_acceptance():
    context = SimpleNamespace(
        paper_name="paper",
        source_path="paper.pdf",
        summary="",
        legacy_summary="",
        legacy_asset_manifest=[],
        assets=[],
        asset_candidate_pools=[],
        verification=None,
        qa_result=SimpleNamespace(
            status="warning",
            findings=[
                SimpleNamespace(
                    severity="warning",
                    reason_code="renderer_failed",
                    message="Word COM unavailable",
                    asset_id=None,
                    suggested_actions=(),
                )
            ],
        ),
        qa_path=Path("paper-qa.json"),
        repair_history=[],
        agent_trace=[],
        guard_results=[],
        workflow_started_at=None,
    )

    result = build_acceptance_result(context)

    assert result.status == "blocked"
    assert result.meets_exit_criteria
    assert result.blockers[0].reason_code == "renderer_failed"
    assert "rerun_render_qa" in result.blockers[0].suggested_actions
