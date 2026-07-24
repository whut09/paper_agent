"""Typed RenderQA and summary diagnostics results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RenderQAFinding:
    reason_code: str
    severity: str
    message: str
    page_number: int | None = None
    asset_id: int | None = None
    measurements: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RenderPageMeasurement:
    page_number: int
    width: float
    height: float
    image_count: int
    text_block_count: int
    image_path: Path | None = None
    overflow: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["image_path"] = str(self.image_path) if self.image_path else ""
        return payload


@dataclass(frozen=True)
class RenderAssetMeasurement:
    asset_id: int
    kind: str
    caption: str
    source_page: int
    media_path: str = ""
    pixel_width: int = 0
    pixel_height: int = 0
    document_width_emu: int = 0
    document_height_emu: int = 0
    rendered_page: int | None = None
    rendered_bbox: tuple[float, float, float, float] | None = None
    caption_adjacent: bool = False
    cropped: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rendered_bbox"] = list(self.rendered_bbox) if self.rendered_bbox else None
        return payload


@dataclass
class RenderQAResult:
    status: str
    renderer: str
    page_count: int | None
    findings: list[RenderQAFinding] = field(default_factory=list)
    pages: list[RenderPageMeasurement] = field(default_factory=list)
    assets: list[RenderAssetMeasurement] = field(default_factory=list)
    rendered_pdf_path: Path | None = None

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def downloadable(self) -> bool:
        return self.status in {"pass", "warning"}

    @property
    def reason_codes(self) -> list[str]:
        return list(dict.fromkeys(item.reason_code for item in self.findings))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "renderer": self.renderer,
            "page_count": self.page_count,
            "downloadable": self.downloadable,
            "reason_codes": self.reason_codes,
            "rendered_pdf_path": str(self.rendered_pdf_path) if self.rendered_pdf_path else "",
            "findings": [item.to_dict() for item in self.findings],
            "pages": [item.to_dict() for item in self.pages],
            "assets": [item.to_dict() for item in self.assets],
        }


@dataclass
class SummaryRunResult:
    status: str
    message: str
    current_stage: str = ""
    progress: float = 0.0
    progress_message: str = ""
    repair_count: int = 0
    reason_codes: list[str] = field(default_factory=list)
    docx_path: Path | None = None
    trace_path: Path | None = None
    verification_path: Path | None = None
    qa_path: Path | None = None
    failure_report_path: Path | None = None
    downloadable: bool = False
    warning: bool = False
    traceback_text: str = field(default="", repr=False)
    exception: BaseException | None = field(default=None, repr=False)

    @property
    def diagnostic_paths(self) -> list[Path]:
        paths = [self.trace_path, self.verification_path, self.qa_path, self.failure_report_path]
        return [path for path in paths if path is not None and path.exists()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "current_stage": self.current_stage,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "repair_count": self.repair_count,
            "reason_codes": list(self.reason_codes),
            "docx_path": str(self.docx_path) if self.docx_path else "",
            "trace_path": str(self.trace_path) if self.trace_path else "",
            "verification_path": str(self.verification_path) if self.verification_path else "",
            "qa_path": str(self.qa_path) if self.qa_path else "",
            "failure_report_path": str(self.failure_report_path) if self.failure_report_path else "",
            "downloadable": self.downloadable,
            "warning": self.warning,
        }


__all__ = [
    "RenderAssetMeasurement",
    "RenderPageMeasurement",
    "RenderQAFinding",
    "RenderQAResult",
    "SummaryRunResult",
]
