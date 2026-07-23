"""Typed verification findings with legacy-string compatibility."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class FindingSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class FindingStage(str, Enum):
    LOCAL_GUARD = "local_guard"
    VISUAL_GUARD = "visual_guard"
    VERIFIER = "verifier"
    TRANSPORT = "transport"
    REPAIR = "repair"


class FindingReasonCode(str, Enum):
    TABLE_BODY_MISSING = "table_body_missing"
    TABLE_TRUNCATED = "table_truncated"
    CAPTION_TRUNCATED = "caption_truncated"
    MIXED_OBJECTS = "mixed_objects"
    TYPE_MISMATCH = "type_mismatch"
    FORMULA_CONTAMINATION = "formula_contamination"
    MISSING_CRITICAL_ASSET = "missing_critical_asset"
    VERIFIER_TRANSPORT_FAILURE = "verifier_transport_failure"
    VERIFIER_INVALID_JSON = "verifier_invalid_json"
    LEGACY_ERROR = "legacy_error"
    VISUAL_CROP_INVALID = "visual_crop_invalid"
    MISSING_ASSET_FILE = "missing_asset_file"
    UNSUPPORTED_CORE_CLAIM = "unsupported_core_claim"
    WEAK_EVIDENCE = "weak_evidence"
    VERIFIER_FAILED_WITHOUT_REASON = "verifier_failed_without_reason"


KNOWN_REASON_CODES = frozenset(item.value for item in FindingReasonCode)
_TRANSPORT_CODES = {
    FindingReasonCode.VERIFIER_TRANSPORT_FAILURE.value,
    FindingReasonCode.VERIFIER_INVALID_JSON.value,
}


@dataclass(frozen=True)
class Finding:
    finding_id: str
    stage: str
    severity: str
    confidence: float
    asset_id: int | None
    claim_id: str | None
    evidence_refs: tuple[str, ...]
    reason_code: str
    human_message: str
    suggested_actions: tuple[str, ...]
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        severity = self.severity.value if isinstance(self.severity, FindingSeverity) else str(self.severity).lower()
        if severity not in {item.value for item in FindingSeverity}:
            severity = FindingSeverity.WARNING.value
        reason_code = normalize_reason_code(self.reason_code)
        confidence = max(0.0, min(1.0, float(self.confidence)))
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "evidence_refs", tuple(str(item) for item in self.evidence_refs))
        object.__setattr__(self, "suggested_actions", tuple(str(item) for item in self.suggested_actions))
        object.__setattr__(self, "provenance", tuple(str(item) for item in self.provenance if str(item).strip()))

    @classmethod
    def create(
        cls,
        *,
        stage: str,
        severity: str,
        confidence: float,
        reason_code: str,
        human_message: str,
        asset_id: int | None = None,
        claim_id: str | None = None,
        evidence_refs: Iterable[str] = (),
        suggested_actions: Iterable[str] = (),
        provenance: Iterable[str] = (),
    ) -> "Finding":
        normalized_reason = normalize_reason_code(reason_code)
        refs = tuple(str(item) for item in evidence_refs)
        actions = tuple(str(item) for item in suggested_actions) or default_actions(normalized_reason)
        provenance_tuple = tuple(str(item) for item in provenance if str(item).strip())
        identity = json.dumps(
            {
                "stage": stage,
                "severity": severity,
                "asset_id": asset_id,
                "claim_id": claim_id,
                "evidence_refs": refs,
                "reason_code": normalized_reason,
                "human_message": human_message,
                "suggested_actions": actions,
                "provenance": provenance_tuple,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        finding_id = "finding-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return cls(
            finding_id=finding_id,
            stage=stage,
            severity=severity,
            confidence=confidence,
            asset_id=asset_id,
            claim_id=claim_id,
            evidence_refs=refs,
            reason_code=normalized_reason,
            human_message=str(human_message).strip(),
            suggested_actions=actions,
            provenance=provenance_tuple,
        )

    @property
    def is_transport(self) -> bool:
        return self.reason_code in _TRANSPORT_CODES

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "stage": self.stage,
            "severity": self.severity,
            "confidence": self.confidence,
            "asset_id": self.asset_id,
            "claim_id": self.claim_id,
            "evidence_refs": list(self.evidence_refs),
            "reason_code": self.reason_code,
            "human_message": self.human_message,
            "suggested_actions": list(self.suggested_actions),
            "provenance": list(self.provenance),
        }

    def legacy_message(self) -> str:
        return self.human_message


def normalize_reason_code(value: str) -> str:
    if isinstance(value, Enum):
        value = value.value
    lowered = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    aliases = {
        "missing_table_body": FindingReasonCode.TABLE_BODY_MISSING.value,
        "truncated_table": FindingReasonCode.TABLE_TRUNCATED.value,
        "table_cropped": FindingReasonCode.TABLE_TRUNCATED.value,
        "content_cropped": FindingReasonCode.TABLE_TRUNCATED.value,
        "caption_cropped": FindingReasonCode.CAPTION_TRUNCATED.value,
        "mixed_figure_table": FindingReasonCode.MIXED_OBJECTS.value,
        "declared_type_mismatch": FindingReasonCode.TYPE_MISMATCH.value,
        "type_mismatch": FindingReasonCode.TYPE_MISMATCH.value,
        "invalid_visual_guard_json": FindingReasonCode.VERIFIER_INVALID_JSON.value,
        "verifier_timeout_warning": FindingReasonCode.VERIFIER_TRANSPORT_FAILURE.value,
        "api_connection_error": FindingReasonCode.VERIFIER_TRANSPORT_FAILURE.value,
        "timeout": FindingReasonCode.VERIFIER_TRANSPORT_FAILURE.value,
    }
    return aliases.get(lowered, lowered if lowered in KNOWN_REASON_CODES else FindingReasonCode.LEGACY_ERROR.value)


def infer_reason_code(value: str, message: str = "") -> str:
    normalized = normalize_reason_code(value)
    if normalized != FindingReasonCode.LEGACY_ERROR.value:
        return normalized
    combined = f"{value} {message}".lower()
    patterns = (
        (r"missing_table_body|table body.*missing|caption/header|lacks numeric body", FindingReasonCode.TABLE_BODY_MISSING.value),
        (r"truncated_table|table_cropped|table.*truncat|表格.*截断|表格.*不完整", FindingReasonCode.TABLE_TRUNCATED.value),
        (r"caption_truncated|caption_cropped|caption.*truncat|caption.*截断", FindingReasonCode.CAPTION_TRUNCATED.value),
        (r"mixed_objects|mixed_figure_table|两个独立对象|图.*表格.*同时", FindingReasonCode.MIXED_OBJECTS.value),
        (r"type_mismatch|declared_type_mismatch|kind mismatch|声明类型.*不符", FindingReasonCode.TYPE_MISMATCH.value),
        (r"formula.*contamin|公式.*正文|surrounding prose", FindingReasonCode.FORMULA_CONTAMINATION.value),
        (r"critical asset.*missing|referenced critical asset|关键.*资产.*缺失", FindingReasonCode.MISSING_CRITICAL_ASSET.value),
        (r"timeout|timed out|connection|network|transport|超时|连接失败|网络", FindingReasonCode.VERIFIER_TRANSPORT_FAILURE.value),
        (r"invalid.*json|not valid json|不是合法 json|missing json object", FindingReasonCode.VERIFIER_INVALID_JSON.value),
    )
    for pattern, reason_code in patterns:
        if re.search(pattern, combined, re.I):
            return reason_code
    return normalized


def default_actions(reason_code: str) -> tuple[str, ...]:
    return {
        FindingReasonCode.TABLE_BODY_MISSING.value: ("recapture_asset", "expand_to_border", "split_at_border"),
        FindingReasonCode.TABLE_TRUNCATED.value: ("recapture_asset", "expand_to_next_border"),
        FindingReasonCode.CAPTION_TRUNCATED.value: ("recapture_asset", "extend_caption_boundary"),
        FindingReasonCode.MIXED_OBJECTS.value: ("split_adjacent_objects", "discard_candidate"),
        FindingReasonCode.TYPE_MISMATCH.value: ("select_matching_candidate", "discard_candidate"),
        FindingReasonCode.FORMULA_CONTAMINATION.value: ("recapture_formula", "tighten_formula_bbox"),
        FindingReasonCode.MISSING_CRITICAL_ASSET.value: ("capture_missing_asset", "rewrite_asset_marker"),
        FindingReasonCode.VERIFIER_TRANSPORT_FAILURE.value: ("retry_verifier", "use_deterministic_checks"),
        FindingReasonCode.VERIFIER_INVALID_JSON.value: ("retry_structured_verifier", "use_legacy_adapter"),
    }.get(reason_code, ("inspect_finding",))


def finding_from_legacy(
    value: object,
    *,
    stage: str,
    severity: str = "error",
    confidence: float | None = None,
    provenance: Iterable[str] = (),
) -> Finding:
    if isinstance(value, Finding):
        return value
    if isinstance(value, dict):
        message = str(value.get("human_message") or value.get("reason") or value.get("message") or "").strip()
        reason_code = infer_reason_code(str(value.get("reason_code") or value.get("type") or ""), message)
        raw_asset = value.get("asset_id")
        asset_id = int(raw_asset) if str(raw_asset).isdigit() else _asset_id_from_text(message)
        claim_id = str(value.get("claim_id") or "").strip() or None
        if claim_id is None and str(value.get("claim") or "").strip():
            claim_text = str(value.get("claim")).strip()
            claim_id = "claim-" + hashlib.sha256(claim_text.encode("utf-8")).hexdigest()[:12]
        evidence_refs = value.get("evidence_refs") or value.get("evidence_ids") or ()
        actions = value.get("suggested_actions") or ()
        item_severity = str(value.get("severity") or severity).lower()
        item_confidence = value.get("confidence", confidence if confidence is not None else 0.8)
        item_provenance = value.get("provenance") or tuple(provenance)
        item_stage = str(value.get("stage") or stage)
        existing_id = str(value.get("finding_id") or "").strip()
    else:
        message = str(value).strip()
        reason_code = infer_reason_code("", message)
        asset_id = _asset_id_from_text(message)
        claim_id = None
        evidence_refs = ()
        actions = ()
        item_severity = severity
        item_confidence = confidence if confidence is not None else 0.8
        item_provenance = tuple(provenance)
        item_stage = stage
        existing_id = ""
    try:
        item_confidence = float(item_confidence)
    except (TypeError, ValueError):
        item_confidence = confidence if confidence is not None else 0.8
    created = Finding.create(
        stage=item_stage,
        severity=item_severity,
        confidence=item_confidence,
        asset_id=asset_id,
        claim_id=claim_id,
        evidence_refs=evidence_refs if isinstance(evidence_refs, (list, tuple, set)) else (str(evidence_refs),),
        reason_code=reason_code,
        human_message=message,
        suggested_actions=actions if isinstance(actions, (list, tuple, set)) else (str(actions),) if actions else (),
        provenance=item_provenance if isinstance(item_provenance, (list, tuple, set)) else (str(item_provenance),),
    )
    if not existing_id:
        return created
    return Finding(
        finding_id=existing_id,
        stage=created.stage,
        severity=created.severity,
        confidence=created.confidence,
        asset_id=created.asset_id,
        claim_id=created.claim_id,
        evidence_refs=created.evidence_refs,
        reason_code=created.reason_code,
        human_message=created.human_message,
        suggested_actions=created.suggested_actions,
        provenance=created.provenance,
    )


def aggregate_findings(findings: Iterable[Finding]) -> list[Finding]:
    grouped: dict[tuple[str, int | None, str | None, str], list[Finding]] = {}
    for finding in findings:
        message_key = finding.human_message if finding.reason_code == FindingReasonCode.LEGACY_ERROR.value else ""
        grouped.setdefault((finding.reason_code, finding.asset_id, finding.claim_id, message_key), []).append(finding)
    result: list[Finding] = []
    for group in grouped.values():
        first = group[0]
        provenance = tuple(dict.fromkeys(item for finding in group for item in finding.provenance))
        confidence = 1.0
        for finding in group:
            confidence *= 1.0 - finding.confidence
        confidence = 1.0 - confidence
        independent = len(provenance) >= 2
        high_confidence = max(item.confidence for item in group) >= 0.8
        severity = FindingSeverity.ERROR.value if any(item.severity == FindingSeverity.ERROR.value for item in group) else FindingSeverity.WARNING.value
        if first.is_transport or first.reason_code == FindingReasonCode.VERIFIER_INVALID_JSON.value:
            severity = FindingSeverity.WARNING.value
        elif severity == FindingSeverity.ERROR.value and not high_confidence and not independent:
            severity = FindingSeverity.WARNING.value
        message = first.human_message
        if len(group) > 1 and independent:
            message = f"{message}（已由 {len(provenance)} 个独立信号确认）"
        result.append(
            Finding.create(
                stage=first.stage,
                severity=severity,
                confidence=confidence,
                asset_id=first.asset_id,
                claim_id=first.claim_id,
                evidence_refs=tuple(dict.fromkeys(ref for item in group for ref in item.evidence_refs)),
                reason_code=first.reason_code,
                human_message=message,
                suggested_actions=tuple(dict.fromkeys(action for item in group for action in item.suggested_actions)),
                provenance=provenance,
            )
        )
    return result


def findings_from_verification_payload(payload: dict[str, Any]) -> list[Finding]:
    """Migrate both typed and pre-Prompt-2 verification JSON."""

    values: list[Finding] = []
    for item in _as_items(payload.get("findings")):
        values.append(finding_from_legacy(item, stage="verifier", provenance=("verifier_json",)))
    for item in _as_items(payload.get("hard_failures")):
        values.append(finding_from_legacy(item, stage="verifier", severity="error", confidence=0.9, provenance=("verifier_json",)))
    for item in _as_items(payload.get("soft_warnings")):
        values.append(finding_from_legacy(item, stage="verifier", severity="warning", confidence=0.6, provenance=("verifier_json",)))
    for item in _as_items(payload.get("errors")):
        values.append(finding_from_legacy(item, stage="verifier", severity="error", confidence=0.8, provenance=("legacy_verification_json",)))
    return aggregate_findings(values)


def migrate_verification_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add typed findings to old direct or sidecar-wrapped verification JSON."""

    if not isinstance(payload, dict):
        raise TypeError("Verification payload must be a JSON object.")
    migrated = dict(payload)
    nested = migrated.get("verification")
    target = dict(nested) if isinstance(nested, dict) else migrated
    if not target.get("findings"):
        target["findings"] = [finding.to_dict() for finding in findings_from_verification_payload(target)]
    if isinstance(nested, dict):
        migrated["verification"] = target
    else:
        migrated = target
    migrated["finding_schema_version"] = 1
    return migrated


def _asset_id_from_text(text: str) -> int | None:
    match = re.search(r"(?:asset\s*(?:id)?\s*[:#]?\s*|ASSET:)\s*(\d+)", text, re.I)
    return int(match.group(1)) if match else None


def _as_items(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


__all__ = [
    "Finding",
    "FindingReasonCode",
    "FindingSeverity",
    "FindingStage",
    "KNOWN_REASON_CODES",
    "aggregate_findings",
    "default_actions",
    "finding_from_legacy",
    "findings_from_verification_payload",
    "infer_reason_code",
    "migrate_verification_payload",
    "normalize_reason_code",
]
