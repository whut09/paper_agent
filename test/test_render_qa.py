from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image

from paper_agent.evaluation import render_qa
from paper_agent.paper_summary import PaperAsset, _write_docx


def _asset(tmp_path: Path, name: str = "figure.png", caption: str = "Figure 1: Overview") -> PaperAsset:
    path = tmp_path / name
    Image.new("RGB", (640, 360), "white").save(path)
    return PaperAsset("figure", 1, path, caption, rect=fitz.Rect(40, 80, 560, 360))


def _docx(tmp_path: Path, assets: list[PaperAsset]) -> Path:
    path = tmp_path / "report.docx"
    summary = "# 测试报告\n\n## 方法主线\n方法流程如图所示。\n\n[[ASSET:1]]"
    _write_docx(path, "paper.pdf", summary, assets[:1])
    return path


def _rendered_pdf(tmp_path: Path, image_path: Path) -> Path:
    path = tmp_path / "rendered.pdf"
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 80), "Figure 1 overview")
    page.insert_image(fitz.Rect(72, 110, 500, 360), filename=str(image_path))
    document.save(path)
    document.close()
    return path


def test_render_qa_passes_structural_and_rendered_checks(tmp_path):
    asset = _asset(tmp_path)
    docx_path = _docx(tmp_path, [asset])
    pdf_path = _rendered_pdf(tmp_path, asset.path)

    with patch.object(render_qa, "_render_docx", return_value=render_qa._RenderAttempt("fixture", pdf_path)):
        result = render_qa.run_render_qa(docx_path, [asset], tmp_path / "render")

    assert result.status == "pass"
    assert result.page_count == 1
    assert result.assets[0].caption_adjacent
    assert result.assets[0].rendered_page == 1


def test_render_qa_warns_when_renderer_is_unavailable(tmp_path):
    asset = _asset(tmp_path)
    docx_path = _docx(tmp_path, [asset])
    unavailable = render_qa._RenderAttempt(
        "none",
        reason_code="renderer_unavailable",
        message="renderer missing",
    )

    with patch.object(render_qa, "_render_docx", return_value=unavailable):
        result = render_qa.run_render_qa(docx_path, [asset], tmp_path / "render")

    assert result.status == "warning"
    assert result.downloadable
    assert result.reason_codes == ["renderer_unavailable"]
    assert "install_libreoffice_or_enable_word_com" in result.findings[0].suggested_actions


def test_render_qa_blocks_missing_manifest_asset(tmp_path):
    first = _asset(tmp_path)
    second = _asset(tmp_path, "table.png", "Figure 2: Results")
    docx_path = _docx(tmp_path, [first])
    unavailable = render_qa._RenderAttempt("none", reason_code="renderer_unavailable", message="renderer missing")

    with patch.object(render_qa, "_render_docx", return_value=unavailable):
        result = render_qa.run_render_qa(docx_path, [first, second], tmp_path / "render")

    assert result.status == "block"
    assert not result.downloadable
    assert "missing_critical_asset" in result.reason_codes
    assert all(item.suggested_actions for item in result.findings if item.severity == "block")


def test_render_qa_renderer_timeout_is_warning_not_content_defect(tmp_path):
    asset = _asset(tmp_path)
    docx_path = _docx(tmp_path, [asset])
    timeout = render_qa._RenderAttempt("fixture", reason_code="renderer_timeout", message="timed out")

    with patch.object(render_qa, "_render_docx", return_value=timeout):
        result = render_qa.run_render_qa(docx_path, [asset], tmp_path / "render")

    assert result.status == "warning"
    assert result.reason_codes == ["renderer_timeout"]
