from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from paper_agent.paper_summary import (
    PaperAsset,
    _local_visual_asset_issues,
    _parse_verification_result,
    _parse_visual_asset_guard_response,
    _visual_asset_guard,
)
from paper_agent.schemas.findings import (
    Finding,
    FindingReasonCode,
    aggregate_findings,
    finding_from_legacy,
    migrate_verification_payload,
)


def make_finding(*, severity="error", confidence=0.6, provenance=("local:geometry",), **kwargs):
    return Finding.create(
        stage="local_guard",
        severity=severity,
        confidence=confidence,
        asset_id=4,
        evidence_refs=("page:7",),
        suggested_actions=("recapture_asset",),
        provenance=provenance,
        reason_code="table_truncated",
        human_message="asset 4 table crop is truncated",
        **kwargs,
    )


def test_finding_contains_required_typed_fields_and_legacy_message():
    finding = make_finding()
    payload = finding.to_dict()
    assert finding.finding_id.startswith("finding-")
    assert payload["reason_code"] == FindingReasonCode.TABLE_TRUNCATED.value
    assert payload["asset_id"] == 4
    assert payload["evidence_refs"] == ["page:7"]
    assert payload["suggested_actions"] == ["recapture_asset"]
    assert finding.legacy_message() == "asset 4 table crop is truncated"


def test_low_confidence_local_finding_is_warning():
    result = aggregate_findings([make_finding()])
    assert result[0].severity == "warning"
    assert result[0].confidence == 0.6


def test_two_independent_signals_upgrade_low_confidence_finding_to_error():
    result = aggregate_findings(
        [
            make_finding(provenance=("local:geometry",)),
            make_finding(provenance=("vision_model",)),
        ]
    )
    assert result[0].severity == "error"
    assert set(result[0].provenance) == {"local:geometry", "vision_model"}
    assert result[0].confidence > 0.8


def test_same_signal_does_not_confirm_low_confidence_finding():
    result = aggregate_findings(
        [
            make_finding(provenance=("local:geometry",)),
            make_finding(provenance=("local:geometry",)),
        ]
    )
    assert result[0].severity == "warning"


def test_transport_and_invalid_json_never_become_blocking_content_findings():
    transport = Finding.create(
        stage="transport",
        severity="error",
        confidence=1.0,
        reason_code="verifier_transport_failure",
        human_message="Verifier network timeout",
        provenance=("verifier:transport",),
    )
    invalid = Finding.create(
        stage="verifier",
        severity="error",
        confidence=1.0,
        reason_code="verifier_invalid_json",
        human_message="Verifier output is not JSON",
        provenance=("verifier:parser",),
    )
    result = aggregate_findings([transport, invalid])
    assert {item.severity for item in result} == {"warning"}
    assert {item.reason_code for item in result} == {
        "verifier_transport_failure",
        "verifier_invalid_json",
    }


def test_old_verification_payload_is_migrated_without_losing_legacy_fields():
    old = {
        "passed": False,
        "hard_failures": [{"type": "unsupported_core_claim", "claim": "c", "reason": "not grounded"}],
        "soft_warnings": [{"type": "weak_evidence", "claim": "w", "reason": "narrow evidence"}],
    }
    migrated = migrate_verification_payload(old)
    assert migrated["finding_schema_version"] == 1
    assert len(migrated["findings"]) == 2
    assert migrated["hard_failures"] == old["hard_failures"]

    wrapped = migrate_verification_payload({"verification": old, "run_id": "run-1"})
    assert wrapped["verification"]["findings"]
    assert wrapped["run_id"] == "run-1"


def test_legacy_verification_parser_adds_findings_and_keeps_strings():
    result = _parse_verification_result(
        '{"passed": false, "hard_failures": [{"type": "unsupported_core_claim", "claim": "c", "reason": "not grounded"}]}'
    )
    assert result.hard_failures[0]["type"] == "unsupported_core_claim"
    assert result.findings[0].reason_code == "unsupported_core_claim"
    assert "not grounded" in result.errors

    invalid = _parse_verification_result("not json")
    assert invalid.findings[0].reason_code == "verifier_invalid_json"
    assert invalid.findings[0].severity == "warning"


def test_local_visual_issue_has_confidence_provenance_and_reason_code():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "table.png"
        Image.new("RGB", (1200, 300), "white").save(path)
        asset = PaperAsset("table", 7, path, "Table 3. Results", "Header | Metric")
        issues = _local_visual_asset_issues(1, asset)
    issue = next(item for item in issues if "caption/header" in item["message"])
    assert issue["reason_code"] == "table_body_missing"
    assert 0.0 <= issue["confidence"] <= 1.0
    assert issue["provenance"] == "local:visual-heuristic"


def test_visual_transport_failure_is_not_a_content_error():
    class FailingCompletions:
        def create(self, **_kwargs):
            raise RuntimeError("network timeout while calling verifier")

    class Client:
        class Chat:
            completions = FailingCompletions()

        chat = Chat()

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "table.png"
        Image.new("RGB", (800, 400), "white").save(path)
        result = _visual_asset_guard(
            "[[ASSET:1]]",
            [PaperAsset("table", 1, path, "Table 1. Results", "A 1 2\nB 3 4")],
            Client(),
            "vision-model",
        )
    assert result.errors == []
    assert result.status == "warning"
    assert result.findings[0].reason_code == "verifier_transport_failure"


def test_visual_guard_response_migrates_issue_reason_code():
    response = _parse_visual_asset_guard_response(
        '{"passed": false, "issues": [{"severity": "error", "type": "mixed_figure_table", "reason": "two objects"}]}'
    )
    assert response["issues"][0]["reason_code"] == "mixed_objects"
    assert response["issues"][0]["provenance"] == "vision_model"
