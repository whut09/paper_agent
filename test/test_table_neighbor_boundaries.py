from pathlib import Path
from types import SimpleNamespace

import fitz

import paper_agent.paper_summary as ps


def caption_and_lines(page, label):
    lines = ps._page_text_lines(page)
    index = next(i for i, line in enumerate(lines) if line.text.startswith(label))
    _, rect = ps._caption_text_and_rect(lines, index, page, "table")
    return rect, lines


def test_header_rules_in_stacked_tables_are_not_paired_as_outer_borders():
    with fitz.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_text((135, 125), "Table 3: Comparison of operators.")
        page.insert_text((140, 155), "metric")
        page.insert_text((250, 155), "full")
        page.draw_line((135, 165), (480, 165))
        page.insert_text((140, 182), "MME-P")
        page.insert_text((250, 182), "236.6")
        page.insert_text((140, 198), "POPE")
        page.insert_text((250, 198), "42.2")
        page.insert_text((135, 225), "Table 4: Ablation across scales.")
        page.draw_line((135, 255), (480, 255))
        page.insert_text((140, 275), "full parameter")
        page.insert_text((250, 275), "521.0")
        rect, lines = caption_and_lines(page, "Table 3")
        bordered, _ = ps._border_enclosed_table_rect_for_caption(page, rect)
        assert bordered is None
        body, _ = ps._table_rect_for_caption(page, rect, lines)
        assert body is not None
        assert body.y0 < 155 and body.y1 < 215


def test_prose_beside_plot_legend_terminates_table_body():
    with fitz.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_text((135, 125), "Table 2: Linear probe results across datasets.")
        for y, values in [(155, ("dataset", "full", "rank=32")),
                          (175, ("ImageNet", "83.6", "99.8")),
                          (195, ("CUB", "49.9", "61.6"))]:
            for x, value in zip((140, 285, 420), values):
                page.insert_text((x, y), value)
        page.draw_line((138, 160), (480, 160))
        page.insert_text((135, 222), "This asymmetry directly supports")
        page.insert_text((340, 222), "full")
        page.insert_text((385, 222), "rank: 128")
        page.insert_text((450, 222), "rank: 64")
        page.draw_rect(fitz.Rect(320, 235, 480, 370))
        rect, lines = caption_and_lines(page, "Table 2")
        body, text = ps._table_rect_below_caption(page, rect, lines)
        assert body is not None and body.y1 < 215
        assert "CUB" in text and "asymmetry" not in text


def test_candidate_pool_keeps_corrected_table_body_boundary(tmp_path):
    pdf = tmp_path / "source.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_text((135, 125), "Table 2: Linear probe results.")
        page.insert_text((140, 155), "dataset     full     rank=32")
        page.insert_text((140, 175), "CUB          49.9         61.6")
        doc.save(pdf)
    asset = ps.PaperAsset("table", 1, Path(), "Table 2: Linear probe results.",
                          rect=fitz.Rect(138, 140, 480, 185),
                          caption_rect=fitz.Rect(135, 113, 400, 128))
    pool = ps._build_asset_candidate_pool(asset, [asset], source_pdf=pdf, work_dir=tmp_path)
    assert pool is not None
    assert all(candidate.bbox[3] <= 185 for candidate in pool.candidates)


def test_descriptive_method_names_with_scores_remain_table_rows():
    with fitz.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_text((135, 125), "Table 1: Comparison of methods across all benchmark datasets.")
        page.insert_text((140, 155), "Method")
        page.insert_text((400, 155), "Accuracy")
        page.insert_text((140, 175), "Linear Probe")
        page.insert_text((400, 175), "81.2")
        page.insert_text((140, 195), "Training With Frozen Backbone")
        page.insert_text((400, 195), "83.4")
        page.insert_text((135, 222), "4")
        page.insert_text((155, 222), "Ablation Study: Low-rank Vision-Language Alignment")
        rect, lines = caption_and_lines(page, "Table 1")
        body, text = ps._table_rect_below_caption(page, rect, lines)
        assert body is not None and body.y1 >= 195
        assert "Linear Probe" in text and "Training With Frozen Backbone" in text
        assert body.y1 < 215 and "Ablation Study" not in text


def test_table_detector_global_state_stays_locked_through_text_extraction(tmp_path):
    class Table:
        bbox = (100, 100, 400, 200)

        def extract(self):
            assert ps._TABLE_DETECTION_LOCK.locked()
            return [["Method", "Score"], ["Ours", "85.2"]]

    class Page:
        def find_tables(self):
            assert ps._TABLE_DETECTION_LOCK.locked()
            return SimpleNamespace(tables=[Table()])

    regions = ps._detected_table_regions(Page())
    assert len(regions) == 1 and "85.2" in regions[0][1]

    class EmptyPage:
        def find_tables(self):
            assert ps._TABLE_DETECTION_LOCK.locked()
            return SimpleNamespace(tables=[])

    assert ps._capture_tables(EmptyPage(), tmp_path, 1, 13, set()) == []
