from pathlib import Path
from tempfile import TemporaryDirectory

import fitz

from paper_agent.paper_summary import (
    GenerateReport,
    PaperAgentRole,
    PaperAsset,
    PaperWorkflow,
    PaperWorkflowContext,
    PaperWorkflowNode,
    TextLine,
    _asset_display_label,
    _attach_claims_to_grounding_map,
    _build_prompt_patches,
    _build_grounding_map,
    _build_knowledge_graph,
    _caption_is_figure,
    _caption_is_table,
    _correction_memory_context,
    _ensure_asset_markers,
    _enforce_core_original_title,
    _extract_abstract_from_text,
    _extract_verifiable_claims,
    _load_correction_memories,
    _expand_table_rect_to_borders,
    _missing_asset_references,
    _paragraph,
    _prompt_patch_context,
    _parse_verification_result,
    _resolve_codex_config,
    _row_is_prose_after_table,
    _row_looks_table_section_label,
    _row_looks_table_like,
    _sync_inline_asset_references,
    _with_asset_references,
    get_self_improving_prompt_patches,
    record_summary_correction,
    summarize_paper,
)


def line(text: str, x0: float, y0: float, x1: float, y1: float) -> TextLine:
    return TextLine(text=text, rect=fitz.Rect(x0, y0, x1, y1))


class DummyWorkflowNode(PaperWorkflowNode):
    def __init__(self, name: str, depends_on: tuple[str, ...] = ()):
        self.name = name
        self.depends_on = depends_on

    def run(self, context: PaperWorkflowContext) -> None:
        context.chunk_notes.append(self.name)


class FinishWorkflowNode(PaperWorkflowNode):
    name = "Finish"

    def run(self, context: PaperWorkflowContext) -> None:
        context.docx_path = Path("custom-summary.docx")


def workflow_context() -> PaperWorkflowContext:
    return PaperWorkflowContext(
        input_path="paper.pdf",
        output_dir=Path("."),
        pages=None,
        summary_language="中文",
        codex_envs={},
        max_assets=0,
    )


def test_paper_workflow_runs_nodes_by_dependency():
    workflow = PaperWorkflow(
        [
            DummyWorkflowNode("GenerateReport", ("VerifyClaims",)),
            DummyWorkflowNode("VerifyClaims", ("ExtractMethods",)),
            DummyWorkflowNode("ExtractMethods", ("SummarizeContribution",)),
            DummyWorkflowNode("SummarizeContribution", ("ExtractSections",)),
            DummyWorkflowNode("ExtractSections", ("ParsePaper",)),
            DummyWorkflowNode("ParsePaper", ("PreparePaper",)),
            DummyWorkflowNode("PreparePaper"),
        ]
    )
    context = workflow.run(workflow_context())

    assert context.chunk_notes == [
        "PreparePaper",
        "ParsePaper",
        "ExtractSections",
        "SummarizeContribution",
        "ExtractMethods",
        "VerifyClaims",
        "GenerateReport",
    ]
    assert [item["agent"] for item in context.agent_trace] == [PaperAgentRole.EXTRACTOR.value] * 7


def test_default_workflow_declares_multi_agent_roles():
    workflow = PaperWorkflow.default()
    roles = {name: node.agent_role for name, node in workflow.nodes.items()}

    assert roles["PreparePaper"] == PaperAgentRole.READER
    assert roles["ParsePaper"] == PaperAgentRole.READER
    assert roles["ExtractSections"] == PaperAgentRole.EXTRACTOR
    assert roles["SummarizeContribution"] == PaperAgentRole.SYNTHESIZER
    assert roles["ExtractMethods"] == PaperAgentRole.SYNTHESIZER
    assert roles["VerifyClaims"] == PaperAgentRole.CRITIC
    assert roles["GenerateReport"] == PaperAgentRole.SYNTHESIZER


def test_paper_workflow_rejects_cycles():
    workflow = PaperWorkflow(
        [
            DummyWorkflowNode("A", ("B",)),
            DummyWorkflowNode("B", ("A",)),
        ]
    )

    try:
        workflow.run(workflow_context())
    except ValueError as exc:
        assert "cyclic" in str(exc)
    else:
        raise AssertionError("Expected cyclic workflow to fail")


def test_summarize_paper_accepts_custom_workflow():
    result = summarize_paper(
        "paper.pdf",
        Path("."),
        workflow=PaperWorkflow([FinishWorkflowNode()]),
    )

    assert result == "custom-summary.docx"


def test_generate_report_writes_knowledge_graph_sidecar():
    with TemporaryDirectory() as tmp:
        context = workflow_context()
        context.output = Path(tmp)
        context.source_path = Path("paper.pdf")
        context.paper_name = "paper"
        context.summary = "# Test\n\n## 总结\n测试。"
        context.knowledge_graph = {
            "nodes": [{"id": "paper:paper", "label": "Paper", "type": "paper", "source_section": ""}],
            "edges": [],
        }
        context.agent_trace = [{"agent": "Reader", "node": "ParsePaper"}]

        GenerateReport().run(context)

        assert context.docx_path and context.docx_path.exists()
        assert context.knowledge_graph_path and context.knowledge_graph_path.exists()
        graph_text = context.knowledge_graph_path.read_text(encoding="utf-8")
        assert "paper:paper" in graph_text
        assert "agent_trace" in graph_text


def test_correction_memory_records_and_loads_by_paper_id():
    with TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / "corrections.jsonl"
        record_summary_correction(
            "Paper A",
            "把图4写成表2",
            "图表引用必须按原始 caption 类型",
            note="不要让无 caption 图伪装成表格编号",
            category="asset_reference",
            memory_path=memory_path,
        )
        record_summary_correction(
            "Paper B",
            "摘要漏掉右栏",
            "双栏摘要要按阅读顺序拼接",
            memory_path=memory_path,
        )

        memories = _load_correction_memories("Paper A", memory_path=memory_path)
        context = _correction_memory_context(memories)

        assert len(memories) == 1
        assert "图表引用" in context
        assert "无 caption 图" in context


def test_self_improving_prompt_patches_route_feedback_by_target():
    with TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / "corrections.jsonl"
        record_summary_correction(
            "Paper A",
            "公式13解释后又写成公式2所示",
            "公式编号必须保持和原文一致",
            category="verification",
            memory_path=memory_path,
        )
        record_summary_correction(
            "Paper A",
            "双栏摘要只抽取了左栏",
            "分页或双栏摘要要按阅读顺序完整拼接",
            category="extraction",
            memory_path=memory_path,
        )
        record_summary_correction(
            "Paper A",
            "方法主线小标题背景太长",
            "小标题只包裹文字，不要拉满整行",
            category="summarization",
            memory_path=memory_path,
        )

        memories = _load_correction_memories("Paper A", memory_path=memory_path)
        patches = _build_prompt_patches(memories)
        extraction_context = _prompt_patch_context(patches, "extraction")
        summary_context = _prompt_patch_context(patches, "summarization")
        evaluation_context = _prompt_patch_context(patches, "evaluation")
        public_context = get_self_improving_prompt_patches("Paper A", memory_path=memory_path)

        assert "双栏摘要" in extraction_context
        assert "方法主线" in summary_context
        assert "公式13" in evaluation_context
        assert set(public_context) == {"extraction", "summarization", "evaluation"}


def test_verifier_claim_extraction_classifies_method_and_contribution():
    summary = """# Test

## 创新点
论文提出一个新的检索增强训练框架，用于减少多轮工具调用中的失败噪声。

## 方法主线
### 机制流程
模型先生成搜索动作，再根据工具返回结果更新视觉上下文。

## 摘要
这里不应该被抽取为 claim。
"""

    claims = _extract_verifiable_claims(summary)

    assert any(claim["type"] == "contribution" for claim in claims)
    assert any(claim["type"] == "method" for claim in claims)
    assert all("摘要" not in claim["section"] for claim in claims)


def test_grounding_map_extracts_paper_sections():
    paper_text = """[Page 1]
1 Introduction
This paper studies multimodal search agents and explains the motivation for reliable evidence grounding.

2 Method
Our method builds a tool-using agent with retrieval, OCR, and image enhancement actions during training.

3 Experiments
Experiments compare the agent on multimodal QA benchmarks and ablation studies.
"""

    grounding_map = _build_grounding_map(paper_text)

    assert grounding_map["intro"][0]["section_id"] == "1"
    assert grounding_map["method"][0]["section_id"] == "2"
    assert grounding_map["experiments"][0]["section_id"] == "3"


def test_grounding_map_attaches_claim_source_section():
    grounding_map = {
        "intro": [{"section_id": "1", "title": "Introduction", "text": "motivation and background"}],
        "method": [{"section_id": "2", "title": "Method", "text": "retrieval OCR image enhancement actions during training"}],
        "experiments": [{"section_id": "3", "title": "Experiments", "text": "benchmark ablation accuracy"}],
        "claims": [],
    }
    claims = [
        {
            "section": "方法主线",
            "type": "method",
            "claim": "模型使用 retrieval、OCR 和 image enhancement actions 完成训练。",
        }
    ]

    grounded = _attach_claims_to_grounding_map(grounding_map, claims)

    assert grounded["claims"][0]["source_section"] == "2"
    assert grounded["claims"][0]["source_title"] == "Method"


def test_knowledge_graph_extracts_research_nodes_and_edges():
    grounding_map = {
        "intro": [{"section_id": "1", "title": "Introduction", "text": "SWE-Agent studies tool-use for GitHub interaction."}],
        "method": [{"section_id": "2", "title": "Method", "text": "The method uses Transformer self-attention and GRPO training."}],
        "experiments": [{"section_id": "3", "title": "Experiments", "text": "Evaluation uses SWE-Bench benchmark and reports accuracy ablation."}],
        "claims": [],
    }

    graph = _build_knowledge_graph(grounding_map)
    node_types = {node["type"] for node in graph["nodes"]}
    edge_relations = {edge["relation"] for edge in graph["edges"]}

    assert {"concept", "method", "dataset", "evaluation"} <= node_types
    assert "describes_method" in edge_relations
    assert "uses_dataset" in edge_relations
    assert "reports_evaluation" in edge_relations


def test_knowledge_graph_links_claims_to_source_sections():
    grounding_map = {
        "intro": [],
        "method": [{"section_id": "2", "title": "Method", "text": "retrieval OCR image enhancement actions during training"}],
        "experiments": [],
        "claims": [],
    }
    summary = """## 方法主线
模型使用 retrieval、OCR 和 image enhancement actions 完成训练。
"""

    graph = _build_knowledge_graph(grounding_map, summary)

    assert any(node["type"] == "claim" for node in graph["nodes"])
    assert any(edge["relation"] == "grounded_in" and edge["source_section"] == "2" for edge in graph["edges"])


def test_verifier_json_parser_is_conservative():
    passed = _parse_verification_result('{"pass": true, "errors": []}')
    failed = _parse_verification_result('```json\n{"pass": false, "errors": ["新增了原文没有的贡献"]}\n```')
    invalid = _parse_verification_result("not json")

    assert passed.passed
    assert not failed.passed
    assert failed.errors == ["新增了原文没有的贡献"]
    assert not invalid.passed
    assert invalid.errors


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
