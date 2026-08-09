from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from paper_agent.assets.candidates import (
    build_asset_candidate_pool,
    candidate_bboxes_for_asset,
)
from paper_agent.harness.contracts import (
    contract_for_node,
    validate_declared_contract,
    validate_observed_contract,
)
from paper_agent.paper_summary import (
    ExtractMethods,
    ExtractSections,
    GenerateReport,
    ParsePaper,
    PreparePaper,
    ReviseReport,
    SummarizeContribution,
    VerifyClaims,
    _build_asset_candidate_pools,
    _commit_selected_asset_candidates,
)
from paper_agent.schemas.contracts import (
    CandidateStrategy,
    EvidenceBundle,
)


def evidence(kind="table", caption="Table 1. Results", text="Method 1 2\nOurs 3 4"):
    return EvidenceBundle(
        page_number=3,
        source_bbox=(100.0, 200.0, 400.0, 500.0),
        caption_text=caption,
        object_type=kind,
        table_or_formula_text=text,
        image_path=Path("selected.png"),
    )


def test_evidence_bundle_and_candidate_pool_are_immutable():
    item = evidence()
    with pytest.raises(FrozenInstanceError):
        item.page_number = 4

    pool = build_asset_candidate_pool(
        item,
        ((strategy, item.source_bbox, Path(f"{strategy.value}.png")) for strategy in CandidateStrategy),
    )
    with pytest.raises(FrozenInstanceError):
        pool.candidates = ()
    assert pool.selected.strategy is CandidateStrategy.DETECTOR


def test_complete_multiline_table_keeps_all_strategies_and_score_explanation():
    item = evidence(text="Method mAP AP50\nBase 35.1 62.0\nOurs 39.4 68.2")
    geometry = candidate_bboxes_for_asset(
        item.source_bbox,
        caption_bbox=(100.0, 175.0, 400.0, 195.0),
    )
    pool = build_asset_candidate_pool(
        item,
        ((strategy, bbox, Path(f"{strategy.value}.png")) for strategy, bbox in geometry),
        border_closed={CandidateStrategy.BORDER_ENCLOSED: True},
    )
    assert {candidate.strategy for candidate in pool.candidates} == {
        CandidateStrategy.DETECTOR,
        CandidateStrategy.TEXT_HEURISTIC,
        CandidateStrategy.BORDER_ENCLOSED,
        CandidateStrategy.ADJACENT_SPLIT,
    }
    assert pool.selected.strategy is CandidateStrategy.BORDER_ENCLOSED
    assert all(candidate.score.explanation for candidate in pool.candidates)
    assert pool.selected.score.numeric_cell_coverage > 0.7


def test_golden_complete_table_prefers_closed_border_candidate():
    item = evidence(text="Method Score\nBase 0.31\nOurs 0.47")
    geometry = candidate_bboxes_for_asset(
        item.source_bbox,
        caption_bbox=(100.0, 170.0, 400.0, 195.0),
    )
    text_box = next(box for strategy, box in geometry if strategy is CandidateStrategy.TEXT_HEURISTIC)
    assert text_box != item.source_bbox
    pool = build_asset_candidate_pool(
        item,
        ((strategy, box, None) for strategy, box in geometry),
        border_closed={CandidateStrategy.BORDER_ENCLOSED: True},
    )
    assert pool.selected.strategy is CandidateStrategy.BORDER_ENCLOSED


def test_golden_formula_below_caption_is_not_promoted_to_table():
    formula = evidence(
        kind="formula",
        caption="Table 2. Results",
        text="x = y + 1",
    )
    pool = build_asset_candidate_pool(
        formula,
        ((CandidateStrategy.DETECTOR, formula.source_bbox, Path("formula.png")),),
    )
    assert pool.selected.evidence.object_type == "formula"
    assert pool.selected.score.numeric_cell_coverage == 1.0


def test_golden_split_caption_is_one_evidence_bundle():
    split = evidence(caption="Table 1. Main results on the\nbenchmark")
    pool = build_asset_candidate_pool(
        split,
        ((CandidateStrategy.TEXT_HEURISTIC, split.source_bbox, None),),
    )
    assert pool.evidence.caption_text.count("Table 1") == 1
    assert pool.selected.score.caption_identity == 1.0


def test_adjacent_table_and_figure_produce_split_candidate_without_model():
    item = evidence()
    geometry = candidate_bboxes_for_asset(
        item.source_bbox,
        adjacent_bboxes=((390.0, 200.0, 520.0, 500.0),),
    )
    split = next(bbox for strategy, bbox in geometry if strategy is CandidateStrategy.ADJACENT_SPLIT)
    assert split[2] <= 390.0
    pool = build_asset_candidate_pool(
        item,
        ((strategy, bbox, None) for strategy, bbox in geometry),
        object_bboxes=((390.0, 200.0, 520.0, 500.0),),
    )
    assert any(candidate.strategy is CandidateStrategy.ADJACENT_SPLIT for candidate in pool.candidates)
    assert all(candidate.image_path is None or candidate.image_path.name for candidate in pool.candidates)


def test_caption_below_formula_and_split_caption_are_scored_as_formula_evidence():
    item = evidence(
        kind="formula",
        caption="Equation (2). The normalized score",
        text="r = (x - y) / z",
    )
    pool = build_asset_candidate_pool(
        item,
        ((CandidateStrategy.DETECTOR, item.source_bbox, Path("formula.png")),),
    )
    assert pool.selected.score.caption_identity == 1.0
    split_caption = evidence(caption="Table 1. Main results on the\nbenchmark")
    assert build_asset_candidate_pool(
        split_caption,
        ((CandidateStrategy.TEXT_HEURISTIC, split_caption.source_bbox, None),),
    ).selected.score.caption_identity == 1.0


def test_legacy_asset_adapter_retains_candidate_pools_and_selected_image(tmp_path):
    from paper_agent.paper_summary import PaperAsset
    import fitz

    assets = [
        PaperAsset("table", 1, tmp_path / "table.png", "Table 1. Results", "A 1 2\nB 3 4", rect=fitz.Rect(10, 20, 100, 150)),
        PaperAsset("figure", 1, tmp_path / "figure.png", "Figure 1. Overview", rect=fitz.Rect(120, 20, 220, 150)),
    ]
    pools = _build_asset_candidate_pools(assets)
    assert len(pools) == 2
    assert pools[0].selected.image_path == assets[0].path
    assert pools[0].selected.score.explanation.startswith("total=")


def test_pdf_candidate_pool_renders_distinct_bitmaps_for_distinct_geometry(tmp_path):
    import fitz
    from PIL import Image
    from paper_agent.paper_summary import PaperAsset, _asset_bitmap_digest

    pdf_path = tmp_path / "source.pdf"
    document = fitz.open()
    page = document.new_page(width=400, height=300)
    page.draw_rect(fitz.Rect(80, 80, 300, 200), color=(0, 0, 0), width=2)
    page.insert_text((80, 70), "Figure 1. Overview")
    page.insert_text((55, 140), "left evidence")
    document.save(pdf_path)
    document.close()

    image_path = tmp_path / "original.png"
    Image.new("RGB", (400, 300), "white").save(image_path)
    asset = PaperAsset(
        "figure",
        1,
        image_path,
        "Figure 1. Overview",
        rect=fitz.Rect(80, 80, 300, 200),
        caption_rect=fitz.Rect(80, 55, 250, 72),
    )
    pool = _build_asset_candidate_pools(
        [asset],
        source_pdf=pdf_path,
        work_dir=tmp_path,
    )[0]
    paths = {str(candidate.image_path) for candidate in pool.candidates}
    digests = {
        _asset_bitmap_digest(candidate.image_path)
        for candidate in pool.candidates
        if candidate.image_path is not None
    }
    assert len(paths) > 1
    assert len(digests) > 1


def test_selected_candidate_becomes_workflow_asset_incumbent(tmp_path):
    import fitz
    from PIL import Image
    from paper_agent.paper_summary import PaperAsset

    original_path = tmp_path / "original.png"
    selected_path = tmp_path / "selected.png"
    Image.new("RGB", (100, 100), "white").save(original_path)
    Image.new("RGB", (140, 100), "white").save(selected_path)
    asset = PaperAsset(
        "figure",
        2,
        original_path,
        "Figure 1. Overview",
        rect=fitz.Rect(100, 80, 220, 180),
        caption_rect=fitz.Rect(100, 185, 220, 205),
    )
    item = EvidenceBundle(
        page_number=2,
        source_bbox=(100.0, 80.0, 220.0, 180.0),
        caption_text=asset.caption,
        object_type="figure",
        table_or_formula_text="",
        image_path=original_path,
    )
    pool = build_asset_candidate_pool(
        item,
        ((CandidateStrategy.TEXT_HEURISTIC, (70.0, 80.0, 250.0, 205.0), selected_path),),
    )
    committed = _commit_selected_asset_candidates([asset], [pool])
    assert committed[0].path == selected_path
    assert tuple(committed[0].rect) == (70.0, 80.0, 250.0, 180.0)
    assert committed[0].caption_rect == asset.caption_rect


@pytest.mark.parametrize(
    "node_type",
    [
        PreparePaper,
        ParsePaper,
        ExtractSections,
        SummarizeContribution,
        ExtractMethods,
        VerifyClaims,
        ReviseReport,
        GenerateReport,
    ],
)
def test_legacy_nodes_match_audited_typed_contract(node_type):
    node = node_type()
    assert not validate_declared_contract(node), contract_for_node(node.name)
    assert not validate_observed_contract(node), contract_for_node(node.name)
