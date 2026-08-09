from pathlib import Path
import subprocess
from unittest.mock import patch

import fitz
from PIL import Image

from paper_agent.evaluation import render_qa
from paper_agent.evaluation.image_integrity import inspect_crop_integrity
from paper_agent.paper_summary import PaperAsset, _write_docx
from paper_agent.paper_summary import RenderQA
from paper_agent.harness.context import PaperWorkflowContext
from paper_agent.schemas.qa import RenderAssetMeasurement, RenderQAFinding, RenderQAResult


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


def test_crop_integrity_detects_content_cut_off_at_left_edge(tmp_path):
    path = tmp_path / "clipped.png"
    image = Image.new("RGB", (640, 360), "white")
    pixels = image.load()
    for y in range(40, 320, 18):
        for x in range(0, 105):
            for dy in range(5):
                pixels[x, y + dy] = (20, 20, 20)
    image.save(path)
    result = inspect_crop_integrity(path, kind="figure")
    assert result is not None
    assert "left" in result.clipped_sides


def test_crop_integrity_does_not_reject_single_table_border(tmp_path):
    path = tmp_path / "table-border.png"
    image = Image.new("RGB", (640, 360), "white")
    pixels = image.load()
    for x in range(640):
        pixels[x, 359] = (0, 0, 0)
    image.save(path)
    result = inspect_crop_integrity(path, kind="table")
    assert result is not None
    assert "bottom" not in result.clipped_sides


def test_render_qa_blocks_internally_clipped_source_bitmap(tmp_path):
    asset = _asset(tmp_path)
    image = Image.open(asset.path).convert("RGB")
    pixels = image.load()
    for y in range(35, 330, 20):
        for x in range(0, 100):
            for dy in range(5):
                pixels[x, y + dy] = (10, 10, 10)
    image.save(asset.path)
    docx_path = _docx(tmp_path, [asset])
    unavailable = render_qa._RenderAttempt("none", reason_code="renderer_unavailable", message="renderer missing")
    with patch.object(render_qa, "_render_docx", return_value=unavailable):
        result = render_qa.run_render_qa(docx_path, [asset], tmp_path / "render")
    assert result.status == "block"
    assert "visual_crop_invalid" in result.reason_codes


def test_word_com_cleanup_failure_keeps_successful_export(tmp_path):
    docx_path = tmp_path / "report.docx"
    docx_path.write_bytes(b"docx")
    render_dir = tmp_path / "render"

    def fake_run(*_args, **_kwargs):
        render_dir.mkdir(parents=True, exist_ok=True)
        (render_dir / "report.pdf").write_bytes(b"pdf")
        return subprocess.CompletedProcess([], 1, "", "Word.Quit RPC unavailable")

    with (
        patch.object(render_qa, "_soffice_path", return_value=""),
        patch.object(render_qa, "_windows_word_available", return_value=True),
        patch.object(render_qa.subprocess, "run", side_effect=fake_run),
    ):
        attempt = render_qa._render_docx(docx_path, render_dir)

    assert attempt.renderer == "word-com"
    assert attempt.pdf_path == render_dir / "report.pdf"
    assert attempt.reason_code == ""


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
        result = render_qa.run_render_qa(
            docx_path,
            [first, second],
            tmp_path / "render",
            required_asset_ids={1, 2},
        )

    assert result.status == "block"
    assert not result.downloadable
    assert "missing_critical_asset" in result.reason_codes
    assert all(item.suggested_actions for item in result.findings if item.severity == "block")


def test_render_qa_does_not_require_unreferenced_manifest_asset(tmp_path):
    first = _asset(tmp_path)
    unused = _asset(tmp_path, "unused-formula.png", "Formula screenshot")
    unused.kind = "formula"
    docx_path = _docx(tmp_path, [first])
    unavailable = render_qa._RenderAttempt("none", reason_code="renderer_unavailable", message="renderer missing")

    with patch.object(render_qa, "_render_docx", return_value=unavailable):
        result = render_qa.run_render_qa(
            docx_path,
            [first, unused],
            tmp_path / "render",
            required_asset_ids={1},
        )

    assert result.status == "warning"
    assert result.downloadable
    assert "missing_critical_asset" not in result.reason_codes


def test_render_qa_ignores_quarantined_source_crop(tmp_path):
    first = _asset(tmp_path)
    quarantined = _asset(tmp_path, "quarantined.png", "Figure 2: Detail")
    image = Image.open(quarantined.path).convert("RGB")
    pixels = image.load()
    for y in range(35, 330, 20):
        for x in range(0, 100):
            for dy in range(5):
                pixels[x, y + dy] = (10, 10, 10)
    image.save(quarantined.path)
    docx_path = _docx(tmp_path, [first])
    unavailable = render_qa._RenderAttempt("none", reason_code="renderer_unavailable", message="renderer missing")

    with patch.object(render_qa, "_render_docx", return_value=unavailable):
        result = render_qa.run_render_qa(
            docx_path,
            [first, quarantined],
            tmp_path / "render",
            required_asset_ids={1},
            excluded_asset_ids={2},
        )

    assert result.status == "warning"
    assert "visual_crop_invalid" not in result.reason_codes
    assert [asset.asset_id for asset in result.assets] == [1]


def test_render_qa_accepts_readable_wide_formula(tmp_path):
    formula_path = tmp_path / "formula.png"
    Image.new("RGB", (465, 53), "white").save(formula_path)
    formula = PaperAsset("formula", 1, formula_path, "公式 12 截图", text="x = y (12)")
    docx_path = tmp_path / "formula-report.docx"
    _write_docx(
        docx_path,
        "paper.pdf",
        "## 方法主线\n### 关键公式\n公式12描述候选偏移。\n[[ASSET:1]]",
        [formula],
    )
    unavailable = render_qa._RenderAttempt(
        "none",
        reason_code="renderer_unavailable",
        message="renderer missing",
    )

    with patch.object(render_qa, "_render_docx", return_value=unavailable):
        result = render_qa.run_render_qa(
            docx_path,
            [formula],
            tmp_path / "render",
            required_asset_ids={1},
        )

    assert result.status == "warning"
    assert "image_too_small" not in result.reason_codes


def test_render_qa_renderer_timeout_is_warning_not_content_defect(tmp_path):
    asset = _asset(tmp_path)
    docx_path = _docx(tmp_path, [asset])
    timeout = render_qa._RenderAttempt("fixture", reason_code="renderer_timeout", message="timed out")

    with patch.object(render_qa, "_render_docx", return_value=timeout):
        result = render_qa.run_render_qa(docx_path, [asset], tmp_path / "render")

    assert result.status == "warning"
    assert result.reason_codes == ["renderer_timeout"]


def test_render_qa_node_regenerates_docx_and_rechecks_layout_once(tmp_path):
    asset = _asset(tmp_path)
    docx_path = _docx(tmp_path, [asset])
    context = PaperWorkflowContext(
        input_path="paper.pdf",
        output_dir=tmp_path,
        pages=None,
        summary_language="Chinese",
        codex_envs={},
        max_assets=13,
    )
    context.output = tmp_path
    context.source_path = tmp_path / "paper.pdf"
    context.paper_name = "paper"
    context.work_dir = tmp_path / "assets"
    context.docx_path = docx_path
    context.summary = "方法如图所示。\n\n[[ASSET:1]]"
    context.assets = [asset]
    blocked = RenderQAResult(
        "block",
        "fixture",
        None,
        findings=[RenderQAFinding("image_cropped", "block", "too tall", asset_id=1)],
        assets=[RenderAssetMeasurement(1, "figure", asset.caption, 1, document_width_emu=100, document_height_emu=200)],
    )
    warning = RenderQAResult(
        "warning",
        "fixture",
        None,
        findings=[RenderQAFinding("renderer_failed", "warning", "Word COM unavailable")],
        assets=[RenderAssetMeasurement(1, "figure", asset.caption, 1, document_width_emu=100, document_height_emu=150)],
    )

    with patch("paper_agent.paper_summary._run_render_qa", side_effect=[blocked, warning]) as run_qa:
        result = RenderQA().run(context)

    assert run_qa.call_count == 2
    assert result.status == "warning"
    assert context.download_ready
    assert context.repair_attempts["render_qa:layout"] == 1
    assert context.repair_history[-1]["changed"] is True
