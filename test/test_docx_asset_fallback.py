from pathlib import Path

from paper_agent.paper_summary import PaperAsset, _document_xml


def test_table_asset_is_not_duplicated_by_fallback_section():
    assets = [
        PaperAsset("table", 7, Path("table7.png"), "Table 7. Performance comparison", text="Table 7"),
    ]
    summary = (
        "## 关键结果\n"
        "如第7页表格截图所示。\n"
        "[[ASSET:1]]\n"
    )

    xml = _document_xml("paper.pdf", summary, assets, [(Path("table7.png"), "image1.png", "rId4")])

    assert xml.count("image1.png") == 1
    assert xml.count("关键图表") == 0
