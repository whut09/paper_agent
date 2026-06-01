import fitz

from paper_agent.paper_summary import (
    TextLine,
    _missing_asset_references,
    _row_is_prose_after_table,
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


def test_formula_reference_keeps_original_paper_number():
    text = (
        "公式 13 给出模态贡献的融合方式：C_m = (1−α) I_intra,m + α I_inter,m，"
        "其中 α ∈ [0,1]。这表示每个模态的最终贡献由两部分决定，如公式13所示。"
    )
    labels = ["公式 2"]

    assert _sync_inline_asset_references(text, labels) == text
    assert _missing_asset_references(text, labels) == []
    assert _with_asset_references(text, labels) == text
