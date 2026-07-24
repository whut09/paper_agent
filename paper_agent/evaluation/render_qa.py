"""Post-generation DOCX rendering and deterministic quality checks."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import fitz
from PIL import Image

from paper_agent.evaluation.acceptance import suggested_actions
from paper_agent.schemas.qa import (
    RenderAssetMeasurement,
    RenderPageMeasurement,
    RenderQAFinding,
    RenderQAResult,
)


_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_EMU_PER_TWIP = 635


@dataclass(frozen=True)
class _RenderAttempt:
    renderer: str
    pdf_path: Path | None = None
    reason_code: str = ""
    message: str = ""


def _finding(
    reason_code: str,
    severity: str,
    message: str,
    *,
    page_number: int | None = None,
    asset_id: int | None = None,
    **measurements: Any,
) -> RenderQAFinding:
    return RenderQAFinding(
        reason_code,
        severity,
        message,
        page_number,
        asset_id,
        measurements,
        suggested_actions(reason_code),
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def write_render_qa_sidecar(path: Path, result: RenderQAResult, *, run_id: str = "") -> Path:
    payload = result.to_dict()
    payload["run_id"] = run_id
    _atomic_write_json(path, payload)
    return path


def _windows_word_available() -> bool:
    if os.name != "nt" or not shutil.which("powershell"):
        return False
    try:
        result = subprocess.run(
            ["reg", "query", r"HKCR\Word.Application\CLSID"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _soffice_path() -> str:
    candidate = shutil.which("soffice") or shutil.which("libreoffice")
    if candidate:
        return candidate
    if os.name == "nt":
        for path in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LibreOffice/program/soffice.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "LibreOffice/program/soffice.exe",
        ):
            if path.exists():
                return str(path)
    return ""


def _render_docx(docx_path: Path, render_dir: Path, timeout_seconds: float = 120.0) -> _RenderAttempt:
    render_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = render_dir / f"{docx_path.stem}.pdf"
    pdf_path.unlink(missing_ok=True)
    soffice = _soffice_path()
    try:
        if soffice:
            result = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(render_dir), str(docx_path)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            if result.returncode == 0 and pdf_path.exists():
                return _RenderAttempt("libreoffice", pdf_path)
            return _RenderAttempt("libreoffice", reason_code="renderer_failed", message=(result.stderr or result.stdout).strip()[:500])

        if _windows_word_available():
            quoted_docx = str(docx_path.resolve()).replace("'", "''")
            quoted_pdf = str(pdf_path.resolve()).replace("'", "''")
            script = (
                "$ErrorActionPreference='Stop'; $word=$null; $doc=$null; try {"
                "$word=New-Object -ComObject Word.Application; $word.Visible=$false; $word.DisplayAlerts=0;"
                f"$doc=$word.Documents.Open('{quoted_docx}', $false, $true);"
                f"$doc.ExportAsFixedFormat('{quoted_pdf}', 17);"
                "} finally { if($doc){$doc.Close($false)}; if($word){$word.Quit()};"
                "if($doc){[void][Runtime.InteropServices.Marshal]::ReleaseComObject($doc)};"
                "if($word){[void][Runtime.InteropServices.Marshal]::ReleaseComObject($word)} }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            if result.returncode == 0 and pdf_path.exists():
                return _RenderAttempt("word-com", pdf_path)
            return _RenderAttempt("word-com", reason_code="renderer_failed", message=(result.stderr or result.stdout).strip()[:500])
    except subprocess.TimeoutExpired:
        return _RenderAttempt("libreoffice" if soffice else "word-com", reason_code="renderer_timeout", message="DOCX renderer timed out")
    except OSError as exc:
        return _RenderAttempt("libreoffice" if soffice else "word-com", reason_code="renderer_transport_failure", message=str(exc)[:500])
    return _RenderAttempt("none", reason_code="renderer_unavailable", message="No supported DOCX renderer is available")


def _paragraph_text(paragraph: ET.Element) -> str:
    return "".join(item.text or "" for item in paragraph.findall(".//w:t", _NS)).strip()


def _document_geometry(root: ET.Element) -> tuple[int, int]:
    section = root.find(".//w:sectPr", _NS)
    if section is None:
        return 0, 0
    size = section.find("w:pgSz", _NS)
    margins = section.find("w:pgMar", _NS)
    width = int(size.get(f"{{{_NS['w']}}}w", "0")) if size is not None else 0
    height = int(size.get(f"{{{_NS['w']}}}h", "0")) if size is not None else 0
    if margins is not None:
        width -= int(margins.get(f"{{{_NS['w']}}}left", "0")) + int(margins.get(f"{{{_NS['w']}}}right", "0"))
        height -= int(margins.get(f"{{{_NS['w']}}}top", "0")) + int(margins.get(f"{{{_NS['w']}}}bottom", "0"))
    return max(0, width) * _EMU_PER_TWIP, max(0, height) * _EMU_PER_TWIP


def _asset_reference_token(kind: str) -> str:
    return {"figure": "图", "table": "表", "formula": "公式"}.get(kind, "")


def _caption_is_adjacent(kind: str, previous_text: str, next_text: str) -> bool:
    token = _asset_reference_token(kind)
    adjacent = f"{previous_text} {next_text}".strip()
    if not adjacent:
        return False
    if token and token in adjacent:
        return True
    english = {"figure": r"fig(?:ure)?", "table": r"table", "formula": r"(?:equation|eq\.)"}.get(kind, "")
    return bool(english and re.search(english, adjacent, re.IGNORECASE))


def _manifest_labels(assets: list[Any]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    counters: dict[str, int] = {}
    for asset in assets:
        kind = str(getattr(asset, "kind", ""))
        if kind not in {"figure", "table"}:
            continue
        counters[kind] = counters.get(kind, 0) + 1
        caption = str(getattr(asset, "caption", ""))
        match = re.search(r"(?i)(?:fig(?:ure)?|table|图|表)\s*([0-9]+)", caption)
        result.add((kind, match.group(1) if match else str(counters[kind])))
    return result


def _critical_references(text: str) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for kind, pattern in (
        ("figure", r"(?i)(?:fig(?:ure)?|图)\s*([12])"),
        ("table", r"(?i)(?:table|表)\s*([12])"),
    ):
        refs.update((kind, number) for number in re.findall(pattern, text))
    return refs


def _inspect_docx(docx_path: Path, assets: list[Any]) -> tuple[list[RenderAssetMeasurement], list[RenderQAFinding]]:
    findings: list[RenderQAFinding] = []
    measurements: list[RenderAssetMeasurement] = []
    try:
        with zipfile.ZipFile(docx_path) as archive:
            document_xml = archive.read("word/document.xml")
            relationships_xml = archive.read("word/_rels/document.xml.rels")
            root = ET.fromstring(document_xml)
            rel_root = ET.fromstring(relationships_xml)
            relationships = {
                item.get("Id", ""): item.get("Target", "")
                for item in rel_root.findall("rel:Relationship", _NS)
            }
            paragraphs = root.findall(".//w:body/w:p", _NS)
            text_by_paragraph = [_paragraph_text(item) for item in paragraphs]
            document_text = "\n".join(text_by_paragraph)
            if b"[[ASSET:" in document_xml or "[[ASSET:" in document_text:
                findings.append(_finding("unresolved_asset_marker", "block", "DOCX contains an unreplaced asset marker"))

            content_width, content_height = _document_geometry(root)
            drawings: dict[int, tuple[str, int, int, bool]] = {}
            for index, paragraph in enumerate(paragraphs):
                drawing = paragraph.find(".//w:drawing", _NS)
                if drawing is None:
                    continue
                docpr = drawing.find(".//wp:docPr", _NS)
                blip = drawing.find(".//a:blip", _NS)
                extent = drawing.find(".//wp:extent", _NS)
                if docpr is None or blip is None or extent is None:
                    continue
                asset_id = int(docpr.get("id", "0") or 0)
                rel_id = blip.get(f"{{{_NS['r']}}}embed", "")
                cx = int(extent.get("cx", "0") or 0)
                cy = int(extent.get("cy", "0") or 0)
                previous = text_by_paragraph[index - 1] if index > 0 else ""
                following = text_by_paragraph[index + 1] if index + 1 < len(paragraphs) else ""
                kind = str(getattr(assets[asset_id - 1], "kind", "")) if 0 < asset_id <= len(assets) else ""
                drawings[asset_id] = (rel_id, cx, cy, _caption_is_adjacent(kind, previous, following))

            for asset_id, asset in enumerate(assets, 1):
                kind = str(getattr(asset, "kind", ""))
                caption = str(getattr(asset, "caption", ""))
                source_page = int(getattr(asset, "page_number", 0) or 0)
                drawing = drawings.get(asset_id)
                if drawing is None:
                    findings.append(_finding("missing_critical_asset", "block", f"Asset {asset_id} is not embedded in the DOCX", asset_id=asset_id))
                    measurements.append(RenderAssetMeasurement(asset_id, kind, caption, source_page))
                    continue
                rel_id, cx, cy, adjacent = drawing
                target = relationships.get(rel_id, "")
                media_name = target.replace("\\", "/").split("/")[-1]
                media_path = f"word/media/{media_name}" if media_name else ""
                pixel_width = pixel_height = 0
                if media_path and media_path in archive.namelist():
                    with Image.open(io.BytesIO(archive.read(media_path))) as image:
                        pixel_width, pixel_height = image.size
                else:
                    findings.append(_finding("missing_critical_asset", "block", f"Asset {asset_id} media relationship is missing", asset_id=asset_id))
                if pixel_width < 64 or pixel_height < 64:
                    findings.append(_finding("image_too_small", "block", f"Asset {asset_id} is too small for a readable report", asset_id=asset_id, width=pixel_width, height=pixel_height))
                if content_width and cx > content_width + 1000:
                    findings.append(_finding("page_overflow", "block", f"Asset {asset_id} exceeds the document content width", asset_id=asset_id, width_emu=cx, content_width_emu=content_width))
                if content_height and cy > content_height + 1000:
                    findings.append(_finding("image_cropped", "block", f"Asset {asset_id} exceeds the document content height", asset_id=asset_id, height_emu=cy, content_height_emu=content_height))
                if not adjacent:
                    findings.append(_finding("caption_not_adjacent", "warning", f"Asset {asset_id} has no adjacent caption or reference paragraph", asset_id=asset_id))
                measurements.append(
                    RenderAssetMeasurement(
                        asset_id,
                        kind,
                        caption,
                        source_page,
                        media_path,
                        pixel_width,
                        pixel_height,
                        cx,
                        cy,
                        caption_adjacent=adjacent,
                    )
                )

            manifest_labels = _manifest_labels(assets)
            for kind, number in sorted(_critical_references(document_text) - manifest_labels):
                findings.append(_finding("missing_critical_asset", "block", f"Referenced critical {kind} {number} is absent from the DOCX asset manifest"))
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        findings.append(_finding("invalid_docx", "block", f"DOCX package cannot be inspected: {exc}"))
    return measurements, findings


def _inspect_rendered_pdf(
    pdf_path: Path,
    render_dir: Path,
    assets: list[RenderAssetMeasurement],
) -> tuple[list[RenderPageMeasurement], list[RenderAssetMeasurement], list[RenderQAFinding]]:
    render_dir.mkdir(parents=True, exist_ok=True)
    pages: list[RenderPageMeasurement] = []
    findings: list[RenderQAFinding] = []
    rendered_images: list[tuple[int, tuple[float, float, float, float], bool]] = []
    try:
        document = fitz.open(pdf_path)
    except (OSError, RuntimeError) as exc:
        return pages, assets, [_finding("rendered_pdf_invalid", "block", f"Rendered PDF cannot be opened: {exc}")]
    try:
        if document.page_count <= 0:
            findings.append(_finding("empty_render", "block", "Rendered document has no pages"))
        for page_index in range(document.page_count):
            page = document[page_index]
            page_rect = page.rect
            image_infos = page.get_image_info()
            text_blocks = page.get_text("blocks")
            overflow = False
            for block in text_blocks:
                x0, y0, x1, y1 = block[:4]
                if x0 < -1 or y0 < -1 or x1 > page_rect.width + 1 or y1 > page_rect.height + 1:
                    overflow = True
            for info in image_infos:
                bbox = tuple(float(value) for value in info.get("bbox", (0, 0, 0, 0)))
                cropped = bbox[0] < -1 or bbox[1] < -1 or bbox[2] > page_rect.width + 1 or bbox[3] > page_rect.height + 1
                near_edge = bbox[0] <= 1 or bbox[1] <= 1 or bbox[2] >= page_rect.width - 1 or bbox[3] >= page_rect.height - 1
                rendered_images.append((page_index + 1, bbox, cropped or near_edge))
                if cropped or near_edge:
                    findings.append(_finding("image_cropped", "block", "Rendered image touches or exceeds the page boundary", page_number=page_index + 1, bbox=list(bbox)))
            if overflow:
                findings.append(_finding("page_overflow", "block", "Rendered content exceeds the page boundary", page_number=page_index + 1))
            image_path = render_dir / f"page-{page_index + 1:03d}.png"
            page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False).save(image_path)
            pages.append(
                RenderPageMeasurement(
                    page_index + 1,
                    float(page_rect.width),
                    float(page_rect.height),
                    len(image_infos),
                    len(text_blocks),
                    image_path,
                    overflow,
                )
            )
    finally:
        document.close()

    updated = list(assets)
    for index, measurement in enumerate(updated):
        if index >= len(rendered_images):
            break
        page_number, bbox, cropped = rendered_images[index]
        updated[index] = replace(measurement, rendered_page=page_number, rendered_bbox=bbox, cropped=cropped)
    if assets and len(rendered_images) < len(assets):
        findings.append(
            _finding(
                "rendered_asset_count_mismatch",
                "warning",
                "Rendered PDF exposes fewer raster images than the DOCX asset manifest",
                expected=len(assets),
                rendered=len(rendered_images),
            )
        )
    return pages, updated, findings


def run_render_qa(
    docx_path: Path,
    assets: list[Any],
    render_dir: Path,
    *,
    timeout_seconds: float = 120.0,
) -> RenderQAResult:
    measurements, findings = _inspect_docx(docx_path, assets)
    attempt = _render_docx(docx_path, render_dir, timeout_seconds)
    pages: list[RenderPageMeasurement] = []
    if attempt.pdf_path is not None:
        pages, measurements, rendered_findings = _inspect_rendered_pdf(attempt.pdf_path, render_dir, measurements)
        findings.extend(rendered_findings)
    elif attempt.reason_code:
        findings.append(_finding(attempt.reason_code, "warning", attempt.message or attempt.reason_code))

    status = "block" if any(item.severity == "block" for item in findings) else "warning" if findings else "pass"
    return RenderQAResult(status, attempt.renderer, len(pages) if pages else None, findings, pages, measurements, attempt.pdf_path)


__all__ = ["run_render_qa", "write_render_qa_sidecar"]
