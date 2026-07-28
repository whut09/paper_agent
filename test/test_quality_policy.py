from types import SimpleNamespace

from paper_agent.evaluation.quality_policy import QualityDisposition, decide_quality


def _finding(reason_code: str, severity: str):
    return SimpleNamespace(reason_code=reason_code, severity=severity)


def test_quality_policy_auto_repairs_only_layout_findings():
    decision = decide_quality(
        [
            _finding("image_cropped", "block"),
            _finding("renderer_failed", "warning"),
        ]
    )

    assert decision.disposition == QualityDisposition.AUTO_REPAIR
    assert decision.repairable_reason_codes == ("image_cropped",)
    assert decision.warning_reason_codes == ("renderer_failed",)


def test_quality_policy_blocks_content_integrity_findings():
    decision = decide_quality(
        [
            _finding("page_overflow", "block"),
            _finding("missing_critical_asset", "block"),
        ]
    )

    assert decision.disposition == QualityDisposition.BLOCK
    assert decision.blocking_reason_codes == ("missing_critical_asset",)


def test_quality_policy_warns_for_renderer_infrastructure_failure():
    decision = decide_quality([_finding("renderer_unavailable", "warning")])

    assert decision.disposition == QualityDisposition.WARN
