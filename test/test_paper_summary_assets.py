from pathlib import Path
from tempfile import TemporaryDirectory

import fitz

from paper_agent.agents.contracts import PaperAgentRole
from paper_agent.harness import PaperWorkflow, PaperWorkflowContext, PaperWorkflowNode
from paper_agent.schemas.evidence import EvidenceMap
from paper_agent.paper_summary import (
    GenerateReport,
    CorrectionMemory,
    PaperAsset,
    TextLine,
    _asset_display_label,
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
    _document_xml,
    _evidence_guard,
    _ensure_asset_markers,
    _enforce_core_original_title,
    _extract_abstract_from_text,
    _fallback_visual_rect_for_caption,
    _extract_verifiable_claims,
    _format_guard,
    _load_correction_memories,
    _expand_table_rect_to_borders,
    _figure_caption_continuation_is_body_text,
    _graphic_region_is_page_artifact,
    _memory_guard,
    _postprocess_summary,
    _missing_asset_references,
    _normalize_final_sections,
    _paragraph,
    _prompt_patch_context,
    _parse_verification_result,
    _verification_format_warning,
    _resolve_codex_config,
    _row_is_prose_after_table,
    _row_looks_table_section_label,
    _row_looks_table_like,
    _sync_inline_asset_references,
    _visual_rect_for_caption,
    _visual_rect_for_caption_direction,
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
        context.summary = "# Test\n\n## 总结\n测试。"
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

    assert "- 标题: Linear Image Generation by Synthesizing Exposure Brackets" in result
    assert "text-to-linear-image generation" not in result


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
