from pathlib import Path
from tempfile import TemporaryDirectory

import fitz
from PIL import Image

from paper_agent.agents.contracts import PaperAgentRole
from paper_agent.harness import PaperWorkflow, PaperWorkflowContext, PaperWorkflowNode
from paper_agent.schemas.evidence import EvidenceMap
from paper_agent.paper_summary import (
    GenerateReport,
    CorrectionMemory,
    PaperAsset,
    TextLine,
    _asset_display_label,
    _asset_context,
    _apply_verifier_patch_suggestions,
    _attach_claims_to_grounding_map,
    _build_prompt_patches,
    _build_grounding_map,
    _build_knowledge_graph,
    _caption_is_figure,
    _caption_is_table,
    _caption_text_and_rect,
    _asset_guard,
    _assert_report_ready_for_docx,
    _correction_memory_context,
    _deduplicate_assets,
    _document_xml,
    _evidence_guard,
    _ensure_asset_markers,
    _ensure_chinese_report_title,
    _ensure_required_report_sections,
    _enforce_core_original_title,
    _extract_abstract_from_text,
    _fallback_visual_rect_for_caption,
    _extract_verifiable_claims,
    _format_guard,
    _formula_anchor_score,
    _formula_candidate_is_noise,
    _formula_column_bounds,
    _load_correction_memories,
    _expand_table_rect_to_borders,
    _figure_caption_continuation_is_body_text,
    _graphic_region_is_page_artifact,
    _image_size_emu,
    _is_formula_continuation_line,
    _local_visual_asset_issues,
    _memory_guard,
    _postprocess_summary,
    _missing_asset_references,
    _normalize_final_sections,
    _normalize_inline_text,
    _original_asset_label,
    _paragraph,
    _reflow_markdown_lines,
    _stream_request_allows_partial_content,
    _prompt_patch_context,
    _parse_verification_result,
    _parse_visual_asset_guard_response,
    _verification_format_warning,
    _resolve_codex_config,
    _row_is_prose_after_table,
    _row_looks_table_section_label,
    _row_looks_table_like,
    _report_substance_issues,
    _remove_mismatched_asset_markers,
    _sync_inline_asset_references,
    _suppress_formula_text_when_assets_present,
    _styles_xml,
    _table_rect_for_caption,
    _tighten_table_rect_to_borders,
    _visual_rect_for_caption,
    _visual_rect_for_caption_direction,
    _visual_asset_guard,
    _verification_should_block_report,
    _with_asset_references,
    _run_harness_guards,
    get_self_improving_prompt_patches,
    record_summary_correction,
    summarize_paper,
    VerificationResult,
)


def line(text: str, x0: float, y0: float, x1: float, y1: float) -> TextLine:
    return TextLine(text=text, rect=fitz.Rect(x0, y0, x1, y1))


def _valid_test_summary() -> str:
    return """# Test

## 核心信息
- 标题: Test Paper
- 中文标题: 测试论文
- 领域: 图像恢复

## 摘要
这是一份用于测试 Word 生成链路的完整中文论文报告。报告用足够长的中文自然段描述论文的问题、方法和实验结论，避免被质量门误判为英文原文或残缺报告。该测试文本不追求真实论文结论，只用于验证文档、sidecar 和结构化产物能够稳定写出。

## 背景与问题
图像恢复任务通常需要在低分辨率、噪声、压缩伪影和模糊等退化条件下恢复清晰图像。已有方法如果只针对单一退化训练，遇到真实场景中的混合退化时容易泛化不足。论文类报告需要先说明任务为什么重要，再解释方法针对哪些具体问题给出设计。
对于工程使用者来说，背景章节还要解释输入数据的退化来源、输出质量的评价方式，以及为什么单纯提高模型规模不能自动解决泛化问题。这个测试段落刻意写得更完整，用来覆盖真实报告中背景说明的最低信息量。

## 创新点
测试报告模拟一种面向复杂输入的多阶段方法：先识别输入条件，再选择合适模块处理，最后通过实验比较验证效果。创新点不写成空泛口号，而是说明每个设计环节解决什么问题、为什么影响最终输出质量。

## 一句话总结
这篇测试报告验证 PaperAgent 能生成结构完整、中文内容充足、可写入 Word 的论文精读文档。

## 方法主线
### 机制流程
方法流程可以概括为三步：第一步分析输入样本和任务边界，第二步选择合适的处理模块并组织执行顺序，第三步根据输出质量和实验指标判断改进是否有效。这个过程强调从输入到输出的可解释链路，而不是只给出一个黑盒结论。

### 关键公式
测试文本不插入真实公式，但保留对关键机制的中文解释位置。真实报告中这里应解释论文最重要的公式、变量含义和工程作用，避免只堆公式截图而不说明含义。

## 关键结果
关键结果章节需要概括主要实验现象、对比对象和评价指标。测试报告说明，完整 Word 输出应包含足够中文正文、必要标题、图表引用位置和 sidecar 文件，避免因为缺少章节、正文过短或英文未整理而被质量门拦截。

## 深度分析
深度分析部分关注证据强度、方法假设和边界条件。对于真实论文，应说明哪些结论由实验直接支持，哪些现象仍需要更多数据或消融验证。测试报告在这里提供足够长的中文段落，用来验证生成器不会把空泛模板当作高质量内容。

## 局限
测试报告的局限在于它不是一篇真实论文的科学结论，只能用于覆盖文档生成逻辑。真实报告还需要结合论文原文、图表、公式和实验表格判断方法适用范围、复杂度、数据边界和泛化能力。

## 总结
总体来看，这份测试报告用于验证从 Markdown 到 Word 的完整链路，包括结构检查、正文长度检查、知识图谱 sidecar、verification sidecar 和 trace 文件写入。它强调报告必须包含可读中文内容，不能用模板句或英文原文填充关键章节。
"""


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
    assert [item["status"] for item in context.agent_trace] == ["success"] * 7
    assert set(context.node_results) == {
        "PreparePaper",
        "ParsePaper",
        "ExtractSections",
        "SummarizeContribution",
        "ExtractMethods",
        "VerifyClaims",
        "GenerateReport",
    }


def test_default_workflow_declares_multi_agent_roles():
    workflow = PaperWorkflow.default()
    roles = {name: node.agent_role for name, node in workflow.nodes.items()}

    assert roles["PreparePaper"] == PaperAgentRole.READER
    assert roles["ParsePaper"] == PaperAgentRole.READER
    assert roles["ExtractSections"] == PaperAgentRole.EXTRACTOR
    assert roles["SummarizeContribution"] == PaperAgentRole.SYNTHESIZER
    assert roles["ExtractMethods"] == PaperAgentRole.SYNTHESIZER
    assert roles["VerifyClaims"] == PaperAgentRole.CRITIC
    assert roles["ReviseReport"] == PaperAgentRole.CRITIC
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


def test_summarize_paper_raises_when_verifier_blocks_report():
    class BlockedWorkflow:
        def run(self, context):
            context.output = Path(context.output_dir)
            context.output.mkdir(parents=True, exist_ok=True)
            context.verification_failed_path = context.output / "paper-verification-failed.md"
            context.verification_failed_path.write_text("blocked", encoding="utf-8")
            return context

    with TemporaryDirectory() as tmp:
        try:
            summarize_paper("paper.pdf", Path(tmp), workflow=BlockedWorkflow())
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("Expected blocked verification to raise")

    assert "Verifier Agent 未通过" in message
    assert "verification-failed.md" in message


def test_generate_report_writes_knowledge_graph_sidecar():
    with TemporaryDirectory() as tmp:
        context = workflow_context()
        context.output = Path(tmp)
        context.source_path = Path("paper.pdf")
        context.paper_name = "paper"
        context.summary = _valid_test_summary()
        context.knowledge_graph = {
            "nodes": [{"id": "paper:paper", "label": "Paper", "type": "paper", "source_section": ""}],
            "edges": [],
        }
        context.agent_trace = [{"agent": "Reader", "node": "ParsePaper"}]

        GenerateReport().run(context)

        assert context.docx_path and context.docx_path.exists()
        assert context.trace_path and context.trace_path.exists()
        assert context.grounding_map_path and context.grounding_map_path.exists()
        assert context.verification_path and context.verification_path.exists()
        assert context.knowledge_graph_path and context.knowledge_graph_path.exists()
        trace_text = context.trace_path.read_text(encoding="utf-8")
        grounding_text = context.grounding_map_path.read_text(encoding="utf-8")
        verification_text = context.verification_path.read_text(encoding="utf-8")
        graph_text = context.knowledge_graph_path.read_text(encoding="utf-8")
        assert "run_id" in trace_text
        assert "grounding_map" in grounding_text
        assert "verification" in verification_text
        assert "guards" in verification_text
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
            confidence=0.9,
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
        assert memories[0].scope == "paper"
        assert memories[0].confidence == 0.9
        assert "图表引用" in context
        assert "无 caption 图" in context


def test_memory_guard_warns_about_global_low_confidence_rules():
    memories = [
        CorrectionMemory(
            "global",
            "错误规则",
            "低置信度规则",
            category="asset_reference",
            scope="global",
            confidence=0.2,
        )
    ]
    result = _memory_guard(memories)

    assert result.status == "warning"
    assert result.metrics["global_memory_count"] == 1
    assert result.metrics["low_confidence_count"] == 1


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

    assert isinstance(grounding_map, EvidenceMap)
    assert grounding_map["intro"][0]["section_id"] == "1"
    assert grounding_map["method"][0]["section_id"] == "2"
    assert grounding_map["experiments"][0]["section_id"] == "3"
    assert grounding_map["evidence"][0]["id"].startswith("evidence-intro")


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
    assert grounded["claims"][0]["evidence_ids"]
    assert grounded["claim_groundings"][0]["evidence_ids"] == grounded["claims"][0]["evidence_ids"]


def test_evidence_guard_fails_ungrounded_claims():
    result = _evidence_guard(
        {
            "claims": [
                {"claim": "模型显著提升所有数据集表现", "core": True, "evidence_ids": []},
                {"claim": "方法使用 OCR", "core": True, "evidence_ids": ["evidence-method-2"]},
                {"claim": "非核心背景描述", "core": False, "evidence_ids": []},
            ]
        }
    )

    assert result.status == "failed"
    assert result.metrics["ungrounded_count"] == 1
    assert "evidence_ids" in result.errors[0]


def test_asset_guard_fails_invalid_and_mismatched_assets():
    assets = [PaperAsset("figure", 1, Path("figure.png"), "Figure 2. Trend")]
    invalid = _asset_guard("如表2所示。\n[[ASSET:1]]\n[[ASSET:9]]", assets)

    assert invalid.status == "failed"
    assert any("kind mismatch" in error for error in invalid.errors)
    assert any("not in asset manifest" in error for error in invalid.errors)


def test_asset_guard_ignores_distant_reference_text_for_formula_marker():
    assets = [PaperAsset("formula", 1, Path("formula.png"), "关键公式截图：normalized advantage")]
    summary = """如表2所示，训练配置保持一致。

对 `k` 个 response 的 reward 做归一化，得到 normalized advantage。
[[ASSET:1]]"""

    result = _asset_guard(summary, assets)

    assert result.status == "passed"


def test_asset_guard_accepts_table_reference_with_nearby_figure_position_text():
    assets = [PaperAsset("table", 1, Path("table.png"), "Table 1. Performance comparison")]
    summary = """第1页表格截图位于图1附近，包含 32MB Constraint、Ours/µVLM、RFNet、InceptionNet、ResNet-50。
[[ASSET:1]]"""

    result = _asset_guard(summary, assets)

    assert result.status == "passed"


def test_asset_guard_still_fails_adjacent_reference_kind_mismatch():
    assets = [PaperAsset("formula", 1, Path("formula.png"), "关键公式截图：normalized advantage")]

    result = _asset_guard("如图3所示。\n[[ASSET:1]]", assets)

    assert result.status == "failed"
    assert any("kind mismatch" in error for error in result.errors)


def test_mismatched_formula_marker_is_removed_from_generic_problem_text():
    assets = [PaperAsset("formula", 3, Path("formula.png"), "Formula screenshot")]
    summary = (
        "## 方法主线\n"
        "在问题定义中，论文将复杂图像恢复描述为包含多个 restoration tools 的工具库。\n"
        "[[ASSET:1]]\n"
        "训练部分采用 actor-critic 思路。\n"
    )

    result = _remove_mismatched_asset_markers(summary, assets)

    assert "[[ASSET:1]]" not in result
    assert "actor-critic" in result


def test_harness_guards_report_coverage_warnings():
    guards = _run_harness_guards(
        "# Title\n\n## 摘要\n内容。\n",
        {"intro": [], "method": [{"section_id": "2", "title": "Method", "text": "method"}], "experiments": [], "claims": []},
        [],
        "Title",
        [],
    )
    by_name = {guard.name: guard for guard in guards}

    assert by_name["Coverage Guard"].status == "warning"
    assert any("方法" in warning or "method" in warning for warning in by_name["Coverage Guard"].warnings)


def test_postprocess_strips_model_process_preface_before_report():
    summary = _postprocess_summary(
        "我先把分段笔记去重、纠错并整合成完整 Markdown。\n\n"
        "# 论文精读笔记\n\n"
        "## 核心信息\n正文"
    )

    assert summary.startswith("# 论文精读笔记")
    assert "我先把分段笔记" not in summary


def test_format_guard_blocks_incomplete_report_and_process_preface():
    result = _format_guard(
        "我先把分段笔记去重、纠错并整合成完整 Markdown。\n\n"
        "# 论文精读笔记\n\n"
        "## 方法主线\n如图2所示。\n[[ASSET:1]]"
    )

    assert result.status == "failed"
    assert any("model process preface" in error for error in result.errors)
    assert any("missing required section" in error for error in result.errors)
    assert any("required section is too short: 方法主线" in error for error in result.errors)


def test_format_guard_does_not_mark_parent_section_empty_when_it_has_children():
    result = _format_guard(
        "## 核心信息\n- 标题: Test\n\n"
        "## 摘要\n这是一段足够长的摘要，用来描述论文提出的问题、方法和实验结论，避免被误判为空章节。\n\n"
        "## 背景与问题\n这是一段足够长的背景说明，解释任务为什么存在、已有方法的问题以及本文试图解决的具体痛点，确保章节有信息量。\n\n"
        "## 创新点\n本文围绕方法设计、训练流程和实验验证给出改进，并解释每个改进解决的问题和意义。\n\n"
        "## 一句话总结\n本文解决了一个具体研究问题并给出可验证的方法。\n\n"
        "## 方法主线\n"
        "### 机制流程\n"
        "方法先分析输入退化，再选择专家工具，最后聚合多个阶段的恢复结果，形成可解释的图像复原流程。\n"
        "### 关键公式\n"
        "核心公式用于描述阶段选择和工具调度之间的关系，并约束不同专家输出的融合方式。\n\n"
        "## 关键结果\n实验结果比较了多个基线、约束条件和退化场景，说明方法在主要指标上取得更稳定的表现。\n\n"
        "## 深度分析\n论文证据显示主要收益来自分阶段退化建模和专家工具调度，但证据仍集中在有限任务设置中。\n\n"
        "## 局限\n实验覆盖范围有限，部署复杂度和更多退化组合下的泛化仍需要进一步验证。\n\n"
        "## 总结\n这篇论文给出了一个围绕退化先验和专家调度的图像复原框架，复现时应重点关注阶段划分和工具选择。"
    )

    assert "empty required section: 方法主线" not in result.errors


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


def test_required_report_sections_do_not_fabricate_sparse_draft_content():
    sparse = """# 论文精读笔记

### 1. 方法
模型先对输入进行分解，再通过专家模块和训练流程完成预测，实验部分围绕多个任务设置比较了主要指标。

### 2. 结论
结果表明该方法在论文设定的任务上具有稳定收益，但仍需要结合更多数据和部署条件判断泛化范围。
"""

    result = _ensure_required_report_sections(
        sparse,
        "The paper studies a model that decomposes inputs, uses expert modules, and evaluates the method on multiple task settings.",
        "A Test Paper About Expert Modules",
    )
    guard = _format_guard(result)

    assert "## 核心信息" in result
    assert "## 方法主线" in result
    assert "报告生成时只保留" not in result
    assert any("missing required section" in error for error in guard.errors)


def test_english_heavy_and_generic_sections_are_flagged_before_docx():
    english_abstract = (
        "This paper presents a model that decomposes visual inputs into multiple stages, "
        "uses expert modules for reasoning, and evaluates the approach on several task settings. "
        "The experiments compare the method with baseline systems and discuss limitations in deployment. "
        "The paper also studies ablation settings, robustness behavior, and metric changes across datasets."
    )
    summary = _ensure_required_report_sections(
        f"# Test\n\n## 摘要\n{english_abstract}",
        english_abstract,
        "A Test Paper About Expert Modules",
    )

    issues = _report_substance_issues(
        summary
        + "\n\n## 方法主线\n当前章节采用保守中文概括，后续可结合原文继续细化。"
    )

    joined = "；".join(issues)
    assert "疑似英文原文未整理" in joined
    assert "疑似模板兜底" in joined


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
    structured = _parse_verification_result(
        """{
            "passed": false,
            "hard_failures": [
                {
                    "type": "unsupported_core_claim",
                    "claim": "论文提出了新的数据集 XXX",
                    "reason": "grounding map 中没有数据集 XXX"
                }
            ],
            "soft_warnings": [
                {
                    "type": "weak_evidence",
                    "claim": "方法有较强泛化能力",
                    "reason": "原文只有单数据集实验"
                }
            ],
            "patch_suggestions": [
                {
                    "operation": "delete_claim",
                    "target": "论文提出了新的数据集 XXX"
                }
            ]
        }"""
    )
    invalid = _parse_verification_result("not json")

    assert passed.passed
    assert not failed.passed
    assert failed.errors == ["新增了原文没有的贡献"]
    assert failed.hard_failures[0]["type"] == "legacy_error"
    assert structured.hard_failures[0]["type"] == "unsupported_core_claim"
    assert structured.soft_warnings[0]["type"] == "weak_evidence"
    assert structured.patch_suggestions[0]["operation"] == "delete_claim"
    assert not invalid.passed
    assert invalid.errors


def test_verifier_soft_warnings_do_not_block_report():
    warning_only = _parse_verification_result(
        """{
            "passed": true,
            "hard_failures": [],
            "soft_warnings": [
                {
                    "type": "weak_evidence",
                    "claim": "方法有较强泛化能力",
                    "reason": "原文只有单数据集实验"
                }
            ],
            "patch_suggestions": []
        }"""
    )

    assert warning_only.passed
    assert warning_only.soft_warnings
    assert not _verification_should_block_report(warning_only)


def test_docx_document_xml_omits_verifier_warnings_appendix():
    xml = _document_xml(
        "paper.pdf",
        "# 测试论文\n## 方法主线\n方法描述。",
        [],
        [],
    )

    assert "Verifier Warnings" not in xml
    assert "weak_evidence" not in xml


def test_docx_quality_gate_blocks_incomplete_report():
    incomplete = "## 总结\n这篇论文讨论了图像恢复中的两个问题。"

    try:
        _assert_report_ready_for_docx(incomplete)
    except RuntimeError as exc:
        assert "总结完整性自检未通过" in str(exc)
        assert "缺少必要章节" in str(exc)
        return
    raise AssertionError("incomplete report was not blocked")


def test_verifier_format_error_does_not_block_report():
    invalid_json = VerificationResult(False, ["Verifier Agent 输出不是合法 JSON：missing JSON object"])
    unsupported_claim = VerificationResult(
        False,
        ["新增了原文没有的贡献"],
        hard_failures=[{"type": "unsupported_core_claim", "claim": "claim", "reason": "新增了原文没有的贡献"}],
    )
    mixed_guard_error = VerificationResult(
        False,
        ["Verifier Agent 输出不是合法 JSON：missing JSON object", "Asset Guard: asset id 9 is not in asset manifest"],
        hard_failures=[{"type": "guard_failure", "claim": "", "reason": "Asset Guard: asset id 9 is not in asset manifest"}],
    )

    assert not _verification_should_block_report(invalid_json)
    assert _verification_should_block_report(unsupported_claim)
    assert _verification_should_block_report(mixed_guard_error)


def test_verifier_format_error_is_recorded_as_soft_warning():
    invalid_json = VerificationResult(False, ["Verifier Agent 输出不是合法 JSON：missing JSON object"])

    result = _verification_format_warning(invalid_json)

    assert result.passed
    assert not result.errors
    assert result.soft_warnings[0]["type"] == "verifier_format_warning"
    assert "missing JSON object" in result.soft_warnings[0]["reason"]


def test_verifier_patch_suggestions_apply_one_revision_pass():
    summary = """## 方法主线
论文提出了新的数据集 XXX。
方法使用已有数据集验证。"""
    revised = _apply_verifier_patch_suggestions(
        summary,
        [{"operation": "delete_claim", "target": "论文提出了新的数据集 XXX。"}],
    )

    assert "新的数据集 XXX" not in revised
    assert "已有数据集" in revised


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

    assert "- 原文标题: Linear Image Generation by Synthesizing Exposure Brackets" in result
    assert "text-to-linear-image generation" not in result


def test_core_info_original_title_deduplicates_legacy_title_fields():
    summary = """# 中文标题

## 核心信息
- 标题: Old English Title
- 论文题目: Old English Title
- 中文标题: 旧中文标题
- 作者: Test Author

## 摘要
正文。
"""

    result = _enforce_core_original_title(summary, "Correct Original English Title")

    assert result.count("Correct Original English Title") == 1
    assert "- 原文标题: Correct Original English Title" in result
    assert "- 标题:" not in result
    assert "- 论文题目:" not in result
    assert "- 中文标题: 旧中文标题" in result


def test_english_report_title_gets_chinese_fallback_title():
    summary = """# Self-Evolving Agentic Image Restoration via Deliberate Planning and Intuitive Execution

## 核心信息
- 标题: Self-Evolving Agentic Image Restoration via Deliberate Planning and Intuitive Execution
- 研究任务: 真实世界图像恢复，即从复杂退化观测图像中恢复高质量图像
- 方法名称: SEAR
- 核心思想: 慢速规划与快速记忆执行协同完成工具序列选择

## 摘要
正文。
"""

    result = _ensure_chinese_report_title(summary)

    assert result.startswith("# SEAR：面向真实世界图像恢复的慢速规划与快速记忆执行框架")
    assert "- 中文标题: SEAR：面向真实世界图像恢复的慢速规划与快速记忆执行框架" in result


def test_background_sections_normalize_to_background_and_problem():
    summary = """# Title

## 研究背景
本文讨论长文档理解为什么困难。

## 研究问题
已有方法难以定位证据。
"""

    result = _normalize_final_sections(summary)

    assert result.count("## 背景与问题") == 1
    assert "## 背景与问题" in result
    assert "## 研究背景" not in result
    assert "## 研究问题" not in result


def test_docx_headings_use_legacy_teal_bar_style():
    heading1_xml = _paragraph("摘要", "Heading1")
    heading3_xml = _paragraph("机制流程", "Heading3")
    styles_xml = _styles_xml()
    heading1_properties = heading1_xml.split("</w:pPr>", 1)[0]
    heading3_properties = heading3_xml.split("</w:pPr>", 1)[0]
    heading3_run_properties = heading3_xml.split("<w:rPr>", 1)[1].split("</w:rPr>", 1)[0]

    assert '<w:shd w:val="clear" w:color="auto" w:fill="E7F2F0"/>' in heading1_properties
    assert '<w:pBdr><w:left w:val="single"' in heading1_properties
    assert 'w:color="0F766E"' in heading1_properties
    assert '<w:shd w:val="clear" w:color="auto" w:fill="EAF4F2"/>' in heading3_properties
    assert '<w:color w:val="0F766E"/>' in heading3_run_properties
    assert "2563EB" not in styles_xml
    assert "1D4ED8" not in styles_xml


def test_metadata_uses_legacy_light_teal_fill():
    xml = _paragraph("- 标题: Test", "Metadata")
    paragraph_properties = xml.split("</w:pPr>", 1)[0]

    assert '<w:shd w:val="clear" w:color="auto" w:fill="F2F8F7"/>' in paragraph_properties


def test_latex_text_is_rendered_as_readable_inline_text():
    text = _normalize_inline_text(r"$max(H_I, W_I) \times s \geq 4000$ and $\{2, 4, 8, 16\}$ geq eta")

    assert "\\" not in text
    assert "$" not in text
    assert "≥" in text
    assert "geq" not in text
    assert "η" in text
    assert "×" in text


def test_latex_set_notation_is_rendered_without_escape_garbage():
    text = _normalize_inline_text(r"从候选集 $\{2, 4, 8, 16\}$ 中选择 $s$，使 $Q_I \geq \eta$。")

    assert "$" not in text
    assert "\\" not in text
    assert "geq" not in text
    assert "{2, 4, 8, 16}" not in text
    assert "2, 4, 8, 16" in text
    assert "Q_I ≥ η" in text


def test_section_lists_reflow_to_plain_paragraphs_not_note_cards():
    lines = _reflow_markdown_lines(
        """# Title

## 创新点
- 第一点说明。
- 第二点说明。
"""
    )

    assert not any("[NOTE_CARD]" in line for line in lines)
    assert any("第一点说明。第二点说明。" in line for line in lines)


def test_document_xml_does_not_append_unused_assets_to_tail_section():
    summary = """# Title

## 摘要
这里解释正文。

[[ASSET:1]]
"""
    assets = [
        PaperAsset("figure", 1, Path("used.png"), "Fig. 1 overview"),
        PaperAsset("formula", 2, Path("unused.png"), "关键公式：x = y (271)"),
    ]

    xml = _document_xml(
        "paper.pdf",
        summary,
        assets,
        [(assets[0].path, "image1.png", "rId4"), (assets[1].path, "image2.png", "rId5")],
    )

    assert "关键图表" not in xml
    assert "rId4" in xml
    assert "rId5" not in xml


def test_formula_asset_label_ignores_unreasonable_ocr_numbers():
    asset = PaperAsset("formula", 2, Path("formula.png"), "关键公式：x = y (271)")

    assert _original_asset_label(asset) == ""


def test_partial_stream_content_only_allowed_for_chunk_notes():
    enough_content = "这是已经生成的中文分段笔记。" * 30
    chunk_request = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "请阅读论文第 5/14 段内容，生成分段笔记。"},
        ]
    }
    final_request = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "请整合为完整报告。"},
        ]
    }

    assert _stream_request_allows_partial_content(chunk_request, enough_content)
    assert not _stream_request_allows_partial_content(final_request, enough_content)
    assert not _stream_request_allows_partial_content(chunk_request, "太短")


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


def test_captioned_figure_crop_does_not_cross_previous_table():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_text(self, kind):
            return {"blocks": []}

        def get_drawings(self):
            return [
                {"rect": fitz.Rect(58, 120, 520, 390)},
                {"rect": fitz.Rect(86, 432, 282, 626)},
                {"rect": fitz.Rect(320, 432, 522, 626)},
            ]

    lines = [
        line("RefCOCO", 75, 158, 140, 171),
        line("Ref-L4", 190, 158, 250, 171),
        line("Lisa", 335, 158, 380, 171),
        line("90.62 (+0.17)", 64, 210, 150, 224),
        line("74.54 (+1.28)", 190, 210, 280, 224),
        line("65.86 (+1.99)", 330, 210, 420, 224),
        line("59.44 (+2.94)", 430, 376, 520, 390),
        line("Figure 3. Performance curves of different post-training paradigms", 50, 690, 555, 704),
    ]

    rect = _visual_rect_for_caption(FakePage(), lines[-1].rect, lines)

    assert rect is not None
    assert rect.y0 > 400
    assert rect.y1 < 680


def test_captioned_figure_crop_does_not_absorb_left_column_body():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_text(self, kind):
            return {"blocks": []}

        def get_drawings(self):
            return [
                {"rect": fitz.Rect(48, 180, 285, 440)},
                {"rect": fitz.Rect(405, 175, 548, 260)},
                {"rect": fitz.Rect(393, 278, 548, 392)},
            ]

    lines = [
        line("Over the past decades, researchers have abstracted", 48, 188, 284, 202),
        line("various degradation phenomena into independent IR tasks,", 48, 206, 284, 220),
        line("Figure 1: The five stages of human process of IR", 393, 416, 552, 430),
    ]

    rect = _visual_rect_for_caption(FakePage(), lines[-1].rect, lines)

    assert rect is not None
    assert rect.x0 > 360
    assert rect.x1 < 570


def test_figure_caption_does_not_absorb_following_body_text():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

    lines = [
        line("Figure 5. The impact of hard data ratio on OOD performance.", 62, 530, 520, 545),
        line("To investigate why a small number of hard samples can", 70, 574, 520, 589),
    ]

    text, rect = _caption_text_and_rect(lines, 0, FakePage(), "figure")

    assert "To investigate" not in text
    assert rect.y1 < 560


def test_fallback_figure_crop_trims_body_text_above_plot():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

    caption = line("Figure 5. The impact of hard data ratio on OOD performance.", 62, 530, 520, 545)
    lines = [
        line("We observe that standard SFT, which incorporates only", 48, 120, 520, 136),
        line("but emerges clearly even at low mixing ratios.", 48, 236, 472, 252),
        line("ImageNet-R", 143, 272, 225, 286),
        line("+5% hard data", 168, 330, 258, 344),
        caption,
    ]

    rect = _fallback_visual_rect_for_caption(FakePage(), caption.rect, lines)

    assert rect is not None
    assert rect.y0 > 255
    assert rect.y0 < 285


def test_fallback_figure_crop_does_not_extend_below_caption():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

    caption = line("Fig. 5: Qualitative comparison on the real-world dataset.", 192, 314, 424, 326)
    lines = [
        line("Input", 180, 279, 200, 292),
        line("Noise+Blur", 169, 290, 211, 302),
        caption,
        line("Table 4: Ablation study on the Group B dataset.", 135, 337, 481, 348),
    ]

    rect = _fallback_visual_rect_for_caption(FakePage(), caption.rect, lines)

    assert rect is not None
    assert rect.y1 < caption.rect.y0


def test_fallback_figure_crop_keeps_wide_short_graphic_group():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_text(self, kind):
            assert kind == "dict"
            return {
                "blocks": [
                    {"type": 1, "bbox": (246, 116, 291, 185)},
                    {"type": 1, "bbox": (293, 163, 338, 199)},
                    {"type": 1, "bbox": (340, 212, 385, 249)},
                    {"type": 1, "bbox": (387, 212, 432, 249)},
                    {"type": 1, "bbox": (435, 259, 479, 295)},
                    {"type": 1, "bbox": (135, 212, 245, 280)},
                ]
            }

        def get_drawings(self):
            return []

    caption = line("Fig. 5: Qualitative comparison on the real-world dataset.", 192, 314, 424, 326)
    lines = [
        line("Input", 180, 279, 200, 292),
        line("Noise+Blur", 169, 290, 211, 302),
        caption,
        line("Table 4: Ablation study on the Group B dataset.", 135, 337, 481, 348),
    ]

    rect = _fallback_visual_rect_for_caption(FakePage(), caption.rect, lines)

    assert rect is not None
    assert rect.y0 < 125
    assert rect.y1 < caption.rect.y0
    assert rect.width > 500


def test_captioned_figure_prefers_graphic_region_above_caption_over_body_below():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_text(self, kind):
            assert kind == "dict"
            return {
                "blocks": [
                    {"type": 1, "bbox": (52, 57, 561, 305)},
                    {"type": 1, "bbox": (138, 132, 296, 303)},
                    {"type": 1, "bbox": (304, 144, 478, 303)},
                    {"type": 1, "bbox": (52, 328, 561, 388)},
                ]
            }

        def get_drawings(self):
            return []

    caption = line("Fig. 3: Overview of the SEAR Framework.", 135, 315, 481, 327)
    lines = [
        line("Fast Track", 200, 258, 225, 267),
        line("Deliberate Planner", 260, 276, 288, 293),
        caption,
        line("The method starts with problem formulation.", 135, 394, 481, 404),
    ]

    rect = _visual_rect_for_caption(FakePage(), caption.rect, lines)

    assert rect is not None
    assert rect.y0 < 70
    assert rect.y1 < caption.rect.y0


def test_captioned_figure_crop_trims_front_matter_above_first_page_figure():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_text(self, kind):
            assert kind == "dict"
            return {
                "blocks": [
                    {"type": 1, "bbox": (219, 184, 637, 419)},
                    {"type": 1, "bbox": (318, 232, 342, 250)},
                    {"type": 1, "bbox": (477, 232, 553, 307)},
                ]
            }

        def get_drawings(self):
            return []

    caption = line("Figure 1.", 317, 380, 351, 389)
    lines = [
        line("1Amazon, 2Northeastern University", 145, 176, 487, 190),
        line("jianglinlu@outlook.com, author@example.com", 103, 193, 526, 203),
        line("Input", 352, 237, 366, 243),
        caption,
    ]

    rect = _visual_rect_for_caption(FakePage(), caption.rect, lines)

    assert rect is not None
    assert rect.y0 > 205
    assert rect.y1 < caption.rect.y0


def test_local_visual_asset_guard_blocks_caption_only_figure():
    with TemporaryDirectory() as tmp:
        image_path = Path(tmp) / "caption-only.png"
        Image.new("RGB", (900, 150), "white").save(image_path)
        asset = PaperAsset("figure", 1, image_path, "Fig. 3: Overview")

        issues = _local_visual_asset_issues(1, asset)

    assert any(issue["severity"] == "error" and "text-only" in issue["message"] for issue in issues)


def test_formula_reference_keeps_original_paper_number():
    text = (
        "公式 13 给出模态贡献的融合方式：C_m = (1−α) I_intra,m + α I_inter,m，"
        "其中 α ∈ [0,1]。这表示每个模态的最终贡献由两部分决定，如公式13所示。"
    )
    labels = ["公式 2"]

    assert _sync_inline_asset_references(text, labels) == text
    assert _missing_asset_references(text, labels) == []
    assert _with_asset_references(text, labels) == text


def test_formula_marker_is_not_inserted_without_explicit_numbered_reference():
    assets = [
        PaperAsset("figure", 1, Path("figure1.png"), "Figure 1. Overview"),
        PaperAsset("figure", 1, Path("figure2.png"), "Figure 2. Framework"),
        PaperAsset("formula", 1, Path("formula2.png"), "Formula screenshot (2)", text="Y = X (2)"),
    ]
    summary = "## 方法主线\n相关公式的工程含义是把恢复过程改写为工具选择问题。"

    result = _ensure_asset_markers(summary, assets)

    assert "[[ASSET:3]]" not in result
    assert "[[ASSET:1]]" in result

def test_formula_marker_is_inserted_after_plain_formula_reference():
    assets = [
        PaperAsset("formula", 6, Path("formula1.png"), "Formula screenshot", text="R(I_H) = score (1)"),
    ]
    summary = "## 方法主线\n公式1用于定义终端恢复结果的 hybrid reward。\n"

    result = _ensure_asset_markers(summary, assets)

    assert "公式1用于定义终端恢复结果的 hybrid reward。\n[[ASSET:1]]" in result


def test_formula_text_is_suppressed_when_formula_screenshot_exists():
    assets = [PaperAsset("formula", 3, Path("formula.png"), "关键公式截图：quality score")]
    summary = (
        "## 方法主线\n"
        "### 关键公式\n"
        "公式 1 定义了 4KAgent 自动选择放大倍率的规则：s = min({s ∈ 2, 4, 8, 16 | max(HI, WI) * s ≥ 4000} ∪ {16})。其中 HI 和 WI 是输入图像的高和宽。\n"
        "[[ASSET:1]]\n"
        "公式 2 为 Qs(Ti(Ik−1)) = H(Ti(Ik−1, CI)) + Qnr(Ti(Ik−1))/4，其中 H 表示 HPSv2。\n"
        "公式 4 的核心选择规则可写为：Ik = arg max(T1(Ik−1), T2(Ik−1), ..., TN(Ik−1))，即从候选输出中选择质量分数最高者。\n"
    )

    result = _suppress_formula_text_when_assets_present(summary, assets)

    assert "[[ASSET:1]]" in result
    assert "输入图像的高和宽" in result
    assert "HPSv2" in result
    assert "质量分数最高者" in result
    assert "s = min" not in result
    assert "Qs(" not in result
    assert "Qnr(" not in result
    assert "arg max" not in result
    assert "≥" not in result
    assert "为，其中" not in result
    assert "规则：其中" not in result


def test_formula_text_is_kept_without_formula_screenshot():
    summary = "公式 2 为 Qs(Ti(Ik−1)) = H(Ti(Ik−1, CI)) + Qnr(Ti(Ik−1))/4，其中 H 表示 HPSv2。"

    assert _suppress_formula_text_when_assets_present(summary, []) == summary


def test_formula_suppression_does_not_strip_non_formula_code_spans():
    assets = [PaperAsset("formula", 3, Path("formula.png"), "关键公式截图")]
    summary = "Profile 使用 `ExpSR-s4-F`、`ExpSR-s4-P` 和 `GenSR-s4-P` 三种设置对比。"

    result = _suppress_formula_text_when_assets_present(summary, assets)

    assert "ExpSR-s4-F" in result
    assert "ExpSR-s4-P" in result
    assert "GenSR-s4-P" in result


def test_formula_asset_context_does_not_leak_latex_or_ocr_text():
    assets = [
        PaperAsset(
            "formula",
            2,
            Path("formula.png"),
            "关键公式截图：原文公式本体见截图",
            text="Qs(Ti(Ik−1)) = H(Ti(Ik−1, CI)) + Qnr(Ti(Ik−1))/4",
            latex=r"Q_s(T_i(I_{k-1})) = H(T_i(I_{k-1}, C_I)) + Q_{nr}(T_i(I_{k-1}))/4",
        )
    ]

    context = _asset_context(assets, text_preview_chars=500, latex_preview_chars=800)

    assert "[[ASSET:1]]" in context
    assert "公式截图" in context
    assert "Qs(" not in context
    assert "Q_s" not in context
    assert "TexTeller" not in context


def test_figure_caption_period_stops_before_following_body_text():
    assert _figure_caption_continuation_is_body_text(
        "Fig. 4. Degradation-aware accuracy of MLLMs on Q-Bench.",
        "restoration operations in the subsequent iteration lead to an improvement.",
    )


def test_page_sized_graphic_region_is_ignored_as_artifact():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

    assert _graphic_region_is_page_artifact(FakePage(), fitz.Rect(-120, -70, 696, 443))
    assert not _graphic_region_is_page_artifact(FakePage(), fitz.Rect(48, 44, 562, 372))


def test_tight_above_figure_wins_over_larger_below_body_region():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_text(self, *_args, **_kwargs):
            return {"blocks": []}

        def get_drawings(self):
            return [
                {"rect": fitz.Rect(318, 410, 575, 545)},
                {"rect": fitz.Rect(318, 560, 575, 750)},
            ]

    caption = fitz.Rect(312, 547, 564, 555)
    lines = [TextLine("Fig. 5. The superiority of Q-Agent.", caption)]

    above = _visual_rect_for_caption_direction(FakePage(), caption, lines, "above")
    below = _visual_rect_for_caption_direction(FakePage(), caption, lines, "below")
    selected = _visual_rect_for_caption(FakePage(), caption, lines)

    assert above is not None and below is not None
    assert selected == above


def test_docx_image_sizing_enlarges_formula_and_table_assets():
    with TemporaryDirectory() as tmp:
        formula = Path(tmp) / "formula.png"
        table = Path(tmp) / "table.png"
        Image.new("RGB", (515, 21), "white").save(formula)
        Image.new("RGB", (482, 281), "white").save(table)

        formula_cx, _formula_cy = _image_size_emu(formula, "formula")
        table_cx, _table_cy = _image_size_emu(table, "table")

    assert formula_cx >= int(3.8 * 914400)
    assert table_cx >= int(5.6 * 914400)


def test_docx_image_sizing_does_not_over_enlarge_low_resolution_tables():
    with TemporaryDirectory() as tmp:
        table = Path(tmp) / "tiny-table.png"
        Image.new("RGB", (286, 169), "white").save(table)

        table_cx, _table_cy = _image_size_emu(table, "table")

    assert table_cx < int(4.2 * 914400)


def test_docx_image_sizing_uses_pdf_rect_for_small_tables():
    with TemporaryDirectory() as tmp:
        table = Path(tmp) / "high-dpi-small-table.png"
        Image.new("RGB", (570, 337), "white").save(table)

        table_cx, _table_cy = _image_size_emu(table, "table", fitz.Rect(345, 590, 477, 646))

    assert int(3.7 * 914400) <= table_cx <= int(4.1 * 914400)


def test_local_visual_asset_guard_blocks_low_resolution_table():
    with TemporaryDirectory() as tmp:
        table = Path(tmp) / "tiny-table.png"
        Image.new("RGB", (286, 169), "white").save(table)
        asset = PaperAsset("table", 10, table, "Table 2: Efficiency analysis")

        issues = _local_visual_asset_issues(1, asset)

    assert any("resolution is too low" in issue["message"] for issue in issues)


def test_inline_math_prose_is_not_formula_asset_candidate():
    prose = "c \u2208 C, the planner pi conditions on the concatenated input"
    parameter_sentence = "c = 0.8, eta = 0.55, zeta = 0.7, and distance penalty alpha = 0.05. Experiments"
    standalone_formula = r"\ma t hcal {C} ( \tau _i) = R_{\max}(\tau_i) - \alpha \|\mathbf {f}_q - \mathbf {f}_i\|_2, (2)"

    assert _formula_anchor_score(prose) == 0
    assert _formula_anchor_score(parameter_sentence) == 0
    assert _formula_anchor_score(standalone_formula) > 0
    assert _formula_anchor_score("human-aligned aesthetics [54] and no-reference IQA") == 0
    assert _formula_candidate_is_noise(prose)
    assert not _is_formula_continuation_line("If F(k) != empty and k < Kmax, the planner uses F(k).")


def test_table_rect_tightens_to_detected_horizontal_borders():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_drawings(self):
            return [
                {"rect": fitz.Rect(300, 110, 560, 111)},
                {"rect": fitz.Rect(300, 190, 560, 191)},
            ]

    wide_rect = fitz.Rect(40, 100, 570, 205)

    tightened = _tighten_table_rect_to_borders(FakePage(), wide_rect)

    assert tightened.x0 == 300
    assert tightened.x1 == 560


def test_right_column_table_caption_does_not_absorb_left_column_body_text():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_drawings(self):
            return [
                {"rect": fitz.Rect(345, 596, 478, 597)},
                {"rect": fitz.Rect(345, 646, 478, 647)},
            ]

    lines = [
        line("Efficiency Analysis. Table 2 evaluates ef-", 135, 548, 332, 558),
        line("Table 2: Efficiency analysis on the", 342, 566, 481, 578),
        line("and tool calls per image. SEAR balances fi-", 135, 572, 332, 581),
        line("Group B dataset [64].", 342, 577, 430, 589),
        line("Method PSNR Time Tool Calls", 346, 597, 478, 606),
        line("AgenticIR, SEAR improves PSNR by 0.42", 135, 607, 332, 617),
        line("AgenticIR 21.72 1.09 6.11", 346, 610, 478, 619),
        line("dB with limited runtime and tool overhead.", 135, 619, 332, 629),
        line("4KAgent 21.54 2.55 8.26", 346, 622, 478, 631),
        line("SEAR also outperforms 4KAgent in restora-", 135, 631, 332, 641),
        line("SEAR 22.13 1.98 8.15", 346, 634, 478, 643),
    ]

    caption_text, caption_rect = _caption_text_and_rect(lines, 1, FakePage(), "table")
    table_rect, table_text = _table_rect_for_caption(FakePage(), caption_rect, lines)

    assert "tool calls per image" not in caption_text
    assert "Group B dataset" in caption_text
    assert "improves PSNR" not in table_text
    assert "AgenticIR 21.72" in table_text
    assert table_rect is not None
    assert table_rect.x0 >= 340


def test_table_crop_stops_before_unnumbered_section_heading():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_drawings(self):
            return [
                {"rect": fitz.Rect(40, 470, 610, 471)},
                {"rect": fitz.Rect(40, 586, 610, 587)},
            ]

    lines = [
        line("Table 3: Quantitative comparison on the real-world dataset.", 184, 433, 431, 444),
        line("Method", 42, 456, 100, 468),
        line("PSNR", 140, 456, 180, 468),
        line("SSIM", 215, 456, 255, 468),
        line("LPIPS", 280, 456, 330, 468),
        line("MANIQA", 360, 456, 420, 468),
        line("CLIP-IQA", 455, 456, 525, 468),
        line("MUSIQ", 555, 456, 610, 468),
        line("AirNet [28]", 42, 480, 120, 490),
        line("23.3067", 140, 480, 190, 490),
        line("0.7471", 215, 480, 260, 490),
        line("0.4484", 280, 480, 330, 490),
        line("0.2356", 360, 480, 410, 490),
        line("0.3272", 455, 480, 505, 490),
        line("35.2690", 555, 480, 610, 490),
        line("PromptIR [37]", 42, 500, 120, 510),
        line("23.8647", 140, 500, 190, 510),
        line("0.7542", 215, 500, 260, 510),
        line("0.4341", 280, 500, 330, 510),
        line("0.2405", 360, 500, 410, 510),
        line("0.3270", 455, 500, 505, 510),
        line("35.4811", 555, 500, 610, 510),
        line("4KAgent [66]", 42, 540, 120, 550),
        line("23.6295", 140, 540, 190, 550),
        line("0.7242", 215, 540, 260, 550),
        line("0.3569", 280, 540, 330, 550),
        line("0.3200", 360, 540, 410, 550),
        line("0.4513", 455, 540, 505, 550),
        line("54.4647", 555, 540, 610, 550),
        line("SEAR", 42, 560, 85, 570),
        line("24.4078", 140, 560, 190, 570),
        line("0.7425", 215, 560, 260, 570),
        line("0.3371", 280, 560, 330, 570),
        line("0.3174", 360, 560, 410, 570),
        line("0.4519", 455, 560, 505, 570),
        line("54.6686", 555, 560, 610, 570),
        line("Real-World Generalization", 161, 598, 295, 608),
        line("Evaluation on the real-world dataset demonstrates robustness.", 135, 619, 481, 629),
    ]

    caption_text, caption_rect = _caption_text_and_rect(lines, 0, FakePage(), "table")
    table_rect, table_text = _table_rect_for_caption(FakePage(), caption_rect, lines)

    assert "Real-World Generalization" not in table_text
    assert table_rect is not None
    assert table_rect.y1 < 598


def test_captioned_assets_win_over_generic_table_covering_figure_and_table():
    assets = [
        PaperAsset("table", 12, Path("page-012-captioned-table-01.png"), "Table 4.", rect=fitz.Rect(138, 370, 478, 452)),
        PaperAsset("figure", 12, Path("page-012-captioned-figure-01.png"), "Fig. 5.", rect=fitz.Rect(37, 267, 575, 347)),
        PaperAsset("table", 12, Path("page-012-table-01.png"), "Table 4.", rect=fitz.Rect(35, 260, 580, 704)),
    ]

    result = _deduplicate_assets(assets)

    assert [asset.path.name for asset in result] == [
        "page-012-captioned-table-01.png",
        "page-012-captioned-figure-01.png",
    ]


def test_visual_asset_guard_blocks_model_detected_mixed_crop():
    class FakeMessage:
        content = (
            '{"passed": false, "issues": ['
            '{"severity": "error", "type": "mixed_figure_table", "reason": "table screenshot also contains a separate figure"}'
            "]}"
        )

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **_request):
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    with TemporaryDirectory() as tmp:
        image_path = Path(tmp) / "bad-table.png"
        Image.new("RGB", (640, 360), "white").save(image_path)
        assets = [PaperAsset("table", 1, image_path, "Table 4. Quantitative comparison")]
        result = _visual_asset_guard("[[ASSET:1]]", assets, FakeClient(), "vision-test-model")

    assert result.status == "failed"
    assert any("mixed_figure_table" in error for error in result.errors)


def test_parse_visual_asset_guard_response_normalizes_bad_json_as_warning():
    result = _parse_visual_asset_guard_response("not json")

    assert result["issues"][0]["severity"] == "warning"
    assert result["issues"][0]["type"] == "invalid_visual_guard_json"


def test_formula_column_bounds_allow_cross_column_equation_overhang():
    class FakePage:
        rect = fitz.Rect(0, 0, 612, 792)

    left, right = _formula_column_bounds(FakePage(), fitz.Rect(408, 207, 461, 219))

    assert left < 318
    assert right > 575

