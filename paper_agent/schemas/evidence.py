"""Structured claim and evidence schemas."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Evidence:
    id: str
    section_id: str
    title: str
    category: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    type: str = "claim"
    section: str = ""
    core: bool = True
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["claim"] = self.text
        return payload


@dataclass(frozen=True)
class ClaimGrounding:
    claim_id: str
    evidence_ids: list[str] = field(default_factory=list)
    source_section: str = ""
    source_title: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceMap(dict):
    """Dict-compatible Evidence Map used by legacy harness code and JSON sidecars."""

    def __init__(
        self,
        *,
        intro: list[dict[str, Any]] | None = None,
        method: list[dict[str, Any]] | None = None,
        experiments: list[dict[str, Any]] | None = None,
        claims: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        claim_groundings: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            intro=intro or [],
            method=method or [],
            experiments=experiments or [],
            claims=claims or [],
            evidence=evidence or [],
            claim_groundings=claim_groundings or [],
        )

    @classmethod
    def coerce(cls, value: dict[str, Any] | "EvidenceMap") -> "EvidenceMap":
        if isinstance(value, cls):
            return value
        return cls(
            intro=[dict(item) for item in value.get("intro", [])],
            method=[dict(item) for item in value.get("method", [])],
            experiments=[dict(item) for item in value.get("experiments", [])],
            claims=[dict(item) for item in value.get("claims", [])],
            evidence=[dict(item) for item in value.get("evidence", [])],
            claim_groundings=[dict(item) for item in value.get("claim_groundings", [])],
        )


__all__ = ["Claim", "ClaimGrounding", "Evidence", "EvidenceMap"]
