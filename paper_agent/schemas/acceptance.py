"""Typed migration and end-to-end acceptance results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AcceptanceBlocker:
    reason_code: str
    message: str
    suggested_actions: tuple[str, ...]
    stage: str = "acceptance"
    asset_id: int | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("An acceptance blocker requires a reason_code.")
        if not self.suggested_actions:
            raise ValueError("An acceptance blocker requires at least one suggested action.")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["suggested_actions"] = list(self.suggested_actions)
        return payload


@dataclass(frozen=True)
class ManifestComparison:
    legacy_count: int
    current_count: int
    matched_count: int
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    legacy_available: bool = True

    @property
    def match_rate(self) -> float:
        denominator = max(self.legacy_count, self.current_count)
        return 1.0 if denominator == 0 else self.matched_count / denominator

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "match_rate": round(self.match_rate, 6),
                "added": list(self.added),
                "removed": list(self.removed),
                "changed": list(self.changed),
            }
        )
        return payload


@dataclass(frozen=True)
class SectionCoverageComparison:
    required_sections: tuple[str, ...]
    current_sections: tuple[str, ...]
    missing_sections: tuple[str, ...]
    score: float
    legacy_sections: tuple[str, ...] = ()
    legacy_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for name in ("required_sections", "current_sections", "missing_sections", "legacy_sections"):
            payload[name] = list(payload[name])
        return payload


@dataclass(frozen=True)
class AcceptanceMetrics:
    elapsed_seconds: float
    model_call_count: int
    model_call_count_source: str
    repair_count: int
    ineffective_repair_count: int
    hard_failure_count: int
    warning_count: int
    final_qa: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MigrationAcceptanceResult:
    status: str
    paper_name: str
    source_path: str
    metrics: AcceptanceMetrics
    manifest_comparison: ManifestComparison
    section_coverage: SectionCoverageComparison
    blockers: list[AcceptanceBlocker] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    migration_phases: list[dict[str, Any]] = field(default_factory=list)
    sidecar_compatibility: dict[str, Any] = field(default_factory=dict)
    qa_path: Path | None = None

    @property
    def meets_exit_criteria(self) -> bool:
        if self.status == "passed":
            return self.metrics.final_qa == "pass"
        return self.status == "blocked" and bool(self.blockers) and all(
            blocker.reason_code and blocker.suggested_actions for blocker in self.blockers
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "meets_exit_criteria": self.meets_exit_criteria,
            "paper_name": self.paper_name,
            "source_path": self.source_path,
            "metrics": self.metrics.to_dict(),
            "manifest_comparison": self.manifest_comparison.to_dict(),
            "section_coverage": self.section_coverage.to_dict(),
            "blockers": [item.to_dict() for item in self.blockers],
            "warnings": list(self.warnings),
            "migration_phases": list(self.migration_phases),
            "sidecar_compatibility": dict(self.sidecar_compatibility),
            "qa_path": str(self.qa_path) if self.qa_path else "",
        }


__all__ = [
    "AcceptanceBlocker",
    "AcceptanceMetrics",
    "ManifestComparison",
    "MigrationAcceptanceResult",
    "SectionCoverageComparison",
]
