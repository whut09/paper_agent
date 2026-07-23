"""Bounded repair state machine for typed verification findings."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from paper_agent.schemas.findings import Finding, FindingReasonCode, aggregate_findings


class RepairState(str, Enum):
    DETECTED = "DETECTED"
    CLASSIFIED = "CLASSIFIED"
    RECAPTURE = "RECAPTURE"
    RECHECK = "RECHECK"
    ACCEPTED = "ACCEPTED"
    SPLIT = "SPLIT"
    ALTERNATE_CANDIDATE = "ALTERNATE_CANDIDATE"
    REPORT_REWRITE = "REPORT_REWRITE"
    BLOCKED = "BLOCKED"


class RepairAction(str, Enum):
    SCORE_CANDIDATES = "score_candidates"
    EXPAND_BORDER = "expand_border"
    SPLIT_AT_BORDER = "split_at_border"
    VISUAL_ARBITRATION = "visual_arbitration"
    RECAPTURE_ASSET = "recapture_asset"
    EXTEND_CAPTION = "extend_caption_boundary"
    SPLIT_OBJECTS = "split_adjacent_objects"
    DISCARD_CANDIDATE = "discard_candidate"
    SELECT_ALTERNATE = "select_alternate_candidate"
    TIGHTEN_FORMULA = "tighten_formula_bbox"
    CAPTURE_MISSING = "capture_missing_asset"
    REWRITE_REPORT = "rewrite_report_section"
    RETRY_VERIFIER = "retry_verifier"
    USE_DETERMINISTIC = "use_deterministic_checks"


@dataclass(frozen=True)
class RepairStep:
    target: str
    asset_id: int | None
    reason_code: str
    state_from: RepairState
    state_to: RepairState
    action: RepairAction
    confidence: float
    cost: float
    next_action: RepairAction | None

    @property
    def attempt_key(self) -> str:
        return f"state:{self.target}:{self.action.value}"

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "asset_id": self.asset_id,
            "reason_code": self.reason_code,
            "state_from": self.state_from.value,
            "state_to": self.state_to.value,
            "action": self.action.value,
            "confidence": self.confidence,
            "cost": self.cost,
            "next_action": self.next_action.value if self.next_action else "",
        }


@dataclass(frozen=True)
class RepairTransition:
    target: str
    asset_id: int | None
    reason_code: str
    state_from: RepairState
    state_to: RepairState
    action: RepairAction
    before_signature: str
    after_signature: str
    confidence: float
    cost: float
    next_action: str
    changed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "asset_id": self.asset_id,
            "reason_code": self.reason_code,
            "state_from": self.state_from.value,
            "state_to": self.state_to.value,
            "action": self.action.value,
            "before_signature": self.before_signature,
            "after_signature": self.after_signature,
            "confidence": self.confidence,
            "cost": self.cost,
            "next_action": self.next_action,
            "changed": self.changed,
        }


REPAIR_LADDERS: Mapping[str, tuple[tuple[RepairAction, RepairState, float], ...]] = {
    FindingReasonCode.TABLE_BODY_MISSING.value: (
        (RepairAction.SCORE_CANDIDATES, RepairState.CLASSIFIED, 0.5),
        (RepairAction.EXPAND_BORDER, RepairState.RECAPTURE, 1.0),
        (RepairAction.SPLIT_AT_BORDER, RepairState.SPLIT, 1.0),
        (RepairAction.VISUAL_ARBITRATION, RepairState.RECHECK, 2.0),
    ),
    FindingReasonCode.TABLE_TRUNCATED.value: (
        (RepairAction.SCORE_CANDIDATES, RepairState.CLASSIFIED, 0.5),
        (RepairAction.EXPAND_BORDER, RepairState.RECAPTURE, 1.0),
        (RepairAction.SPLIT_AT_BORDER, RepairState.SPLIT, 1.0),
        (RepairAction.VISUAL_ARBITRATION, RepairState.RECHECK, 2.0),
    ),
    FindingReasonCode.CAPTION_TRUNCATED.value: (
        (RepairAction.SCORE_CANDIDATES, RepairState.CLASSIFIED, 0.5),
        (RepairAction.EXTEND_CAPTION, RepairState.RECAPTURE, 1.0),
        (RepairAction.RECAPTURE_ASSET, RepairState.RECAPTURE, 1.0),
        (RepairAction.REWRITE_REPORT, RepairState.REPORT_REWRITE, 1.5),
    ),
    FindingReasonCode.MIXED_OBJECTS.value: (
        (RepairAction.SPLIT_OBJECTS, RepairState.SPLIT, 1.0),
        (RepairAction.DISCARD_CANDIDATE, RepairState.BLOCKED, 0.5),
    ),
    FindingReasonCode.TYPE_MISMATCH.value: (
        (RepairAction.SELECT_ALTERNATE, RepairState.ALTERNATE_CANDIDATE, 1.0),
        (RepairAction.DISCARD_CANDIDATE, RepairState.BLOCKED, 0.5),
    ),
    FindingReasonCode.FORMULA_CONTAMINATION.value: (
        (RepairAction.SCORE_CANDIDATES, RepairState.CLASSIFIED, 0.5),
        (RepairAction.TIGHTEN_FORMULA, RepairState.RECAPTURE, 1.0),
        (RepairAction.SELECT_ALTERNATE, RepairState.ALTERNATE_CANDIDATE, 1.0),
    ),
    FindingReasonCode.MISSING_CRITICAL_ASSET.value: (
        (RepairAction.CAPTURE_MISSING, RepairState.RECAPTURE, 1.0),
    ),
    FindingReasonCode.VERIFIER_TRANSPORT_FAILURE.value: (
        (RepairAction.RETRY_VERIFIER, RepairState.RECHECK, 1.0),
        (RepairAction.USE_DETERMINISTIC, RepairState.ACCEPTED, 0.5),
    ),
    FindingReasonCode.VERIFIER_INVALID_JSON.value: (
        (RepairAction.RETRY_VERIFIER, RepairState.RECHECK, 1.0),
        (RepairAction.USE_DETERMINISTIC, RepairState.ACCEPTED, 0.5),
    ),
    FindingReasonCode.VISUAL_CROP_INVALID.value: (
        (RepairAction.SCORE_CANDIDATES, RepairState.CLASSIFIED, 0.5),
        (RepairAction.SELECT_ALTERNATE, RepairState.ALTERNATE_CANDIDATE, 1.0),
        (RepairAction.DISCARD_CANDIDATE, RepairState.BLOCKED, 0.5),
    ),
    FindingReasonCode.MISSING_ASSET_FILE.value: (
        (RepairAction.CAPTURE_MISSING, RepairState.RECAPTURE, 1.0),
        (RepairAction.DISCARD_CANDIDATE, RepairState.BLOCKED, 0.5),
    ),
    FindingReasonCode.UNSUPPORTED_CORE_CLAIM.value: (
        (RepairAction.REWRITE_REPORT, RepairState.REPORT_REWRITE, 1.5),
        (RepairAction.RETRY_VERIFIER, RepairState.RECHECK, 1.0),
    ),
    FindingReasonCode.WEAK_EVIDENCE.value: (
        (RepairAction.REWRITE_REPORT, RepairState.REPORT_REWRITE, 1.5),
    ),
    FindingReasonCode.VERIFIER_FAILED_WITHOUT_REASON.value: (
        (RepairAction.RETRY_VERIFIER, RepairState.RECHECK, 1.0),
        (RepairAction.USE_DETERMINISTIC, RepairState.ACCEPTED, 0.5),
    ),
    FindingReasonCode.LEGACY_ERROR.value: (
        (RepairAction.REWRITE_REPORT, RepairState.REPORT_REWRITE, 1.5),
    ),
}


class RepairStateMachine:
    """Plan at most two new actions per asset under a global cost budget."""

    def __init__(self, *, max_actions_per_asset: int = 2, max_global_cost: float = 8.0):
        self.max_actions_per_asset = max(1, max_actions_per_asset)
        self.max_global_cost = max(0.5, max_global_cost)

    def classify(self, findings: Iterable[Finding]) -> list[Finding]:
        return aggregate_findings(
            finding
            for finding in findings
            if finding.reason_code in REPAIR_LADDERS
            and (
                finding.severity == "error"
                or finding.reason_code
                in {
                    FindingReasonCode.VERIFIER_TRANSPORT_FAILURE.value,
                    FindingReasonCode.VERIFIER_INVALID_JSON.value,
                }
            )
        )

    def plan(
        self,
        findings: Iterable[Finding],
        *,
        attempted: Mapping[str, int] | None = None,
        global_cost_used: float = 0.0,
    ) -> list[RepairStep]:
        attempted = attempted or {}
        classified = self.classify(findings)
        result: list[RepairStep] = []
        used_cost = max(0.0, global_cost_used)
        per_target: dict[str, int] = {}
        selected_targets: set[str] = set()
        # Prefer findings that make expansion unsafe.  A mixed-object finding
        # must win over a truncation finding for the same crop; otherwise the
        # old "expand until it fits" behavior can merge two unrelated objects.
        priority = {
            FindingReasonCode.MIXED_OBJECTS.value: 0,
            FindingReasonCode.TYPE_MISMATCH.value: 1,
            FindingReasonCode.FORMULA_CONTAMINATION.value: 2,
            FindingReasonCode.TABLE_BODY_MISSING.value: 3,
            FindingReasonCode.TABLE_TRUNCATED.value: 4,
            FindingReasonCode.CAPTION_TRUNCATED.value: 5,
            FindingReasonCode.MISSING_CRITICAL_ASSET.value: 6,
            FindingReasonCode.VERIFIER_TRANSPORT_FAILURE.value: 7,
            FindingReasonCode.VERIFIER_INVALID_JSON.value: 8,
        }
        classified = sorted(
            classified,
            key=lambda item: (
                self._target(item),
                priority.get(item.reason_code, 99),
                -item.confidence,
                item.finding_id,
            ),
        )
        for finding in classified:
            target = self._target(finding)
            if target in selected_targets:
                continue
            selected_targets.add(target)
            ladder = REPAIR_LADDERS[finding.reason_code]
            target_used = per_target.get(
                target,
                sum(
                    1
                    for action, _state, _cost in ladder
                    if attempted.get(f"state:{target}:{action.value}", 0) > 0
                ),
            )
            for index, (action, state_to, cost) in enumerate(ladder):
                if target_used >= self.max_actions_per_asset or used_cost + cost > self.max_global_cost:
                    break
                step = RepairStep(
                    target=target,
                    asset_id=finding.asset_id,
                    reason_code=finding.reason_code,
                    state_from=RepairState.DETECTED if index == 0 else ladder[index - 1][1],
                    state_to=state_to,
                    action=action,
                    confidence=finding.confidence,
                    cost=cost,
                    next_action=ladder[index + 1][0] if index + 1 < len(ladder) else None,
                )
                if attempted.get(step.attempt_key, 0) > 0:
                    continue
                result.append(step)
                target_used += 1
                used_cost += cost
                # Candidate scoring is a diagnostic/classification step.  It
                # may be followed immediately by one bounded mutation; every
                # other action must be rechecked before the next ladder step.
                if action is not RepairAction.SCORE_CANDIDATES:
                    break
            per_target[target] = target_used
        return result

    def blocked_step(self, finding: Finding, *, reason: str = "repair budget exhausted") -> RepairStep:
        return RepairStep(
            target=self._target(finding),
            asset_id=finding.asset_id,
            reason_code=finding.reason_code,
            state_from=RepairState.RECHECK,
            state_to=RepairState.BLOCKED,
            action=RepairAction.DISCARD_CANDIDATE,
            confidence=finding.confidence,
            cost=0.0,
            next_action=None,
        )

    @staticmethod
    def transition(
        step: RepairStep,
        *,
        before_signature: str,
        after_signature: str,
    ) -> RepairTransition:
        changed = before_signature != after_signature
        observational = step.action in {
            RepairAction.SCORE_CANDIDATES,
            RepairAction.VISUAL_ARBITRATION,
            RepairAction.RETRY_VERIFIER,
            RepairAction.USE_DETERMINISTIC,
        }
        return RepairTransition(
            target=step.target,
            asset_id=step.asset_id,
            reason_code=step.reason_code,
            state_from=step.state_from,
            state_to=step.state_to if changed or observational else RepairState.BLOCKED,
            action=step.action,
            before_signature=before_signature,
            after_signature=after_signature,
            confidence=step.confidence,
            cost=step.cost,
            next_action=(
                step.next_action.value
                if (changed or observational) and step.next_action
                else "recheck" if observational
                else "blocked"
            ),
            changed=changed,
        )

    @staticmethod
    def signature(value: object) -> str:
        if hasattr(value, "kind") and hasattr(value, "page_number"):
            rect = getattr(value, "rect", None)
            payload = {
                "kind": getattr(value, "kind", ""),
                "page": getattr(value, "page_number", 0),
                "caption": str(getattr(value, "caption", "")),
                "text": str(getattr(value, "text", "")),
                "rect": tuple(round(float(item), 2) for item in rect) if rect is not None else None,
                "path": str(getattr(value, "path", "")),
            }
        else:
            payload = value
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _target(finding: Finding) -> str:
        if finding.asset_id is not None:
            return f"asset:{finding.asset_id}"
        if finding.reason_code == FindingReasonCode.MISSING_CRITICAL_ASSET.value:
            match = re.search(
                r"(?i)(figure|fig\.?|table|图|表)\s*([12一二])",
                finding.human_message,
            )
            if match:
                kind = "figure" if match.group(1).lower() in {"figure", "fig.", "图"} else "table"
                number = {"一": "1", "二": "2"}.get(match.group(2), match.group(2))
                return f"missing:{kind}:{number}"
        if finding.claim_id:
            return f"claim:{finding.claim_id}"
        return f"finding:{finding.finding_id}"


__all__ = [
    "REPAIR_LADDERS",
    "RepairAction",
    "RepairState",
    "RepairStateMachine",
    "RepairStep",
    "RepairTransition",
]
