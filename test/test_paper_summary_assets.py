from pathlib import Path

import fitz

from paper_agent.paper_summary import (
    PaperAsset,
    TextLine,
    _asset_display_label,
    _caption_is_figure,
    _caption_is_table,
    _ensure_asset_markers,
    _enforce_core_original_title,
    _extract_abstract_from_text,
    _expand_table_rect_to_borders,
    _missing_asset_references,
    _paragraph,
    _resolve_codex_config,
    _row_is_prose_after_table,
    _row_looks_table_section_label,
    _row_looks_table_like,
    _sync_inline_asset_references,
    _with_asset_references,
)


def line(text: str, x0: float, y0: float, x1: float, y1: float) -> TextLine:
    return TextLine(text=text, rect=fitz.Rect(x0, y0, x1, y1))


def test_two_column_prose_after_table_is_not_table_row():
    group = [
        line("DocVQA [24] (document question answering),", 60, 210, 260, 222),
        line("ChartQA, demonstrating strong effectiveness on", 320, 210, 550, 222),
    ]
    text = " ".join(item.text for item in group)

    assert not _row_looks_table_like(text, group)
    assert _row_is_prose_after_table(text, group, selected=True)


def test_numeric_multi_cell_row_still_looks_like_table():
    group = [
        line("Qwen3-VL", 60, 150, 120, 162),
        line("72.18", 150, 150, 180, 162),
        line("78.28", 210, 150, 240, 162),
        line("65.75", 280, 150, 310, 162),
    ]
    text = " ".join(item.text for item in group)

    assert _row_looks_table_like(text, group)
    assert not _row_is_prose_after_table(text, group, selected=True)


def test_table_section_label_stays_inside_table():
    group = [line("Closed-source commercial models", 66, 262, 155, 268)]
    text = " ".join(item.text for item in group)

    assert _row_looks_table_section_label(text, group)
    assert not _row_is_prose_after_table(text, group, selected=True)


def test_running_text_reference_is_not_table_caption():
    assert _caption_is_table("Table 1. Comparison of methods")
    assert _caption_is_table("Tab. 2: Video-MME results")
    assert _caption_is_table("Table 2 | Performance on multimodal benchmarks")
    assert not _caption_is_table("Tab. 1 presents a comprehensive comparison")


def test_running_text_reference_is_not_figure_caption():
    assert _caption_is_figure("Figure 3. Training stability analysis")
    assert _caption_is_figure("Fig. 2: Overview")
    assert _caption_is_figure("Fig 4. Ablation results")
    assert _caption_is_figure("Figure 4 | Fatal-aware masking")
    assert not _caption_is_figure("Figure 3 showing the per-seed test mAP distribution.")


def test_abstract_text_removes_footnote_and_joins_hyphenation():
    text = """Abstract
The model addresses text-to-linear-image generation: synthe-
*Work done during internship at Adobe.
sizing a high-quality scene-referred linear image for post-
processing. It represents the result as exposure brackets and uses
flow matching to generate aligned brackets for downstream editing.

1. Introduction
Display-referred images are limited.
"""

    abstract = _extract_abstract_from_text(text)

    assert "internship" not in abstract
    assert "synthesizing" in abstract
    assert "postprocessing" in abstract
    assert "Introduction" not in abstract


def test_core_info_title_uses_original_paper_title():
    summary = """# 中文标题

## 核心信息
- 标题: text-to-linear-image generation
- 中文标题: 线性图像生成
- 机构: Adobe

## 摘要
测试。
"""

    result = _enforce_core_original_title(
        summary,
        "Linear Image Generation by Synthesizing Exposure Brackets",
    )

    assert "- 标题: Linear Image Generation by Synthesizing Exposure Brackets" in result
    assert "text-to-linear-image generation" not in result


def test_heading3_background_only_wraps_text():
    xml = _paragraph("机制流程", "Heading3")
    paragraph_properties = xml.split("</w:pPr>", 1)[0]
    run_properties = xml.split("<w:rPr>", 1)[1].split("</w:rPr>", 1)[0]

    assert "<w:shd" not in paragraph_properties
    assert '<w:shd w:fill="DDEDEA"/>' in run_properties


def test_codex_config_disables_proxy_by_default():
    config = _resolve_codex_config(
        {
            "CODEX_BASE_URL": "https://api.example.test/v1",
            "CODEX_API_KEY": "test-key",
            "CODEX_MODEL": "test-model",
        }
    )
    proxied_config = _resolve_codex_config(
        {
            "CODEX_BASE_URL": "https://api.example.test/v1",
            "CODEX_API_KEY": "test-key",
            "CODEX_MODEL": "test-model",
            "CODEX_USE_PROXY": "true",
        }
    )

    assert not config.use_proxy
    assert proxied_config.use_proxy


def test_unlabeled_table_asset_does_not_get_fake_table_number():
    asset = PaperAsset("table", 11, Path("chart.png"), "Table on page 11", "legend text")
    label = _asset_display_label(2, asset, {"figure": 0, "table": 1, "formula": 0}, {})

    assert label == "第 11 页表格截图"
    assert "表 2" not in label


def test_table_rect_expands_to_zero_height_bottom_border():
    class FakePage:
        rect = fitz.Rect(0, 0, 600, 800)

        def get_drawings(self):
            return [{"rect": fitz.Rect(100, 198.5, 300, 198.5)}]

    rect = fitz.Rect(110, 120, 290, 195)
    expanded = _expand_table_rect_to_borders(FakePage(), rect)

    assert expanded.y1 > rect.y1


def test_formula_reference_keeps_original_paper_number():
    text = (
        "公式 13 给出模态贡献的融合方式：C_m = (1−α) I_intra,m + α I_inter,m，"
        "其中 α ∈ [0,1]。这表示每个模态的最终贡献由两部分决定，如公式13所示。"
    )
    labels = ["公式 2"]

    assert _sync_inline_asset_references(text, labels) == text
    assert _missing_asset_references(text, labels) == []
    assert _with_asset_references(text, labels) == text


def test_formula_marker_is_inserted_before_fallback_figures():
    assets = [
        PaperAsset("figure", 1, Path("figure1.png"), "Figure 1. Overview"),
        PaperAsset("figure", 1, Path("figure2.png"), "Figure 2. Framework"),
        PaperAsset("formula", 1, Path("formula2.png"), "关键公式截图：Y = X (2)", text="Y = X (2)"),
    ]
    summary = "## 方法主线\n公式（2）描述输出结构，如公式2所示。"

    result = _ensure_asset_markers(summary, assets)

    assert result.index("[[ASSET:3]]") < result.index("[[ASSET:1]]")
    assert "[[ASSET:3]]" in result
