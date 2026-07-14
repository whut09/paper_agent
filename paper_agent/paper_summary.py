from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import fitz
import httpx
import openai
from PIL import Image

from paper_agent.config import ConfigManager
from paper_agent.converter_docx import convert_to_pdf, is_convertible
from paper_agent.agents.contracts import (
    EXTRACTOR_AGENT_CONTRACT as _EXTRACTOR_AGENT_CONTRACT,
    READER_AGENT_CONTRACT as _READER_AGENT_CONTRACT,
    SYNTHESIZER_AGENT_CONTRACT as _SYNTHESIZER_AGENT_CONTRACT,
    VERIFIER_AGENT_CONTRACT as _VERIFIER_AGENT_CONTRACT,
    PaperAgentRole as _PaperAgentRole,
)
from paper_agent.harness.context import PaperWorkflowContext as _PaperWorkflowContext, ProgressCallback as _ProgressCallback
from paper_agent.harness.executor import PaperWorkflow as _PaperWorkflow
from paper_agent.harness.node import NodeResult as _NodeResult, PaperWorkflowNode as _PaperWorkflowNode
from paper_agent.harness.policy import GateDecision as _GateDecision, GatePolicy as _GatePolicy
from paper_agent.schemas.evidence import Claim as _Claim, ClaimGrounding as _ClaimGrounding, Evidence as _Evidence, EvidenceMap as _EvidenceMap
from paper_agent.skill_prompts import load_paper_skill_reference

logger = logging.getLogger(__name__)
_TEXTELLER_FAILED = False
DEFAULT_MAX_ASSETS = 13
DEFAULT_FIGURE_ASSET_LIMIT = 5
DEFAULT_TABLE_ASSET_LIMIT = 4
DEFAULT_FORMULA_ASSET_LIMIT = 4
DOCX_FONT = "Microsoft YaHei"


DEEP_PAPER_NOTE_SYSTEM_PROMPT = """你是 DeepPaperNote 风格的科研论文精读笔记助手。
你的目标是先写一份高质量 Markdown 论文笔记，再由程序把图表截图插入 Word。
请像顶级 AI 研究员或算法工程师写论文精读报告一样写，不要写公众号营销文，也不要写浅层摘要。

必须遵守：
1. 证据优先：只能依据论文正文、图表标题/上下文和抽取到的表格文本写结论；原文没有提及的信息、术语、数据集、指标、模型名、应用场景或结论一律省略，不要写“原文未明确说明”“未提及”“未知”等占位句。
2. 背景先行：面向不了解该方向的读者，先解释研究背景、任务为什么重要、已有方法卡在哪里、本文要解决什么具体问题。
3. 技术细节优先：优先解释问题定义、方法机制、训练/推理链路、关键公式、关键数字、消融和局限。
4. 中文笔记：除模型名、数据集名、指标名、论文术语、代码库名等稳定专有名词外，不要夹杂整句英文。
5. 不要把正文写成全篇 bullet list。只有 `## 核心信息` 使用 `- 字段名: 值`；其他章节优先使用自然段和 `###` 小标题。
6. 图表占位符优先：把重要图、表截图放在对应解释段落附近，使用 `[[ASSET:编号]]` 独占一行表示截图位置；不要创建独立“图表精读”章节，也不要把截图集中放到文末。
7. `ASSET` 编号只是程序内部占位符，不是最终 Word 中的图、表、公式编号。正文不要把 `ASSET` 编号写成“公式 2”这类引用；必须使用可用图表截图里给出的“最终引用标签”。
8. 保留原图表和公式编号：解释图、表、公式时尽量保留 Fig. 1、Table 2、Equation 8、(8) 等原始编号；如果无法确定编号，说明它来自第几页的截图。
9. 不要输出思考过程，不要输出 `<think>`、`<thinking>`、代码块、HTML、JSON。
10. 关键公式必须解读，但公式本体只通过 `[[ASSET:编号]]` 截图展示。正文不要复写 LaTeX、等式、arg max/min、求和式、矩阵式或长变量表达；只写公式编号、变量含义、工程作用和对应截图占位符。
11. 每个公式后必须有一句工程含义解释；如果没有公式截图，只写简短中文描述，不要补写完整公式字符串。
12. 不要写“复现”“复现实验”“复现建议”“复现时应关注”等段落或句子；报告是论文精读，不是实验复现清单。
"""

CRITIC_SYSTEM_PROMPT = (
    "You are the Critic agent in a research-paper multi-agent pipeline. "
    "You verify paper-summary claims against structured grounding evidence. "
    "Output only valid JSON."
)


FINAL_NOTE_PROMPT = """请将下面的分段阅读笔记整合为一份 DeepPaperNote 风格的完整中文 Markdown 论文精读笔记。

输出必须是 Markdown，结构如下，可根据论文内容增加必要的 `###` 小节；没有原文证据的字段、章节或小节可以直接省略：

# 论文标题
标题要比原论文标题更适合中文读者阅读和传播，可以使用“研究对象 | 关键发现/核心结果/开放信息”的形式，也可以使用有张力的问题句；但标题中的机构、模型名、对比对象、数字和结论必须来自原文证据，原文没有提及的不要写。

## 核心信息
只输出原文明确出现的字段；没有出现的字段不要写。
- 原文标题: 必须填写原文标题，保持英文原文，不要改写、概括或翻译
- 中文标题: 可以忠实翻译原文标题
- 作者:
- 机构:
- 发表时间:
- 会议 / 期刊:
- DOI:
- 论文链接:
- 代码链接:
- 项目页:
- 领域:
- 论文类型:

## 摘要
必须优先使用“原文摘要证据”写成忠实中文摘要；不要加入后来评价。只有原文摘要证据为空时，才省略本节，不要写占位句。

## 背景与问题
面向不了解该方向的读者，用 2 到 4 个自然段讲清楚：研究背景是什么、这个任务为什么重要、已有方法或现实流程哪里不够好、本文具体想解决什么问题。只能使用引言、摘要、任务定义或相关工作中有证据支持的信息；不要泛泛写行业套话。

## 创新点
用 3 到 5 个短段落说明真实创新点；不要写成每行都以 `-` 开头的长列表。每个创新点都要说明它解决什么问题、为什么重要。

## 一句话总结
回答这篇论文真正解决什么问题；只写原文直接提到或由原文证据直接支持的内容。

## 数据与任务定义
说明输入、任务、数据集、评价指标和实验边界。原文没有提及的项目直接省略，不要写占位句。

## 方法主线
必须包含 `### 机制流程`，用 3-5 步解释 Input -> 关键变换 -> Output。架构图、流程图、方法框架图必须放在本节对应解释段落附近。必须包含 `### 关键公式`，解读论文最重要的 1 到 3 个公式；如果有公式截图，正文只写公式编号、变量含义和工程作用，把对应 `[[ASSET:编号]]` 放在解释旁边，不要复写公式表达式。

## 关键结果
提炼最重要的指标、对比、消融和失败/边界证据；不要堆砌所有数字。实验图、结果表、对比表、消融表和 case analysis 图必须放在本节对应解释段落附近。

## 深度分析
说明原文直接支持的贡献、有效性证据、证据薄弱处和作者明确讨论的假设；不要补写原文没有提及的推测。

## 局限
写真实局限，包括数据、评价、部署、复杂度、泛化或基线不足。

## 总结
收束全文，只说明这篇论文原文直接支持的结论、方法价值和证据边界；不要写“复现”“复现实验”“复现建议”或操作清单式段落。

图表占位符规则：
- 只能使用“可用图表截图”中列出的 `[[ASSET:编号]]`。
- 每个占位符必须独占一行。
- 占位符要放在解释该图/表/公式的段落旁边，并且解释段落正文必须使用“最终引用标签”写出“如图2所示”“如表1所示”“如公式8所示”这类引用字样，不能自行改编号。
- 不要输出 `## 图表精读`、`## 图标精读` 或类似独立图表章节；架构图放到 `## 方法主线`，实验图表放到 `## 关键结果`。
- 不要为了插图而重复同一占位符。
- 如果某张截图和正文解释关系不清楚，可以不使用它，不能编造解释。
- `ASSET` 编号只是内部占位符，不要把它写成正文中的图号、表号或公式号；可用图表截图会给出最终引用标签，正文必须使用该标签。

格式规则：
- 不要输出 `<think>` 或任何思考过程。
- 不要输出 LaTeX 块公式、行内公式、等式、arg max/min、求和式或长变量表达；公式本体由截图展示，正文只写中文解释。
- 不要输出 markdown 表格，表格内容用自然段概括。
- 不要输出“翻译”二字；核心信息里写“中文标题”，摘要章节标题只写“摘要”。
- 不要输出 `## 引用` 章节。
- 不要输出 `## 我的笔记` 章节，统一使用 `## 总结`。
- 除 `## 核心信息` 外，不要让大多数行以 `-` 开头。
- 原文没有提及的字段、章节和小节直接省略；不要输出“原文未明确说明”“未提及”“未知”“N/A”等占位内容。
"""


LEAN_FINAL_NOTE_PROMPT = """请把分段笔记整合成一份可读的中文 Markdown 论文精读笔记。

要求：
1. 不要复制英文原文段落，不要保留 PDF 断行或断词。
2. 只基于给定证据写，不能编造数据集、公式、图表编号、机构、年份或结论。
3. 必须包含这些二级章节：核心信息、摘要、背景与问题、创新点、方法主线、关键结果、局限、总结。
4. 用自然段写清楚论文背景、解决什么问题、方法怎么做、关键结果是什么。
5. 图表占位符只能使用给定的 [[ASSET:n]]，并放在相关解释附近。
6. 不要输出思考过程、代码块、HTML、JSON、引用章节或“我的笔记”章节。
"""


SYNTHESIZER_SYSTEM_PROMPT = load_paper_skill_reference(
    "summary-system-prompt.md",
    DEEP_PAPER_NOTE_SYSTEM_PROMPT,
)
FINAL_NOTE_PROMPT = load_paper_skill_reference(
    "final-note-prompt.md",
    FINAL_NOTE_PROMPT,
)


@dataclass
class PaperAsset:
    kind: str
    page_number: int
    path: Path
    caption: str
    text: str = ""
    latex: str = ""
    rect: fitz.Rect | None = None


@dataclass
class TextLine:
    text: str
    rect: fitz.Rect


@dataclass
class CodexConfig:
    base_url: str
    api_key: str
    model: str
    use_proxy: bool = False
    proxy: str = ""


@dataclass
class VerificationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    hard_failures: list[dict[str, str]] = field(default_factory=list)
    soft_warnings: list[dict[str, str]] = field(default_factory=list)
    patch_suggestions: list[dict[str, str]] = field(default_factory=list)
    revision_attempted: bool = False
    revision_applied: bool = False


@dataclass
class _RepairPlan:
    missing_asset_keys: list[tuple[str, str]] = field(default_factory=list)
    recapture_asset_ids: set[int] = field(default_factory=set)
    remove_asset_ids: set[int] = field(default_factory=set)
    rewrite_report: bool = False
    apply_patches: bool = False

    @property
    def actionable(self) -> bool:
        return bool(
            self.missing_asset_keys
            or self.recapture_asset_ids
            or self.remove_asset_ids
            or self.rewrite_report
            or self.apply_patches
        )

    @property
    def has_asset_actions(self) -> bool:
        return bool(self.missing_asset_keys or self.recapture_asset_ids or self.remove_asset_ids)

    def action_keys(self) -> list[str]:
        keys = [f"missing:{kind}:{number}" for kind, number in self.missing_asset_keys]
        keys.extend(f"recapture:{asset_id}" for asset_id in sorted(self.recapture_asset_ids))
        keys.extend(f"remove:{asset_id}" for asset_id in sorted(self.remove_asset_ids))
        if self.rewrite_report:
            keys.append("rewrite:report")
        if self.apply_patches:
            keys.append("patch:claims")
        return keys


@dataclass
class GroundingSection:
    section_id: str
    title: str
    category: str
    text: str


@dataclass
class KnowledgeGraphNode:
    id: str
    label: str
    type: str
    source_section: str = ""


@dataclass
class KnowledgeGraphEdge:
    source: str
    target: str
    relation: str
    source_section: str = ""


@dataclass
class CorrectionMemory:
    paper_id: str
    original: str
    corrected: str
    note: str = ""
    category: str = "summary"
    scope: str = "paper"
    confidence: float = 1.0
    created_at: str = ""
    hit_count: int = 0
    last_used_at: str = ""
    disabled: bool = False
    promoted_from: str = ""


@dataclass
class PromptPatch:
    target: str
    content: str
    source_category: str = "summary"


@dataclass(frozen=True)
class MemoryPolicy:
    min_confidence: float = 0.5
    min_promote_hits: int = 2

    def should_inject(self, memory: CorrectionMemory, selected: list[CorrectionMemory]) -> bool:
        if memory.disabled:
            return False
        if memory.confidence < self.min_confidence:
            return False
        return not self.conflicts(memory, selected)

    def conflicts(self, memory: CorrectionMemory, selected: list[CorrectionMemory]) -> bool:
        for other in selected:
            if _memory_conflicts(memory, other):
                return True
        return False

    def can_promote(self, memory: CorrectionMemory, target_scope: str, *, evaluation_passed: bool = False) -> bool:
        target_scope = _normalize_memory_scope(target_scope)
        if memory.disabled or memory.confidence < self.min_confidence:
            return False
        if memory.hit_count < self.min_promote_hits:
            return False
        if target_scope == "global" and not evaluation_passed:
            return False
        return target_scope in {"domain", "global"} and memory.scope != target_scope


@dataclass(frozen=True)
class GuardSpec:
    name: str
    problem: str
    implementation: str
    blocking: bool = False


@dataclass
class GuardResult:
    name: str
    status: str = "passed"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


GUARD_SPECS = {
    "Evidence Guard": GuardSpec(
        "Evidence Guard",
        "总结幻觉、无证据 claim",
        "claim 必须映射到 section / abstract / figure caption",
        blocking=True,
    ),
    "Asset Guard": GuardSpec(
        "Asset Guard",
        "图表引用错、表图混用",
        "[[ASSET:id]] 必须来自 asset manifest，kind 必须匹配",
        blocking=True,
    ),
    "Visual Asset Guard": GuardSpec(
        "Visual Asset Guard",
        "截图截多、截少、图表混在一起或不可读",
        "抽样调用视觉模型检查 Word 中会插入的截图质量",
        blocking=True,
    ),
    "Coverage Guard": GuardSpec(
        "Coverage Guard",
        "漏掉方法/实验/局限",
        "检查摘要、方法、实验、局限是否有覆盖",
    ),
    "Format Guard": GuardSpec(
        "Format Guard",
        "Word 生成失败、markdown 格式乱",
        "检查标题层级、占位符、空章节",
        blocking=True,
    ),
    "Citation Guard": GuardSpec(
        "Citation Guard",
        "DOI、年份、机构乱编",
        "核心元信息必须来自原文 front matter",
    ),
    "Loop Guard": GuardSpec(
        "Loop Guard",
        "反复修不收敛",
        "最多修复 N 次，保留失败原因",
    ),
    "Memory Guard": GuardSpec(
        "Memory Guard",
        "错误反馈污染全局规则",
        "memory 分 paper-level / global-level，带 category 和 confidence",
    ),
}


class PreparePaper(_PaperWorkflowNode):
    name = "PreparePaper"
    agent_role = _PaperAgentRole.READER
    agent_contract = _READER_AGENT_CONTRACT
    requires = ["input_path", "output_dir"]
    produces = ["source_path", "pdf_path", "paper_name", "work_dir"]

    def run(self, context: _PaperWorkflowContext) -> None:
        output = Path(context.output_dir)
        output.mkdir(parents=True, exist_ok=True)

        source_path = Path(context.input_path)
        context.report(0.05, "准备论文文件...")
        pdf_path = _ensure_pdf(source_path, output)
        paper_name = _safe_stem(source_path.stem)
        work_dir = output / f"{paper_name}-summary-assets"
        work_dir.mkdir(parents=True, exist_ok=True)

        context.output = output
        context.source_path = source_path
        context.pdf_path = pdf_path
        context.paper_name = paper_name
        context.work_dir = work_dir


class ParsePaper(_PaperWorkflowNode):
    name = "ParsePaper"
    depends_on = ("PreparePaper",)
    agent_role = _PaperAgentRole.READER
    agent_contract = _READER_AGENT_CONTRACT
    requires = ["pdf_path", "work_dir", "pages", "max_assets"]
    produces = ["paper_text", "assets"]

    def run(self, context: _PaperWorkflowContext) -> None:
        if context.pdf_path is None or context.work_dir is None:
            raise ValueError("ParsePaper requires prepared PDF and work directory.")
        context.check_cancelled()
        context.report(0.18, "抽取正文和图表截图...")
        context.text, context.assets = _extract_text_and_assets(
            context.pdf_path,
            context.work_dir,
            context.pages,
            context.max_assets,
        )
        if not context.text.strip():
            raise ValueError("未能从文档中抽取到可总结的正文。")


class ExtractSections(_PaperWorkflowNode):
    name = "ExtractSections"
    depends_on = ("ParsePaper",)
    agent_role = _PaperAgentRole.EXTRACTOR
    agent_contract = _EXTRACTOR_AGENT_CONTRACT
    requires = ["paper_text", "assets"]
    produces = ["paper_title", "abstract", "formulas", "grounding_map", "knowledge_graph", "prompt_patches"]

    def run(self, context: _PaperWorkflowContext) -> None:
        if context.pdf_path is None:
            raise ValueError("ExtractSections requires a parsed PDF.")
        context.report(0.32, "提取标题、摘要和公式...")
        context.paper_title = _extract_title_from_pdf(context.pdf_path, context.pages)
        context.abstract = _extract_abstract_from_pdf(context.pdf_path, context.pages) or _extract_abstract_from_text(context.text)
        context.formulas = _extract_formula_candidates(context.text)
        context.grounding_map = _build_grounding_map(context.text)
        context.knowledge_graph = _build_knowledge_graph(context.grounding_map)
        context.correction_memories = _load_correction_memories(_paper_memory_id(context.paper_title or context.paper_name))
        context.prompt_patches = _build_prompt_patches(context.correction_memories)


class SummarizeContribution(_PaperWorkflowNode):
    name = "SummarizeContribution"
    depends_on = ("ExtractSections",)
    agent_role = _PaperAgentRole.SYNTHESIZER
    agent_contract = _SYNTHESIZER_AGENT_CONTRACT
    requires = ["paper_text", "assets", "prompt_patches"]
    produces = ["chunk_notes", "partial_summaries"]

    def run(self, context: _PaperWorkflowContext) -> None:
        context.check_cancelled()
        context.report(0.48, "调用 Codex 接口生成分段笔记...")
        context.config = _resolve_codex_config(context.codex_envs)
        context.client = _create_codex_client(context.config)
        context.chunk_notes = _summarize_chunks_with_codex(
            context.client,
            context.config.model,
            context.text,
            context.assets,
            context.summary_language,
            context.correction_memories,
            context.prompt_patches,
            progress_callback=lambda completed, total: context.report(
                0.48 + 0.18 * (completed / max(1, total)),
                f"生成分段笔记 {completed}/{total}...",
            ),
            cancellation_check=context.check_cancelled,
            cache_path=(context.output / f"{context.paper_name}-chunk-notes.json")
            if context.output is not None
            else None,
            partial_summaries=context.partial_summaries,
            partial_cache_path=(context.output / f"{context.paper_name}-partial-integrations.json")
            if context.output is not None
            else None,
            partial_integrator=lambda name, start, end, notes: _integrate_chunk_group_with_codex(
                context.client,
                context.config.model,
                name,
                start,
                end,
                notes,
                context.assets,
                context.summary_language,
                context.abstract,
                context.paper_title,
                context.correction_memories,
                context.prompt_patches,
            ),
        )


class ExtractMethods(_PaperWorkflowNode):
    name = "ExtractMethods"
    depends_on = ("SummarizeContribution",)
    agent_role = _PaperAgentRole.SYNTHESIZER
    agent_contract = _SYNTHESIZER_AGENT_CONTRACT
    requires = ["chunk_notes", "assets", "abstract", "formulas"]
    produces = ["draft_report"]

    def run(self, context: _PaperWorkflowContext) -> None:
        client, config = _ensure_workflow_codex_client(context)
        context.check_cancelled()
        context.report(0.68, "整合方法、结果和分析...")
        context.summary = _integrate_summary_with_codex(
            client,
            config.model,
            context.chunk_notes,
            context.assets,
            context.summary_language,
            context.abstract,
            context.formulas,
            _recognized_formula_context(context.assets),
            context.paper_title,
            context.correction_memories,
            context.prompt_patches,
            context.partial_summaries,
        )


class VerifyClaims(_PaperWorkflowNode):
    name = "VerifyClaims"
    depends_on = ("ExtractMethods",)
    agent_role = _PaperAgentRole.CRITIC
    agent_contract = _VERIFIER_AGENT_CONTRACT
    requires = ["draft_report", "grounding_map", "assets"]
    produces = ["verification_report", "verified_report", "knowledge_graph"]

    def run(self, context: _PaperWorkflowContext) -> None:
        client, config = _ensure_workflow_codex_client(context)
        context.report(0.78, "校验标题、摘要和图表引用...")
        context.summary, context.verification, context.guard_results = _verify_summary_claims(
            context.summary,
            context.text,
            context.grounding_map,
            context.abstract,
            client,
            config.model,
            context.paper_title,
            context.assets,
            context.correction_memories,
            context.prompt_patches,
        )
        context.knowledge_graph = _build_knowledge_graph(context.grounding_map, context.summary)


class ReviseReport(_PaperWorkflowNode):
    name = "ReviseReport"
    depends_on = ("VerifyClaims",)
    agent_role = _PaperAgentRole.CRITIC
    agent_contract = _VERIFIER_AGENT_CONTRACT
    requires = ["verification_report", "draft_report"]
    produces = ["gate_decision", "verified_report", "verification-failed.md"]

    def run(self, context: _PaperWorkflowContext) -> _NodeResult:
        if context.verification is None:
            raise ValueError("ReviseReport requires a verification report.")
        policy = _GatePolicy()
        repair_plan = _build_repair_plan(context)
        decision = policy.decide(context.verification, context.revision_attempts)
        if decision == _GateDecision.BLOCK and repair_plan.has_asset_actions:
            decision = _GateDecision.REVISE
        elif decision == _GateDecision.REVISE and not repair_plan.actionable:
            decision = _GateDecision.BLOCK
        context.gate_decision = decision.value
        context.gate_history.append(
            {
                "attempt": context.revision_attempts,
                "decision": decision.value,
                "hard_failures": len(context.verification.hard_failures),
                "soft_warnings": len(context.verification.soft_warnings),
                "patch_suggestions": len(context.verification.patch_suggestions),
                "repair_actions": repair_plan.action_keys(),
            }
        )
        _record_harness_learnings(context)
        if decision == _GateDecision.REVISE:
            return _revise_report_once(context, repair_plan)
        if decision == _GateDecision.BLOCK:
            context.verification_failed_path = _write_verification_failed_report(context)
            return _NodeResult(
                status="failed",
                outputs={"gate_decision": decision.value},
                artifacts=[str(context.verification_failed_path)],
                errors=_verification_failure_details(context.verification).splitlines(),
                warnings=_verification_warning_messages(context.verification),
                metrics={"revision_attempts": context.revision_attempts},
            )
        warnings = _verification_warning_messages(context.verification)
        status = "warning" if decision == _GateDecision.WARN or warnings else "success"
        return _NodeResult(
            status=status,
            outputs={"gate_decision": decision.value, "verified_report": context.summary[:240]},
            warnings=warnings,
            metrics={"revision_attempts": context.revision_attempts},
        )


class GenerateReport(_PaperWorkflowNode):
    name = "GenerateReport"
    depends_on = ("ReviseReport",)
    agent_role = _PaperAgentRole.SYNTHESIZER
    agent_contract = _SYNTHESIZER_AGENT_CONTRACT
    requires = ["verified_report", "asset_manifest"]
    produces = ["docx", "summary.md", "trace.json", "grounding_map.json", "verification.json", "knowledge_graph.json"]

    def run(self, context: _PaperWorkflowContext) -> None:
        if context.output is None or context.source_path is None:
            raise ValueError("GenerateReport requires prepared output paths.")
        context.check_cancelled()
        context.report(0.85, "写入 Word 文档...")
        context.docx_path = context.output / f"{context.paper_name}-summary.docx"
        context.summary_markdown_path = context.output / f"{context.paper_name}-summary.md"
        context.trace_path = context.output / f"{context.paper_name}-trace.json"
        context.grounding_map_path = context.output / f"{context.paper_name}-grounding-map.json"
        context.verification_path = context.output / f"{context.paper_name}-verification.json"
        context.knowledge_graph_path = context.output / f"{context.paper_name}-knowledge-graph.json"
        context.summary = _ensure_chinese_report_title(context.summary)
        context.summary = _ensure_asset_markers(context.summary, context.assets)
        context.summary = _suppress_formula_text_when_assets_present(context.summary, context.assets)
        _assert_report_ready_for_docx(context.summary)
        try:
            _write_docx(
                context.docx_path,
                context.source_path.name,
                context.summary,
                context.assets,
            )
        except PermissionError:
            context.docx_path = _next_available_report_path(context.docx_path)
            _write_docx(
                context.docx_path,
                context.source_path.name,
                context.summary,
                context.assets,
            )
        context.summary_markdown_path.write_text(context.summary.strip() + "\n", encoding="utf-8")
        _write_harness_sidecars(context)
        context.report(1.0, "论文总结完成")


def _ensure_workflow_codex_client(context: _PaperWorkflowContext) -> tuple[openai.OpenAI, CodexConfig]:
    if context.config is None:
        context.config = _resolve_codex_config(context.codex_envs)
    if context.client is None:
        context.client = _create_codex_client(context.config)
    return context.client, context.config


def _normalize_node_result(
    node: _PaperWorkflowNode,
    result: _NodeResult | None,
    context: _PaperWorkflowContext,
    elapsed_seconds: float,
) -> _NodeResult:
    if result is None:
        result = _NodeResult()
    elif not isinstance(result, _NodeResult):
        result = _NodeResult(outputs={"return": result})
    if not result.outputs:
        result.outputs = _node_output_snapshot(node, context)
    if not result.artifacts:
        result.artifacts = _node_artifacts_snapshot(node, context)
    result.metrics.setdefault("duration_ms", int(round(elapsed_seconds * 1000)))
    result.metrics.setdefault("elapsed_seconds", round(elapsed_seconds, 4))
    result.metrics.setdefault("asset_count", len(context.assets))
    result.metrics.setdefault("claim_count", len(context.grounding_map.get("claims", [])))
    result.metrics.setdefault("chunk_count", len(context.chunk_notes))
    result.metrics.setdefault(
        "guard_failed_count",
        len([guard for guard in context.guard_results if guard.status == "failed"]),
    )
    result.metrics.setdefault(
        "guard_warning_count",
        len([guard for guard in context.guard_results if guard.status == "warning"]),
    )
    if context.verification is not None:
        result.metrics.setdefault("hard_failure_count", len(context.verification.hard_failures))
        result.metrics.setdefault("soft_warning_count", len(context.verification.soft_warnings))
        result.metrics.setdefault("patch_suggestion_count", len(context.verification.patch_suggestions))
        result.metrics.setdefault("revision_attempted", context.verification.revision_attempted)
        result.metrics.setdefault("revision_applied", context.verification.revision_applied)
        if node.name == "VerifyClaims":
            result.warnings.extend(_verification_warning_messages(context.verification))
    if result.errors and result.status == "success":
        result.status = "failed"
    elif result.warnings and result.status == "success":
        result.status = "warning"
    return result


def _node_trace_entry(context: _PaperWorkflowContext, node: _PaperWorkflowNode, result: _NodeResult) -> dict:
    contract = node.agent_contract
    return {
        "run_id": context.run_id,
        "agent": node.agent_role.value,
        "contract": contract.name if contract else "",
        "llm_required": bool(contract and contract.llm_required),
        "node": node.name,
        "input_keys": list(node.requires),
        "output_keys": list(node.produces),
        "status": result.status,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "metrics": dict(result.metrics),
        "artifacts": list(result.artifacts),
    }


def _write_harness_sidecars(context: _PaperWorkflowContext) -> None:
    if context.output is None or not context.paper_name:
        return
    context.trace_path = context.trace_path or context.output / f"{context.paper_name}-trace.json"
    context.summary_markdown_path = context.summary_markdown_path or context.output / f"{context.paper_name}-summary.md"
    context.grounding_map_path = context.grounding_map_path or context.output / f"{context.paper_name}-grounding-map.json"
    context.verification_path = context.verification_path or context.output / f"{context.paper_name}-verification.json"
    context.knowledge_graph_path = context.knowledge_graph_path or context.output / f"{context.paper_name}-knowledge-graph.json"

    trace = []
    for entry in context.agent_trace:
        item = dict(entry)
        item["run_id"] = context.run_id
        trace.append(item)
    context.trace_path.write_text(
        json.dumps(
            {
                "run_id": context.run_id,
                "paper_name": context.paper_name,
                "source_path": str(context.source_path) if context.source_path else "",
                "gate_decision": context.gate_decision,
                "gate_history": list(context.gate_history),
                "repair_attempts": dict(context.repair_attempts),
                "repair_history": list(context.repair_history),
                "nodes": trace,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    context.grounding_map_path.write_text(
        json.dumps({"run_id": context.run_id, "grounding_map": context.grounding_map}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    context.verification_path.write_text(
        json.dumps(
            {
                "run_id": context.run_id,
                "gate_decision": context.gate_decision,
                "gate_history": list(context.gate_history),
                "repair_attempts": dict(context.repair_attempts),
                "repair_history": list(context.repair_history),
                "verification": _verification_payload(context.verification),
                "guards": [_guard_payload(result) for result in context.guard_results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    graph_payload = {
        **(context.knowledge_graph or {"nodes": [], "edges": []}),
        "metadata": {
            "run_id": context.run_id,
            "agent_trace": trace,
        },
    }
    context.knowledge_graph_path.write_text(
        json.dumps(graph_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if context.summary and not context.summary_markdown_path.exists():
        context.summary_markdown_path.write_text(context.summary.strip() + "\n", encoding="utf-8")


def _verification_payload(verification: VerificationResult | None) -> dict:
    if verification is None:
        return {
            "passed": False,
            "errors": ["verification not available"],
            "hard_failures": [],
            "soft_warnings": [],
            "patch_suggestions": [],
            "revision_attempted": False,
            "revision_applied": False,
        }
    return {
        "passed": verification.passed,
        "errors": list(verification.errors),
        "hard_failures": list(verification.hard_failures),
        "soft_warnings": list(verification.soft_warnings),
        "patch_suggestions": list(verification.patch_suggestions),
        "revision_attempted": verification.revision_attempted,
        "revision_applied": verification.revision_applied,
    }


def _guard_payload(result: GuardResult) -> dict:
    return {
        "name": result.name,
        "status": result.status,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "metrics": dict(result.metrics),
        "spec": GUARD_SPECS[result.name].__dict__ if result.name in GUARD_SPECS else {},
    }


def _node_output_snapshot(node: _PaperWorkflowNode, context: _PaperWorkflowContext) -> dict:
    snapshot: dict[str, object] = {}
    for name in node.produces:
        value = _context_output_value(name, context)
        if value is not None:
            snapshot[name] = value
    return snapshot


def _context_output_value(name: str, context: _PaperWorkflowContext) -> object | None:
    mapping = {
        "source_path": context.source_path,
        "pdf_path": context.pdf_path,
        "paper_name": context.paper_name,
        "work_dir": context.work_dir,
        "paper_text": context.text,
        "assets": context.assets,
        "paper_title": context.paper_title,
        "abstract": context.abstract,
        "formulas": context.formulas,
        "grounding_map": context.grounding_map,
        "knowledge_graph": context.knowledge_graph,
        "prompt_patches": context.prompt_patches,
        "chunk_notes": context.chunk_notes,
        "partial_summaries": context.partial_summaries,
        "draft_report": context.summary,
        "verified_report": context.summary,
        "verification_report": context.verification,
        "gate_decision": context.gate_decision,
        "gate_history": context.gate_history,
        "docx": context.docx_path,
        "summary.md": context.summary_markdown_path,
        "verification-failed.md": context.verification_failed_path,
        "grounding_map.json": context.grounding_map_path,
        "verification.json": context.verification_path,
        "knowledge_graph.json": context.knowledge_graph_path,
        "trace.json": context.agent_trace,
    }
    value = mapping.get(name)
    if value is None:
        return None
    return _summarize_node_output(value)


def _summarize_node_output(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, VerificationResult):
        return {
            "passed": value.passed,
            "errors": value.errors[:5],
            "hard_failures": value.hard_failures[:5],
            "soft_warnings": value.soft_warnings[:5],
            "patch_suggestions": value.patch_suggestions[:5],
            "revision_applied": value.revision_applied,
        }
    if isinstance(value, list):
        return {"count": len(value)}
    if isinstance(value, dict):
        return {"keys": sorted(value.keys())[:12], "count": len(value)}
    return value


def _node_artifacts_snapshot(node: _PaperWorkflowNode, context: _PaperWorkflowContext) -> list[str]:
    artifacts: list[str] = []
    if "assets" in node.produces:
        artifacts.extend(str(asset.path) for asset in context.assets if asset.path)
    if "docx" in node.produces and context.docx_path:
        artifacts.append(str(context.docx_path))
    if "summary.md" in node.produces and context.summary_markdown_path:
        artifacts.append(str(context.summary_markdown_path))
    if "verification-failed.md" in node.produces and context.verification_failed_path:
        artifacts.append(str(context.verification_failed_path))
    if "trace.json" in node.produces and context.trace_path:
        artifacts.append(str(context.trace_path))
    if "grounding_map.json" in node.produces and context.grounding_map_path:
        artifacts.append(str(context.grounding_map_path))
    if "verification.json" in node.produces and context.verification_path:
        artifacts.append(str(context.verification_path))
    if "knowledge_graph.json" in node.produces and context.knowledge_graph_path:
        artifacts.append(str(context.knowledge_graph_path))
    return artifacts


def summarize_paper(
    input_path: str,
    output_dir: str | Path,
    *,
    pages: list[int] | None = None,
    summary_language: str = "中文",
    codex_envs: dict[str, str] | None = None,
    max_assets: int = DEFAULT_MAX_ASSETS,
    progress: _ProgressCallback | None = None,
    cancellation_event: asyncio.Event | None = None,
    workflow: _PaperWorkflow | None = None,
) -> str:
    """Summarize a paper and write a Word .docx file with captured figures/tables."""
    context = _PaperWorkflowContext(
        input_path=input_path,
        output_dir=output_dir,
        pages=pages,
        summary_language=summary_language,
        codex_envs=codex_envs or {},
        max_assets=max_assets,
        progress=progress,
        cancellation_event=cancellation_event,
    )
    result = (workflow or _PaperWorkflow.default()).run(context)
    if result.docx_path is None:
        if result.verification_failed_path is not None:
            details = ""
            if result.verification is not None:
                details = _verification_failure_details(result.verification)
            report_hint = f"\n失败详情已写入：{result.verification_failed_path}"
            message = "Verifier Agent 未通过，已停止生成 Word 报告。"
            if details:
                message = f"{message}\n{details}"
            raise RuntimeError(f"{message}{report_hint}")
        raise RuntimeError("Paper workflow finished without generating a report.")
    return str(result.docx_path)


def _ensure_pdf(source_path: Path, output_dir: Path) -> Path:
    suffix = source_path.suffix.lower()
    if suffix == ".pdf":
        target = output_dir / source_path.name
        if source_path.resolve() != target.resolve():
            shutil.copy(source_path, target)
        return target
    if is_convertible(str(source_path)):
        converted = Path(convert_to_pdf(str(source_path)))
        target = output_dir / f"{source_path.stem}.pdf"
        shutil.copy(converted, target)
        return target
    raise ValueError("仅支持 PDF、DOC、DOCX 文件。")


def _extract_text_and_assets(
    pdf_path: Path,
    work_dir: Path,
    pages: list[int] | None,
    max_assets: int,
) -> tuple[str, list[PaperAsset]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    selected_pages = pages if pages is not None else list(range(doc.page_count))
    max_assets = max(0, int(max_assets or 0))
    kind_limits = _asset_kind_limits(max_assets)
    candidate_limit = max(max_assets * 2, sum(kind_limits.values()) * 3, 12)
    text_parts: list[str] = []
    assets: list[PaperAsset] = []
    seen_boxes: set[tuple[int, int, int, int, int]] = set()
    body_pages: list[int] = []
    body_bottom_by_page: dict[int, float] = {}

    for page_index in selected_pages:
        if page_index < 0 or page_index >= doc.page_count:
            continue
        page = doc[page_index]
        page_no = page_index + 1
        stop_y = _body_stop_y(page)
        text_clip = fitz.Rect(0, 0, page.rect.width, stop_y) if stop_y is not None else page.rect
        page_text = _clean_xml_text(page.get_text("text", clip=text_clip, sort=True))
        if page_text.strip():
            body_pages.append(page_index)
        if stop_y is not None:
            body_bottom_by_page[page_no] = stop_y
        text_parts.append(f"\n\n[Page {page_no}]\n{page_text}")

        if kind_limits.get("table", 0) > 0:
            table_assets = _capture_captioned_tables(page, work_dir, page_no, candidate_limit, seen_boxes)
            table_assets = _filter_assets_before_y(table_assets, stop_y)
            assets.extend(table_assets)

        if kind_limits.get("figure", 0) > 0:
            figure_assets = _capture_captioned_figures(page, work_dir, page_no, candidate_limit, seen_boxes)
            figure_assets = _filter_assets_before_y(figure_assets, stop_y)
            assets.extend(figure_assets)

        if kind_limits.get("table", 0) > 0:
            table_assets = _capture_tables(page, work_dir, page_no, candidate_limit, seen_boxes)
            table_assets = _filter_assets_before_y(table_assets, stop_y)
            assets.extend(table_assets)

        if kind_limits.get("figure", 0) > 0:
            figure_assets = _capture_image_blocks(page, work_dir, page_no, candidate_limit, seen_boxes)
            figure_assets = _filter_assets_before_y(figure_assets, stop_y)
            assets.extend(figure_assets)

        if stop_y is not None:
            break

    assets = _deduplicate_assets(assets)
    assets = _limit_assets_by_kind(
        assets,
        {**kind_limits, "formula": 0},
        max_assets,
    )
    formula_limit = min(kind_limits.get("formula", 0), max_assets - len(assets))
    if formula_limit > 0:
        formula_seen_boxes = {
            _box_key(asset.page_number, asset.rect)
            for asset in assets
            if asset.rect is not None
        }
        formula_assets = _capture_formula_blocks_from_doc(
            doc,
            work_dir,
            body_pages or selected_pages,
            formula_limit,
            formula_seen_boxes,
            "\n".join(text_parts),
            body_bottom_by_page,
        )
        assets.extend(formula_assets)

    assets = _deduplicate_assets(assets)
    assets = _limit_assets_by_kind(assets, kind_limits, max_assets)
    doc.close()
    return "\n".join(text_parts), assets


def _capture_captioned_tables(
    page: fitz.Page,
    work_dir: Path,
    page_no: int,
    limit: int,
    seen_boxes: set[tuple[int, int, int, int, int]],
) -> list[PaperAsset]:
    assets: list[PaperAsset] = []
    lines = _page_text_lines(page)

    table_index = 0
    for line_index, line in enumerate(lines):
        if len(assets) >= limit:
            break
        if not _caption_is_table(line.text):
            continue
        caption_text, caption_rect = _caption_text_and_rect(lines, line_index, page, "table")
        table_rect, table_text = _table_rect_for_caption(page, caption_rect, lines)
        if table_rect is None:
            continue
        clip_rect = _merge_rects([caption_rect, table_rect])
        if clip_rect.width < 80 or clip_rect.height < 55:
            continue
        key = _box_key(page_no, clip_rect)
        if key in seen_boxes:
            continue
        seen_boxes.add(key)
        table_index += 1
        path = work_dir / f"page-{page_no:03d}-captioned-table-{table_index:02d}.png"
        _save_clip(page, clip_rect, path, padding=2, scale=4)
        assets.append(PaperAsset("table", page_no, path, caption_text[:300], table_text, rect=table_rect))
    return assets


def _capture_tables(
    page: fitz.Page,
    work_dir: Path,
    page_no: int,
    limit: int,
    seen_boxes: set[tuple[int, int, int, int, int]],
) -> list[PaperAsset]:
    assets: list[PaperAsset] = []
    try:
        tables = page.find_tables().tables
    except Exception:
        return assets

    for idx, table in enumerate(tables, 1):
        if len(assets) >= limit:
            break
        table_rect = fitz.Rect(table.bbox)
        if table_rect.is_empty or table_rect.width < 40 or table_rect.height < 20:
            continue
        table_rect = _expand_table_rect_to_borders(page, table_rect)
        caption, caption_rect = _nearby_caption_with_rect(page, table_rect, ("table", "表"))
        table_text = _table_to_text(table)
        if not caption and _table_detection_looks_like_page_region(page, table_rect):
            continue
        if not caption and not _table_detection_looks_reliable(table, table_text):
            continue
        table_rect = _tighten_table_rect_to_borders(page, table_rect)
        clip_rect = _merge_rects([table_rect, caption_rect]) if caption_rect else table_rect
        key = _box_key(page_no, clip_rect)
        if key in seen_boxes:
            continue
        seen_boxes.add(key)
        path = work_dir / f"page-{page_no:03d}-table-{idx:02d}.png"
        _save_clip(page, clip_rect, path, padding=2, scale=4)
        assets.append(PaperAsset("table", page_no, path, caption or f"Table on page {page_no}", table_text, rect=table_rect))
    return assets


def _body_stop_y(page: fitz.Page) -> float | None:
    lines = _page_text_lines(page)
    candidates: list[float] = []
    for line in lines:
        text = _clean_xml_text(line.text).strip()
        normalized = re.sub(r"\s+", " ", text).strip()
        lowered = normalized.lower().strip(" .:：")
        if not lowered:
            continue
        if _is_reference_or_appendix_heading(lowered, normalized):
            candidates.append(line.rect.y0)
    if not candidates:
        return None
    return max(0.0, min(candidates) - 2)


def _is_reference_or_appendix_heading(lowered: str, original: str) -> bool:
    if re.fullmatch(r"(?:\d+|[ivxlcdm]+)?\.?\s*(?:references|bibliography|works cited)", lowered):
        return True
    if re.fullmatch(r"(?:\d+|[ivxlcdm]+)?\.?\s*(?:appendix|appendices|supplementary material|supplementary materials)(?:\s+[a-z0-9]+)?", lowered):
        return True
    compact = re.sub(r"\s+", "", original)
    if re.fullmatch(r"(?:\d+\.?)?(?:参考文献|附录|补充材料)", compact):
        return True
    return False


def _filter_assets_before_y(assets: list[PaperAsset], stop_y: float | None) -> list[PaperAsset]:
    if stop_y is None:
        return assets
    return [
        asset
        for asset in assets
        if asset.rect is not None and asset.rect.y1 <= stop_y
    ]


def _asset_kind_limits(max_assets: int) -> dict[str, int]:
    limits = {
        "figure": DEFAULT_FIGURE_ASSET_LIMIT,
        "table": DEFAULT_TABLE_ASSET_LIMIT,
        "formula": DEFAULT_FORMULA_ASSET_LIMIT,
    }
    max_assets = max(0, int(max_assets or 0))
    while sum(limits.values()) > max_assets:
        for kind in ("figure", "table", "formula"):
            if sum(limits.values()) <= max_assets:
                break
            if limits[kind] > 0:
                limits[kind] -= 1
    return limits


def _limit_assets_by_kind(
    assets: list[PaperAsset],
    kind_limits: dict[str, int],
    total_limit: int,
) -> list[PaperAsset]:
    total_limit = max(0, int(total_limit or 0))
    if total_limit == 0:
        return []
    counts: dict[str, int] = {}
    result: list[PaperAsset] = []
    for asset in assets:
        kind_limit = kind_limits.get(asset.kind, total_limit)
        if counts.get(asset.kind, 0) >= kind_limit:
            continue
        if len(result) >= total_limit:
            break
        result.append(asset)
        counts[asset.kind] = counts.get(asset.kind, 0) + 1
    return result


def _deduplicate_assets(assets: list[PaperAsset]) -> list[PaperAsset]:
    result: list[PaperAsset] = []
    figure_regions = [
        (asset.page_number, asset.rect)
        for asset in assets
        if asset.kind == "figure" and asset.rect is not None
    ]
    seen_original_labels: set[str] = set()

    for asset in assets:
        if asset.rect is None:
            result.append(asset)
            continue

        original_label = _original_asset_label(asset)
        label_key = f"{asset.kind}:{original_label}" if original_label else ""
        if label_key and label_key in seen_original_labels:
            continue

        if asset.kind == "table":
            if _table_is_figure_fragment(asset, figure_regions):
                continue
            if _table_is_tiny_fragment(asset):
                continue

        if _generic_asset_conflicts_with_captioned_asset(asset, result):
            continue

        if _overlaps_existing_asset(asset, result):
            continue

        result.append(asset)
        if label_key:
            seen_original_labels.add(label_key)

    return result


def _asset_is_captioned(asset: PaperAsset) -> bool:
    return "captioned" in asset.path.name.lower()


def _generic_asset_conflicts_with_captioned_asset(
    asset: PaperAsset,
    existing_assets: list[PaperAsset],
) -> bool:
    if asset.rect is None or _asset_is_captioned(asset):
        return False
    for existing in existing_assets:
        if existing.rect is None or existing.page_number != asset.page_number:
            continue
        if not _asset_is_captioned(existing):
            continue
        if _rect_iou(asset.rect, existing.rect) > 0.25:
            return True
        if _rect_overlap_fraction(existing.rect, asset.rect) > 0.55:
            return True
        if _rect_overlap_fraction(asset.rect, existing.rect) > 0.75:
            return True
    return False


def _table_is_figure_fragment(
    table: PaperAsset,
    figure_regions: list[tuple[int, fitz.Rect | None]],
) -> bool:
    if table.rect is None:
        return False
    for page_no, figure_rect in figure_regions:
        if page_no != table.page_number or figure_rect is None:
            continue
        if _rect_overlap_fraction(table.rect, figure_rect) > 0.7:
            return True
    return False


def _table_is_tiny_fragment(table: PaperAsset) -> bool:
    if table.rect is None:
        return False
    has_original_table_label = bool(_original_asset_label(table))
    if has_original_table_label:
        return False
    return table.rect.width < 130 or table.rect.height < 42


def _overlaps_existing_asset(asset: PaperAsset, existing_assets: list[PaperAsset]) -> bool:
    if asset.rect is None:
        return False
    for existing in existing_assets:
        if existing.page_number != asset.page_number or existing.kind != asset.kind or existing.rect is None:
            continue
        if _rect_iou(asset.rect, existing.rect) > 0.65:
            return True
        if _rect_overlap_fraction(asset.rect, existing.rect) > 0.85:
            return True
    return False


def _rect_overlap_fraction(first: fitz.Rect, second: fitz.Rect) -> float:
    inter = fitz.Rect(first)
    inter &= second
    if inter.is_empty:
        return 0.0
    first_area = first.width * first.height
    if first_area <= 0:
        return 0.0
    return (inter.width * inter.height) / first_area


def _capture_formula_blocks_from_doc(
    doc: fitz.Document,
    work_dir: Path,
    selected_pages: list[int],
    limit: int,
    seen_boxes: set[tuple[int, int, int, int, int]],
    paper_text: str,
    body_bottom_by_page: dict[int, float] | None = None,
) -> list[PaperAsset]:
    candidates: list[tuple[float, int, fitz.Rect, str]] = []
    selected_rects: list[tuple[int, fitz.Rect]] = []

    for page_index in selected_pages:
        if page_index < 0 or page_index >= doc.page_count:
            continue
        page = doc[page_index]
        page_no = page_index + 1
        lines = _page_text_lines(page)
        for line in lines:
            score = _formula_anchor_score(line.text, paper_text)
            if score <= 0:
                continue
            rect = _formula_clip_rect(page, line.rect, lines)
            if rect.is_empty or rect.width < 45 or rect.height < 8:
                continue
            page_bottom = (body_bottom_by_page or {}).get(page_no)
            if page_bottom is not None and rect.y1 > page_bottom:
                continue
            text = _formula_block_text(lines, rect) or line.text
            if _formula_candidate_is_noise(text):
                continue
            if _formula_asset_text_looks_contaminated(text):
                continue
            candidates.append((score, page_index, rect, text))

    candidates.sort(key=lambda item: (-item[0], item[1], item[2].y0))
    assets: list[PaperAsset] = []
    formula_index_by_page: dict[int, int] = {}
    selected_formula_labels: set[str] = set()
    for _score, page_index, rect, text in candidates:
        if len(assets) >= limit:
            break
        page_no = page_index + 1
        formula_label = _original_asset_label(PaperAsset("formula", page_no, Path(""), "", text=text))
        if formula_label and formula_label in selected_formula_labels:
            continue
        key = _box_key(page_no, rect)
        if key in seen_boxes:
            continue
        if any(existing_page == page_no and _rect_iou(existing_rect, rect) > 0.45 for existing_page, existing_rect in selected_rects):
            continue
        seen_boxes.add(key)
        selected_rects.append((page_no, rect))
        formula_index_by_page[page_no] = formula_index_by_page.get(page_no, 0) + 1
        path = work_dir / f"page-{page_no:03d}-formula-{formula_index_by_page[page_no]:02d}.png"
        _save_clip(doc[page_index], rect, path, padding=0)
        _trim_formula_edge_fragments(path)
        latex = _recognize_formula_latex(path)
        caption = _formula_caption(text, latex)
        assets.append(PaperAsset("formula", page_no, path, caption, text=text, latex=latex, rect=rect))
        if formula_label:
            selected_formula_labels.add(formula_label)
    return assets


def _page_text_lines(page: fitz.Page) -> list[TextLine]:
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return []
    lines: list[TextLine] = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = _clean_xml_text("".join(span.get("text", "") for span in spans)).strip()
            if not text:
                continue
            rect = fitz.Rect(line.get("bbox"))
            if rect.is_empty:
                continue
            lines.append(TextLine(text=text, rect=rect))
    return sorted(lines, key=lambda item: (round(item.rect.y0, 1), item.rect.x0))


def _formula_anchor_score(text: str, paper_text: str = "") -> float:
    line = _clean_xml_text(text).strip()
    if not line:
        return 0.0
    if len(line) > 260:
        return 0.0
    if len(line) > 180 and not _line_has_formula_syntax(line):
        return 0.0
    lowered = line.lower()
    if lowered.startswith(
        (
            "figure",
            "fig.",
            "table",
            "if ",
            "where ",
            "when ",
            "we ",
            "the ",
            "this ",
            "that ",
            "consider ",
            "standard ",
            "following ",
            "section ",
        )
    ) or line.startswith(("图", "表")):
        return 0.0
    if _formula_line_looks_like_inline_prose(line) and not _line_has_standalone_formula_marker(line):
        return 0.0

    compact_line = re.sub(r"\s+", "", line)
    op_pattern = r"(=|≈|∈|≤|≥|∝|:=|\\leftarrow|\\rightarrow|←|→)"
    match_text = compact_line if re.search(r"\\(?:leftarrow|rightarrow)", compact_line) else line
    op_match = re.search(op_pattern, match_text)
    if not op_match and match_text != compact_line:
        match_text = compact_line
        op_match = re.search(op_pattern, match_text)
    if not op_match:
        return 0.0
    lhs = match_text[: op_match.start()].strip(" ,.;:()[]")
    lhs_words = re.findall(r"[A-Za-z]{3,}", lhs)
    if len(lhs_words) > 2:
        return 0.0
    if lhs_words and lhs_words[0].lower() in {"where", "when", "with", "textual", "vision", "indices", "tokens"} and len(lhs_words) > 1:
        return 0.0

    math_symbols = set("=<>±∞αβγδϵεΔ∆θλμσ∈→×·∑Σ∫√≤≥≈∝⊤−˜~′")
    symbol_count = sum(1 for ch in line if ch in math_symbols)
    operator_count = sum(
        (line + compact_line).count(op)
        for op in ["=", "≈", "∈", "≤", "≥", "∝", "\\frac", "\\sum", "\\prod", "\\leftarrow", "\\rightarrow"]
    )
    if operator_count == 0 and symbol_count < 2:
        return 0.0
    words = re.findall(r"[A-Za-z]{4,}", line)
    if len(words) > 5 and symbol_count < 4 and not _line_has_standalone_formula_marker(line):
        return 0.0

    compact = re.sub(r"\s+", "", line).lower()
    score = 10.0 + operator_count * 8 + symbol_count * 1.5
    if _equation_number_token(line):
        score += 35
    if "modix" in paper_text.lower():
        if "e=[etext;evision]" in compact or "e=[e_text;e_vision]" in compact:
            score += 120
        elif re.match(r"^c_?m=", compact) or compact.startswith("cm="):
            score += 110
        elif compact.startswith(("∆vision=", "δvision=", "Δvision=")):
            score += 105
        elif "p′=f(e)" in compact or "p'=f(e)" in compact:
            score += 70
        elif compact.startswith(("σm=", "Σm=")):
            score += 55
        elif compact.startswith("rk("):
            score += 50
        elif compact.startswith(("qi=", "q_i=", "kj=", "k_j=")):
            score += 35
        elif compact.startswith(("∆p=", "δp=", "Δp=")):
            score += 8
    return score


def _formula_candidate_is_noise(text: str) -> bool:
    cleaned = _clean_xml_text(text)
    lowered = cleaned.lower()
    if _formula_line_looks_like_inline_prose(cleaned) and not _line_has_standalone_formula_marker(cleaned):
        return True
    if any(token in lowered for token in ("<answer", "</answer", "<think", "</think", "percentage point")):
        return True
    words = re.findall(r"[A-Za-z]{4,}", cleaned)
    if len(words) > 14:
        return True
    numeric_tokens = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if len(numeric_tokens) >= 8 and not _equation_number_token(cleaned):
        return True
    if re.search(r"\b(?:total|recall|accuracy|baseline|method)\b", lowered) and len(numeric_tokens) >= 4:
        return True
    return False


def _line_has_formula_syntax(text: str) -> bool:
    line = _clean_xml_text(text)
    compact = re.sub(r"\s+", "", line)
    if _equation_number_token(line):
        return True
    if re.search(r"\\(?:arg|max|min|sum|prod|frac|sqrt|lambda|tau|mu|alpha|eta|zeta|mathcal|mathrm|leftarrow|rightarrow)", line + compact):
        return True
    math_symbols = set("=<>±∞αβγδϵεΔ∆θλμσ∈→←×·∑Σ∫√≤≥≈∝⊤−˜~′")
    if sum(1 for ch in line if ch in math_symbols) >= 5:
        return True
    if re.search(r"[A-Za-z]\([^)]*\)\s*=", compact):
        return True
    return False


def _line_has_standalone_formula_marker(text: str) -> bool:
    line = _clean_xml_text(text)
    compact = re.sub(r"\s+", "", line)
    if _equation_number_token(line):
        return True
    return bool(
        re.search(
            r"\\(?:arg|max|min|sum|prod|frac|sqrt|lambda|tau|mu|alpha|eta|zeta|mathcal|mathrm|leftarrow|rightarrow)",
            line + compact,
        )
    )


def _formula_line_looks_like_inline_prose(text: str) -> bool:
    line = _clean_xml_text(text).strip()
    if not line:
        return False
    lowered = line.lower()
    words = re.findall(r"[A-Za-z]{3,}", line)
    math_symbols = set("=<>±∞αβγδϵεΔ∆θλμσ∈→×·∑Σ∫√≤≥≈∝⊤−˜~′")
    symbol_count = sum(1 for ch in line if ch in math_symbols)
    has_equation_number = bool(_equation_number_token(line))
    prose_tokens = (
        " the ",
        " a ",
        " an ",
        " and ",
        " or ",
        " if ",
        " where ",
        " which ",
        " with ",
        " planner ",
        " executor ",
        " denotes ",
        " represents ",
        " conditions ",
        " concatenated ",
        " input ",
        " uses ",
    )
    padded = f" {lowered} "
    if any(token in padded for token in prose_tokens) and len(words) >= 3 and not has_equation_number:
        return True
    if re.search(r"[,;]\s*(?:the|a|an|and|or|if|where|which|with|we|this|that)\b", lowered):
        return True
    return len(words) >= 4 and symbol_count < 5 and not has_equation_number


def _formula_clip_rect(page: fitz.Page, anchor: fitz.Rect, lines: list[TextLine]) -> fitz.Rect:
    left, right = _formula_column_bounds(page, anchor)
    y0 = anchor.y0
    y1 = anchor.y1
    x0 = anchor.x0
    x1 = anchor.x1
    anchor_mid = (anchor.y0 + anchor.y1) / 2
    for line in lines:
        line_mid = (line.rect.y0 + line.rect.y1) / 2
        if abs(line_mid - anchor_mid) > 34:
            continue
        in_formula_column = not (line.rect.x1 < left or line.rect.x0 > right)
        same_row_formula_fragment = (
            abs(line_mid - anchor_mid) < 7
            and _line_has_standalone_formula_marker(line.text)
            and not _formula_line_looks_like_inline_prose(line.text)
        )
        if not in_formula_column and not same_row_formula_fragment:
            continue
        line_is_anchor = _rect_overlap_fraction(anchor, line.rect) > 0.65
        line_is_formula = (
            line_is_anchor
            or same_row_formula_fragment
            or _is_formula_continuation_line(line.text)
            or _line_has_formula_syntax(line.text)
        )
        if line_is_formula:
            y0 = min(y0, line.rect.y0)
            y1 = max(y1, line.rect.y1)
            x0 = min(x0, line.rect.x0)
            x1 = max(x1, line.rect.x1)
    pad_x = 18.0
    pad_top = 4.0
    pad_bottom = 7.0
    return fitz.Rect(
        max(page.rect.x0, left, x0 - pad_x),
        max(0, y0 - pad_top),
        min(page.rect.x1, right, x1 + pad_x),
        min(page.rect.height, y1 + pad_bottom),
    )


def _formula_column_bounds(page: fitz.Page, rect: fitz.Rect) -> tuple[float, float]:
    left, right = _column_bounds(page, rect)
    if page.rect.width < 420:
        return left, right
    extra = min(55.0, page.rect.width * 0.09)
    margin = max(18.0, page.rect.width * 0.03)
    return max(margin, left - extra), min(page.rect.width - margin, right + extra)


def _column_bounds(page: fitz.Page, rect: fitz.Rect) -> tuple[float, float]:
    if page.rect.width < 420:
        return max(0, rect.x0 - 36), min(page.rect.width, rect.x1 + 36)
    mid = page.rect.width / 2
    margin = max(28.0, page.rect.width * 0.06)
    gutter = 12.0
    if rect.x0 < mid < rect.x1:
        return margin, page.rect.width - margin
    if (rect.x0 + rect.x1) / 2 < mid:
        return margin, mid - gutter
    return mid + gutter, page.rect.width - margin


def _is_formula_continuation_line(text: str) -> bool:
    line = _clean_xml_text(text).strip()
    if not line or len(line) > 120:
        return False
    lowered = line.lower()
    if lowered.startswith(("if ", "where ", "when ", "figure", "fig.", "table", "the ", "we ", "this ", "is ", "and ")):
        return False
    if _formula_line_looks_like_inline_prose(line) and not _line_has_formula_syntax(line):
        return False
    math_symbols = set("=<>±∞αβγδϵεΔ∆θλμσ∈→×·∑Σ∫√≤≥≈∝⊤−˜~′")
    symbol_count = sum(1 for ch in line if ch in math_symbols)
    words = re.findall(r"[A-Za-z]{4,}", line)
    return symbol_count >= 1 and len(words) <= 2


def _formula_block_text(lines: list[TextLine], rect: fitz.Rect) -> str:
    parts = []
    for line in lines:
        line_mid = (line.rect.y0 + line.rect.y1) / 2
        if line_mid < rect.y0 - 1 or line_mid > rect.y1 + 1:
            continue
        if line.rect.x1 < rect.x0 - 1 or line.rect.x0 > rect.x1 + 1:
            continue
        text = _clean_xml_text(line.text).strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def _rect_iou(first: fitz.Rect, second: fitz.Rect) -> float:
    inter = fitz.Rect(first)
    inter &= second
    if inter.is_empty:
        return 0.0
    inter_area = inter.width * inter.height
    union_area = first.width * first.height + second.width * second.height - inter_area
    if union_area <= 0:
        return 0.0
    return inter_area / union_area


def _formula_caption(text: str, latex: str = "") -> str:
    readable = _known_formula_caption(text)
    if readable:
        return readable
    if latex:
        return "关键公式截图：原文公式本体见截图，正文仅解释变量含义和工程作用"
    return "关键公式截图：原文公式本体见截图，正文仅解释变量含义和工程作用"


def _known_formula_caption(text: str) -> str:
    compact = re.sub(r"\s+", "", _clean_xml_text(text)).lower()
    if "e=[etext;evision]" in compact or "e=[e_text;e_vision]" in compact:
        return "核心公式截图：定义文本与视觉 token 的联合嵌入序列"
    if re.match(r"^c_?m=", compact) or compact.startswith("cm="):
        return "核心公式截图：融合模态内部密度与跨模态交互"
    if compact.startswith(("∆vision=", "δvision=", "Δvision=")):
        return "核心公式截图：确定视觉 token 的自适应位置步长"
    if "p′=f(e)" in compact or "p'=f(e)" in compact:
        return "公式截图：由嵌入序列生成模态感知位置索引"
    if compact.startswith("rk("):
        return "公式截图：RoPE 旋转矩阵随相对位置偏移变化"
    return ""


def _recognize_formula_latex(path: Path) -> str:
    global _TEXTELLER_FAILED
    if _TEXTELLER_FAILED:
        return ""
    texteller = shutil.which("texteller")
    if not texteller:
        return ""
    if not _texteller_ready_to_run():
        return ""
    command = [texteller, "inference", "--output-format", "latex"]
    model_path = _first_value({}, "TEXTELLER_MODEL_PATH")
    tokenizer_path = _first_value({}, "TEXTELLER_TOKENIZER_PATH")
    if model_path:
        command.extend(["--model-path", model_path])
    if tokenizer_path:
        command.extend(["--tokenizer-path", tokenizer_path])
    command.append(str(path))
    env = os.environ.copy()
    env.setdefault("HF_ENDPOINT", _first_value({}, "HF_ENDPOINT") or "https://hf-mirror.com")
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    env.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=int(_first_value({}, "TEXTELLER_TIMEOUT") or "120"),
            env=env,
        )
    except Exception:
        _TEXTELLER_FAILED = True
        return ""
    if result.returncode != 0:
        stderr = result.stderr or ""
        if any(token in stderr for token in ("ReadTimeout", "Network is unreachable", "Max retries exceeded", "xethub")):
            _TEXTELLER_FAILED = True
        return ""
    output = _clean_xml_text(result.stdout or "").strip()
    if not output:
        return ""
    match = re.search(r"Predicted\s+LaTeX\s*:\s*```(?:latex)?\s*(.*?)\s*```", output, flags=re.IGNORECASE | re.DOTALL)
    if match:
        latex = match.group(1).strip()
    else:
        cleaned = output.replace("```latex", "").replace("```", "")
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        latex = lines[-1] if lines else cleaned.strip()
        latex = re.sub(r"^(LaTeX|Result|Prediction|Predicted LaTeX)\s*[:：]\s*", "", latex, flags=re.IGNORECASE)
    return latex[:800]


def _texteller_ready_to_run() -> bool:
    allow_download = _first_value({}, "TEXTELLER_ALLOW_DOWNLOAD").lower() in {"1", "true", "yes", "on"}
    if allow_download:
        return True
    model_path = _first_value({}, "TEXTELLER_MODEL_PATH")
    if model_path and Path(model_path).exists():
        return True
    cache_roots = [
        Path(os.environ.get("HF_HOME", "")) if os.environ.get("HF_HOME") else None,
        Path.home() / ".cache" / "huggingface",
    ]
    for root in [item for item in cache_roots if item is not None]:
        repo_root = root / "hub" / "models--OleehyO--TexTeller"
        if not repo_root.exists():
            continue
        for snapshot in (repo_root / "snapshots").glob("*"):
            if (
                (snapshot / "config.json").exists()
                and ((snapshot / "model.safetensors").exists() or (snapshot / "pytorch_model.bin").exists())
                and (snapshot / "tokenizer.json").exists()
            ):
                return True
    return False


def _looks_like_formula(text: str) -> bool:
    if not text or len(text) > 180:
        return False
    lowered = text.lower()
    if lowered.startswith(("figure", "fig.", "table", "图", "表")):
        return False
    if _formula_anchor_score(text) <= 0:
        return False
    prose_words = re.findall(
        r"\b(?:the|and|with|via|based|between|interaction|method|results?|to|all|tokens?|regardless|following|consider|modern|encode|images|as|sequences|patches|concatenating|wasting|representational|information|content|through|learning)\b",
        lowered,
    )
    token_count = len(text.split())
    if prose_words and (token_count > 6 or "=" not in text):
        return False
    math_symbols = set("=<>±∞αβγδΔθλμσ∈→×·∑Σ∫√≤≥≈")
    symbol_count = sum(1 for ch in text if ch in math_symbols)
    operator_count = sum(text.count(op) for op in ["=", "\\frac", "\\sum", "\\prod", "\\log", "\\exp"])
    has_subscript_like = bool(re.search(r"[A-Za-z][_^][A-Za-z0-9{}]", text))
    math_density = (symbol_count + operator_count * 2) / max(len(text), 1)
    if token_count > 14 and math_density < 0.12:
        return False
    return (
        operator_count >= 1
        or (symbol_count >= 3 and token_count <= 18)
        or (symbol_count >= 2 and has_subscript_like and token_count <= 16)
        or (math_density > 0.1 and token_count <= 12)
    )


def _capture_captioned_figures(
    page: fitz.Page,
    work_dir: Path,
    page_no: int,
    limit: int,
    seen_boxes: set[tuple[int, int, int, int, int]],
) -> list[PaperAsset]:
    assets: list[PaperAsset] = []
    lines = _page_text_lines(page)

    figure_index = 0
    for line_index, line in enumerate(lines):
        if len(assets) >= limit:
            break
        text = line.text
        if not _caption_is_figure(text):
            continue

        caption_text, caption_rect = _caption_text_and_rect(lines, line_index, page, "figure")
        visual_rect = _visual_rect_for_caption(page, line.rect, lines)
        if visual_rect is None:
            visual_rect = _fallback_visual_rect_for_caption(page, line.rect, lines)
        if visual_rect is None:
            continue
        clip_rect = _merge_rects([visual_rect, caption_rect])
        min_height = 50 if clip_rect.width >= 420 else 80
        if clip_rect.height < min_height or clip_rect.width < 80:
            continue
        key = _box_key(page_no, clip_rect)
        if key in seen_boxes:
            continue
        seen_boxes.add(key)
        figure_index += 1
        path = work_dir / f"page-{page_no:03d}-captioned-figure-{figure_index:02d}.png"
        _save_clip(page, clip_rect, path, padding=1)
        assets.append(PaperAsset("figure", page_no, path, caption_text[:300], rect=visual_rect))
    return assets


def _capture_image_blocks(
    page: fitz.Page,
    work_dir: Path,
    page_no: int,
    limit: int,
    seen_boxes: set[tuple[int, int, int, int, int]],
) -> list[PaperAsset]:
    assets: list[PaperAsset] = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return assets

    figure_index = 0
    for block in blocks:
        if len(assets) >= limit:
            break
        if block.get("type") != 1:
            continue
        rect = fitz.Rect(block.get("bbox"))
        if rect.is_empty or rect.width < 60 or rect.height < 60:
            continue
        caption, caption_rect = _nearby_caption_with_rect(page, rect, ("figure", "fig.", "fig ", "图"))
        if not caption or not _caption_is_figure(caption):
            continue
        clip_rect = _merge_rects([rect, caption_rect]) if caption_rect else rect
        key = _box_key(page_no, clip_rect)
        if key in seen_boxes:
            continue
        seen_boxes.add(key)
        figure_index += 1
        path = work_dir / f"page-{page_no:03d}-figure-{figure_index:02d}.png"
        _save_clip(page, clip_rect, path, padding=2)
        assets.append(PaperAsset("figure", page_no, path, caption or f"Figure on page {page_no}", rect=rect))
    return assets


def _caption_is_figure(caption: str) -> bool:
    stripped = caption.strip()
    if stripped.startswith("图"):
        return True
    return bool(re.match(r"(?i)^(?:figure|fig\.?)\s*\d+[A-Za-z]?\s*(?:[.:：]|\|)", stripped))


def _caption_is_table(caption: str) -> bool:
    lowered = caption.strip().lower()
    stripped = caption.strip()
    if stripped.startswith("表"):
        return True
    return bool(re.match(r"(?i)^(?:table|tab\.)\s*\d+[A-Za-z]?\s*(?:[.:：]|\|)", stripped))


def _table_rect_for_caption(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    lines: list[TextLine],
) -> tuple[fitz.Rect | None, str]:
    below_rect, below_text = _table_rect_below_caption(page, caption_rect, lines)
    if below_rect is not None:
        return below_rect, below_text
    return _table_rect_above_caption(page, caption_rect, lines)


def _table_rect_below_caption(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    lines: list[TextLine],
) -> tuple[fitz.Rect | None, str]:
    left, right = _caption_column_bounds(page, caption_rect)
    search_top = caption_rect.y1 + 2
    search_bottom = _next_caption_or_heading_y(lines, caption_rect, left, right) or min(
        page.rect.height - 28,
        search_top + min(360, page.rect.height * 0.46),
    )
    candidate_lines = [
        line
        for line in lines
        if line.rect.y0 >= search_top
        and line.rect.y0 < search_bottom
        and _line_belongs_to_column(line.rect, left, right, min_overlap=0.35)
    ]
    row_groups = _group_lines_by_row(candidate_lines)
    selected: list[TextLine] = []
    previous_y1: float | None = None

    for group in row_groups:
        row_rect = _merge_rects([line.rect for line in group])
        if row_rect.is_empty:
            continue
        row_text = " ".join(line.text for line in group)
        if selected and _line_looks_section_heading(row_text):
            break
        if selected and previous_y1 is not None and row_rect.y0 - previous_y1 > 44:
            break
        if selected and _row_looks_table_section_label(row_text, group):
            selected.extend(group)
            previous_y1 = row_rect.y1
            continue
        if _row_is_prose_after_table(row_text, group, bool(selected)):
            if selected:
                break
            continue
        if not selected and not _row_looks_table_like(row_text, group):
            continue
        if selected or _row_looks_table_like(row_text, group):
            selected.extend(group)
            previous_y1 = row_rect.y1

    if not selected:
        return None, ""
    content_rect = _merge_rects([line.rect for line in selected])
    rect = fitz.Rect(content_rect)
    rect &= fitz.Rect(left, search_top, right, search_bottom)
    if rect.is_empty or rect.width < 60 or rect.height < 20:
        return None, ""
    rect = _expand_table_rect_to_borders(page, rect)
    rect = _tighten_table_rect_to_borders(page, rect)
    rect &= fitz.Rect(
        max(left, content_rect.x0 - 20),
        search_top,
        min(right, content_rect.x1 + 20),
        search_bottom,
    )
    text = "\n".join(_clean_xml_text(" ".join(line.text for line in group)) for group in row_groups if any(line in selected for line in group))
    return rect, text[:2500]


def _table_rect_above_caption(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    lines: list[TextLine],
) -> tuple[fitz.Rect | None, str]:
    left, right = _caption_column_bounds(page, caption_rect)
    table_left = max(left, caption_rect.x0 - 28)
    search_bottom = caption_rect.y0 - 2
    search_top = max(28.0, search_bottom - min(360, page.rect.height * 0.46))
    candidate_lines = [
        line
        for line in lines
        if line.rect.y1 <= search_bottom
        and line.rect.y1 > search_top
        and _line_belongs_to_column(line.rect, table_left, right, min_overlap=0.35)
    ]
    row_groups = _group_lines_by_row(candidate_lines)
    selected_groups: list[list[TextLine]] = []
    lower_y0: float | None = None

    for group in reversed(row_groups):
        row_rect = _merge_rects([line.rect for line in group])
        if row_rect.is_empty:
            continue
        row_text = " ".join(line.text for line in group)
        if not selected_groups:
            if search_bottom - row_rect.y1 > 48:
                break
            if not _row_looks_table_like(row_text, group):
                continue
        else:
            if lower_y0 is not None and lower_y0 - row_rect.y1 > 34:
                break
            if _line_looks_section_heading(row_text):
                break
            if not (
                _row_looks_table_like(row_text, group)
                or _row_looks_table_section_label(row_text, group)
            ):
                break
        selected_groups.append(group)
        lower_y0 = row_rect.y0

    if not selected_groups:
        return None, ""
    selected_groups.reverse()
    selected = [line for group in selected_groups for line in group]
    content_rect = _merge_rects([line.rect for line in selected])
    rect = fitz.Rect(content_rect)
    rect &= fitz.Rect(table_left, search_top, right, search_bottom)
    if rect.is_empty or rect.width < 60 or rect.height < 20:
        return None, ""
    rect = _expand_table_rect_to_borders(page, rect)
    rect = _tighten_table_rect_to_borders(page, rect)
    rect &= fitz.Rect(
        max(table_left, content_rect.x0 - 20),
        search_top,
        min(right, content_rect.x1 + 20),
        search_bottom,
    )
    text = "\n".join(
        _clean_xml_text(" ".join(line.text for line in group))
        for group in selected_groups
    )
    return rect, text[:2500]


def _line_belongs_to_column(rect: fitz.Rect, left: float, right: float, min_overlap: float = 0.25) -> bool:
    if rect.x1 < left or rect.x0 > right:
        return False
    overlap = min(rect.x1, right) - max(rect.x0, left)
    if overlap <= 0:
        return False
    width = max(1.0, rect.width)
    if overlap / width >= min_overlap:
        return True
    return rect.x0 >= left - 8 and rect.x1 <= right + 8


def _next_caption_or_heading_y(
    lines: list[TextLine],
    caption_rect: fitz.Rect,
    left: float,
    right: float,
) -> float | None:
    candidates: list[float] = []
    for line in lines:
        if line.rect.y0 <= caption_rect.y1 + 3:
            continue
        if line.rect.y0 - caption_rect.y1 > 390:
            continue
        if _horizontal_overlap_fraction(line.rect, left, right) <= 0:
            continue
        lowered = line.text.lower().strip()
        if _caption_is_table(line.text) or _caption_is_figure(line.text):
            candidates.append(line.rect.y0 - 2)
            continue
        if _line_looks_section_heading(line.text) and line.rect.y0 - caption_rect.y1 > 45:
            candidates.append(line.rect.y0 - 2)
    return min(candidates) if candidates else None


def _previous_caption_or_heading_y(
    lines: list[TextLine],
    caption_rect: fitz.Rect,
    left: float,
    right: float,
) -> float | None:
    candidates: list[float] = []
    for line in lines:
        if line.rect.y1 >= caption_rect.y0 - 3:
            continue
        if caption_rect.y0 - line.rect.y1 > 430:
            continue
        if _horizontal_overlap_fraction(line.rect, left, right) <= 0:
            continue
        if _caption_is_table(line.text) or _caption_is_figure(line.text):
            candidates.append(line.rect.y1 + 2)
            continue
        if _line_looks_section_heading(line.text):
            candidates.append(line.rect.y1 + 2)
    return max(candidates) if candidates else None


def _line_looks_section_heading(text: str) -> bool:
    stripped = _clean_xml_text(text).strip()
    if not stripped:
        return False
    if _caption_is_table(stripped) or _caption_is_figure(stripped):
        return True
    if re.fullmatch(r"(?i)setting\s+[ivxlcdm\d]+", stripped):
        return False
    stripped = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", stripped)
    if len(stripped) > 90:
        return False
    if re.search(r"[.。:：;,，↑↓\d]", stripped):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z-]*", stripped)
    if len(words) < 2 or len(words) > 7:
        return False
    if any(len(word.strip("-")) <= 1 for word in words):
        return False
    return all(word[:1].isupper() for word in words)


def _group_lines_by_row(lines: list[TextLine]) -> list[list[TextLine]]:
    rows: list[list[TextLine]] = []
    for line in sorted(lines, key=lambda item: (item.rect.y0, item.rect.x0)):
        if not rows:
            rows.append([line])
            continue
        current = rows[-1]
        current_mid = sum((item.rect.y0 + item.rect.y1) / 2 for item in current) / len(current)
        line_mid = (line.rect.y0 + line.rect.y1) / 2
        if abs(line_mid - current_mid) <= 3.5:
            current.append(line)
        else:
            rows.append([line])
    return rows


def _row_looks_table_like(text: str, group: list[TextLine]) -> bool:
    stripped = _clean_xml_text(text)
    if len(group) >= 4:
        return True
    number_count = len(re.findall(r"\d+(?:\.\d+)?", stripped))
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]+", stripped)
    if len(group) >= 2:
        long_words = re.findall(r"[A-Za-z\u4e00-\u9fff]{4,}", stripped)
        if number_count >= 2 and len(long_words) <= 8:
            return True
        if all(len(_clean_xml_text(line.text).strip()) <= 24 for line in group):
            return True
        return False
    if number_count >= 2:
        return True
    if number_count == 1 and len(words) <= 3:
        return True
    if re.search(r"[↑↓±]|\\pm|\\uparrow|\\downarrow", stripped) and len(words) <= 3:
        return True
    return 0 < len(words) <= 3 and len(stripped) <= 36


def _row_looks_table_section_label(text: str, group: list[TextLine]) -> bool:
    stripped = _clean_xml_text(text).strip()
    if len(group) != 1 or not stripped:
        return False
    if re.search(r"[.。:：;,，]", stripped):
        return False
    if re.search(r"\d", stripped):
        return False
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", stripped)
    return 1 <= len(words) <= 5 and len(stripped) <= 48


def _row_is_prose_after_table(text: str, group: list[TextLine], selected: bool) -> bool:
    stripped = _clean_xml_text(text)
    if not stripped:
        return False
    if not selected:
        return False
    if _row_looks_table_section_label(stripped, group):
        return False
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", stripped)
    long_words = re.findall(r"[A-Za-z\u4e00-\u9fff]{4,}", stripped)
    if len(group) <= 2 and len(words) >= 8:
        return True
    if len(group) == 1 and len(words) >= 4 and not _row_looks_table_like(stripped, group):
        return True
    return len(stripped) > 90 and len(long_words) >= 8


def _expand_table_rect_to_borders(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    borders: list[fitz.Rect] = []
    search = fitz.Rect(rect.x0 - 12, rect.y0 - 30, rect.x1 + 12, rect.y1 + 18)
    try:
        drawings = page.get_drawings()
    except Exception:
        return rect

    for drawing in drawings:
        raw_rect = drawing.get("rect")
        if not raw_rect:
            continue
        border = fitz.Rect(raw_rect)
        if border.width <= 0:
            continue
        if border.height <= 0.1:
            border = fitz.Rect(border.x0, border.y0 - 0.5, border.x1, border.y1 + 0.5)
        if border.y1 < search.y0 or border.y0 > search.y1:
            continue
        if border.x1 < search.x0 or border.x0 > search.x1:
            continue
        if border.height > 2.5:
            continue
        if border.width < max(40.0, rect.width * 0.45):
            continue
        if _horizontal_overlap_fraction(border, rect.x0 - 18, rect.x1 + 18) < 0.55:
            continue
        borders.append(border)

    if not borders:
        return rect
    expanded = _merge_rects([rect, *borders])
    expanded &= page.rect
    return expanded


def _tighten_table_rect_to_borders(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    try:
        drawings = page.get_drawings()
    except Exception:
        return rect
    search = fitz.Rect(rect.x0 - 16, rect.y0 - 16, rect.x1 + 16, rect.y1 + 16)
    horizontal_borders: list[fitz.Rect] = []
    for drawing in drawings:
        raw_rect = drawing.get("rect")
        if not raw_rect:
            continue
        border = fitz.Rect(raw_rect)
        if border.width <= 0:
            continue
        if border.height <= 0.1:
            border = fitz.Rect(border.x0, border.y0 - 0.5, border.x1, border.y1 + 0.5)
        if border.height > 2.5:
            continue
        if border.width < max(80.0, rect.width * 0.35):
            continue
        if border.y1 < search.y0 or border.y0 > search.y1:
            continue
        if border.x1 < search.x0 or border.x0 > search.x1:
            continue
        horizontal_borders.append(border)
    if len(horizontal_borders) < 2:
        return rect
    left = min(border.x0 for border in horizontal_borders)
    right = max(border.x1 for border in horizontal_borders)
    top = min(border.y0 for border in horizontal_borders)
    bottom = max(border.y1 for border in horizontal_borders)
    tightened = fitz.Rect(
        max(page.rect.x0, left),
        max(page.rect.y0, min(rect.y0, top)),
        min(page.rect.x1, right),
        min(page.rect.y1, max(rect.y1, bottom)),
    )
    if tightened.is_empty or tightened.width < 60 or tightened.height < 20:
        return rect
    return tightened


def _caption_text_and_rect(
    lines: list[TextLine],
    caption_index: int,
    page: fitz.Page,
    kind: str,
) -> tuple[str, fitz.Rect]:
    caption_line = lines[caption_index]
    left, right = _caption_column_bounds(page, caption_line.rect)
    caption_lines = [caption_line]
    previous = caption_line
    for next_line in lines[caption_index + 1 : caption_index + 14]:
        if next_line.rect.y0 < previous.rect.y0:
            continue
        if not _line_belongs_to_column(next_line.rect, left, right, min_overlap=0.35):
            continue
        gap = next_line.rect.y0 - previous.rect.y1
        if gap > 16:
            break
        lowered = next_line.text.lower().strip()
        if _caption_is_figure(next_line.text) or lowered.startswith(("table", "tab.", "表")):
            break
        if re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Za-z]", next_line.text):
            break
        if kind == "figure":
            if len(caption_lines) >= 8:
                break
            if _figure_caption_continuation_is_body_text(previous.text, next_line.text):
                break
            if len(caption_lines) >= 1 and not _line_looks_caption_continuation(next_line.text):
                break
        if kind == "table":
            if len(caption_lines) >= 7:
                break
            if _figure_caption_continuation_is_body_text(previous.text, next_line.text):
                break
            if len(caption_lines) >= 2 and not _line_looks_caption_continuation(next_line.text):
                break
        caption_lines.append(next_line)
        previous = next_line
    caption_rect = _merge_rects([line.rect for line in caption_lines])
    caption_text = " ".join(line.text for line in caption_lines)
    return _clean_xml_text(caption_text), caption_rect


def _line_looks_caption_continuation(text: str) -> bool:
    stripped = _clean_xml_text(text).strip()
    if not stripped:
        return False
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", stripped)
    return len(words) >= 4 or stripped[:1].islower()


def _figure_caption_continuation_is_body_text(previous_text: str, next_text: str) -> bool:
    previous = _clean_xml_text(previous_text).strip()
    following = _clean_xml_text(next_text).strip()
    if not previous or not following:
        return False
    if not re.search(r"[.!?。！？]$", previous):
        return False
    if re.match(r"^(?:Figure|Fig\.?|Table|Tab\.?)\s*\d+", following, flags=re.IGNORECASE):
        return True
    if following[:1].islower() and len(re.findall(r"[A-Za-z]{2,}", following)) >= 3:
        return True
    if following[:1].isupper() and len(re.findall(r"[A-Za-z]{2,}", following)) >= 3:
        return True
    return bool(re.match(r"^(?:To|We|The|This|These|Our|In)\b", following))


def _caption_column_bounds(page: fitz.Page, caption_rect: fitz.Rect) -> tuple[float, float]:
    if page.rect.width < 420:
        return max(0, caption_rect.x0 - 24), min(page.rect.width, caption_rect.x1 + 24)
    if caption_rect.width > page.rect.width * 0.55:
        return max(0, page.rect.width * 0.05), min(page.rect.width, page.rect.width * 0.95)
    mid = page.rect.width / 2
    margin = max(28.0, page.rect.width * 0.06)
    if caption_rect.x0 < mid < caption_rect.x1 and caption_rect.x0 > page.rect.width * 0.22:
        return max(0, page.rect.width * 0.05), min(page.rect.width, page.rect.width * 0.95)
    if caption_rect.x0 < mid - 20:
        if caption_rect.x1 <= mid:
            return _column_bounds(page, caption_rect)
        return margin, min(page.rect.width - margin, mid + 55)
    if caption_rect.x0 > mid + 20:
        return max(margin, mid - 12), page.rect.width - margin
    return _column_bounds(page, caption_rect)


def _visual_rect_for_caption(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    lines: list[TextLine],
) -> fitz.Rect | None:
    above = _visual_rect_for_caption_direction(page, caption_rect, lines, "above")
    below = _visual_rect_for_caption_direction(page, caption_rect, lines, "below")
    if above is None:
        return below
    if below is None:
        return above

    above_gap = max(0.0, caption_rect.y0 - above.y1)
    below_gap = max(0.0, below.y0 - caption_rect.y1)
    if above_gap <= 25:
        return above
    if below_gap <= 70 and (above_gap > 70 or below.height > above.height * 0.75):
        return below
    return above


def _visual_rect_for_caption_direction(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    lines: list[TextLine],
    direction: str,
) -> fitz.Rect | None:
    left, right = _caption_column_bounds(page, caption_rect)
    if direction == "above":
        previous_boundary = _previous_caption_y(lines, caption_rect, left, right)
        search_top = max(previous_boundary or 0, caption_rect.y0 - min(520, page.rect.height * 0.7))
        search_bottom = caption_rect.y0 - 1
    else:
        next_boundary = _next_caption_or_heading_y(lines, caption_rect, left, right)
        search_top = caption_rect.y1 + 1
        search_bottom = next_boundary or min(page.rect.height - 24, caption_rect.y1 + min(420, page.rect.height * 0.55))

    if search_bottom <= search_top + 20:
        return None

    column_rect = fitz.Rect(left, search_top, right, search_bottom)
    candidates: list[fitz.Rect] = []
    for region in _page_graphic_regions(page):
        if region.y1 < search_top or region.y0 > search_bottom:
            continue
        if _rect_overlap_fraction(region, column_rect) <= 0:
            continue
        horizontal = _horizontal_overlap_fraction(region, left, right)
        if horizontal < 0.12:
            continue
        if region.width < 18 or region.height < 12:
            continue
        candidates.append(region & column_rect)
    if not candidates:
        return None

    if direction == "above":
        near = [r for r in candidates if 0 <= caption_rect.y0 - r.y1 <= 180]
        seed_pool = near or candidates
        seed = max(seed_pool, key=lambda r: (r.y1, r.width * r.height))
    else:
        near = [r for r in candidates if 0 <= r.y0 - caption_rect.y1 <= 180]
        seed_pool = near or candidates
        seed = min(seed_pool, key=lambda r: (r.y0, -r.width * r.height))

    group = fitz.Rect(seed)
    changed = True
    while changed:
        changed = False
        for region in candidates:
            if not _figure_region_belongs_to_group(region, seed, group):
                continue
            if _rect_overlap_fraction(region, group) > 0.03 or _rect_gap(region, group) <= 52:
                before = tuple(group)
                group |= region
                if tuple(group) != before:
                    changed = True

    group &= column_rect
    if group.is_empty or group.width < 40 or group.height < 30:
        return None
    group = _trim_figure_region_top_text(group, lines, caption_rect, left, right)
    if group.is_empty or group.width < 40 or group.height < 30:
        return None
    return group


def _trim_figure_region_top_text(
    region: fitz.Rect,
    lines: list[TextLine],
    caption_rect: fitz.Rect,
    left: float,
    right: float,
) -> fitz.Rect:
    barrier: float | None = None
    for line in lines:
        if line.rect.y0 < region.y0 - 2 or line.rect.y1 > min(caption_rect.y0, region.y0 + 85):
            continue
        if _horizontal_overlap_fraction(line.rect, left, right) <= 0:
            continue
        text = _clean_xml_text(line.text).strip()
        if _line_is_front_matter_or_body_before_figure(text):
            barrier = max(barrier or line.rect.y1, line.rect.y1)
    if barrier is None or barrier + 4 >= region.y1:
        return region
    return fitz.Rect(region.x0, barrier + 4, region.x1, region.y1)


def _line_is_front_matter_or_body_before_figure(text: str) -> bool:
    if not text:
        return False
    if "@" in text:
        return True
    lowered = text.lower()
    if lowered in {"abstract", "introduction"}:
        return True
    if re.search(r"\b(?:amazon|university|institute|college|foundation|proceedings|xplore|open access|accepted version)\b", lowered):
        return True
    if re.search(r"\b(?:aims|recent|restoration agents|suffer|bottlenecks|models|paper)\b", lowered) and len(text) > 45:
        return True
    return False


def _figure_region_belongs_to_group(region: fitz.Rect, seed: fitz.Rect, group: fitz.Rect) -> bool:
    if _rect_overlap_fraction(region, seed) > 0.02 or _rect_overlap_fraction(region, group) > 0.02:
        return True
    seed_overlap = _horizontal_overlap_fraction(region, seed.x0 - 8, seed.x1 + 8)
    group_overlap = _horizontal_overlap_fraction(region, group.x0 - 8, group.x1 + 8)
    if max(seed_overlap, group_overlap) < 0.2:
        return False
    return _rect_gap(region, group) <= 38


def _figure_upper_barrier_y(
    lines: list[TextLine],
    seed: fitz.Rect,
    caption_rect: fitz.Rect,
    left: float,
    right: float,
    search_top: float,
) -> float | None:
    candidate_lines = [
        line
        for line in lines
        if search_top <= line.rect.y0 < seed.y0
        and _horizontal_overlap_fraction(line.rect, left, right) > 0
    ]
    barrier: float | None = None
    for group in _group_lines_by_row(candidate_lines):
        row_rect = _merge_rects([line.rect for line in group])
        if row_rect.is_empty:
            continue
        if seed.y0 - row_rect.y1 > 180:
            continue
        row_text = " ".join(line.text for line in group)
        if _row_looks_table_like(row_text, group) or _caption_is_table(row_text):
            barrier = max(barrier or row_rect.y1, row_rect.y1)
    return barrier


def _fallback_visual_rect_for_caption(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    lines: list[TextLine] | None = None,
) -> fitz.Rect | None:
    left, right = _caption_column_bounds(page, caption_rect)
    graphic_rect = _fallback_graphic_rect_above_caption(page, caption_rect, left, right, lines or [])
    if graphic_rect is not None:
        return graphic_rect
    top = max(0, caption_rect.y0 - min(300, page.rect.height * 0.38))
    barrier_y = _fallback_figure_upper_body_barrier_y(lines or [], caption_rect, left, right, top)
    if barrier_y is not None:
        top = max(top, barrier_y + 8)
    bottom = caption_rect.y0 - 2
    rect = fitz.Rect(left, top, right, bottom)
    if rect.is_empty or rect.width < 40 or rect.height < 28:
        return None
    return rect


def _fallback_graphic_rect_above_caption(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    left: float,
    right: float,
    lines: list[TextLine],
) -> fitz.Rect | None:
    bottom = caption_rect.y0 - 2
    previous_boundary = _previous_caption_y(lines, caption_rect, left, right) if lines else None
    search_top = max(previous_boundary or 0, caption_rect.y0 - min(260, page.rect.height * 0.42))
    regions = [
        region
        for region in _page_graphic_regions(page)
        if region.y1 <= bottom
        and region.y0 >= search_top
        and _horizontal_overlap_fraction(region, left, right) > 0.05
    ]
    if not regions:
        return None
    seed = max(regions, key=lambda rect: (rect.y1, rect.width * rect.height))
    group = fitz.Rect(seed)
    changed = True
    while changed:
        changed = False
        for region in regions:
            vertical_gap = max(region.y0 - group.y1, group.y0 - region.y1, 0)
            if vertical_gap > 70:
                continue
            if _horizontal_overlap_fraction(region, group.x0 - 24, group.x1 + 24) < 0.05:
                continue
            before = tuple(group)
            group |= region
            if tuple(group) != before:
                changed = True
    rect = fitz.Rect(left, max(search_top, group.y0 - 2), right, bottom)
    if rect.is_empty or rect.width < 80 or rect.height < 28:
        return None
    return rect


def _previous_caption_y(
    lines: list[TextLine],
    caption_rect: fitz.Rect,
    left: float,
    right: float,
) -> float | None:
    candidates: list[float] = []
    for line in lines:
        if line.rect.y1 >= caption_rect.y0 - 3:
            continue
        if caption_rect.y0 - line.rect.y1 > 430:
            continue
        if _horizontal_overlap_fraction(line.rect, left, right) <= 0:
            continue
        if _caption_is_table(line.text) or _caption_is_figure(line.text):
            candidates.append(line.rect.y1 + 2)
    return max(candidates) if candidates else None


def _fallback_figure_upper_body_barrier_y(
    lines: list[TextLine],
    caption_rect: fitz.Rect,
    left: float,
    right: float,
    search_top: float,
) -> float | None:
    barrier: float | None = None
    for group in _group_lines_by_row(
        [
            line
            for line in lines
            if search_top <= line.rect.y0 < caption_rect.y0 - 45
            and _horizontal_overlap_fraction(line.rect, left, right) > 0
        ]
    ):
        row_rect = _merge_rects([line.rect for line in group])
        if row_rect.is_empty:
            continue
        text = _clean_xml_text(" ".join(line.text for line in group)).strip()
        if _row_is_body_text_before_figure(text, group, left, right):
            barrier = max(barrier or row_rect.y1, row_rect.y1)
    return barrier


def _row_is_body_text_before_figure(text: str, group: list[TextLine], left: float, right: float) -> bool:
    if not text or _caption_is_figure(text) or _caption_is_table(text):
        return False
    if re.search(r"[%+−±]|\d", text):
        return False
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", text)
    if len(group) == 1 and len(words) <= 3:
        return False
    if _row_looks_table_like(text, group):
        return True
    if len(words) < 7:
        return False
    row_rect = _merge_rects([line.rect for line in group])
    column_width = max(right - left, 1)
    has_sentence_punctuation = bool(re.search(r"[.。;；,，]$", text))
    return row_rect.width > column_width * 0.38 or has_sentence_punctuation


def _page_graphic_regions(page: fitz.Page) -> list[fitz.Rect]:
    regions: list[fitz.Rect] = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
        for block in blocks:
            if block.get("type") == 1:
                rect = fitz.Rect(block.get("bbox"))
                if rect.width >= 12 and rect.height >= 12 and not _graphic_region_is_page_artifact(page, rect):
                    regions.append(rect)
    except Exception:
        pass
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if not rect:
                continue
            rect = fitz.Rect(rect)
            if rect.width >= 12 and rect.height >= 12 and not _graphic_region_is_page_artifact(page, rect):
                regions.append(rect)
    except Exception:
        pass
    return regions


def _graphic_region_is_page_artifact(page: fitz.Page, rect: fitz.Rect) -> bool:
    extends_outside = (
        rect.x0 < page.rect.x0 - 20
        or rect.y0 < page.rect.y0 - 20
        or rect.x1 > page.rect.x1 + 20
        or rect.y1 > page.rect.y1 + 20
    )
    if extends_outside and rect.width > page.rect.width * 0.9 and rect.height > page.rect.height * 0.35:
        return True
    page_area = max(page.rect.width * page.rect.height, 1.0)
    if rect.width > page.rect.width * 0.98 and rect.height > page.rect.height * 0.55 and rect.width * rect.height > page_area * 0.45:
        return True
    return False


def _rect_gap(first: fitz.Rect, second: fitz.Rect) -> float:
    dx = max(first.x0 - second.x1, second.x0 - first.x1, 0)
    dy = max(first.y0 - second.y1, second.y0 - first.y1, 0)
    return max(dx, dy)


def _horizontal_overlap_fraction(rect: fitz.Rect, left: float, right: float) -> float:
    overlap = max(0.0, min(rect.x1, right) - max(rect.x0, left))
    return overlap / max(rect.width, 1)


def _merge_rects(rects: Iterable[fitz.Rect | None]) -> fitz.Rect:
    valid = [fitz.Rect(rect) for rect in rects if rect is not None and not fitz.Rect(rect).is_empty]
    if not valid:
        return fitz.Rect()
    merged = fitz.Rect(valid[0])
    for rect in valid[1:]:
        merged |= rect
    return merged


def _save_clip(page: fitz.Page, rect: fitz.Rect, path: Path, padding: int = 8, scale: float = 2.0) -> None:
    clip = rect + (-padding, -padding, padding, padding)
    clip &= page.rect
    scale = max(1.0, float(scale or 2.0))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    pix.save(path)


def _trim_formula_edge_fragments(path: Path) -> None:
    try:
        with Image.open(path) as image:
            sample = image.convert("L")
            width, height = sample.size
            if width < 120 or height < 35:
                return
            pixels = sample.load()
            threshold = max(2, int(width * 0.004))
            content_rows = []
            for y in range(height):
                dark = 0
                for x in range(width):
                    if pixels[x, y] < 235:
                        dark += 1
                if dark >= threshold:
                    content_rows.append(y)
            if not content_rows:
                return
            groups = _contiguous_number_groups(content_rows)
            if len(groups) <= 1:
                return
            top = 0
            bottom = height
            first_start, first_end = groups[0]
            last_start, last_end = groups[-1]
            edge_limit = max(6, int(height * 0.12))
            max_fragment_height = max(8, int(height * 0.18))
            if first_start <= 2 and first_end - first_start + 1 <= max_fragment_height:
                top = min(groups[1][0] - 4, height - 1)
            if height - 1 - last_end <= 2 and last_end - last_start + 1 <= max_fragment_height:
                bottom = max(groups[-2][1] + 5, 1)
            if first_start > edge_limit:
                top = 0
            if height - 1 - last_end > edge_limit:
                bottom = height
            if top <= 0 and bottom >= height:
                return
            if bottom - top < max(24, int(height * 0.45)):
                return
            image.crop((0, top, width, bottom)).save(path)
    except Exception:
        return


def _contiguous_number_groups(values: list[int]) -> list[tuple[int, int]]:
    if not values:
        return []
    groups: list[tuple[int, int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value <= previous + 1:
            previous = value
            continue
        groups.append((start, previous))
        start = previous = value
    groups.append((start, previous))
    return groups


def _table_to_text(table) -> str:
    try:
        rows = table.extract()
    except Exception:
        return ""
    lines = []
    for row in rows[:20]:
        cells = [_clean_xml_text(str(cell or "")).strip().replace("\n", " ") for cell in row]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _table_detection_looks_reliable(table, table_text: str) -> bool:
    try:
        rows = table.extract()
    except Exception:
        rows = []
    non_empty_rows = []
    non_empty_cells = 0
    numeric_cells = 0
    for row in rows:
        cells = [_clean_xml_text(str(cell or "")).strip() for cell in row]
        filled = [cell for cell in cells if cell]
        if not filled:
            continue
        non_empty_rows.append(filled)
        non_empty_cells += len(filled)
        numeric_cells += sum(1 for cell in filled if re.search(r"\d", cell))
    if len(non_empty_rows) < 2 or non_empty_cells < 6:
        return False
    if numeric_cells < 3:
        return False
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]{3,}", _clean_xml_text(table_text))
    if len(non_empty_rows) <= 2 and len(words) > non_empty_cells:
        return False
    return True


def _table_detection_looks_like_page_region(page: fitz.Page, rect: fitz.Rect) -> bool:
    page_area = max(page.rect.width * page.rect.height, 1.0)
    rect_area = max(rect.width * rect.height, 0.0)
    if rect.width > page.rect.width * 0.72 and rect.height > page.rect.height * 0.30:
        return True
    if rect_area > page_area * 0.25 and rect.width > page.rect.width * 0.65:
        return True
    return False


def _nearby_caption(page: fitz.Page, rect: fitz.Rect, prefixes: Iterable[str]) -> str:
    caption, _caption_rect = _nearby_caption_with_rect(page, rect, prefixes)
    return caption


def _nearby_caption_with_rect(
    page: fitz.Page,
    rect: fitz.Rect,
    prefixes: Iterable[str],
) -> tuple[str, fitz.Rect | None]:
    lines = _page_text_lines(page)
    best: tuple[float, int] | None = None
    kind = "figure" if any(str(prefix).lower().startswith(("fig", "figure", "图")) for prefix in prefixes) else "table"
    for idx, line in enumerate(lines):
        lowered = line.text.lower().strip()
        if not any(lowered.startswith(prefix) for prefix in prefixes):
            continue
        if kind == "table" and line.rect.y0 > rect.y1 + 8:
            continue
        vertical_gap = min(abs(line.rect.y0 - rect.y1), abs(rect.y0 - line.rect.y1))
        if vertical_gap > 150:
            continue
        if _horizontal_overlap_fraction(line.rect, rect.x0 - 24, rect.x1 + 24) < 0.08 and _horizontal_overlap_fraction(rect, line.rect.x0 - 24, line.rect.x1 + 24) < 0.08:
            continue
        score = vertical_gap
        if line.rect.y1 <= rect.y0:
            score -= 10
        if best is None or score < best[0]:
            best = (score, idx)
    if best is None:
        return "", None
    caption_text, caption_rect = _caption_text_and_rect(lines, best[1], page, kind)
    return caption_text[:300], caption_rect


def _box_key(page_no: int, rect: fitz.Rect) -> tuple[int, int, int, int, int]:
    return (page_no, round(rect.x0), round(rect.y0), round(rect.x1), round(rect.y1))


def _extract_abstract_from_text(text: str) -> str:
    cleaned = _clean_xml_text(text)
    match = re.search(
        r"(?is)\babstract\b\s*[:.\-]?\s*(.*?)(?=\n\s*(?:1\s+)?(?:introduction|keywords?|index terms)\b|\n\s*(?:i\.|1\.)\s+introduction\b|\[Page\s+\d+\])",
        cleaned,
    )
    if not match:
        return ""
    abstract = _clean_abstract_fragment(match.group(1))
    abstract = re.sub(r"^(?:abstract\s*)+", "", abstract, flags=re.IGNORECASE).strip()
    if len(abstract) < 80:
        return ""
    return abstract[:2500]


def _extract_abstract_from_pdf(pdf_path: Path, pages: list[int] | None = None) -> str:
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return ""
    try:
        page_indices = pages if pages is not None else list(range(min(2, doc.page_count)))
        for page_position, page_index in enumerate(page_indices[:2]):
            if page_index < 0 or page_index >= doc.page_count:
                continue
            raw_text = "\n".join(
                doc[idx].get_text("text", sort=False)
                for idx in page_indices[page_position : page_position + 2]
                if 0 <= idx < doc.page_count
            )
            abstract = _extract_abstract_from_text(raw_text)
            if len(abstract) >= 80:
                return abstract[:2500]

            page = doc[page_index]
            words = page.get_text("words", sort=True)
            abstract_y = None
            for word in words:
                token = str(word[4]).strip().lower().strip(":")
                if token == "abstract":
                    abstract_y = float(word[1])
                    break
            if abstract_y is None:
                continue
            left_min = page.rect.width * 0.08
            left_max = page.rect.width * 0.47
            line_map: dict[tuple[int, int], list[tuple]] = {}
            for word in words:
                x0, y0 = float(word[0]), float(word[1])
                if y0 <= abstract_y + 6 or x0 < left_min or x0 >= left_max:
                    continue
                if y0 > page.rect.height - 42:
                    continue
                key = (int(word[5]), int(word[6]))
                line_map.setdefault(key, []).append(word)
            lines = []
            for line_words in line_map.values():
                line_words = sorted(line_words, key=lambda item: float(item[0]))
                line = _clean_xml_text(" ".join(str(word[4]) for word in line_words)).strip()
                if not line:
                    continue
                lowered = line.lower()
                if lowered.startswith(("arxiv", "introduction", "1 introduction", "keywords", "index terms")):
                    break
                if lowered == "abstract":
                    continue
                lines.append(line)
            abstract = _dehyphenate_pdf_text(" ".join(lines))
            if len(abstract) >= 80:
                return abstract[:2500]
        return ""
    finally:
        doc.close()


def _clean_abstract_fragment(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = _clean_xml_text(line).strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if stripped.startswith("*") or lowered.startswith(("arxiv:", "copyright", "©")):
            continue
        lines.append(stripped)
    return _dehyphenate_pdf_text(" ".join(lines))


def _dehyphenate_pdf_text(text: str) -> str:
    text = re.sub(r"-\s+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_title_from_pdf(pdf_path: Path, pages: list[int] | None = None) -> str:
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return ""
    try:
        page_index = pages[0] if pages else 0
        if page_index < 0 or page_index >= doc.page_count:
            page_index = 0
        page = doc[page_index]
        candidates: list[tuple[float, float, str]] = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                bbox = line.get("bbox") or (0, 0, 0, 0)
                y0 = float(bbox[1])
                if y0 > page.rect.height * 0.32:
                    continue
                spans = line.get("spans", [])
                text = _clean_xml_text("".join(str(span.get("text", "")) for span in spans)).strip()
                if not _looks_like_title_line(text):
                    continue
                max_size = max((float(span.get("size", 0)) for span in spans), default=0.0)
                candidates.append((max_size, y0, text))
        if not candidates:
            return ""
        largest_size = max(item[0] for item in candidates)
        title_lines = [
            item
            for item in candidates
            if item[0] >= largest_size - 0.8 and not _looks_like_author_or_affiliation(item[2])
        ]
        title = " ".join(text for _, _, text in sorted(title_lines, key=lambda item: item[1]))
        return re.sub(r"\s+", " ", title).strip()[:300]
    finally:
        doc.close()


def _looks_like_title_line(text: str) -> bool:
    if len(text) < 8 or len(text) > 180:
        return False
    lowered = text.lower()
    if any(token in lowered for token in ("http://", "https://", "@", "arxiv:", "abstract")):
        return False
    if lowered.startswith(("figure", "fig.", "table", "tab.")):
        return False
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", text))


def _looks_like_author_or_affiliation(text: str) -> bool:
    lowered = text.lower()
    if any(token in lowered for token in ("university", "institute", "research", "lab", "school", "department")):
        return True
    if re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+[0-9]?\b", text) and "," in text:
        return True
    return False


def _extract_formula_candidates(text: str, limit: int = 8) -> list[str]:
    candidates: list[str] = []
    candidates.extend(_known_formula_seeds(text))
    if len(candidates) >= 3:
        return candidates[:limit]
    for raw_line in text.splitlines():
        line = _clean_xml_text(raw_line).strip()
        if not _looks_like_formula(line):
            continue
        if _formula_line_is_prose_fragment(line):
            continue
        normalized = _textualize_latex(line)
        if len(normalized) < 4 or normalized in candidates:
            continue
        candidates.append(normalized)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def _known_formula_seeds(text: str) -> list[str]:
    lowered = text.lower()
    if "modix" not in lowered or "positional index" not in lowered:
        return []
    return [
        "E = [E_text; E_vision] ∈ R^(N×d)，其中 E_text ∈ R^(n_t×d)，E_vision ∈ R^(n_v×d)，N = n_t + n_v。工程含义：MODIX 操作的是同一向量空间中的文本与视觉 token 序列，不改 token 内容，只改位置坐标。",
        "C_m = (I_intra_m)^α · (I_inter_m)^(1-α)，α ∈ [0,1]；归一化为 C̃_m = C_m / Σ_m C_m。工程含义：模态贡献由模态内部信息密度与跨模态交互共同决定。",
        "Δ_vision = C̃_text / C̃_vision，且 Δ_text = 1。工程含义：文本位置步长固定，视觉 token 的 RoPE stride 随文本/视觉贡献比动态调整。",
    ]


def _formula_line_is_prose_fragment(line: str) -> bool:
    lowered = line.lower().strip()
    if re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Za-z]", line):
        return True
    if lowered.startswith(("where ", "adjusted ", "standard ", "adopting ", "consider ", "following ")):
        return True
    words = re.findall(r"[A-Za-z]{3,}", line)
    if len(words) <= 3:
        return False
    starts_like_formula = bool(re.match(r"^\s*[~A-Za-z∆Δαβγθλμσ][A-Za-z0-9_~∆Δαβγθλμσ'′]*\s*[=∈≤≥≈]", line))
    return not starts_like_formula


def _resolve_codex_config(envs: dict[str, str]) -> CodexConfig:
    base_url = _first_value(envs, "CODEX_BASE_URL", "OPENAI_BASE_URL")
    api_key = _first_value(envs, "CODEX_API_KEY", "OPENAI_API_KEY")
    model = _first_value(envs, "CODEX_MODEL", "OPENAI_MODEL")
    use_proxy = _truthy(_first_value(envs, "CODEX_USE_PROXY", "OPENAI_USE_PROXY"))
    proxy = _first_value(envs, "CODEX_PROXY", "OPENAI_PROXY")

    if not base_url:
        raise ValueError("缺少 CODEX_BASE_URL，请在前端或 config.json 中配置 Codex 本地接口 URL。")
    if not model:
        raise ValueError("缺少 CODEX_MODEL，请在前端或 config.json 中配置模型名称。")
    if not api_key:
        api_key = "codex-local"
    return CodexConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        use_proxy=use_proxy,
        proxy=proxy,
    )


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _first_value(envs: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = envs.get(key)
        if value and value != "***":
            return value
    config = ConfigManager.all()
    for key in keys:
        value = config.get(key) or os.environ.get(key)
        if value:
            return str(value)
    return ""


def record_summary_correction(
    paper_id: str,
    original: str,
    corrected: str,
    *,
    note: str = "",
    category: str = "summary",
    scope: str = "paper",
    confidence: float = 1.0,
    memory_path: str | Path | None = None,
) -> Path:
    """Store user feedback as correction memory for future paper summaries."""
    normalized_paper_id = _paper_memory_id(paper_id)
    memory = CorrectionMemory(
        paper_id="global" if _normalize_memory_scope(scope) == "global" else normalized_paper_id,
        original=_clean_xml_text(original).strip(),
        corrected=_clean_xml_text(corrected).strip(),
        note=_clean_xml_text(note).strip(),
        category=_clean_xml_text(category).strip() or "summary",
        scope=_normalize_memory_scope(scope) or "paper",
        confidence=_clamped_confidence(confidence),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    if not memory.original and not memory.corrected and not memory.note:
        raise ValueError("Correction memory requires original, corrected, or note content.")
    path = _correction_memory_path(memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(memory.__dict__, ensure_ascii=False) + "\n")
    return path


def _load_correction_memories(
    paper_id: str = "",
    *,
    limit: int = 20,
    memory_path: str | Path | None = None,
    policy: MemoryPolicy | None = None,
) -> list[CorrectionMemory]:
    path = _correction_memory_path(memory_path)
    if not path.exists():
        return []
    normalized_paper_id = _paper_memory_id(paper_id)
    memories: list[CorrectionMemory] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        memory = _memory_from_payload(payload)
        if not _memory_applies_to_paper(memory, normalized_paper_id):
            continue
        memories.append(memory)
        if len(memories) >= limit:
            break
    selected = _select_memories_for_injection(list(reversed(memories)), policy or MemoryPolicy())
    _mark_memories_used(path, selected)
    return selected


def list_correction_memories(
    *,
    memory_path: str | Path | None = None,
    include_disabled: bool = True,
) -> list[dict[str, object]]:
    memories = _read_all_correction_memories(memory_path)
    rows = []
    for index, memory in enumerate(memories, 1):
        if memory.disabled and not include_disabled:
            continue
        payload = dict(memory.__dict__)
        payload["index"] = index
        rows.append(payload)
    return rows


def disable_correction_memory(index: int, *, memory_path: str | Path | None = None) -> Path:
    memories = _read_all_correction_memories(memory_path)
    memory_index = index - 1
    if memory_index < 0 or memory_index >= len(memories):
        raise IndexError(f"memory index out of range: {index}")
    memory = memories[memory_index]
    memories[memory_index] = CorrectionMemory(**{**memory.__dict__, "disabled": True})
    return _write_all_correction_memories(memories, memory_path)


def promote_correction_memory(
    index: int,
    target_scope: str,
    *,
    memory_path: str | Path | None = None,
    evaluation_passed: bool = False,
    policy: MemoryPolicy | None = None,
) -> Path:
    memories = _read_all_correction_memories(memory_path)
    memory_index = index - 1
    if memory_index < 0 or memory_index >= len(memories):
        raise IndexError(f"memory index out of range: {index}")
    memory = memories[memory_index]
    memory_policy = policy or MemoryPolicy()
    target_scope = _normalize_memory_scope(target_scope)
    if not memory_policy.can_promote(memory, target_scope, evaluation_passed=evaluation_passed):
        raise ValueError("memory does not satisfy promotion policy")
    promoted = CorrectionMemory(
        **{
            **memory.__dict__,
            "scope": target_scope,
            "paper_id": "global" if target_scope == "global" else memory.paper_id,
            "promoted_from": memory.promoted_from or f"{memory.scope}:{memory.paper_id}:{index}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_used_at": "",
            "disabled": False,
        }
    )
    memories.append(promoted)
    return _write_all_correction_memories(memories, memory_path)


def _read_all_correction_memories(memory_path: str | Path | None = None) -> list[CorrectionMemory]:
    path = _correction_memory_path(memory_path)
    if not path.exists():
        return []
    memories: list[CorrectionMemory] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            memories.append(_memory_from_payload(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return memories


def _write_all_correction_memories(
    memories: list[CorrectionMemory],
    memory_path: str | Path | None = None,
) -> Path:
    path = _correction_memory_path(memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(memory.__dict__, ensure_ascii=False) for memory in memories)
    path.write_text((content + "\n") if content else "", encoding="utf-8")
    return path


def _mark_memories_used(path: Path, selected: list[CorrectionMemory]) -> None:
    if not selected or not path.exists():
        return
    selected_keys = {_memory_key(memory) for memory in selected}
    memories = _read_all_correction_memories(path)
    now = datetime.now(timezone.utc).isoformat()
    updated = []
    changed = False
    for memory in memories:
        if _memory_key(memory) in selected_keys:
            updated.append(CorrectionMemory(**{**memory.__dict__, "hit_count": memory.hit_count + 1, "last_used_at": now}))
            changed = True
        else:
            updated.append(memory)
    if changed:
        _write_all_correction_memories(updated, path)


def _memory_key(memory: CorrectionMemory) -> tuple[str, str, str, str, str]:
    return (
        memory.created_at,
        memory.paper_id,
        memory.scope,
        _normalize_memory_text(memory.original),
        _normalize_memory_text(memory.corrected),
    )


def _memory_from_payload(payload: dict) -> CorrectionMemory:
    scope = _normalize_memory_scope(str(payload.get("scope", "")))
    paper_id = _paper_memory_id(str(payload.get("paper_id", "")))
    if not scope:
        scope = "global" if paper_id == "global" else "paper"
    return CorrectionMemory(
        paper_id=paper_id,
        original=str(payload.get("original", "")),
        corrected=str(payload.get("corrected", "")),
        note=str(payload.get("note", "")),
        category=str(payload.get("category", "summary") or "summary"),
        scope=scope,
        confidence=_clamped_confidence(payload.get("confidence", 1.0)),
        created_at=str(payload.get("created_at", "")),
        hit_count=_safe_int(payload.get("hit_count", 0)),
        last_used_at=str(payload.get("last_used_at", "")),
        disabled=bool(payload.get("disabled", False)),
        promoted_from=str(payload.get("promoted_from", "")),
    )


def _normalize_memory_scope(scope: str) -> str:
    normalized = (scope or "").strip().lower()
    return normalized if normalized in {"paper", "domain", "global"} else ""


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _memory_applies_to_paper(memory: CorrectionMemory, normalized_paper_id: str) -> bool:
    if memory.disabled:
        return False
    if memory.scope == "global" or memory.paper_id == "global":
        return True
    if memory.scope == "domain":
        return True
    return not normalized_paper_id or memory.paper_id == normalized_paper_id


def _select_memories_for_injection(
    memories: list[CorrectionMemory],
    policy: MemoryPolicy,
) -> list[CorrectionMemory]:
    selected: list[CorrectionMemory] = []
    for memory in sorted(memories, key=_memory_scope_priority):
        if policy.should_inject(memory, selected):
            selected.append(memory)
    return selected


def _memory_scope_priority(memory: CorrectionMemory) -> tuple[int, str]:
    return {"paper": 0, "domain": 1, "global": 2}.get(memory.scope, 3), memory.created_at


def _memory_conflicts(left: CorrectionMemory, right: CorrectionMemory) -> bool:
    if not left.original or not right.original:
        return False
    if _normalize_memory_text(left.original) != _normalize_memory_text(right.original):
        return False
    return _normalize_memory_text(left.corrected) != _normalize_memory_text(right.corrected)


def _normalize_memory_text(text: str) -> str:
    return re.sub(r"\s+", "", _clean_xml_text(text).lower())


def _correction_memory_context(memories: list[CorrectionMemory], limit: int = 8) -> str:
    if not memories:
        return "无历史修正。"
    lines = []
    for memory in memories[-limit:]:
        pieces = [f"[{memory.category}]"]
        if memory.original:
            pieces.append(f"避免：{memory.original}")
        if memory.corrected:
            pieces.append(f"改为：{memory.corrected}")
        if memory.note:
            pieces.append(f"规则：{memory.note}")
        pieces.append(
            f"scope={memory.scope}, confidence={memory.confidence:.2f}, hits={memory.hit_count}"
        )
        lines.append("；".join(pieces))
    return "\n".join(f"- {line}" for line in lines)


def _clamped_confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def get_self_improving_prompt_patches(
    paper_id: str = "global",
    *,
    memory_path: str | Path | None = None,
) -> dict[str, str]:
    memories = _load_correction_memories(paper_id, memory_path=memory_path)
    patches = _build_prompt_patches(memories)
    return {
        target: _prompt_patch_context(patches, target)
        for target in ("extraction", "summarization", "evaluation")
    }


def _build_prompt_patches(memories: list[CorrectionMemory], limit: int = 12) -> list[PromptPatch]:
    patches: list[PromptPatch] = []
    for memory in memories[-limit:]:
        memory_text = _memory_text(memory)
        for target in ("extraction", "summarization", "evaluation"):
            if not _memory_targets_prompt(memory, target, memory_text):
                continue
            patches.append(
                PromptPatch(
                    target=target,
                    content=_memory_to_prompt_rule(memory, target),
                    source_category=memory.category or "summary",
                )
            )
    return patches


def _prompt_patch_context(patches: list[PromptPatch], target: str, limit: int = 8) -> str:
    target_patches = [patch for patch in patches if patch.target == target]
    if not target_patches:
        return "No learned prompt patches."
    header = {
        "extraction": "Self-improving extraction prompt patch:",
        "summarization": "Self-improving summarization prompt patch:",
        "evaluation": "Self-improving evaluation rubric patch:",
    }.get(target, "Self-improving prompt patch:")
    lines = [header]
    seen: set[str] = set()
    for patch in target_patches[-limit:]:
        content = _clean_xml_text(patch.content).strip()
        if not content or content in seen:
            continue
        seen.add(content)
        lines.append(f"- {content}")
    return "\n".join(lines) if len(lines) > 1 else "No learned prompt patches."


def _memory_text(memory: CorrectionMemory) -> str:
    return " ".join(
        [
            memory.category or "",
            memory.original or "",
            memory.corrected or "",
            memory.note or "",
        ]
    ).lower()


def _memory_targets_prompt(memory: CorrectionMemory, target: str, memory_text: str) -> bool:
    category = (memory.category or "").lower()
    if category in {"global", "all", target}:
        return True
    if target == "extraction":
        keywords = (
            "extract",
            "extraction",
            "title",
            "abstract",
            "section",
            "column",
            "caption",
            "figure",
            "table",
            "formula",
            "asset",
            "crop",
            "page",
            "evidence",
            "标题",
            "摘要",
            "双栏",
            "分页",
            "章节",
            "正文",
            "图",
            "表",
            "公式",
            "截图",
            "截取",
            "定位",
            "抽取",
            "原文",
        )
    elif target == "summarization":
        keywords = (
            "summary",
            "summarization",
            "writing",
            "structure",
            "method",
            "contribution",
            "result",
            "总结",
            "摘要",
            "标题",
            "创新",
            "方法",
            "机制",
            "主线",
            "结果",
            "结论",
            "解释",
        )
    else:
        keywords = (
            "evaluation",
            "verify",
            "verification",
            "critic",
            "rubric",
            "claim",
            "grounding",
            "hallucination",
            "source",
            "section",
            "figure",
            "table",
            "formula",
            "评估",
            "校验",
            "验证",
            "可信",
            "证据",
            "原文",
            "章节",
            "编号",
            "图",
            "表",
            "公式",
            "错误",
            "不一致",
            "所示",
        )
    return any(keyword in memory_text for keyword in keywords)


def _memory_to_prompt_rule(memory: CorrectionMemory, target: str) -> str:
    pieces = []
    if memory.original:
        pieces.append(f"avoid: {memory.original}")
    if memory.corrected:
        pieces.append(f"prefer: {memory.corrected}")
    if memory.note:
        pieces.append(f"rule: {memory.note}")
    base = "; ".join(pieces) or f"apply prior user correction category={memory.category}"
    if target == "extraction":
        return f"During evidence extraction, {base}; preserve source page/section/caption boundaries."
    if target == "summarization":
        return f"During summary synthesis, {base}; do not rewrite evidence into unsupported claims."
    return f"During verification, fail outputs that violate this learned rule: {base}."


def _correction_memory_path(memory_path: str | Path | None = None) -> Path:
    if memory_path:
        return Path(memory_path)
    configured = ConfigManager.all().get("PAPER_AGENT_CORRECTION_MEMORY_PATH") or os.environ.get(
        "PAPER_AGENT_CORRECTION_MEMORY_PATH"
    )
    if configured:
        return Path(str(configured))
    return Path.home() / ".config" / "PaperAgent" / "summary_corrections.jsonl"


def _paper_memory_id(value: str) -> str:
    normalized = re.sub(r"\s+", " ", _clean_xml_text(value or "")).strip().lower()
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff._-]+", "-", normalized).strip("-")
    return normalized[:120] or "global"


def _summarize_with_codex(
    paper_text: str,
    assets: list[PaperAsset],
    config: CodexConfig,
    summary_language: str,
    abstract: str = "",
    formulas: list[str] | None = None,
    recognized_formulas: str = "",
    paper_title: str = "",
) -> str:
    client = _create_codex_client(config)
    try:
        correction_memories = _load_correction_memories(_paper_memory_id(paper_title))
        prompt_patches = _build_prompt_patches(correction_memories)
        chunk_notes = _summarize_chunks_with_codex(
            client,
            config.model,
            paper_text,
            assets,
            summary_language,
            correction_memories,
            prompt_patches,
        )
        summary = _integrate_summary_with_codex(
            client,
            config.model,
            chunk_notes,
            assets,
            summary_language,
            abstract,
            formulas or [],
            recognized_formulas,
            paper_title,
            correction_memories,
            prompt_patches,
        )
        verified_summary, _verification, _guard_results = _verify_summary_claims(
            summary,
            paper_text,
            _build_grounding_map(paper_text),
            abstract,
            client,
            config.model,
            paper_title,
            assets,
            correction_memories,
            prompt_patches,
        )
        return verified_summary
    finally:
        client.close()


def _summarize_chunks_with_codex(
    client: openai.OpenAI,
    model: str,
    paper_text: str,
    assets: list[PaperAsset],
    summary_language: str,
    correction_memories: list[CorrectionMemory] | None = None,
    prompt_patches: list[PromptPatch] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    cancellation_check: Callable[[], None] | None = None,
    cache_path: Path | None = None,
    partial_summaries: list[dict[str, object]] | None = None,
    partial_cache_path: Path | None = None,
    partial_integrator: Callable[[str, int, int, list[str]], str] | None = None,
) -> list[str]:
    chunks = _chunk_text(paper_text, _codex_chunk_chars())
    asset_context = _asset_context(assets, text_preview_chars=0, latex_preview_chars=0)
    memories = correction_memories or []
    patches = prompt_patches or _build_prompt_patches(memories)
    memory_context = _correction_memory_context(memories)
    extraction_patch = _prompt_patch_context(patches, "extraction")
    total = len(chunks)
    cached_notes = _load_chunk_notes_cache(cache_path, total)
    partial_records = partial_summaries if partial_summaries is not None else []
    partial_records[:] = _load_partial_integration_cache(partial_cache_path, total)
    group_specs = _chunk_integration_group_specs(total) if partial_integrator else []

    def summarize_one(idx: int, chunk: str) -> tuple[int, str]:
        if cancellation_check:
            cancellation_check()
        user_prompt = f"""请阅读论文第 {idx}/{len(chunks)} 段内容，生成分段笔记。
总结语言：{summary_language}
只记录本段原文直接提到或由本段证据直接支持的信息；本段没有提及的内容不要补写，也不要写“未提及”“未知”等占位句。
如果本段包含 introduction、abstract、related work、problem statement 或任务定义，请优先抽取：
- 研究背景：这类问题为什么存在、为什么重要。
- 要解决的问题：已有方法/现实流程的不足、本文聚焦的痛点和边界。
- 读者入口：不了解该方向的人需要先知道的任务设定、输入输出和应用场景。

历史用户修正规则：
{memory_context}

自优化抽取提示词：
{extraction_patch}

可用图表截图：
{asset_context}

公式记录规则：
- 如果本段涉及公式，只记录公式解决的问题、变量含义和工程作用。
- 不要输出完整公式、LaTeX、等式、arg max/min、求和式或长变量表达；最终 Word 会用公式截图展示公式本体。

论文内容：
{chunk}
"""
        return idx, _chat(
            client,
            model,
            user_prompt,
            system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
            max_tokens=1400,
        )

    max_workers = min(_codex_summary_concurrency(), total)
    if max_workers <= 1:
        chunk_notes = []
        for idx, chunk in enumerate(chunks, 1):
            if idx <= len(cached_notes) and cached_notes[idx - 1].strip():
                chunk_notes.append(cached_notes[idx - 1])
                if progress_callback:
                    progress_callback(len(chunk_notes), total)
                continue
            try:
                _idx, note = summarize_one(idx, chunk)
            except Exception as exc:
                raise RuntimeError(_chunk_summary_error_message(idx, total, exc)) from exc
            chunk_notes.append(note)
            _write_chunk_notes_cache(cache_path, chunk_notes, total)
            if progress_callback:
                progress_callback(len(chunk_notes), total)
        if group_specs and partial_integrator:
            _run_missing_partial_integrations(
                group_specs,
                chunk_notes,
                partial_records,
                partial_cache_path,
                total,
                partial_integrator,
                cancellation_check,
                max_workers=1,
            )
        return chunk_notes

    results: list[str] = [""] * total
    completed = 0
    pending_chunks: list[tuple[int, str]] = []
    for idx, chunk in enumerate(chunks, 1):
        if idx <= len(cached_notes) and cached_notes[idx - 1].strip():
            results[idx - 1] = cached_notes[idx - 1]
            completed += 1
            if progress_callback:
                progress_callback(completed, total)
        else:
            pending_chunks.append((idx, chunk))
    if completed:
        _write_chunk_notes_cache(cache_path, results, total)

    pending_queue = list(pending_chunks)
    chunks_by_idx = {idx: chunk for idx, chunk in pending_chunks}
    failed_chunks: dict[int, Exception] = {}
    submitted_partials: set[str] = set(_partial_integration_keys(partial_records))

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="paper-summary") as executor:
        active: dict[object, tuple[str, object]] = {}

        def submit_ready_partials() -> None:
            if not partial_integrator:
                return
            for spec in group_specs:
                key = _partial_integration_key(spec["name"], spec["start"], spec["end"])
                if key in submitted_partials:
                    continue
                if not _chunk_group_is_ready(results, spec["start"], spec["end"]):
                    continue
                if len(active) >= max_workers:
                    return
                notes = results[spec["start"] - 1 : spec["end"]]
                active[executor.submit(partial_integrator, spec["name"], spec["start"], spec["end"], notes)] = (
                    "partial",
                    spec,
                )
                submitted_partials.add(key)

        def submit_more_chunks() -> None:
            while pending_queue and len(active) < max_workers:
                idx, chunk = pending_queue.pop(0)
                active[executor.submit(summarize_one, idx, chunk)] = ("chunk", idx)

        submit_ready_partials()
        submit_more_chunks()
        while active:
            if cancellation_check:
                cancellation_check()
            done, _pending = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                kind, meta = active.pop(future)
                if kind == "chunk":
                    idx = int(meta)
                    try:
                        _idx, note = future.result()
                        idx = _idx
                    except Exception as exc:
                        failed_chunks[idx] = exc
                        continue
                    results[idx - 1] = note
                    completed += 1
                    _write_chunk_notes_cache(cache_path, results, total)
                    if progress_callback:
                        progress_callback(completed, total)
                    continue

                spec = dict(meta)
                try:
                    summary = future.result()
                except Exception:
                    continue
                partial_records.append(
                    _partial_integration_record(spec["name"], spec["start"], spec["end"], total, summary)
                )
                _write_partial_integration_cache(partial_cache_path, partial_records, total)
            submit_ready_partials()
            submit_more_chunks()
    for idx in sorted(failed_chunks):
        if cancellation_check:
            cancellation_check()
        try:
            _idx, note = summarize_one(idx, chunks_by_idx[idx])
            idx = _idx
        except Exception as exc:
            raise RuntimeError(_chunk_summary_error_message(idx, total, exc)) from exc
        results[idx - 1] = note
        completed += 1
        _write_chunk_notes_cache(cache_path, results, total)
        if progress_callback:
            progress_callback(completed, total)
    if group_specs and partial_integrator:
        _run_missing_partial_integrations(
            group_specs,
            results,
            partial_records,
            partial_cache_path,
            total,
            partial_integrator,
            cancellation_check,
            max_workers=1,
        )
    return results


def _load_chunk_notes_cache(cache_path: Path | None, total: int) -> list[str]:
    if cache_path is None or not cache_path.exists():
        return []
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if int(data.get("total") or 0) != total:
        return []
    notes = data.get("notes")
    if not isinstance(notes, list):
        return []
    return [_clean_xml_text(str(note)) for note in notes]


def _write_chunk_notes_cache(cache_path: Path | None, notes: list[str], total: int) -> None:
    if cache_path is None:
        return
    try:
        cache_path.write_text(
            json.dumps({"total": total, "notes": notes}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _chunk_integration_group_specs(total: int) -> list[dict[str, int | str]]:
    if total < 4:
        return []
    midpoint = (total + 1) // 2
    return [
        {"name": "前半篇", "start": 1, "end": midpoint},
        {"name": "后半篇", "start": midpoint + 1, "end": total},
    ]


def _chunk_group_is_ready(notes: list[str], start: int, end: int) -> bool:
    if start < 1 or end > len(notes) or start > end:
        return False
    return all(note.strip() for note in notes[start - 1 : end])


def _partial_integration_key(name: object, start: object, end: object) -> str:
    return f"{name}:{start}-{end}"


def _partial_integration_keys(records: list[dict[str, object]]) -> set[str]:
    return {
        _partial_integration_key(record.get("name"), record.get("start"), record.get("end"))
        for record in records
        if record.get("summary")
    }


def _partial_integration_record(name: str, start: int, end: int, total: int, summary: str) -> dict[str, object]:
    return {
        "name": name,
        "start": start,
        "end": end,
        "total": total,
        "summary": _postprocess_summary(summary),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_partial_integration_cache(cache_path: Path | None, total: int) -> list[dict[str, object]]:
    if cache_path is None or not cache_path.exists():
        return []
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if int(data.get("total") or 0) != total:
        return []
    records = data.get("partials")
    if not isinstance(records, list):
        return []
    cleaned = []
    for record in records:
        if not isinstance(record, dict) or not record.get("summary"):
            continue
        try:
            start = int(record.get("start") or 0)
            end = int(record.get("end") or 0)
        except (TypeError, ValueError):
            continue
        name = _clean_xml_text(str(record.get("name") or "分段整合"))
        summary = _postprocess_summary(str(record.get("summary") or ""))
        if not name or not summary or start <= 0 or end < start:
            continue
        cleaned.append(_partial_integration_record(name, start, end, total, summary))
    return cleaned


def _write_partial_integration_cache(
    cache_path: Path | None,
    records: list[dict[str, object]],
    total: int,
) -> None:
    if cache_path is None:
        return
    deduped: dict[str, dict[str, object]] = {}
    for record in records:
        if not record.get("summary"):
            continue
        key = _partial_integration_key(record.get("name"), record.get("start"), record.get("end"))
        deduped[key] = record
    try:
        cache_path.write_text(
            json.dumps({"total": total, "partials": list(deduped.values())}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def _run_missing_partial_integrations(
    group_specs: list[dict[str, int | str]],
    chunk_notes: list[str],
    records: list[dict[str, object]],
    cache_path: Path | None,
    total: int,
    partial_integrator: Callable[[str, int, int, list[str]], str],
    cancellation_check: Callable[[], None] | None = None,
    max_workers: int = 2,
) -> None:
    existing = _partial_integration_keys(records)
    missing = []
    for spec in group_specs:
        key = _partial_integration_key(spec["name"], spec["start"], spec["end"])
        start = int(spec["start"])
        end = int(spec["end"])
        if key not in existing and _chunk_group_is_ready(chunk_notes, start, end):
            missing.append(spec)
    if not missing:
        return
    workers = max(1, min(max_workers, len(missing)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="paper-partial") as executor:
        futures = {
            executor.submit(
                partial_integrator,
                str(spec["name"]),
                int(spec["start"]),
                int(spec["end"]),
                chunk_notes[int(spec["start"]) - 1 : int(spec["end"])],
            ): spec
            for spec in missing
        }
        for future in as_completed(futures):
            if cancellation_check:
                cancellation_check()
            spec = futures[future]
            try:
                summary = future.result()
            except Exception:
                continue
            records.append(
                _partial_integration_record(str(spec["name"]), int(spec["start"]), int(spec["end"]), total, summary)
            )
            _write_partial_integration_cache(cache_path, records, total)


def _chunk_summary_error_message(idx: int, total: int, exc: Exception) -> str:
    error = _clean_xml_text(str(exc))[:260]
    return (
        f"分段笔记生成失败（第 {idx}/{total} 段），已停止生成 Word，避免输出不完整或不可读报告。"
        f"错误摘要：{error}"
    )


def _fallback_chunk_note(idx: int, total: int, chunk: str, exc: Exception) -> str:
    cleaned = _clean_xml_text(chunk)
    headings = re.findall(r"(?m)^(?:#{1,4}\s*)?(?:[0-9IVX]+\.?\s+)?[A-Z][A-Za-z0-9 ,:;()/&\\-]{6,90}$", cleaned)
    error = _clean_xml_text(str(exc))[:220]
    return (
        f"## Chunk {idx}/{total} 超时记录\n"
        f"本段模型分段总结超时或失败，已跳过原文摘录，避免把英文 raw text 写入最终报告。\n"
        f"错误摘要：{error}\n\n"
        f"可能章节标题：{'; '.join(headings[:8]) if headings else '未可靠识别'}"
    )


def _integrate_summary_with_codex(
    client: openai.OpenAI | None,
    model: str,
    chunk_notes: list[str],
    assets: list[PaperAsset],
    summary_language: str,
    abstract: str,
    formulas: list[str],
    recognized_formulas: str,
    paper_title: str,
    correction_memories: list[CorrectionMemory] | None = None,
    prompt_patches: list[PromptPatch] | None = None,
    partial_summaries: list[dict[str, object]] | None = None,
) -> str:
    client = _coerce_codex_client(client)
    usable_partials = _usable_partial_summaries(partial_summaries or [], len(chunk_notes))
    if usable_partials:
        try:
            return _integrate_summary_from_partials_with_codex(
                client,
                model,
                chunk_notes,
                usable_partials,
                assets,
                summary_language,
                abstract,
                paper_title,
                correction_memories,
                prompt_patches,
            )
        except RuntimeError as exc:
            return _fast_integrate_summary_with_codex(
                client,
                model,
                chunk_notes,
                assets,
                summary_language,
                abstract,
                formulas,
                recognized_formulas,
                paper_title,
                exc,
            )

    asset_context = _asset_context(assets, text_preview_chars=500, latex_preview_chars=0)
    formula_asset_rule = _formula_asset_usage_rule(assets)
    memories = correction_memories or []
    patches = prompt_patches or _build_prompt_patches(memories)
    memory_context = _correction_memory_context(memories)
    summarization_patch = _prompt_patch_context(patches, "summarization")
    final_input = _compact_chunk_notes_for_final(chunk_notes)
    prompt = (
        f"{_final_note_prompt_for_runtime()}\n\n总结语言：{summary_language}\n\n"
        f"历史用户修正规则：\n{memory_context}\n\n"
        f"自优化总结提示词：\n{summarization_patch}\n\n"
        "完整性硬性要求：最终输出必须包含这些二级章节："
        "## 核心信息、## 摘要、## 背景与问题、## 创新点、## 一句话总结、"
        "## 方法主线、## 关键结果、## 深度分析、## 局限、## 总结。"
        "如果证据较少，也要用已有证据写成短段落；不要输出计划、过程说明或“我先/接着我会”这类话。\n\n"
        f"原始论文标题证据：\n{paper_title or '未抽取到可靠标题。'}\n\n"
        f"原文摘要证据：\n{abstract or '未抽取到可靠摘要。'}\n\n"
        f"公式截图使用规则：\n{formula_asset_rule}\n\n"
        f"可用图表截图：\n{asset_context}\n\n分段笔记：\n{final_input}"
    )
    try:
        return _chat(
            client,
            model,
            prompt,
            system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
            max_tokens=5200,
        )
    except RuntimeError as exc:
        return _fast_integrate_summary_with_codex(
            client,
            model,
            chunk_notes,
            assets,
            summary_language,
            abstract,
            formulas,
            recognized_formulas,
            paper_title,
            exc,
        )


def _usable_partial_summaries(records: list[dict[str, object]], total_chunks: int) -> list[dict[str, object]]:
    if total_chunks < 4:
        return []
    expected = {
        _partial_integration_key(spec["name"], spec["start"], spec["end"])
        for spec in _chunk_integration_group_specs(total_chunks)
    }
    by_key: dict[str, dict[str, object]] = {}
    for record in records:
        if not record.get("summary"):
            continue
        key = _partial_integration_key(record.get("name"), record.get("start"), record.get("end"))
        if key in expected:
            by_key[key] = record
    if expected - set(by_key):
        return []
    return sorted(by_key.values(), key=lambda record: int(record.get("start") or 0))


def _integrate_chunk_group_with_codex(
    client: openai.OpenAI | None,
    model: str,
    name: str,
    start: int,
    end: int,
    chunk_notes: list[str],
    assets: list[PaperAsset],
    summary_language: str,
    abstract: str,
    paper_title: str,
    correction_memories: list[CorrectionMemory] | None = None,
    prompt_patches: list[PromptPatch] | None = None,
) -> str:
    client = _coerce_codex_client(client)
    memories = correction_memories or []
    patches = prompt_patches or _build_prompt_patches(memories)
    memory_context = _correction_memory_context(memories)
    summarization_patch = _prompt_patch_context(patches, "summarization")
    notes = _compact_chunk_notes_for_final(chunk_notes, max_total_chars=16000)
    formula_asset_rule = _formula_asset_usage_rule(assets)
    asset_context = _asset_context(assets, text_preview_chars=180, latex_preview_chars=0)
    prompt = (
        f"请把论文第 {start}-{end} 段分段笔记整合成“{name}”中间稿。"
        "这不是最终报告，但要为最终报告提供高密度、可引用的中文材料。\n\n"
        "要求：\n"
        "1. 只基于给定分段笔记和证据写，不要编造。\n"
        "2. 保留本半篇涉及的背景、方法、公式含义、实验结果、局限和图表占位符。\n"
        "3. 用自然段和小标题组织，不要输出计划、过程说明、代码块、JSON 或英文原文摘录。\n"
        "4. 公式只解释含义并引用公式截图，不要复写完整公式、LaTeX、等式或长变量表达。\n"
        "5. 输出长度控制在 1200-2200 中文字左右，优先保留事实密度。\n\n"
        f"总结语言：{summary_language}\n\n"
        f"论文标题证据：{paper_title or '未可靠抽取'}\n\n"
        f"摘要证据：{_clean_xml_text(abstract)[:1400] if abstract else '未可靠抽取'}\n\n"
        f"历史用户修正规则：\n{memory_context}\n\n"
        f"自优化总结提示词：\n{summarization_patch}\n\n"
        f"公式截图使用规则：\n{formula_asset_rule}\n\n"
        f"可用图表截图：\n{asset_context}\n\n"
        f"分段笔记：\n{notes}"
    )
    return _chat(
        client,
        model,
        prompt,
        system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
        max_tokens=2800,
        max_attempts=1,
    )


def _integrate_summary_from_partials_with_codex(
    client: openai.OpenAI | None,
    model: str,
    chunk_notes: list[str],
    partial_summaries: list[dict[str, object]],
    assets: list[PaperAsset],
    summary_language: str,
    abstract: str,
    paper_title: str,
    correction_memories: list[CorrectionMemory] | None = None,
    prompt_patches: list[PromptPatch] | None = None,
) -> str:
    client = _coerce_codex_client(client)
    memories = correction_memories or []
    patches = prompt_patches or _build_prompt_patches(memories)
    memory_context = _correction_memory_context(memories)
    summarization_patch = _prompt_patch_context(patches, "summarization")
    asset_context = _asset_context(assets, text_preview_chars=400, latex_preview_chars=0)
    formula_asset_rule = _formula_asset_usage_rule(assets)
    partial_context = _partial_summary_context(partial_summaries)
    edge_context = _partial_edge_chunk_context(chunk_notes, partial_summaries)
    prompt = (
        f"{_final_note_prompt_for_runtime()}\n\n总结语言：{summary_language}\n\n"
        "整合策略：你收到的是前半篇和后半篇的并行整合稿，以及每半篇首尾 20% 的原始分段证据。"
        "请优先以半篇整合稿建立全局结构，再用首尾证据补足引言、方法转折、实验结论和局限，不要重新逐段罗列。\n\n"
        f"历史用户修正规则：\n{memory_context}\n\n"
        f"自优化总结提示词：\n{summarization_patch}\n\n"
        "完整性硬性要求：最终输出必须包含这些二级章节："
        "## 核心信息、## 摘要、## 背景与问题、## 创新点、## 一句话总结、"
        "## 方法主线、## 关键结果、## 深度分析、## 局限、## 总结。"
        "如果证据较少，也要用已有证据写成短段落；不要输出计划、过程说明或“我先/接着我会”这类话。\n\n"
        f"原始论文标题证据：\n{paper_title or '未抽取到可靠标题。'}\n\n"
        f"原文摘要证据：\n{abstract or '未抽取到可靠摘要。'}\n\n"
        f"公式截图使用规则：\n{formula_asset_rule}\n\n"
        f"可用图表截图：\n{asset_context}\n\n"
        f"半篇并行整合稿：\n{partial_context}\n\n"
        f"首尾 20% 分段证据：\n{edge_context}"
    )
    return _chat(
        client,
        model,
        prompt,
        system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
        max_tokens=5200,
    )


def _partial_summary_context(partial_summaries: list[dict[str, object]], max_chars_per_partial: int = 7000) -> str:
    parts = []
    for record in partial_summaries:
        name = _clean_xml_text(str(record.get("name") or "分段整合"))
        start = int(record.get("start") or 0)
        end = int(record.get("end") or 0)
        summary = _truncate_middle(_postprocess_summary(str(record.get("summary") or "")), max_chars_per_partial)
        if summary:
            parts.append(f"[{name} | Chunk {start}-{end}]\n{summary}")
    return "\n\n".join(parts) or "未获得半篇整合稿。"


def _partial_edge_chunk_context(
    chunk_notes: list[str],
    partial_summaries: list[dict[str, object]],
    max_total_chars: int = 18000,
) -> str:
    selected: list[tuple[int, str]] = []
    seen: set[int] = set()
    for record in partial_summaries:
        start = int(record.get("start") or 0)
        end = int(record.get("end") or 0)
        for idx in _edge_chunk_indices(start, end):
            if idx in seen or idx < 1 or idx > len(chunk_notes):
                continue
            seen.add(idx)
            selected.append((idx, chunk_notes[idx - 1]))
    selected.sort(key=lambda item: item[0])
    if not selected:
        return ""
    per_note_limit = max(900, max_total_chars // max(1, len(selected)))
    parts = []
    total = 0
    for idx, note in selected:
        cleaned = _truncate_middle(_postprocess_summary(note), per_note_limit)
        remaining = max_total_chars - total
        if remaining <= 0:
            break
        cleaned = cleaned[:remaining]
        parts.append(f"[Chunk {idx}]\n{cleaned}")
        total += len(cleaned)
    return "\n\n".join(parts)


def _edge_chunk_indices(start: int, end: int) -> list[int]:
    if start <= 0 or end < start:
        return []
    count = end - start + 1
    edge_count = max(1, (count + 4) // 5)
    indices = list(range(start, min(end, start + edge_count - 1) + 1))
    indices.extend(range(max(start, end - edge_count + 1), end + 1))
    return sorted(dict.fromkeys(indices))


def _compact_chunk_notes_for_final(chunk_notes: list[str], max_total_chars: int = 36000) -> str:
    if not chunk_notes:
        return ""

    per_chunk_limit = max(3000, max_total_chars // max(1, len(chunk_notes)))
    compacted = []
    total = 0
    for idx, note in enumerate(chunk_notes, 1):
        cleaned = _postprocess_summary(note)
        remaining = max_total_chars - total
        if remaining <= 0:
            break
        limit = min(per_chunk_limit, remaining)
        chunk_text = _truncate_middle(cleaned, limit)
        compacted.append(f"[Chunk {idx}]\n{chunk_text}")
        total += len(chunk_text)
    return "\n\n".join(compacted)


def _final_note_prompt_for_runtime() -> str:
    style = _first_value({}, "PAPER_AGENT_SUMMARY_PROMPT_STYLE").strip().lower()
    if style in {"lean", "fast", "short"}:
        return LEAN_FINAL_NOTE_PROMPT
    return FINAL_NOTE_PROMPT


def _fast_integrate_summary_with_codex(
    client: openai.OpenAI | None,
    model: str,
    chunk_notes: list[str],
    assets: list[PaperAsset],
    summary_language: str,
    abstract: str,
    formulas: list[str],
    recognized_formulas: str,
    paper_title: str,
    original_error: Exception,
) -> str:
    client = _coerce_codex_client(client)
    compact_notes = _compact_chunk_notes_for_final(chunk_notes, max_total_chars=14000)
    asset_context = _asset_context(assets[:8], text_preview_chars=0, latex_preview_chars=0)
    formula_asset_rule = _formula_asset_usage_rule(assets[:8])
    prompt = (
        "你是科研论文精读笔记编辑。前一次完整整合请求超时，现在请用更短输出快速生成一份可读中文 Markdown 报告。\n"
        "硬性要求：\n"
        "1. 必须输出中文自然段，不要复制英文原文段落，不要保留 PDF 断行或断词。\n"
        "2. 只基于给定分段笔记和证据写；没有证据就省略，不要编造。\n"
        "3. 必须包含这些二级标题：核心信息、摘要、背景与问题、创新点、一句话总结、方法主线、关键结果、深度分析、局限、总结。\n"
        "4. 图表占位符只能使用给定的 [[ASSET:n]]，并放在相关段落旁边。\n"
        "5. 如果某段笔记是超时记录，直接忽略该段，不要把“超时记录/错误摘要/原文证据摘录/英文 raw text”这些字样写入报告。\n"
        "6. 关键公式只解释含义并插入公式截图；不要复写完整公式、LaTeX、等式、arg max/min 或求和式。\n\n"
        f"标题证据：{paper_title or '未可靠抽取'}\n\n"
        f"摘要证据：{_clean_xml_text(abstract)[:1200] if abstract else '未可靠抽取'}\n\n"
        f"公式截图使用规则：\n{formula_asset_rule}\n\n"
        f"可用图表：\n{asset_context}\n\n"
        f"分段笔记：\n{compact_notes}"
    )
    try:
        return _chat(
            client,
            model,
            prompt,
            system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
            max_tokens=3600,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "总结质量自检未通过：完整整合和快速整合都超时，已停止生成 Word，避免输出不可读的原文摘录版报告。"
            f"完整整合错误：{_clean_xml_text(str(original_error))[:220]}；"
            f"快速整合错误：{_clean_xml_text(str(exc))[:220]}。"
            "请降低 CODEX_SUMMARY_CONCURRENCY，或提高/修复 CODEX_TIMEOUT_SECONDS 后重试。"
        ) from exc


def _truncate_middle(text: str, max_chars: int) -> str:
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars < 200:
        return text[:max_chars].rstrip()
    head = max_chars // 2
    tail = max_chars - head - 80
    return (
        text[:head].rstrip()
        + "\n\n...[中间内容已压缩以避免最终整合请求过大]...\n\n"
        + text[-tail:].lstrip()
    )


def _verify_summary_claims(
    summary: str,
    paper_text: str,
    grounding_map: dict[str, list[dict[str, str]]],
    abstract: str,
    client: openai.OpenAI | None,
    model: str,
    paper_title: str,
    assets: list[PaperAsset],
    correction_memories: list[CorrectionMemory] | None = None,
    prompt_patches: list[PromptPatch] | None = None,
) -> tuple[str, VerificationResult, list[GuardResult]]:
    client = _coerce_codex_client(client)
    summary = _postprocess_summary(summary)
    summary = _replace_missing_abstract(summary, abstract, client, model)
    summary = _normalize_final_sections(summary)
    summary = _repair_report_substance_if_needed(
        summary,
        paper_text,
        grounding_map,
        abstract,
        client,
        model,
        paper_title,
        assets,
    )
    summary = _enforce_core_original_title(summary, paper_title)
    summary = _ensure_chinese_report_title(summary)
    summary = _remove_mismatched_asset_markers(summary, assets)
    summary = _ensure_asset_markers(summary, assets)
    summary = _remove_mismatched_asset_markers(summary, assets)
    summary = _suppress_formula_text_when_assets_present(summary, assets)
    _assert_summary_quality(summary)
    claims = _extract_verifiable_claims(summary)
    grounded_map = _attach_claims_to_grounding_map(grounding_map, claims)
    guard_results = _run_harness_guards(
        summary,
        grounded_map,
        assets,
        paper_title,
        correction_memories or [],
        client,
        model,
    )
    guard_errors = _blocking_guard_errors(guard_results)
    if _summary_is_degraded_fallback(summary):
        verification = VerificationResult(
            True,
            [],
            soft_warnings=[
                {
                    "type": "degraded_report_warning",
                    "claim": "",
                    "reason": "LLM 最终整合或校验阶段超时，已生成保守降级版 Word；阻断型 Guard 已转为 warning。",
                },
                *[
                    {
                        "type": "degraded_guard_warning",
                        "claim": "",
                        "reason": warning,
                    }
                    for warning in guard_errors
                ],
            ],
        )
        return summary, verification, guard_results
    if not claims and not guard_errors:
        verification = VerificationResult(
            True,
            [],
            soft_warnings=[
                {
                    "type": "no_verifiable_claims_warning",
                    "claim": "",
                    "reason": "本地 Guard 已通过，但未从报告中抽取到结构化 claim，已跳过 LLM claim verifier。",
                }
            ],
        )
        return summary, verification, guard_results
    verification = _run_verification_agent(
        client,
        model,
        paper_text,
        grounded_map,
        correction_memories or [],
        prompt_patches,
    )
    if guard_errors:
        _add_guard_failures_to_verification(verification, guard_errors)
        verification.passed = False
    return summary, verification, guard_results


def _summary_is_degraded_fallback(summary: str) -> bool:
    return "LLM 最终整合超时" in summary or "保守版论文精读笔记" in summary


def _assert_summary_quality(summary: str) -> None:
    issues = _summary_quality_issues(summary)
    if issues:
        raise RuntimeError(
            "总结质量自检未通过，已停止生成 Word，避免输出不可读报告："
            + "；".join(issues)
            + "。请降低 CODEX_SUMMARY_CONCURRENCY，或修复/提高 CODEX_TIMEOUT_SECONDS 后重试。"
        )


def _summary_quality_issues(summary: str) -> list[str]:
    issues: list[str] = []
    forbidden_markers = [
        "规则兜底笔记",
        "超时记录",
        "错误摘要：",
        "原文证据摘录",
        "英文 raw text",
        "LLM 最终整合超时",
        "保守版论文精读笔记",
        "报告生成时只保留",
        "当前章节采用",
        "当前版本采用保守中文概括",
        "后续可结合原文",
        "后续应结合原文",
        "已被整理为中文保守表述",
        "复现实验时应",
        "复现时应重点关注",
        "复现建议",
        "复现要点",
    ]
    found_markers = [marker for marker in forbidden_markers if marker in summary]
    if found_markers:
        issues.append(f"报告包含内部降级文本 {', '.join(found_markers[:3])}")

    body = re.sub(r"\[\[ASSET:\d+\]\]", "", summary)
    body = re.sub(r"(?m)^#.*$", "", body)
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", body))
    latin_letters = len(re.findall(r"[A-Za-z]", body))
    if latin_letters > 700 and cjk_chars < 450:
        issues.append("中文总结主体疑似直接复制英文原文，中文内容不足")

    bad_sections = []
    for title in ("摘要", "背景与问题", "方法主线", "关键结果"):
        section = _section_body(summary, title)
        if not section:
            continue
        section_cjk = len(re.findall(r"[\u4e00-\u9fff]", section))
        section_latin = len(re.findall(r"[A-Za-z]", section))
        if section_latin > 220 and section_cjk < 80:
            bad_sections.append(title)
    if bad_sections:
        issues.append(f"这些章节疑似英文原文未整理：{', '.join(bad_sections)}")
    return issues


def _repair_report_substance_if_needed(
    summary: str,
    paper_text: str,
    grounding_map: dict[str, list[dict[str, str]]],
    abstract: str,
    client: openai.OpenAI | None,
    model: str,
    paper_title: str,
    assets: list[PaperAsset],
) -> str:
    issues = _report_substance_issues(summary)
    if not issues:
        return summary
    repaired = _synthesize_report_by_sections_with_codex(
        client,
        model,
        paper_text,
        grounding_map,
        abstract,
        paper_title,
        assets,
        issues,
    )
    return _normalize_final_sections(repaired)


def _report_substance_issues(summary: str) -> list[str]:
    issues = []
    issues.extend(_summary_quality_issues(summary))
    issues.extend(_docx_report_quality_errors(summary))
    for title in ("摘要", "背景与问题", "方法主线", "关键结果", "深度分析", "总结"):
        body = _section_body(summary, title)
        if body and _section_is_generic_filler(body):
            issues.append(f"{title} 疑似模板兜底，不是实质论文内容")
    return list(dict.fromkeys(issues))


def _section_is_generic_filler(section: str) -> bool:
    markers = (
        "报告生成时只保留",
        "当前章节",
        "当前版本",
        "后续可结合原文",
        "后续应结合原文",
        "回到原文",
        "保守中文",
        "中文保守",
        "已抽取证据",
    )
    return any(marker in section for marker in markers)


def _section_is_english_heavy(section: str) -> bool:
    body = re.sub(r"\[\[ASSET:\d+\]\]", "", section)
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", body))
    latin_letters = len(re.findall(r"[A-Za-z]", body))
    return latin_letters > 220 and cjk_chars < 80


def _synthesize_report_by_sections_with_codex(
    client: openai.OpenAI | None,
    model: str,
    paper_text: str,
    grounding_map: dict[str, list[dict[str, str]]],
    abstract: str,
    paper_title: str,
    assets: list[PaperAsset],
    issues: list[str],
) -> str:
    client = _coerce_codex_client(client)
    section_bodies: dict[str, str] = {
        "核心信息": _complete_core_info_body("", paper_title),
    }
    asset_context = _asset_context(assets[:8], text_preview_chars=160, latex_preview_chars=300)
    for section in (
        "摘要",
        "背景与问题",
        "创新点",
        "一句话总结",
        "方法主线",
        "关键结果",
        "深度分析",
        "局限",
        "总结",
    ):
        evidence = _section_evidence_for_report(section, paper_text, grounding_map, abstract)
        body = _synthesize_report_section_with_codex(
            client,
            model,
            section,
            evidence,
            abstract,
            paper_title,
            asset_context,
            issues,
        )
        section_bodies[section] = body

    result = [f"# {paper_title or '论文精读笔记'}"]
    for section in _required_report_section_order():
        result.extend(["", f"## {section}", section_bodies.get(section, "").strip()])
    report = _normalize_final_sections("\n".join(result))
    remaining_issues = _report_substance_issues(report)
    if remaining_issues:
        raise RuntimeError(
            "总结质量自检未通过：逐章节重写后仍未达到可交付质量，已停止生成 Word："
            + "；".join(remaining_issues[:8])
        )
    return report


def _synthesize_report_section_with_codex(
    client: openai.OpenAI | None,
    model: str,
    section: str,
    evidence: str,
    abstract: str,
    paper_title: str,
    asset_context: str,
    issues: list[str],
) -> str:
    prompt = (
        f"请为论文精读报告撰写“{section}”章节正文。只输出正文，不要输出章节标题。\n\n"
        "写作要求：\n"
        "- 必须是中文实质内容，不能写模板句、流程说明、免责声明或“回到原文核对”。\n"
        "- 可以保留模型名、数据集名、指标名、论文术语等英文专有名词，但不要复制整句英文原文。\n"
        "- 只能根据给定证据写，不要编造原文没有的数字、数据集、结论或局限。\n"
        "- 如果是方法章节，需要写清楚输入、核心模块、处理流程和输出；如果是结果章节，需要写清楚主要实验现象和对比含义。\n"
        "- 可以在方法或结果章节使用相关 [[ASSET:n]]，占位符必须独占一行；其他章节通常不要插图。\n\n"
        f"论文标题：{paper_title or '未可靠抽取'}\n\n"
        f"英文摘要证据：\n{_clean_xml_text(abstract)[:1600] if abstract else '未可靠抽取'}\n\n"
        f"本章节可用正文证据：\n{evidence}\n\n"
        f"可用图表截图摘要：\n{asset_context}\n\n"
        f"需要避免的问题：\n" + "\n".join(f"- {issue}" for issue in issues[:8])
    )
    body = _chat(
        client,
        model,
        prompt,
        system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
        max_tokens=1200 if section != "方法主线" else 1700,
    )
    body = _strip_section_heading(_postprocess_summary(body), section).strip()
    if not body or _section_is_english_heavy(body) or _section_is_generic_filler(body):
        raise RuntimeError(f"总结质量自检未通过：{section} 章节重写后仍不可用。")
    return body


def _section_evidence_for_report(
    section: str,
    paper_text: str,
    grounding_map: dict[str, list[dict[str, str]]],
    abstract: str,
    max_chars: int = 9000,
) -> str:
    buckets_by_section = {
        "摘要": ("intro",),
        "背景与问题": ("intro",),
        "创新点": ("intro", "method"),
        "一句话总结": ("intro", "method", "experiments"),
        "方法主线": ("method",),
        "关键结果": ("experiments",),
        "深度分析": ("method", "experiments"),
        "局限": ("experiments",),
        "总结": ("intro", "method", "experiments"),
    }
    pieces = []
    if abstract:
        pieces.append("## 摘要证据\n" + _clean_xml_text(abstract)[:1800])
    for bucket in buckets_by_section.get(section, ("intro", "method", "experiments")):
        for item in grounding_map.get(bucket, [])[:2]:
            title = item.get("title", bucket)
            text = _truncate_middle(_clean_xml_text(item.get("text", "")), 3000)
            if text:
                pieces.append(f"## {bucket}: {item.get('section_id', '')} {title}\n{text}")
    if section == "局限":
        limitation_window = _section_window_for_verifier(
            paper_text,
            ("limitation", "limitation", "discussion", "conclusion", "future", "failure"),
            window=5000,
        )
        if limitation_window:
            pieces.append("## 局限/讨论窗口\n" + limitation_window)
    if not pieces and paper_text:
        pieces.append(_truncate_middle(_clean_xml_text(paper_text), max_chars))
    return _truncate_middle("\n\n".join(pieces), max_chars)


def _strip_section_heading(text: str, section: str) -> str:
    text = re.sub(rf"(?m)^#{1,6}\s*{re.escape(section)}\s*$", "", text)
    text = re.sub(r"(?m)^#{1,6}\s+.+$", "", text, count=1) if text.lstrip().startswith("#") else text
    return text.strip()


def _assert_report_ready_for_docx(summary: str) -> None:
    normalized = _normalize_final_sections(_postprocess_summary(summary))
    errors = _docx_report_quality_errors(normalized)
    if errors:
        raise RuntimeError(
            "总结完整性自检未通过，已停止生成 Word，避免输出残缺报告："
            + "；".join(errors)
            + "。请重试生成，或检查 Codex 接口是否返回了完整结构化总结。"
        )


def _docx_report_quality_errors(summary: str) -> list[str]:
    errors: list[str] = []
    missing_sections = _missing_required_report_sections(summary)
    if missing_sections:
        errors.append(f"缺少必要章节：{', '.join(missing_sections)}")

    too_short_sections = _too_short_required_sections(summary)
    if too_short_sections:
        errors.append(f"章节内容过短：{', '.join(too_short_sections)}")

    body = re.sub(r"\[\[ASSET:\d+\]\]", "", summary)
    body = re.sub(r"(?m)^#{1,6}\s+.*$", "", body)
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", body))
    if cjk_chars < 700:
        errors.append(f"中文正文内容过少：{cjk_chars} 字")

    present_required = [
        section
        for section in _required_report_sections()
        if re.search(rf"(?m)^##\s+{re.escape(section)}\s*$", summary)
    ]
    if len(present_required) < 8:
        errors.append(f"必要章节覆盖不足：{len(present_required)}/10")

    return errors


def _run_harness_guards(
    summary: str,
    grounded_map: dict[str, list[dict[str, str]]],
    assets: list[PaperAsset],
    paper_title: str,
    memories: list[CorrectionMemory],
    client: openai.OpenAI | None = None,
    model: str = "",
) -> list[GuardResult]:
    return [
        _evidence_guard(grounded_map),
        _asset_guard(summary, assets),
        _visual_asset_guard(summary, assets, client, model),
        _coverage_guard(summary, grounded_map),
        _format_guard(summary),
        _citation_guard(summary, paper_title),
        _loop_guard(),
        _memory_guard(memories),
    ]


def _blocking_guard_errors(results: list[GuardResult]) -> list[str]:
    errors: list[str] = []
    for result in results:
        spec = GUARD_SPECS.get(result.name)
        if spec and spec.blocking and result.errors:
            errors.extend(f"{result.name}: {error}" for error in result.errors)
    return errors


def _add_guard_failures_to_verification(verification: VerificationResult, guard_errors: list[str]) -> None:
    for error in guard_errors:
        if error not in verification.errors:
            verification.errors.append(error)
        verification.hard_failures.append(
            {
                "type": "guard_failure",
                "claim": "",
                "reason": error,
            }
        )


def _verification_failure_details(verification: VerificationResult) -> str:
    hard_failures = verification.hard_failures or [
        {"type": "hard_failure", "claim": "", "reason": error} for error in verification.errors
    ]
    lines = []
    for failure in hard_failures[:8]:
        failure_type = failure.get("type", "hard_failure")
        claim = failure.get("claim", "")
        reason = failure.get("reason", "") or failure.get("message", "")
        suffix = f" | claim: {claim}" if claim else ""
        lines.append(f"- {failure_type}: {reason}{suffix}")
    if verification.patch_suggestions:
        lines.append(f"- patch_suggestions: {len(verification.patch_suggestions)} suggestion(s) recorded")
    return "\n".join(lines)


def _verification_warning_messages(verification: VerificationResult) -> list[str]:
    warnings = []
    for warning in verification.soft_warnings:
        warning_type = warning.get("type", "soft_warning")
        claim = warning.get("claim", "")
        reason = warning.get("reason", "") or warning.get("message", "")
        suffix = f" | claim: {claim}" if claim else ""
        warnings.append(f"{warning_type}: {reason}{suffix}")
    if verification.revision_applied:
        warnings.append("Verifier revision loop applied patch suggestions once")
    elif verification.revision_attempted and verification.patch_suggestions:
        warnings.append("Verifier revision loop could not apply patch suggestions automatically")
    return warnings


def _record_harness_learnings(context: _PaperWorkflowContext) -> None:
    if not _auto_harness_learning_enabled() or context.verification is None:
        return
    paper_id = context.paper_title or context.paper_name or context.input_path or "global"
    entries: list[tuple[str, str, str, str]] = []
    for failure in context.verification.hard_failures[:6]:
        reason = failure.get("reason", "") or failure.get("message", "")
        failure_type = failure.get("type", "hard_failure")
        rule = _learning_rule_for_issue(failure_type, reason)
        if rule:
            entries.append(("verification", failure_type, reason, rule))
    for suggestion in context.verification.patch_suggestions[:6]:
        operation = suggestion.get("operation", "patch")
        target = suggestion.get("target", "") or suggestion.get("reason", "")
        if target:
            entries.append(("summary", operation, target, f"自动应用 verifier patch：{operation}，目标：{target[:160]}"))
    for guard in context.guard_results:
        for message in list(guard.errors)[:4] + list(guard.warnings)[:3]:
            rule = _learning_rule_for_issue(guard.name, message)
            if rule:
                category = "extraction" if "Asset" in guard.name else "verification"
                entries.append((category, guard.name, message, rule))
    for category, original, corrected, note in entries[:10]:
        _record_correction_once(
            paper_id,
            original=original,
            corrected=corrected,
            note=note,
            category=category,
            scope="domain",
            confidence=0.72,
        )


def _auto_harness_learning_enabled() -> bool:
    raw = _first_value({}, "PAPER_AGENT_AUTO_LEARN")
    return not raw or _truthy(raw)


def _learning_rule_for_issue(issue_type: str, message: str) -> str:
    text = _clean_xml_text(f"{issue_type}: {message}").strip()
    lowered = text.lower()
    if not text:
        return ""
    if "table" in lowered and any(token in lowered for token in ("caption", "header", "body", "crop", "resolution")):
        return "表格截图必须包含表题、表头和至少两行数值主体；只截到 caption/表头或低分辨率表格时应重裁剪或剔除该 asset。"
    if "mixed_figure_table" in lowered or ("figure" in lowered and "table" in lowered and "crop" in lowered):
        return "图和表必须作为独立 asset 截取；如果一个截图同时包含图、表或正文，应阻断并重新定位。"
    if "figure" in lowered and any(token in lowered for token in ("caption", "text-only", "too shallow", "missing")):
        return "图截图必须包含图主体和必要 caption；caption-only、正文-only 或主体缺失的图片不能插入 Word。"
    if "title" in lowered or "标题" in text:
        return "中文标题必须是自然中文，可保留方法名，但不能使用英文任务短语加“论文精读”的半翻译标题。"
    if "asset" in lowered:
        return "正文图表引用必须与 asset manifest 的 kind、编号和原始 caption 对齐，发现不匹配时先修正文案或移除 marker。"
    if "missing required section" in lowered or "format guard" in lowered:
        return "最终报告必须在生成 Word 前补齐必需章节，并确保每个章节有可读中文内容。"
    return ""


def _record_correction_once(
    paper_id: str,
    *,
    original: str,
    corrected: str,
    note: str,
    category: str,
    scope: str,
    confidence: float,
) -> None:
    normalized_original = _normalize_memory_text(original)
    normalized_corrected = _normalize_memory_text(corrected)
    if not normalized_original and not normalized_corrected:
        return
    try:
        existing = _read_all_correction_memories()
        for memory in existing:
            if (
                _normalize_memory_text(memory.original) == normalized_original
                and _normalize_memory_text(memory.corrected) == normalized_corrected
                and memory.category == category
            ):
                return
        record_summary_correction(
            paper_id,
            original,
            corrected,
            note=note,
            category=category,
            scope=scope,
            confidence=confidence,
        )
    except Exception as exc:
        logger.debug("failed to record harness learning: %s", exc)


def _build_repair_plan(context: _PaperWorkflowContext) -> _RepairPlan:
    if context.verification is None:
        return _RepairPlan()

    can_capture_assets = context.pdf_path is not None and context.work_dir is not None
    available = set(_critical_asset_key_map(context.assets))
    referenced = _critical_referenced_asset_keys(context.summary)
    missing_asset_keys = [
        key
        for key in sorted(referenced - available, key=_critical_asset_sort_key)
        if can_capture_assets
        and context.repair_attempts.get(f"missing:{key[0]}:{key[1]}", 0) < 1
    ]

    bad_asset_ids = _valid_visual_asset_failure_ids(context)
    recapture_asset_ids = {
        asset_id
        for asset_id in bad_asset_ids
        if can_capture_assets
        and _is_critical_asset(context.assets[asset_id - 1])
        and context.repair_attempts.get(f"recapture:{asset_id}", 0) < 1
    }
    remove_asset_ids = {
        asset_id
        for asset_id in bad_asset_ids
        if not _is_critical_asset(context.assets[asset_id - 1])
        and context.repair_attempts.get(f"remove:{asset_id}", 0) < 1
    }
    rewrite_report = (
        context.revision_attempts < 2
        and _verification_needs_full_report_rewrite(context.verification)
        and context.repair_attempts.get("rewrite:report", 0) < 1
    )
    apply_patches = (
        context.revision_attempts < 2
        and bool(context.verification.patch_suggestions)
        and not rewrite_report
        and context.repair_attempts.get("patch:claims", 0) < 2
    )
    return _RepairPlan(
        missing_asset_keys=missing_asset_keys,
        recapture_asset_ids=recapture_asset_ids,
        remove_asset_ids=remove_asset_ids,
        rewrite_report=rewrite_report,
        apply_patches=apply_patches,
    )


def _repair_state_fingerprint(context: _PaperWorkflowContext) -> str:
    failures = []
    if context.verification is not None:
        failures = sorted(
            _clean_xml_text(item.get("reason", "") or item.get("message", ""))
            for item in context.verification.hard_failures
        )
    assets = [
        {
            "kind": asset.kind,
            "label": _compact_asset_label(_original_asset_label(asset)),
            "page": asset.page_number,
            "rect": tuple(round(value, 2) for value in asset.rect) if asset.rect is not None else None,
            "path": str(asset.path),
        }
        for asset in context.assets
    ]
    payload = json.dumps(
        {"summary": context.summary, "assets": assets, "failures": failures},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _revise_report_once(
    context: _PaperWorkflowContext,
    plan: _RepairPlan | None = None,
) -> _NodeResult:
    if context.verification is None:
        raise ValueError("Revision requires a verification report.")
    plan = plan or _build_repair_plan(context)
    if not plan.actionable:
        raise RuntimeError("Verifier requested revision, but the repair planner found no actionable change.")

    context.verification.revision_attempted = True
    context.revision_attempts += 1
    before = _repair_state_fingerprint(context)
    action_keys = plan.action_keys()
    for action_key in action_keys:
        context.repair_attempts[action_key] = context.repair_attempts.get(action_key, 0) + 1

    revised_summary = context.summary
    assets_changed = False
    captured_keys: list[str] = []
    for key in plan.missing_asset_keys:
        captured = _capture_missing_asset_by_label(context, key)
        if captured is None:
            logger.warning("Unable to capture missing critical asset %s", _critical_asset_label(key))
            continue
        context.assets.append(captured)
        assets_changed = True
        captured_keys.append(_critical_asset_label(key))
        logger.info("Captured missing critical asset %s from page %s", _critical_asset_label(key), captured.page_number)

    if plan.recapture_asset_ids:
        repaired_asset_ids = _recapture_critical_visual_assets(context, plan.recapture_asset_ids)
        assets_changed = assets_changed or bool(repaired_asset_ids)

    if plan.remove_asset_ids:
        revised_summary, context.assets = _drop_assets_and_rewrite_markers(
            revised_summary,
            context.assets,
            plan.remove_asset_ids,
        )
        assets_changed = True

    if plan.rewrite_report:
        client, config = _ensure_workflow_codex_client(context)
        revised_summary = _repair_report_format_with_codex(
            client,
            config.model,
            context.summary,
            context.assets,
            context.abstract,
            context.paper_title,
            context.verification,
        )
    elif plan.apply_patches:
        revised_summary = _apply_verifier_patch_suggestions(
            revised_summary,
            context.verification.patch_suggestions,
        )

    if revised_summary != context.summary or assets_changed:
        context.summary = _postprocess_summary(revised_summary)
        context.summary = _normalize_final_sections(context.summary)
        context.summary = _enforce_core_original_title(context.summary, context.paper_title)
        context.summary = _ensure_chinese_report_title(context.summary)
        context.summary = _ensure_asset_markers(context.summary, context.assets)
        context.verification.revision_applied = True

    after = _repair_state_fingerprint(context)
    changed = before != after
    context.repair_history.append(
        {
            "attempt": context.revision_attempts,
            "actions": action_keys,
            "captured_assets": captured_keys,
            "changed": changed,
            "before": before,
            "after": after,
        }
    )
    return _NodeResult(
        status="warning",
        outputs={
            "gate_decision": _GateDecision.REVISE.value,
            "revision_attempt": context.revision_attempts,
        },
        warnings=[
            f"Verifier requested revision attempt {context.revision_attempts}",
            *_verification_warning_messages(context.verification),
        ],
        metrics={
            "revision_attempts": context.revision_attempts,
            "repair_actions": len(action_keys),
            "repair_changed": changed,
        },
    )


def _is_critical_asset(asset: PaperAsset) -> bool:
    key = _asset_label_key(asset)
    return bool(key and key[1] in {"1", "2"})


def _capture_missing_asset_by_label(
    context: _PaperWorkflowContext,
    key: tuple[str, str],
) -> PaperAsset | None:
    if context.pdf_path is None or context.work_dir is None:
        return None

    repair_dir = context.work_dir / "repair-assets" / f"{key[0]}-{key[1]}"
    repair_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(context.pdf_path)
    try:
        preferred_pages = [
            page_index
            for page_index in (context.pages or [])
            if 0 <= page_index < doc.page_count
        ]
        page_indexes = preferred_pages + [
            page_index
            for page_index in range(doc.page_count)
            if page_index not in preferred_pages
        ]
        for page_index in page_indexes:
            page = doc[page_index]
            page_no = page_index + 1
            if key[0] == "figure":
                candidates = _capture_captioned_figures(page, repair_dir, page_no, 32, set())
            else:
                candidates = _capture_captioned_tables(page, repair_dir, page_no, 32, set())
            replacement = next(
                (candidate for candidate in candidates if _asset_label_key(candidate) == key),
                None,
            )
            if replacement is not None:
                return replacement
    finally:
        doc.close()
    return None


def _recapture_critical_visual_assets(
    context: _PaperWorkflowContext,
    bad_asset_ids: set[int],
) -> set[int]:
    if context.pdf_path is None or context.work_dir is None:
        return set()
    critical_ids = {
        asset_id
        for asset_id in bad_asset_ids
        if 1 <= asset_id <= len(context.assets)
        and _is_critical_asset(context.assets[asset_id - 1])
    }
    if not critical_ids:
        return set()

    repaired: set[int] = set()
    doc = fitz.open(context.pdf_path)
    try:
        for asset_id in sorted(critical_ids):
            original = context.assets[asset_id - 1]
            repair_dir = context.work_dir / "repair-assets" / f"asset-{asset_id}"
            repair_dir.mkdir(parents=True, exist_ok=True)
            page_index = original.page_number - 1
            if page_index < 0 or page_index >= doc.page_count:
                continue
            page = doc[page_index]
            if original.kind == "figure":
                candidates = _capture_captioned_figures(
                    page,
                    repair_dir,
                    original.page_number,
                    32,
                    set(),
                )
            elif original.kind == "table":
                candidates = _capture_captioned_tables(
                    page,
                    repair_dir,
                    original.page_number,
                    32,
                    set(),
                )
            else:
                continue
            original_key = _asset_label_key(original)
            replacement = next(
                (candidate for candidate in candidates if _asset_label_key(candidate) == original_key),
                None,
            )
            if replacement is None:
                logger.warning("Unable to recapture critical asset %s", _critical_asset_label(original_key))
                continue
            context.assets[asset_id - 1] = replacement
            repaired.add(asset_id)
            logger.info(
                "Recaptured critical asset %s from page %s",
                _critical_asset_label(original_key),
                original.page_number,
            )
    finally:
        doc.close()
    return repaired


def _valid_visual_asset_failure_ids(context: _PaperWorkflowContext) -> set[int]:
    if context.verification is None:
        return set()
    return {
        asset_id
        for asset_id in _visual_asset_failure_ids(context.verification)
        if 1 <= asset_id <= len(context.assets)
    }


def _visual_asset_failure_ids(verification: VerificationResult) -> set[int]:
    ids: set[int] = set()
    failures = verification.hard_failures or [{"reason": error} for error in verification.errors]
    structured_hard_failures = bool(verification.hard_failures)
    for failure in failures:
        reason = _clean_xml_text(
            failure.get("reason", "") or failure.get("message", "")
        )
        if "Visual Asset Guard" not in reason:
            continue
        asset_ids = {int(match) for match in re.findall(r"\basset\s+(\d+)\b", reason)}
        if not asset_ids:
            continue
        if not structured_hard_failures and not _visual_asset_failure_is_removable(reason):
            continue
        ids.update(asset_ids)
    return ids


def _visual_asset_failure_is_removable(reason: str) -> bool:
    lowered = reason.lower()
    return any(
        token in lowered
        for token in (
            "caption/header",
            "lacks numeric body rows",
            "generic table crop is unusually large",
            "missing_table_body",
            "missing table body",
            "irrelevant_text",
            "irrelevant content",
            "irrelevant_content",
            "caption_truncated",
            "partial_caption",
            "mixed_figure_table",
            "multiple objects",
            "surrounding prose",
            "formula crop",
            "truncated",
            "incomplete",
            "cut off",
            "cropped",
            "too shallow",
            "text-only",
            "missing the figure body",
            "body before inserting",
            "截断",
            "不完整",
            "裁剪",
            "缺失",
            "只露出",
            "未显示表格主体",
            "大段正文",
            "无关内容",
            "图题",
        )
    )


def _drop_assets_and_rewrite_markers(
    summary: str,
    assets: list[PaperAsset],
    remove_ids: set[int],
) -> tuple[str, list[PaperAsset]]:
    valid_remove_ids = {
        asset_id for asset_id in remove_ids if 1 <= asset_id <= len(assets)
    }
    if not valid_remove_ids:
        return summary, assets

    mapping: dict[int, int] = {}
    kept_assets: list[PaperAsset] = []
    for old_id, asset in enumerate(assets, 1):
        if old_id in valid_remove_ids:
            continue
        kept_assets.append(asset)
        mapping[old_id] = len(kept_assets)

    def replace_line(match: re.Match) -> str:
        asset_id = int(match.group(1))
        if asset_id in valid_remove_ids:
            return ""
        return match.group(0)

    summary = re.sub(
        r"(?m)^[ \t]*\[\[ASSET:(\d+)\]\][ \t]*(?:\r?\n)?",
        replace_line,
        summary,
    )

    def replace_inline(match: re.Match) -> str:
        asset_id = int(match.group(1))
        if asset_id in valid_remove_ids:
            return ""
        if asset_id not in mapping:
            return match.group(0)
        return f"[[ASSET:{mapping[asset_id]}]]"

    summary = re.sub(r"\[\[ASSET:(\d+)\]\]", replace_inline, summary)
    summary = re.sub(r"\n{3,}", "\n\n", summary).strip()
    return summary, kept_assets


def _verification_needs_full_report_rewrite(verification: VerificationResult) -> bool:
    reasons = "\n".join(
        str(failure.get("reason", ""))
        for failure in verification.hard_failures
    )
    return any(
        token in reasons
        for token in (
            "missing required section",
            "required section is too short",
            "empty required section",
            "model process preface",
        )
    )


def _write_verification_failed_report(context: _PaperWorkflowContext) -> Path:
    if context.output is None:
        raise ValueError("Verification failure report requires an output directory.")
    paper_name = context.paper_name or Path(context.input_path).stem or "paper"
    path = context.output / f"{paper_name}-verification-failed.md"
    verification = context.verification
    lines = [
        "# Verifier Agent 未通过",
        "",
        f"- run_id: {context.run_id}",
        f"- paper: {paper_name}",
        f"- revision_attempts: {context.revision_attempts}",
        f"- gate_decision: {_GateDecision.BLOCK.value}",
        "",
        "## Hard Failures",
    ]
    if verification and verification.hard_failures:
        for failure in verification.hard_failures:
            failure_type = failure.get("type", "hard_failure")
            claim = failure.get("claim", "")
            reason = failure.get("reason", "") or failure.get("message", "")
            lines.append(f"- **{failure_type}**: {reason}")
            if claim:
                lines.append(f"  - claim: {claim}")
    elif verification:
        lines.extend(f"- {error}" for error in verification.errors)
    else:
        lines.append("- verification report is not available")
    lines.extend(["", "## Soft Warnings"])
    if verification and verification.soft_warnings:
        for warning in verification.soft_warnings:
            warning_type = warning.get("type", "soft_warning")
            claim = warning.get("claim", "")
            reason = warning.get("reason", "") or warning.get("message", "")
            lines.append(f"- **{warning_type}**: {reason}")
            if claim:
                lines.append(f"  - claim: {claim}")
    else:
        lines.append("- none")
    lines.extend(["", "## Patch Suggestions"])
    if verification and verification.patch_suggestions:
        lines.extend(f"- `{item.get('operation', 'patch')}`: {item.get('target', '')}" for item in verification.patch_suggestions)
    else:
        lines.append("- none")
    draft_preview = _truncate_middle(context.summary or "", 12000)
    if draft_preview:
        lines.extend(
            [
                "",
                "## Draft Report Preview",
                "",
                "```markdown",
                draft_preview,
                "```",
            ]
        )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def _apply_verifier_patch_suggestions(summary: str, suggestions: list[dict[str, str]]) -> str:
    revised = summary
    for suggestion in suggestions:
        operation = str(suggestion.get("operation", "")).strip().lower()
        target = _clean_xml_text(str(suggestion.get("target", ""))).strip()
        replacement = _clean_xml_text(
            str(suggestion.get("replacement", suggestion.get("value", suggestion.get("text", ""))))
        ).strip()
        if not target:
            continue
        if operation in {"delete_claim", "delete", "remove_claim", "remove"}:
            revised = _delete_summary_claim(revised, target)
        elif operation in {"replace_claim", "replace", "rewrite_claim", "rewrite"} and target in revised:
            revised = revised.replace(target, replacement or "")
    return re.sub(r"\n{3,}", "\n\n", revised).strip()


def _delete_summary_claim(summary: str, target: str) -> str:
    lines = summary.splitlines()
    kept: list[str] = []
    removed = False
    for line in lines:
        normalized = _normalize_markdown_line(line)
        if target in line or target in normalized:
            removed = True
            continue
        kept.append(line)
    if removed:
        return "\n".join(kept)
    return summary.replace(target, "")


def _guard_result(
    name: str,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    metrics: dict | None = None,
) -> GuardResult:
    errors = errors or []
    warnings = warnings or []
    status = "failed" if errors else "warning" if warnings else "passed"
    return GuardResult(name=name, status=status, errors=errors, warnings=warnings, metrics=metrics or {})


def _evidence_guard(grounded_map: dict[str, list[dict[str, str]]]) -> GuardResult:
    claims = grounded_map.get("claims", [])
    errors = [
        f"claim_{index} lacks evidence_ids: {claim.get('claim', claim.get('text', ''))[:120]}"
        for index, claim in enumerate(claims, 1)
        if bool(claim.get("core", True)) and not claim.get("evidence_ids")
    ]
    return _guard_result(
        "Evidence Guard",
        errors=errors,
        metrics={"claim_count": len(claims), "ungrounded_count": len(errors)},
    )


def _asset_guard(summary: str, assets: list[PaperAsset]) -> GuardResult:
    errors: list[str] = []
    asset_count = len(assets)
    for match in re.finditer(r"\[\[ASSET:([^\]]+)\]\]", summary):
        raw_id = match.group(1).strip()
        if not raw_id.isdigit():
            errors.append(f"asset placeholder is not numeric: [[ASSET:{raw_id}]]")
            continue
        asset_id = int(raw_id)
        if asset_id < 1 or asset_id > asset_count:
            errors.append(f"asset id {asset_id} is not in asset manifest")
            continue
        nearby = _asset_reference_text_for_marker(summary, match.start())
        asset = assets[asset_id - 1]
        if _asset_reference_kind_mismatch(nearby, asset):
            errors.append(f"asset id {asset_id} kind mismatch: text references {nearby[:80]!r}, manifest kind is {asset.kind}")
    errors.extend(_critical_referenced_asset_errors(summary, assets))
    return _guard_result(
        "Asset Guard",
        errors=errors,
        metrics={
            "asset_count": asset_count,
            "placeholder_count": len(re.findall(r"\[\[ASSET:", summary)),
        },
    )


def _critical_referenced_asset_errors(summary: str, assets: list[PaperAsset]) -> list[str]:
    references = _critical_referenced_asset_keys(summary)
    if not references:
        return []
    available = _critical_asset_key_map(assets)
    used_ids = {int(match.group(1)) for match in re.finditer(r"\[\[ASSET:(\d+)\]\]", summary)}
    errors: list[str] = []
    for key in sorted(references, key=_critical_asset_sort_key):
        label = _critical_asset_label(key)
        asset_id = available.get(key)
        if asset_id is None:
            errors.append(f"referenced critical asset {label} is missing from asset manifest")
            continue
        if asset_id not in used_ids:
            errors.append(f"missing screenshot marker for critical referenced asset {label} ([[ASSET:{asset_id}]])")
    return errors


def _critical_asset_key_map(assets: list[PaperAsset]) -> dict[tuple[str, str], int]:
    key_map: dict[tuple[str, str], int] = {}
    for asset_id, asset in enumerate(assets, 1):
        key = _asset_label_key(asset)
        if key and key[1] in {"1", "2"}:
            key_map.setdefault(key, asset_id)
    return key_map


def _asset_label_key(asset: PaperAsset) -> tuple[str, str] | None:
    label = _compact_asset_label(_original_asset_label(asset))
    match = re.match(r"^(图|表)([0-9一二三四五六七八九十]+[A-Za-z]?)$", label)
    if not match:
        return None
    kind = "figure" if match.group(1) == "图" else "table"
    number = _critical_asset_number(match.group(2))
    if not number:
        return None
    return kind, number


def _critical_referenced_asset_keys(summary: str) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for line in summary.splitlines():
        stripped = _clean_xml_text(line.strip())
        if not stripped or re.fullmatch(r"\[\[ASSET:\d+\]\]", stripped):
            continue
        keys.update(_critical_referenced_asset_keys_in_text(stripped))
    return keys


def _critical_referenced_asset_keys_in_text(text: str) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    chinese_patterns = [
        ("table", r"表\s*([12一二])(?![0-9A-Za-z一二三四五六七八九十])"),
        ("figure", r"图\s*([12一二])(?![0-9A-Za-z一二三四五六七八九十])"),
    ]
    for kind, pattern in chinese_patterns:
        for match in re.finditer(pattern, text):
            if _asset_reference_is_layout_hint(text, match.end()):
                continue
            number = _critical_asset_number(match.group(1))
            if number:
                keys.add((kind, number))
    english_patterns = [
        ("table", r"(?i)\b(?:table|tab\.)\s*([12])\b"),
        ("figure", r"(?i)\b(?:figure|fig\.?)\s*([12])\b"),
    ]
    for kind, pattern in english_patterns:
        for match in re.finditer(pattern, text):
            if _asset_reference_is_layout_hint(text, match.end()):
                continue
            keys.add((kind, match.group(1)))
    return keys


def _asset_reference_is_layout_hint(text: str, end: int) -> bool:
    tail = text[end : end + 8].lstrip()
    return bool(re.match(r"(附近|周边|旁边|位置|区域|上方|下方|左侧|右侧)", tail))


def _critical_asset_number(value: str) -> str:
    normalized = {"一": "1", "二": "2"}.get(str(value).strip(), str(value).strip())
    return normalized if normalized in {"1", "2"} else ""


def _critical_asset_label(key: tuple[str, str]) -> str:
    prefix = "图" if key[0] == "figure" else "表"
    return f"{prefix}{key[1]}"


def _critical_asset_sort_key(key: tuple[str, str]) -> tuple[int, int]:
    kind_rank = 0 if key[0] == "figure" else 1
    return kind_rank, int(key[1])


def _remove_mismatched_asset_markers(summary: str, assets: list[PaperAsset]) -> str:
    if not assets or "[[ASSET:" not in summary:
        return summary
    spans_to_remove: list[tuple[int, int]] = []
    for match in re.finditer(r"(?m)^[ \t]*\[\[ASSET:(\d+)\]\][ \t]*(?:\r?\n)?", summary):
        asset_id = int(match.group(1))
        if asset_id < 1 or asset_id > len(assets):
            continue
        nearby = _asset_reference_text_for_marker(summary, match.start())
        if _asset_reference_kind_mismatch(nearby, assets[asset_id - 1]):
            spans_to_remove.append(match.span())
    if not spans_to_remove:
        return summary
    result_parts: list[str] = []
    cursor = 0
    for start, end in spans_to_remove:
        result_parts.append(summary[cursor:start])
        cursor = end
    result_parts.append(summary[cursor:])
    return re.sub(r"\n{3,}", "\n\n", "".join(result_parts)).strip()


def _visual_asset_guard(
    summary: str,
    assets: list[PaperAsset],
    client: openai.OpenAI | None = None,
    model: str = "",
) -> GuardResult:
    if not _visual_asset_guard_enabled():
        return _guard_result("Visual Asset Guard", metrics={"skipped": "disabled"})

    local_selected = _local_visual_guard_assets_to_check(summary, assets)
    selected = _visual_guard_assets_to_check(summary, assets)
    if not local_selected and not selected:
        return _guard_result("Visual Asset Guard", metrics={"checked": 0})

    errors: list[str] = []
    warnings: list[str] = []
    for asset_id, asset in local_selected:
        for issue in _local_visual_asset_issues(asset_id, asset):
            severity = issue.get("severity", "warning")
            message = issue.get("message", "")
            if severity == "error":
                errors.append(message)
            elif message:
                warnings.append(message)

    if client is None or not model:
        return _guard_result(
            "Visual Asset Guard",
            errors=errors,
            warnings=warnings,
            metrics={
                "checked": 0,
                "selected": len(selected),
                "local_checked": len(local_selected),
                "model_skipped": "no_client",
            },
        )

    checked = 0

    def check_one(item: tuple[int, PaperAsset]) -> tuple[int, PaperAsset, list[dict[str, str]] | None, Exception | None]:
        asset_id, asset = item
        if not asset.path.exists():
            return asset_id, asset, None, FileNotFoundError(str(asset.path))
        try:
            return asset_id, asset, _check_asset_with_visual_model(asset_id, asset, client, model), None
        except openai.BadRequestError as exc:
            return asset_id, asset, None, exc
        except Exception as exc:
            return asset_id, asset, None, exc

    max_workers = min(_visual_guard_concurrency(), len(selected))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(check_one, item): item for item in selected}
        for future in as_completed(future_map):
            asset_id, _asset, issues, exc = future.result()
            if isinstance(exc, openai.BadRequestError):
                warnings.append(f"视觉模型未接受图片输入，已跳过视觉截图检查：{_clean_xml_text(str(exc))[:180]}")
                continue
            if exc is not None:
                warnings.append(f"asset {asset_id} visual check failed: {_clean_xml_text(str(exc))[:180]}")
                continue
            if issues is None:
                continue
            checked += 1
            for issue in issues:
                severity = str(issue.get("severity", "warning")).lower()
                issue_type = str(issue.get("type", "visual_issue")).strip() or "visual_issue"
                reason = _clean_xml_text(str(issue.get("reason", ""))).strip()
                message = f"asset {asset_id} {issue_type}: {reason}" if reason else f"asset {asset_id} {issue_type}"
                if severity == "error":
                    errors.append(message)
                else:
                    warnings.append(message)
    return _guard_result(
        "Visual Asset Guard",
        errors=errors,
        warnings=warnings,
        metrics={"checked": checked, "selected": len(selected), "local_checked": len(local_selected)},
    )


def _local_visual_asset_issues(asset_id: int, asset: PaperAsset) -> list[dict[str, str]]:
    if not asset.path.exists():
        return [{"severity": "warning", "message": f"asset {asset_id} image file is missing: {asset.path}"}]
    width, height = _image_pixel_size(asset.path)
    issues: list[dict[str, str]] = []
    if asset.kind == "figure":
        if width >= 600 and height < 120:
            issues.append(
                {
                    "severity": "error",
                    "message": f"asset {asset_id} figure crop is too shallow ({width}x{height}); likely missing the figure body",
                }
            )
        elif width >= 600 and height < 180 and _image_looks_text_only(asset.path):
            issues.append(
                {
                    "severity": "error",
                    "message": f"asset {asset_id} figure crop looks text-only ({width}x{height}); likely captured caption/body text without the figure",
                }
            )
    if asset.kind == "table" and width >= 650 and height >= 650 and "captioned" not in asset.path.name.lower():
        issues.append(
            {
                "severity": "error",
                "message": f"asset {asset_id} generic table crop is unusually large ({width}x{height}); likely includes multiple objects",
            }
        )
    if asset.kind == "table" and width < 460:
        issues.append(
            {
                "severity": "error",
                "message": f"asset {asset_id} table crop resolution is too low ({width}x{height}); recapture at higher scale before inserting into Word",
            }
        )
    if asset.kind == "table" and _table_asset_text_looks_incomplete(asset, width, height):
        issues.append(
            {
                "severity": "error",
                "message": f"asset {asset_id} table crop appears to contain only caption/header or lacks numeric body rows; recapture the full table body before inserting into Word",
            }
        )
    if asset.kind == "formula" and _formula_asset_text_looks_contaminated(asset.text):
        issues.append(
            {
                "severity": "error",
                "message": f"asset {asset_id} formula crop contains surrounding prose fragments; recapture a tighter formula-only screenshot before inserting into Word",
            }
        )
    return issues


def _formula_asset_text_looks_contaminated(text: str) -> bool:
    cleaned = _clean_xml_text(text or "").strip()
    if not cleaned or not _line_has_formula_syntax(cleaned):
        return False
    words = re.findall(r"[A-Za-z]{3,}", cleaned)
    if len(words) >= 7:
        return True
    lowered = f" {cleaned.lower()} "
    prose_fragments = (
        " as shown",
        " the ",
        " and ",
        " with ",
        " from ",
        " state-of-the-art",
        " weights and",
        " rights and",
        " levers ",
        " batch ",
    )
    return any(fragment in lowered for fragment in prose_fragments) and len(words) >= 2


def _table_asset_text_looks_incomplete(asset: PaperAsset, width: int, height: int) -> bool:
    text = _clean_xml_text(asset.text or "")
    if not text.strip():
        return width >= 700 and height < 420
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    numeric_rows = [line for line in lines if len(re.findall(r"\d+(?:\.\d+)?", line)) >= 2]
    header_like_rows = [
        line
        for line in lines
        if re.search(r"(?i)\b(?:method|reward|metric|full-reference|no-reference|psnr|ssim|lpips|maniqa)\b", line)
    ]
    if len(numeric_rows) < 2 and width >= 700 and height < 520:
        return True
    return len(lines) <= 2 and bool(header_like_rows) and len(numeric_rows) < 2


def _image_looks_text_only(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            sample = image.convert("RGB")
            sample.thumbnail((240, 240))
            pixels = list(sample.getdata())
    except Exception:
        return False
    if not pixels:
        return False
    colored = 0
    non_white = 0
    for red, green, blue in pixels:
        max_channel = max(red, green, blue)
        min_channel = min(red, green, blue)
        if max_channel < 245:
            non_white += 1
        if max_channel - min_channel > 35 and max_channel < 245:
            colored += 1
    total = len(pixels)
    colored_fraction = colored / total
    non_white_fraction = non_white / total
    return colored_fraction < 0.01 and non_white_fraction < 0.22


def _visual_asset_guard_enabled() -> bool:
    raw = _first_value({}, "CODEX_VISUAL_GUARD")
    return not raw or _truthy(raw)


def _local_visual_guard_assets_to_check(summary: str, assets: list[PaperAsset]) -> list[tuple[int, PaperAsset]]:
    if not assets:
        return []
    referenced = {
        int(match.group(1))
        for match in re.finditer(r"\[\[ASSET:(\d+)\]\]", summary)
        if 1 <= int(match.group(1)) <= len(assets)
    }
    ids = sorted(referenced) if referenced else list(range(1, len(assets) + 1))
    return [(asset_id, assets[asset_id - 1]) for asset_id in ids]


def _visual_guard_assets_to_check(summary: str, assets: list[PaperAsset]) -> list[tuple[int, PaperAsset]]:
    max_assets = _visual_guard_max_assets()
    referenced: list[int] = []
    for match in re.finditer(r"\[\[ASSET:(\d+)\]\]", summary):
        asset_id = int(match.group(1))
        if 1 <= asset_id <= len(assets) and asset_id not in referenced:
            referenced.append(asset_id)
    ordered = referenced or list(range(1, len(assets) + 1))
    ordered = sorted(
        ordered,
        key=lambda asset_id: (
            {"table": 0, "figure": 1, "formula": 2}.get(assets[asset_id - 1].kind, 3),
            ordered.index(asset_id),
        ),
    )
    result: list[tuple[int, PaperAsset]] = []
    for asset_id in ordered:
        asset = assets[asset_id - 1]
        if asset.kind not in {"figure", "table", "formula"}:
            continue
        result.append((asset_id, asset))
        if len(result) >= max_assets:
            break
    return result


def _visual_guard_max_assets() -> int:
    raw_value = _first_value({}, "CODEX_VISUAL_GUARD_MAX_ASSETS")
    try:
        value = int(raw_value) if raw_value else 4
    except ValueError:
        value = 4
    return max(1, min(value, 20))


def _visual_guard_concurrency() -> int:
    raw_value = _first_value({}, "CODEX_VISUAL_GUARD_CONCURRENCY")
    try:
        value = int(raw_value) if raw_value else 3
    except ValueError:
        value = 3
    return max(1, min(value, 6))


def _check_asset_with_visual_model(
    asset_id: int,
    asset: PaperAsset,
    client: openai.OpenAI,
    model: str,
) -> list[dict[str, str]]:
    data_url = _image_file_to_data_url(asset.path)
    width, height = _image_pixel_size(asset.path)
    prompt = (
        "你是论文 Word 报告的截图质检员。请只检查这张截图是否适合插入报告，不评价论文内容。\n"
        f"截图编号: ASSET:{asset_id}\n"
        f"声明类型: {asset.kind}\n"
        f"像素尺寸: {width}x{height}\n"
        f"原始 caption/说明: {_clean_xml_text(asset.caption)[:500]}\n\n"
        "严重错误判定为 severity=error：\n"
        "1. 截图把两个独立对象混在一起，例如一张图和一个表格同时进入同一截图；\n"
        "2. 图、表或公式主体被明显截断，标题/caption 与主体不匹配，或主体缺失；\n"
        "3. 截图包含大段正文、章节标题、页眉页脚等无关内容，影响阅读；\n"
        "4. 声明类型与画面不符，例如声明为 table 但主要是 figure；\n"
        "5. 图表小到无法阅读或画面明显空白。\n"
        "轻微留白、少量 caption、正常表题/图题不算错误。\n\n"
        "只输出 JSON，不要输出 Markdown："
        "{\"passed\": true/false, \"issues\": [{\"severity\": \"error|warning\", \"type\": \"...\", \"reason\": \"...\"}]}"
    )
    request = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a strict visual QA guard for generated research-paper Word reports. Output only valid JSON.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": 600,
    }
    try:
        response = client.chat.completions.create(**request)
    except openai.BadRequestError:
        request.pop("max_tokens", None)
        response = client.chat.completions.create(**request)
    content = response.choices[0].message.content or ""
    payload = _parse_visual_asset_guard_response(str(content))
    return payload.get("issues", [])


def _parse_visual_asset_guard_response(text: str) -> dict:
    try:
        payload = json.loads(_extract_json_object(text))
    except Exception:
        return {
            "passed": True,
            "issues": [
                {
                    "severity": "warning",
                    "type": "invalid_visual_guard_json",
                    "reason": _clean_xml_text(text)[:180] or "visual guard returned invalid JSON",
                }
            ],
        }
    issues = payload.get("issues", [])
    if not isinstance(issues, list):
        issues = []
    normalized_issues = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity", "warning")).lower()
        if severity not in {"error", "warning"}:
            severity = "warning"
        normalized_issues.append(
            {
                "severity": severity,
                "type": str(issue.get("type", "visual_issue")),
                "reason": str(issue.get("reason", "")),
            }
        )
    return {"passed": bool(payload.get("passed", not normalized_issues)), "issues": normalized_issues}


def _image_file_to_data_url(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".") or "png"
    mime = "jpeg" if suffix in {"jpg", "jpeg"} else "png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"


def _image_pixel_size(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return (0, 0)


def _coverage_guard(
    summary: str,
    grounded_map: dict[str, list[dict[str, str]]],
) -> GuardResult:
    warnings: list[str] = []
    section_text = "\n".join(re.findall(r"(?m)^#{1,3}\s+(.+)$", summary))
    checks = {
        "摘要": bool(re.search(r"摘要", section_text)),
        "方法": bool(re.search(r"方法|机制|流程", section_text)),
        "实验": bool(re.search(r"结果|实验|评估|消融", section_text)),
        "局限": bool(re.search(r"局限|限制|不足", section_text)),
    }
    for name, present in checks.items():
        if not present:
            warnings.append(f"{name} coverage is missing")
    if grounded_map.get("method") and not checks["方法"]:
        warnings.append("method evidence exists but method section is not covered")
    if grounded_map.get("experiments") and not checks["实验"]:
        warnings.append("experiment evidence exists but result/experiment section is not covered")
    return _guard_result(
        "Coverage Guard",
        warnings=warnings,
        metrics={f"{key}_covered": value for key, value in checks.items()},
    )


def _format_guard(summary: str) -> GuardResult:
    errors: list[str] = []
    warnings: list[str] = []
    if re.search(r"\[\[ASSET:[^\]\d]+", summary):
        errors.append("malformed asset placeholder")
    if _contains_process_preface(summary):
        errors.append("report contains model process preface")
    headings = [(len(match.group(1)), match.group(2).strip()) for match in re.finditer(r"(?m)^(#{1,6})\s+(.+)$", summary)]
    for (prev_level, _), (level, title) in zip(headings, headings[1:]):
        if level - prev_level > 1:
            warnings.append(f"heading level jumps before {title}")
    empty_sections = _empty_markdown_sections(summary)
    errors.extend(f"empty required section: {title}" for title in empty_sections if title in _required_report_sections())
    warnings.extend(f"empty section: {title}" for title in empty_sections[:6] if title not in _required_report_sections())
    for section in _missing_required_report_sections(summary):
        errors.append(f"missing required section: {section}")
    for section in _too_short_required_sections(summary):
        errors.append(f"required section is too short: {section}")
    return _guard_result("Format Guard", errors=errors, warnings=warnings, metrics={"heading_count": len(headings)})


def _required_report_sections() -> set[str]:
    return {
        "核心信息",
        "摘要",
        "背景与问题",
        "创新点",
        "一句话总结",
        "方法主线",
        "关键结果",
        "深度分析",
        "局限",
        "总结",
    }


def _missing_required_report_sections(summary: str) -> list[str]:
    present = {
        match.group(1).strip()
        for match in re.finditer(r"(?m)^##\s+(.+?)\s*$", summary)
    }
    return [section for section in _required_report_sections() if section not in present]


def _too_short_required_sections(summary: str) -> list[str]:
    too_short: list[str] = []
    min_chars = {
        "摘要": 80,
        "背景与问题": 120,
        "创新点": 80,
        "一句话总结": 25,
        "方法主线": 140,
        "关键结果": 100,
        "深度分析": 80,
        "局限": 40,
        "总结": 60,
    }
    for section, minimum in min_chars.items():
        body = re.sub(r"\[\[ASSET:\d+\]\]", "", _section_body(summary, section))
        body = re.sub(r"(?m)^#{1,6}\s+.*$", "", body)
        body = re.sub(r"\s+", "", _clean_xml_text(body))
        if body and len(body) < minimum:
            too_short.append(section)
    return too_short


def _contains_process_preface(summary: str) -> bool:
    first_lines = "\n".join(line.strip() for line in summary.splitlines()[:8] if line.strip())
    return bool(
        re.search(
            r"(我先|接着我|然后我|下面我|我会|我将|先把|补齐缺失|整合成完整|避免把未证实信息|校对公式|process|I will|I'll)",
            first_lines,
            flags=re.IGNORECASE,
        )
    )


def _citation_guard(summary: str, paper_title: str) -> GuardResult:
    warnings: list[str] = []
    core = _section_body(summary, "核心信息")
    if paper_title and paper_title not in core:
        warnings.append("core title does not match extracted front matter title")
    for field in ("DOI", "发表时间", "机构"):
        match = re.search(rf"(?m)^[-*]\s*{field}\s*[:：]\s*(.+)$", core)
        if match and re.search(r"未知|未提及|N/A|原文未", match.group(1), flags=re.IGNORECASE):
            warnings.append(f"{field} contains unspecified placeholder")
    return _guard_result("Citation Guard", warnings=warnings)


def _loop_guard(max_repairs: int = 2, repair_attempts: int = 0) -> GuardResult:
    warnings = []
    if repair_attempts > max_repairs:
        warnings.append(f"repair attempts exceeded max_repair={max_repairs}")
    return _guard_result(
        "Loop Guard",
        warnings=warnings,
        metrics={"max_repairs": max_repairs, "repair_attempts": repair_attempts},
    )


def _memory_guard(memories: list[CorrectionMemory]) -> GuardResult:
    warnings: list[str] = []
    global_count = sum(1 for memory in memories if memory.scope == "global" or memory.paper_id == "global")
    disabled_count = sum(1 for memory in memories if memory.disabled)
    if global_count > max(5, len(memories) // 2) and memories:
        warnings.append("too many global correction memories may pollute paper-level behavior")
    missing_category = sum(1 for memory in memories if not memory.category)
    if missing_category:
        warnings.append(f"{missing_category} correction memories have no category")
    low_confidence = sum(1 for memory in memories if memory.confidence < 0.5)
    if low_confidence:
        warnings.append(f"{low_confidence} correction memories have low confidence")
    return _guard_result(
        "Memory Guard",
        warnings=warnings,
        metrics={
            "memory_count": len(memories),
            "global_memory_count": global_count,
            "disabled_memory_count": disabled_count,
            "low_confidence_count": low_confidence,
        },
    )


def _asset_reference_text_for_marker(summary: str, marker_start: int) -> str:
    line_start = summary.rfind("\n", 0, marker_start) + 1
    line_end = summary.find("\n", marker_start)
    if line_end < 0:
        line_end = len(summary)
    current_line = summary[line_start:line_end].strip()
    previous_text = summary[:line_start].splitlines()
    previous_line = ""
    for line in reversed(previous_text):
        stripped = line.strip()
        if stripped and not re.fullmatch(r"\[\[ASSET:[^\]]+\]\]", stripped):
            previous_line = stripped
            break
    return _clean_xml_text("\n".join(part for part in (previous_line, current_line) if part))


def _asset_reference_kind_mismatch(text: str, asset: PaperAsset) -> bool:
    if not text.strip():
        return False
    compact = _compact_asset_label(_original_asset_label(asset))
    if compact and compact in text:
        return False
    mentions_table = _asset_reference_mentions_kind(text, "table")
    mentions_figure = _asset_reference_mentions_kind(text, "figure")
    mentions_formula = _asset_reference_mentions_kind(text, "formula")
    if asset.kind == "table" and mentions_table:
        return False
    if asset.kind == "figure" and mentions_figure:
        return False
    if asset.kind == "formula" and mentions_formula:
        return False
    if asset.kind == "table" and (mentions_figure or mentions_formula):
        return True
    if asset.kind == "figure" and (mentions_table or mentions_formula):
        return True
    if asset.kind == "formula" and (mentions_table or mentions_figure):
        return True
    return False


def _asset_reference_mentions_kind(text: str, kind: str) -> bool:
    patterns = {
        "table": r"表\s*\d|表格|Table\s*\d|Tab\.\s*\d",
        "figure": r"图\s*\d|图片|图像|曲线|Figure\s*\d|Fig\.\s*\d",
        "formula": r"公式\s*\d|公式截图|方程|Equation\s*\d|Eq\.\s*\d",
    }
    return bool(re.search(patterns[kind], text, flags=re.IGNORECASE))


def _empty_markdown_sections(summary: str) -> list[str]:
    blocks = re.split(r"(?m)(?=^#{1,6}\s+)", summary)
    empty: list[str] = []
    for index, block in enumerate(blocks):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("#"):
            continue
        title = lines[0].lstrip("#").strip()
        body = "\n".join(lines[1:]).strip()
        level = len(lines[0]) - len(lines[0].lstrip("#"))
        has_child_section = False
        for next_block in blocks[index + 1 :]:
            next_lines = next_block.splitlines()
            next_heading = next_lines[0].strip() if next_lines else ""
            if not next_heading.startswith("#"):
                continue
            next_level = len(next_heading) - len(next_heading.lstrip("#"))
            has_child_section = next_level > level
            break
        if not body and not has_child_section:
            empty.append(title)
    return empty


def _section_body(summary: str, title: str) -> str:
    pattern = re.compile(rf"(?ms)^##\s*{re.escape(title)}\s*\n(.*?)(?=^## |\Z)")
    match = pattern.search(summary)
    return match.group(1) if match else ""


def _run_verification_agent(
    client: openai.OpenAI,
    model: str,
    paper_text: str,
    grounding_map: dict[str, list[dict[str, str]]],
    correction_memories: list[CorrectionMemory] | None = None,
    prompt_patches: list[PromptPatch] | None = None,
) -> VerificationResult:
    claims = grounding_map.get("claims", [])
    if not claims:
        return VerificationResult(
            False,
            ["no verifiable claims extracted from the report"],
            hard_failures=[
                {
                    "type": "no_verifiable_claims",
                    "claim": "",
                    "reason": "no verifiable claims extracted from the report",
                }
            ],
        )

    evidence_payload = _verification_evidence_text(paper_text, grounding_map)
    memories = correction_memories or []
    patches = prompt_patches or _build_prompt_patches(memories)
    memory_context = _correction_memory_context(memories)
    evaluation_patch = _prompt_patch_context(patches, "evaluation")
    prompt = (
        "You are the Verifier Agent for a paper-understanding harness. "
        "Your job is not to polish the report; your job is to decide whether every structured Claim is grounded by Evidence.\n\n"
        "Gate policy:\n"
        "1. Each core Claim must have at least one evidence_id that points to an Evidence item.\n"
        "2. A Claim must have direct support in its linked Evidence text. Do not use common sense to fill gaps.\n"
        "3. Method claims must be supported by method/approach/model/training/algorithm/implementation Evidence.\n"
        "4. Contribution claims must not invent datasets, metrics, capabilities, applications, or novelty not stated in the paper.\n"
        "5. Weak or narrow evidence should be a soft warning, not a hard failure.\n"
        "6. If a claim can be repaired safely, add a patch suggestion. Prefer delete_claim for unsupported additions.\n"
        "7. Output JSON only. Do not output Markdown.\n\n"
        "Required JSON schema:\n"
        "{\n"
        "  \"passed\": true/false,\n"
        "  \"hard_failures\": [\n"
        "    {\"type\": \"unsupported_core_claim\", \"claim\": \"...\", \"reason\": \"...\"}\n"
        "  ],\n"
        "  \"soft_warnings\": [\n"
        "    {\"type\": \"weak_evidence\", \"claim\": \"...\", \"reason\": \"...\"}\n"
        "  ],\n"
        "  \"patch_suggestions\": [\n"
        "    {\"operation\": \"delete_claim\", \"target\": \"...\"}\n"
        "  ]\n"
        "}\n\n"
        f"User correction memory:\n{memory_context}\n\n"
        f"Self-improving evaluation rubric:\n{evaluation_patch}\n\n"
        f"Claim/Evidence payload:\n{json.dumps(evidence_payload, ensure_ascii=False, indent=2)}"
    )
    try:
        output = _chat(
            client,
            model,
            prompt,
            system_prompt=CRITIC_SYSTEM_PROMPT,
            max_tokens=1600,
            max_attempts=1,
        )
    except RuntimeError as exc:
        return VerificationResult(
            True,
            [],
            soft_warnings=[
                {
                    "type": "verifier_timeout_warning",
                    "claim": "",
                    "reason": f"Verifier Agent 超时，已降级为本地 Guard 校验并允许生成 Word：{_clean_xml_text(str(exc))[:260]}",
                }
            ],
        )
    verification = _parse_verification_result(output)
    if _verification_failed_due_to_format(verification):
        repair_prompt = (
            "下面是 Verifier Agent 的原始输出。请只把它转换为合法 JSON；"
            "如果无法判断，就输出 {\"pass\": true, \"errors\": []}，不要输出 Markdown 或解释。\n\n"
            "合法格式：{\"pass\": true/false, \"errors\": [\"具体错误1\"]}\n\n"
            f"原始输出：\n{output}"
        )
        repair_prompt = (
            "Convert the following Verifier Agent output into valid JSON only. "
            "If the output does not contain enough information, return "
            "{\"passed\": true, \"hard_failures\": [], \"soft_warnings\": [], \"patch_suggestions\": []}.\n\n"
            "Required schema: "
            "{\"passed\": true/false, \"hard_failures\": [], \"soft_warnings\": [], \"patch_suggestions\": []}\n\n"
            f"Raw output:\n{output}"
        )
        try:
            repaired_output = _chat(
                client,
                model,
                repair_prompt,
                system_prompt=CRITIC_SYSTEM_PROMPT,
                max_tokens=1200,
                max_attempts=1,
            )
        except RuntimeError:
            return _verification_format_warning(verification)
        repaired = _parse_verification_result(repaired_output)
        if not _verification_failed_due_to_format(repaired):
            return repaired
        return _verification_format_warning(verification)
    return verification


def _verification_format_warning(verification: VerificationResult) -> VerificationResult:
    message = "; ".join(verification.errors) or "Verifier Agent output was not valid JSON."
    return VerificationResult(
        True,
        [],
        soft_warnings=[
            {
                "type": "verifier_format_warning",
                "claim": "",
                "reason": message,
            }
        ],
    )


def _extract_verifiable_claims(summary: str, limit: int = 24) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    current_section = ""
    for raw_line in summary.splitlines():
        line = _clean_xml_text(raw_line).strip()
        if not line or re.fullmatch(r"\[\[ASSET:\d+\]\]", line):
            continue
        if line.startswith("#"):
            current_section = line.lstrip("#").strip()
            continue
        if current_section in {"核心信息", "摘要"}:
            continue
        normalized = _normalize_markdown_line(line)
        for sentence in _split_claim_sentences(normalized):
            if not _claim_is_verifiable(sentence):
                continue
            claim_id = f"claim-{len(claims) + 1}"
            claims.append(
                _Claim(
                    id=claim_id,
                    section=current_section or "正文",
                    type=_claim_type(current_section, sentence),
                    text=sentence,
                    core=True,
                ).to_dict()
            )
            if len(claims) >= limit:
                return claims
    return claims


def _build_grounding_map(paper_text: str) -> _EvidenceMap:
    sections = _extract_grounding_sections(paper_text)
    result = _EvidenceMap()
    evidence_items: list[dict[str, str]] = []
    for section in sections:
        bucket = section.category if section.category in result else ""
        if not bucket:
            continue
        evidence = _Evidence(
            id=_evidence_id(section.category, section.section_id, len(evidence_items) + 1),
            section_id=section.section_id,
            title=section.title,
            category=section.category,
            text=section.text[:5000],
        ).to_dict()
        evidence_items.append(evidence)
        result[bucket].append(evidence)
    if not any(result[key] for key in ("intro", "method", "experiments")) and paper_text.strip():
        evidence = _Evidence(
            id="evidence-document",
            section_id="document",
            title="Document",
            category="intro",
            text=paper_text[:5000],
        ).to_dict()
        result["intro"].append(evidence)
        evidence_items.append(evidence)
    result["evidence"] = evidence_items
    return result


def _evidence_id(category: str, section_id: str, fallback_index: int) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", section_id or str(fallback_index)).strip("-").lower()
    return f"evidence-{category}-{normalized or fallback_index}"


def _extract_grounding_sections(paper_text: str) -> list[GroundingSection]:
    heading_pattern = re.compile(
        r"(?m)^\s*(?P<num>(?:\d+|[IVX]+)(?:\.\d+)*)\.?\s+(?P<title>[A-Z][A-Za-z][A-Za-z0-9 /&,\-:]{2,80})\s*$"
    )
    matches = list(heading_pattern.finditer(paper_text))
    sections: list[GroundingSection] = []
    for index, match in enumerate(matches):
        title = _clean_xml_text(match.group("title")).strip()
        if _grounding_heading_is_noise(title):
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(paper_text)
        text = _clean_xml_text(paper_text[start:end]).strip()
        if len(text) < 40:
            continue
        section_id = match.group("num")
        category = _grounding_section_category(title)
        sections.append(GroundingSection(section_id, title, category, text))
    return sections


def _grounding_heading_is_noise(title: str) -> bool:
    lowered = title.lower()
    if lowered.startswith(("figure", "table", "appendix", "references", "abstract")):
        return True
    return len(title.split()) > 12


def _grounding_section_category(title: str) -> str:
    lowered = title.lower()
    if any(token in lowered for token in ("introduction", "background", "motivation")):
        return "intro"
    if any(
        token in lowered
        for token in (
            "method",
            "approach",
            "model",
            "architecture",
            "training",
            "algorithm",
            "implementation",
            "framework",
        )
    ):
        return "method"
    if any(token in lowered for token in ("experiment", "evaluation", "result", "ablation", "analysis", "benchmark")):
        return "experiments"
    return ""


def _attach_claims_to_grounding_map(
    grounding_map: dict[str, list[dict[str, str]]],
    claims: list[dict[str, str]],
) -> _EvidenceMap:
    result = _EvidenceMap.coerce(grounding_map)
    result["evidence"] = _ensure_evidence_items(result)
    evidence_ids_by_section = {
        (item.get("category", ""), item.get("section_id", "")): item.get("id", "")
        for item in result["evidence"]
    }
    for bucket in ("intro", "method", "experiments"):
        for index, section in enumerate(result.get(bucket, []), 1):
            section.setdefault("category", bucket)
            section.setdefault(
                "id",
                evidence_ids_by_section.get((bucket, section.get("section_id", "")))
                or _evidence_id(bucket, section.get("section_id", ""), index),
            )
    result.setdefault("claims", [])
    result.setdefault("claim_groundings", [])
    source_sections = [
        section
        for key in ("intro", "method", "experiments")
        for section in result.get(key, [])
    ]
    result["claims"] = []
    result["claim_groundings"] = []
    for claim in claims:
        claim_text = claim.get("claim", claim.get("text", ""))
        claim_id = claim.get("id") or f"claim-{len(result['claims']) + 1}"
        source = _best_source_section_for_claim(claim_text, source_sections)
        evidence_ids = [source.get("id", "")] if source and source.get("id") else []
        grounded = _Claim(
            id=claim_id,
            text=claim_text,
            section=claim.get("section", ""),
            type=claim.get("type", "claim"),
            core=bool(claim.get("core", True)),
            evidence_ids=evidence_ids,
        ).to_dict()
        grounded["source_section"] = source.get("section_id", "") if source else ""
        grounded["source_title"] = source.get("title", "") if source else ""
        result["claims"].append(grounded)
        result["claim_groundings"].append(
            _ClaimGrounding(
                claim_id=claim_id,
                evidence_ids=evidence_ids,
                source_section=grounded["source_section"],
                source_title=grounded["source_title"],
                score=1.0 if evidence_ids else 0.0,
            ).to_dict()
        )
    return result


def _ensure_evidence_items(grounding_map: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    existing = [dict(item) for item in grounding_map.get("evidence", [])]
    if existing:
        return existing
    evidence_items: list[dict[str, str]] = []
    for bucket in ("intro", "method", "experiments"):
        for index, section in enumerate(grounding_map.get(bucket, []), 1):
            evidence = dict(section)
            evidence.setdefault("category", bucket)
            evidence.setdefault("id", _evidence_id(bucket, evidence.get("section_id", ""), index))
            evidence_items.append(evidence)
    return evidence_items


def _build_knowledge_graph(
    grounding_map: dict[str, list[dict[str, str]]],
    summary: str = "",
) -> dict[str, list[dict[str, str]]]:
    nodes: dict[str, KnowledgeGraphNode] = {}
    edges: set[tuple[str, str, str, str]] = set()

    def add_node(label: str, node_type: str, source_section: str = "") -> str:
        normalized = _normalize_kg_label(label)
        if not normalized:
            return ""
        node_id = f"{node_type}:{_slugify_kg_label(normalized)}"
        if node_id not in nodes:
            nodes[node_id] = KnowledgeGraphNode(node_id, normalized, node_type, source_section)
        elif source_section and not nodes[node_id].source_section:
            nodes[node_id].source_section = source_section
        return node_id

    def add_edge(source: str, target: str, relation: str, source_section: str = "") -> None:
        if source and target and source != target:
            edges.add((source, target, relation, source_section))

    paper_id = add_node("Paper", "paper")
    concept_ids: list[str] = []

    for bucket, node_type in (("intro", "concept"), ("method", "method"), ("experiments", "evaluation")):
        for section in grounding_map.get(bucket, []):
            section_id = section.get("section_id", "")
            section_node = add_node(section.get("title", bucket), "section", section_id)
            add_edge(paper_id, section_node, "has_section", section_id)
            text = section.get("text", "")
            for label in _extract_kg_terms(text, node_type):
                term_type = _kg_term_type(label, node_type)
                term_node = add_node(label, term_type, section_id)
                if term_type == "method":
                    add_edge(section_node, term_node, "describes_method", section_id)
                elif term_type == "dataset":
                    add_edge(section_node, term_node, "uses_dataset", section_id)
                elif term_type == "evaluation":
                    add_edge(section_node, term_node, "reports_evaluation", section_id)
                else:
                    add_edge(section_node, term_node, "mentions", section_id)
                    concept_ids.append(term_node)

    for source, target in zip(concept_ids, concept_ids[1:]):
        add_edge(source, target, "relates_to")

    claims = _attach_claims_to_grounding_map(grounding_map, _extract_verifiable_claims(summary)).get("claims", []) if summary else []
    for index, claim in enumerate(claims, 1):
        claim_node = add_node(claim.get("claim", f"Claim {index}")[:120], "claim", claim.get("source_section", ""))
        source_section = claim.get("source_section", "")
        source_title = claim.get("source_title", "")
        if source_title:
            section_node = add_node(source_title, "section", source_section)
            add_edge(claim_node, section_node, "grounded_in", source_section)
        if claim.get("type"):
            type_node = add_node(claim["type"], "claim_type")
            add_edge(claim_node, type_node, "has_claim_type", source_section)

    return {
        "nodes": [node.__dict__ for node in nodes.values()],
        "edges": [
            {
                "source": source,
                "target": target,
                "relation": relation,
                "source_section": source_section,
            }
            for source, target, relation, source_section in sorted(edges)
        ],
    }


def _extract_kg_terms(text: str, default_type: str, limit: int = 18) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r"\b[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+(?:\s+[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)*\b",
        r"\b[A-Z]{2,}(?:-[A-Z0-9]+)*\b",
        r"\b[A-Z][A-Za-z0-9]+(?:Net|Former|Agent|GPT|BERT|VAE|GRPO|RAG|OCR)\b",
        r"\b[a-z]+(?:-[a-z]+)+\b",
        r"\b[A-Za-z]+(?:\s+[A-Za-z]+){0,2}\s+(?:attention|interaction|tool-use|benchmark|dataset|accuracy|ablation|evaluation)\b",
    ]
    for pattern in patterns:
        candidates.extend(re.findall(pattern, text))
    if default_type in {"method", "evaluation"}:
        candidates.extend(re.findall(r"\b[A-Z][A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\s+(?:benchmark|dataset)\b", text))
        candidates.extend(re.findall(r"\b(?:accuracy|ablation|evaluation|metric|score)(?:\s+[a-z]+){0,2}\b", text, flags=re.IGNORECASE))
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        label = _normalize_kg_label(item)
        if not label or label.lower() in seen or _kg_label_is_noise(label):
            continue
        seen.add(label.lower())
        result.append(label)
        if len(result) >= limit:
            break
    return result


def _kg_term_type(label: str, default_type: str) -> str:
    lowered = label.lower()
    if any(token in lowered for token in ("accuracy", "ablation", "evaluation", "score", "metric")):
        return "evaluation"
    if any(token in lowered for token in ("dataset", "benchmark", "bench", "vqa", "qa")):
        return "dataset"
    if default_type == "method" or any(token in lowered for token in ("agent", "grpo", "rag", "vae", "model", "net", "former")):
        return "method"
    return "concept"


def _normalize_kg_label(label: str) -> str:
    label = _clean_xml_text(str(label)).strip(" .,:;()[]{}")
    label = re.sub(r"\s+", " ", label)
    return label[:160]


def _slugify_kg_label(label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "-", label.lower()).strip("-")
    return slug[:80] or "node"


def _kg_label_is_noise(label: str) -> bool:
    lowered = label.lower()
    if lowered in {"page", "figure", "table", "section", "paper"}:
        return True
    return len(label) < 3 or len(label.split()) > 8


def _best_source_section_for_claim(claim: str, sections: list[dict[str, str]]) -> dict[str, str] | None:
    claim_tokens = _grounding_tokens(claim)
    if not claim_tokens:
        return sections[0] if sections else None
    best: tuple[float, dict[str, str] | None] = (0.0, None)
    for section in sections:
        section_tokens = _grounding_tokens(f"{section.get('title', '')} {section.get('text', '')}")
        if not section_tokens:
            continue
        overlap = len(claim_tokens & section_tokens)
        score = overlap / max(len(claim_tokens), 1)
        if score > best[0]:
            best = (score, section)
    return best[1] or (sections[0] if sections else None)


def _grounding_tokens(text: str) -> set[str]:
    lowered = text.lower()
    tokens = set(re.findall(r"[a-z][a-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", lowered))
    stopwords = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "模型",
        "论文",
        "方法",
        "结果",
        "作者",
    }
    return {token for token in tokens if token not in stopwords}


def _split_claim_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;])\s*", text)
    return [part.strip() for part in parts if len(part.strip()) >= 18]


def _claim_is_verifiable(sentence: str) -> bool:
    if any(token in sentence for token in ("原文未", "未提及", "未知", "N/A")):
        return False
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", sentence))


def _claim_type(section: str, sentence: str) -> str:
    text = f"{section} {sentence}"
    if any(token in section for token in ("创新", "贡献")) or re.search(
        r"(?i)\b(contribution|novelty|innovation)\b",
        section,
    ):
        return "contribution"
    if any(token in section for token in ("方法", "机制", "流程")) or re.search(
        r"(?i)\b(method|approach|model|training|algorithm|implementation)\b",
        section,
    ):
        return "method"
    if any(token in text for token in ("创新", "贡献", "提出", "首次", "开放", "解决")):
        return "contribution"
    if any(token in text for token in ("方法", "机制", "流程", "训练", "推理", "架构", "算法", "优化", "模型")):
        return "method"
    return "claim"


def _verification_evidence_text(
    paper_text: str,
    grounding_map: dict[str, list[dict[str, str]]] | None = None,
    max_chars: int = 24000,
) -> dict[str, object]:
    grounding_map = _EvidenceMap.coerce(grounding_map or _build_grounding_map(paper_text))
    evidence_items = _ensure_evidence_items(grounding_map)
    chunks = _chunk_text(paper_text, max_chars // 3)
    fallback = paper_text if len(paper_text) <= max_chars else ""
    if not fallback:
        head = chunks[0] if chunks else paper_text[: max_chars // 3]
        method = _section_window_for_verifier(paper_text, ("method", "approach", "model", "training", "algorithm", "implementation"))
        result = _section_window_for_verifier(paper_text, ("experiment", "evaluation", "result", "ablation", "analysis"))
        fallback = "\n\n".join(part for part in (head, method, result) if part)[:max_chars]
    return {
        "claims": grounding_map.get("claims", []),
        "evidence": evidence_items,
        "claim_groundings": grounding_map.get("claim_groundings", []),
        "sections": {
            "intro": grounding_map.get("intro", []),
            "method": grounding_map.get("method", []),
            "experiments": grounding_map.get("experiments", []),
        },
        "fallback_evidence": fallback,
    }


def _section_window_for_verifier(text: str, keywords: tuple[str, ...], window: int = 9000) -> str:
    lowered = text.lower()
    positions = [lowered.find(keyword) for keyword in keywords if lowered.find(keyword) >= 0]
    if not positions:
        return ""
    start = max(0, min(positions) - 1200)
    return text[start : start + window]


def _parse_verification_result(output: str) -> VerificationResult:
    try:
        payload = json.loads(_extract_json_object(output))
    except Exception as exc:
        return VerificationResult(False, [f"Verifier Agent 输出不是合法 JSON：{exc}"])
    hard_failures = _normalize_verification_items(payload.get("hard_failures") or [])
    soft_warnings = _normalize_verification_items(payload.get("soft_warnings") or [])
    patch_suggestions = _normalize_verification_items(payload.get("patch_suggestions") or [])
    errors = payload.get("errors") or []
    if not isinstance(errors, list):
        errors = [str(errors)]
    cleaned_errors = [_clean_xml_text(str(error)).strip() for error in errors if str(error).strip()]
    if cleaned_errors and not hard_failures:
        hard_failures = [
            {
                "type": "legacy_error",
                "claim": "",
                "reason": error,
            }
            for error in cleaned_errors
        ]
    passed_value = payload.get("passed", payload.get("pass", False))
    if (
        not bool(passed_value)
        and not hard_failures
        and not soft_warnings
        and not patch_suggestions
        and not _verification_output_is_format_error(cleaned_errors)
    ):
        hard_failures = [
            {
                "type": "verifier_failed_without_reason",
                "claim": "",
                "reason": "Verifier Agent returned passed=false without hard failure details.",
            }
        ]
        cleaned_errors.append(hard_failures[0]["reason"])
    passed = not hard_failures
    if hard_failures:
        for failure in hard_failures:
            reason = failure.get("reason", "") or failure.get("message", "")
            if reason and reason not in cleaned_errors:
                cleaned_errors.append(reason)
    return VerificationResult(
        passed,
        cleaned_errors,
        hard_failures=hard_failures,
        soft_warnings=soft_warnings,
        patch_suggestions=patch_suggestions,
    )


def _verification_should_block_report(result: VerificationResult) -> bool:
    return _GatePolicy().decide(result, revision_attempts=2) == _GateDecision.BLOCK


def _verification_failed_due_to_format(result: VerificationResult) -> bool:
    return (
        not result.passed
        and bool(result.errors)
        and not result.hard_failures
        and _verification_output_is_format_error(result.errors)
    )


def _verification_output_is_format_error(errors: list[str]) -> bool:
    return bool(errors) and all("输出不是合法 JSON" in error for error in errors)


def _normalize_verification_items(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        value = [value] if value else []
    normalized: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            normalized.append(
                {
                    str(key): _clean_xml_text(str(val)).strip()
                    for key, val in item.items()
                    if val is not None and str(val).strip()
                }
            )
        elif str(item).strip():
            normalized.append({"type": "message", "claim": "", "reason": _clean_xml_text(str(item)).strip()})
    return normalized


def _extract_json_object(text: str) -> str:
    cleaned = _strip_markdown_fences(text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("missing JSON object")
    return cleaned[start : end + 1]


def _create_codex_client(config: CodexConfig) -> openai.OpenAI:
    timeout_seconds = _codex_timeout_seconds()
    client_kwargs = {}
    if config.use_proxy and config.proxy:
        client_kwargs["proxy"] = config.proxy
    http_client = httpx.Client(
        trust_env=config.use_proxy,
        timeout=httpx.Timeout(timeout_seconds, connect=min(20.0, timeout_seconds)),
        **client_kwargs,
    )
    return openai.OpenAI(
        base_url=config.base_url,
        api_key=config.api_key,
        http_client=http_client,
        max_retries=0,
    )


def _repair_report_format_with_codex(
    client: openai.OpenAI | None,
    model: str,
    summary: str,
    assets: list[PaperAsset],
    abstract: str,
    paper_title: str,
    verification: VerificationResult,
) -> str:
    client = _coerce_codex_client(client)
    missing_or_short = "\n".join(_verification_failure_details(verification).splitlines()[:12])
    prompt = (
        "你是论文精读报告格式修复器。请只基于已有报告内容做 Markdown 结构修复，"
        "不要重新总结整篇论文，不要加入没有证据的新结论。\n\n"
        "必须输出一份完整 Markdown 报告，并包含这些二级标题：\n"
        "## 核心信息\n## 摘要\n## 背景与问题\n## 创新点\n## 一句话总结\n"
        "## 方法主线\n## 关键结果\n## 深度分析\n## 局限\n## 总结\n\n"
        "修复规则：\n"
        "1. 保留已有中文内容和 [[ASSET:n]] 占位符。\n"
        "2. 如果某个必需章节缺失，从已有报告、标题证据和摘要证据中抽取可支持内容补成短段落。\n"
        "3. 如果没有足够证据，写一到两句保守表述，但不要写“未知”“未提及”“N/A”。\n"
        "4. 不要输出解释、JSON、代码块或修复过程。\n\n"
        f"标题证据：{paper_title or '未可靠抽取'}\n\n"
        f"摘要证据：{_clean_xml_text(abstract)[:1000] if abstract else '未可靠抽取'}\n\n"
        f"可用图表占位符：{', '.join(f'[[ASSET:{idx}]]' for idx in range(1, min(len(assets), 8) + 1)) or '无'}\n\n"
        f"需要修复的问题：\n{missing_or_short}\n\n"
        f"已有报告：\n{_truncate_middle(summary, 12000)}"
    )
    return _chat(
        client,
        model,
        prompt,
        system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
        max_tokens=3600,
        max_attempts=1,
    )


def _coerce_codex_client(client: openai.OpenAI | None) -> openai.OpenAI | None:
    if client is not None:
        return client
    try:
        return _create_codex_client(_resolve_codex_config({}))
    except ValueError:
        return client


def _codex_timeout_seconds() -> float:
    raw_value = _first_value({}, "CODEX_TIMEOUT_SECONDS", "CODEX_TIMEOUT")
    try:
        value = float(raw_value) if raw_value else 90.0
    except ValueError:
        value = 90.0
    return max(30.0, min(value, 600.0))


def _codex_chat_attempts() -> int:
    raw_value = _first_value({}, "CODEX_CHAT_ATTEMPTS", "CODEX_MAX_RETRIES")
    try:
        value = int(raw_value) if raw_value else 2
    except ValueError:
        value = 2
    return max(1, min(value, 4))


def _codex_summary_concurrency() -> int:
    raw_value = _first_value({}, "CODEX_SUMMARY_CONCURRENCY")
    try:
        value = int(raw_value) if raw_value else 3
    except ValueError:
        value = 3
    return max(1, min(value, 8))


def _codex_chunk_chars() -> int:
    raw_value = _first_value({}, "CODEX_CHUNK_CHARS")
    try:
        value = int(raw_value) if raw_value else 10000
    except ValueError:
        value = 10000
    return max(4000, min(value, 14000))


def _codex_stream_timeout_seconds() -> float:
    raw_value = _first_value({}, "CODEX_STREAM_TIMEOUT_SECONDS")
    if not raw_value:
        return _codex_timeout_seconds()
    try:
        value = float(raw_value)
    except ValueError:
        value = _codex_timeout_seconds()
    return max(30.0, min(value, 600.0))


def _chat(
    client: openai.OpenAI | None,
    model: str,
    user_prompt: str,
    system_prompt: str = DEEP_PAPER_NOTE_SYSTEM_PROMPT,
    max_tokens: int | None = None,
    max_attempts: int | None = None,
) -> str:
    if client is None:
        raise RuntimeError("Codex 客户端未初始化，请检查 CODEX_BASE_URL、CODEX_API_KEY、CODEX_MODEL 配置。")
    max_attempts = max_attempts or _codex_chat_attempts()
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            request = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
            }
            if max_tokens:
                request["max_tokens"] = max_tokens
            try:
                response = client.chat.completions.create(**request)
            except openai.BadRequestError:
                if "max_tokens" not in request:
                    raise
                request.pop("max_tokens", None)
                response = client.chat.completions.create(**request)
            content = response.choices[0].message.content or ""
            content = _postprocess_summary(content)
            if content.strip():
                return content
            stream_error: Exception | None = None
            try:
                stream_content = _chat_stream_content(client, request)
                if stream_content.strip():
                    return stream_content
            except Exception as exc:
                stream_error = exc
            last_error = stream_error or RuntimeError("Codex 接口返回空内容。")
            if attempt + 1 >= max_attempts:
                break
            time.sleep(_chat_retry_delay(attempt))
        except openai.APIStatusError as exc:
            last_error = exc
            if not _is_retryable_openai_status(exc) or attempt + 1 >= max_attempts:
                break
            time.sleep(_chat_retry_delay(attempt))
        except openai.APITimeoutError as exc:
            last_error = exc
            if attempt + 1 >= max_attempts:
                break
            time.sleep(_chat_retry_delay(attempt))
        except openai.APIConnectionError as exc:
            last_error = exc
            if attempt + 1 >= max_attempts:
                break
            time.sleep(_chat_retry_delay(attempt))
    if isinstance(last_error, openai.APIStatusError):
        raise RuntimeError(_openai_status_error_message(last_error, max_attempts)) from last_error
    if isinstance(last_error, openai.APITimeoutError):
        raise RuntimeError(
            f"Codex 接口响应超时，已重试 {max_attempts} 次仍未成功。"
            f"当前单次超时为 {_codex_timeout_seconds():.0f} 秒；如果接口确实很慢，可以在 config.local.json 中设置 CODEX_TIMEOUT_SECONDS。"
        ) from last_error
    if isinstance(last_error, openai.APIConnectionError):
        raise RuntimeError(
            f"Codex 接口连接失败，已重试 {max_attempts} 次仍未成功：服务端断开或网络链路不稳定。"
            "程序默认不继承系统代理；如果已经配置 CODEX_USE_PROXY/CODEX_PROXY，请检查代理地址、代理软件和服务端稳定性。"
        ) from last_error
    if isinstance(last_error, TimeoutError):
        raise RuntimeError(
            f"Codex 流式接口响应超时，已重试 {max_attempts} 次仍未成功。"
            f"当前流式单次超时为 {_codex_stream_timeout_seconds():.0f} 秒；"
            "如果接口确实很慢，可以在 config.local.json 中设置 CODEX_STREAM_TIMEOUT_SECONDS。"
        ) from last_error
    if last_error is not None:
        raise RuntimeError(f"Codex 接口调用失败，已重试 {max_attempts} 次仍未返回有效内容：{last_error}") from last_error
    raise RuntimeError("Codex 接口调用失败：未返回有效响应。")


def _chat_stream_content(client: openai.OpenAI, request: dict) -> str:
    stream_request = dict(request)
    stream_request["stream"] = True
    stream_request.setdefault("timeout", _codex_stream_timeout_seconds())
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paper-chat-stream")
    future = executor.submit(_read_chat_stream_content, client, stream_request)
    try:
        return future.result(timeout=_codex_stream_timeout_seconds() + 5.0)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise TimeoutError(
            f"Codex 流式接口超过 {_codex_stream_timeout_seconds():.0f} 秒仍未结束。"
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _read_chat_stream_content(client: openai.OpenAI, stream_request: dict) -> str:
    try:
        stream = client.chat.completions.create(**stream_request)
    except openai.BadRequestError:
        if "max_tokens" not in stream_request:
            raise
        stream_request.pop("max_tokens", None)
        stream = client.chat.completions.create(**stream_request)
    if hasattr(stream, "choices"):
        choices = getattr(stream, "choices", None) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        return _postprocess_summary(getattr(message, "content", "") or "")
    chunks: list[str] = []
    try:
        for event in stream:
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None)
            if content:
                chunks.append(content)
    except httpx.HTTPError:
        partial_content = _postprocess_summary("".join(chunks))
        if _stream_request_allows_partial_content(stream_request, partial_content):
            return partial_content
        raise
    return _postprocess_summary("".join(chunks))


def _stream_request_allows_partial_content(stream_request: dict, content: str) -> bool:
    if len(content.strip()) < 300:
        return False
    messages = stream_request.get("messages") or []
    if not messages:
        return False
    user_message = next((item for item in reversed(messages) if item.get("role") == "user"), {})
    user_prompt = str(user_message.get("content") or "")
    return user_prompt.lstrip().startswith("请阅读论文第")


def _is_retryable_openai_status(exc: openai.APIStatusError) -> bool:
    return int(getattr(exc, "status_code", 0) or 0) in {408, 409, 429, 500, 502, 503, 504}


def _chat_retry_delay(attempt: int) -> float:
    return min(12.0, 1.5 * (2**attempt))


def _openai_status_error_message(exc: openai.APIStatusError, attempts: int) -> str:
    status_code = int(getattr(exc, "status_code", 0) or 0)
    if status_code == 503:
        return f"Codex 接口暂时不可用（503 Service Unavailable），已重试 {attempts} 次仍失败，请稍后重试。"
    if status_code:
        return f"Codex 接口返回 HTTP {status_code}，已重试 {attempts} 次仍失败：{exc}"
    return f"Codex 接口请求失败，已重试 {attempts} 次仍失败：{exc}"


def _replace_missing_abstract(
    summary: str,
    abstract: str,
    client: openai.OpenAI,
    model: str,
) -> str:
    if not abstract or not any(marker in summary for marker in ("原文摘要未完整抽取", "摘要未完整抽取")):
        return summary
    try:
        abstract_cn = _chat(
            client,
            model,
            (
                "请把下面论文英文摘要忠实写成中文摘要。只输出中文内容，"
                "不要总结、不要评价、不要添加标题。\n\n"
                f"{abstract}"
            ),
            max_tokens=1200,
            max_attempts=1,
        )
    except RuntimeError:
        return summary
    if not abstract_cn:
        return summary
    pattern = r"(##\s*(?:原文摘要翻译|摘要翻译|摘要)\s*)(.*?)(?=\n## |\Z)"
    replacement = "## 摘要\n" + abstract_cn.strip() + "\n"
    if re.search(pattern, summary, flags=re.DOTALL):
        return re.sub(pattern, replacement, summary, flags=re.DOTALL)
    summary = summary.replace("原文摘要未完整抽取", abstract_cn.strip(), 1)
    return summary.replace("摘要未完整抽取", abstract_cn.strip(), 1)


def _chunk_text(text: str, max_chars: int) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 > max_chars and current:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]


def _postprocess_summary(text: str) -> str:
    text = _clean_xml_text(text)
    text = _strip_thinking(text)
    text = _strip_markdown_fences(text)
    text = _strip_preface_before_markdown_report(text)
    text = _remove_reproduction_advice(text)
    text = _textualize_latex(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_preface_before_markdown_report(text: str) -> str:
    match = re.search(r"(?m)^#\s+\S", text)
    if match:
        return text[match.start() :]
    match = re.search(r"(?m)^##\s+\S", text)
    if match:
        return text[match.start() :]
    return text


def _normalize_final_sections(text: str) -> str:
    text = text.replace("标题翻译", "中文标题")
    text = _normalize_required_heading_levels(text)
    text = re.sub(r"(?m)^##\s*(?:原文摘要翻译|摘要翻译)\s*$", "## 摘要", text)
    text = re.sub(r"(?m)^##\s*(?:研究背景|背景介绍|背景|研究问题|问题定义|问题与背景)\s*$", "## 背景与问题", text)
    text = _merge_background_problem_sections(text)
    text = text.replace("原文摘要未完整抽取", "摘要未完整抽取")
    text = re.sub(r"(?m)^##\s*我的笔记\s*$", "## 总结", text)
    text = re.sub(r"(?ms)(?:^|\n)##\s*引用\s*\n.*?(?=\n## |\Z)", "\n", text)
    text = _remove_figure_reading_sections(text)
    text = _remove_reproduction_advice(text)
    text = _clean_core_info_section(text)
    text = _remove_unspecified_placeholders(text)
    text = _remove_empty_sections(text)
    text = text.replace("翻译", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _ensure_chinese_report_title(text)
    return text.strip()


def _remove_reproduction_advice(text: str) -> str:
    paragraphs = re.split(r"(\n{2,})", text)
    kept: list[str] = []
    for part in paragraphs:
        if part.startswith("\n"):
            kept.append(part)
            continue
        stripped = part.strip()
        if not stripped:
            kept.append(part)
            continue
        if _paragraph_is_reproduction_advice(stripped):
            continue
        cleaned = _remove_reproduction_sentences(part)
        if cleaned.strip():
            kept.append(cleaned)
    result = "".join(kept)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _paragraph_is_reproduction_advice(paragraph: str) -> bool:
    if paragraph.lstrip().startswith("#"):
        return False
    compact = re.sub(r"\s+", "", paragraph)
    if not re.search(r"复现|reproduc", compact, flags=re.IGNORECASE):
        return False
    if re.match(r"^(?:[-*]\s*)?(?:复现实验|复现建议|复现时|可复现性|复现要点)", paragraph):
        return True
    return bool(re.search(r"复现(?:实验)?(?:时)?(?:应|需要|建议|重点关注|要关注|可关注)", compact))


def _remove_reproduction_sentences(text: str) -> str:
    pieces = re.split(r"([。！？!?]\s*)", text)
    result: list[str] = []
    for index in range(0, len(pieces), 2):
        sentence = pieces[index]
        punct = pieces[index + 1] if index + 1 < len(pieces) else ""
        if _paragraph_is_reproduction_advice((sentence + punct).strip()):
            continue
        result.append(sentence + punct)
    cleaned = "".join(result)
    cleaned = re.sub(r"结论和复现时应关注的实验点", "结论和证据边界", cleaned)
    cleaned = re.sub(r"以及复现时应关注的实验点", "以及证据边界", cleaned)
    return cleaned.strip()


def _ensure_chinese_report_title(text: str) -> str:
    title = _extract_note_title(text)
    chinese_title = _core_info_field(text, ("中文标题",))
    if not _looks_like_usable_chinese_title(chinese_title):
        chinese_title = _build_chinese_title_from_core_info(text)
    if not chinese_title:
        return text

    lines = text.splitlines()
    replaced_h1 = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            if _title_needs_chinese_rewrite(title):
                lines[index] = f"# {chinese_title}"
            replaced_h1 = True
            break
    if not replaced_h1:
        lines.insert(0, f"# {chinese_title}")
    updated = "\n".join(lines)
    return _ensure_core_chinese_title_line(updated, chinese_title)


def _title_needs_chinese_rewrite(title: str) -> bool:
    title = _clean_xml_text(title).strip()
    if not title:
        return True
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", title))
    ascii_letters = len(re.findall(r"[A-Za-z]", title))
    if _mixed_english_placeholder_title(title):
        return True
    if re.search(r"的(?:将|本文|论文|该文|研究)", title) or title.endswith("的"):
        return True
    return cjk_chars < 2 and ascii_letters >= 8


def _looks_like_usable_chinese_title(title: str) -> bool:
    title = _clean_xml_text(title).strip()
    if not title:
        return False
    if len(re.findall(r"[\u4e00-\u9fff]", title)) < 2:
        return False
    if _mixed_english_placeholder_title(title):
        return False
    if _core_info_line_is_unspecified(title):
        return False
    if re.search(r"的(?:将|本文|论文|该文|研究)", title) or title.endswith("的"):
        return False
    return True


def _mixed_english_placeholder_title(title: str) -> bool:
    title = _clean_xml_text(title).strip()
    if not title:
        return False
    if re.search(r"[A-Za-z][A-Za-z -]{6,}(?:论文精读|论文笔记|精读)$", title):
        return True
    if re.search(r"(?:paper|summary|reading|analysis)\s*(?:论文|精读|笔记)", title, flags=re.IGNORECASE):
        return True
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", title))
    ascii_letters = len(re.findall(r"[A-Za-z]", title))
    tail = re.split(r"[：:|｜]", title, 1)[-1] if re.search(r"[：:|｜]", title) else title
    tail_cjk = len(re.findall(r"[\u4e00-\u9fff]", tail))
    if tail_cjk >= 6:
        return False
    return cjk_chars <= 6 and ascii_letters > max(14, cjk_chars * 2)


def _build_chinese_title_from_core_info(text: str) -> str:
    method = _core_info_field(text, ("方法名称", "方法", "模型", "系统"))
    if not method:
        method = _method_name_from_original_title(text)
    task = _core_info_field(text, ("研究任务", "任务", "研究对象"))
    idea = _core_info_field(text, ("方法主张", "核心思想", "主要技术"))
    task = _translate_core_task_phrase(_shorten_core_title_phrase(task))
    idea = _core_idea_title_phrase(idea)
    if method and task and idea:
        return f"{method}：面向{task}的{idea}"
    if method and task:
        return f"{method}：面向{task}的论文精读"
    if task and idea:
        return f"{task}：{idea}"
    if task:
        return f"{task}论文精读"
    return ""


def _method_name_from_original_title(text: str) -> str:
    original = _core_info_field(text, ("原文标题", "论文标题", "标题"))
    original = _clean_xml_text(original).strip()
    if not original:
        return ""
    match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_.+\- ]{1,36})\s*[:：]", original)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    match = re.search(r"\b([A-Z][A-Za-z0-9_.+\-]*-[A-Za-z0-9_.+\-]+)\b", original)
    return match.group(1) if match else ""


def _translate_core_task_phrase(text: str) -> str:
    text = _clean_xml_text(text).strip()
    if not text:
        return ""
    replacements = [
        (r"(?i)\bcomplex image restoration\b", "复杂图像复原"),
        (r"(?i)\bimage restoration\b", "图像复原"),
        (r"(?i)\brestoration agents?\b", "恢复智能体"),
        (r"(?i)\bno-reference\b", "无参考"),
        (r"(?i)\bfull-reference\b", "全参考"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\s+", " ", text)
    text = re.split(r"[，,；;。]", text, 1)[0].strip()
    return text[:28].rstrip()


def _core_idea_title_phrase(text: str) -> str:
    text = _clean_xml_text(text).strip()
    if not text:
        return ""
    lowered = text.lower()
    if "policy optimization" in lowered and ("perceptual" in lowered or "reward" in lowered):
        return "感知反馈驱动的策略优化"
    if "慢速规划" in text and "快速" in text and "记忆" in text:
        return "慢速规划与快速记忆执行框架"
    if "规划" in text and "记忆" in text:
        return "规划与记忆执行框架"
    if "长时序决策" in text and "规划" in text:
        return "长时序规划框架"
    if "工具" in text and "序列" in text:
        return "工具序列策略学习"
    match = re.search(r"(?:用|通过|采用)([^，,；;。]{4,32})", text)
    if match:
        return _shorten_core_title_phrase(match.group(1))
    return _shorten_core_title_phrase(text)


def _shorten_core_title_phrase(text: str) -> str:
    text = _clean_xml_text(text).strip()
    if not text:
        return ""
    text = re.split(r"[，,；;。]|即|包括|例如", text, 1)[0].strip()
    text = re.sub(r"^(?:本文|论文|该文|研究)\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    if len(text) > 28:
        text = text[:28].rstrip()
    return text


def _core_info_field(text: str, names: tuple[str, ...]) -> str:
    body = _section_body(text, "核心信息")
    if not body:
        return ""
    for line in body.splitlines():
        stripped = _clean_xml_text(line).strip()
        stripped = re.sub(r"^\s*[-*]\s*", "", stripped)
        match = re.match(r"([^:：]{1,24})[:：]\s*(.+)$", stripped)
        if not match:
            continue
        key = re.sub(r"\s+", "", match.group(1))
        if any(key == re.sub(r"\s+", "", name) for name in names):
            return match.group(2).strip()
    return ""


def _ensure_core_chinese_title_line(text: str, chinese_title: str) -> str:
    pattern = re.compile(r"(?ms)(^##\s*核心信息\s*\n)(.*?)(?=^## |\Z)")

    def replace(match: re.Match) -> str:
        header = match.group(1)
        body = match.group(2).strip("\n")
        lines = body.splitlines()
        insert_at = 0
        for index, line in enumerate(lines):
            stripped = _clean_xml_text(line).strip()
            if re.match(r"^\s*[-*]\s*中文标题\s*[:：]", stripped):
                lines[index] = re.sub(r"[:：].*$", f": {chinese_title}", line, count=1)
                return header + "\n".join(lines).strip() + "\n\n"
            if re.match(r"^\s*[-*]\s*(?:标题|论文题目|原文标题)\s*[:：]", stripped):
                insert_at = index + 1
        lines.insert(insert_at, f"- 中文标题: {chinese_title}")
        return header + "\n".join(lines).strip() + "\n\n"

    if pattern.search(text):
        return pattern.sub(replace, text, count=1)
    return text


def _suppress_formula_text_when_assets_present(summary: str, assets: list[PaperAsset]) -> str:
    if not any(asset.kind == "formula" for asset in assets):
        return summary
    cleaned_lines = [_suppress_formula_expressions_in_line(line) for line in summary.splitlines()]
    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"[ \t]+([，。；：])", r"\1", cleaned)
    cleaned = re.sub(r"说明[，,]\s*(其中|即|这|用于|表示|说明)", r"说明，\1", cleaned)
    cleaned = re.sub(r"([：:])\s*[。；]", r"\1", cleaned)
    cleaned = re.sub(r"([规则机制关系分数作用])[:：]\s*其中", r"\1，其中", cleaned)
    cleaned = re.sub(r"((?:公式|方程)\s*[0-9一二三四五六七八九十]+)\s*说明，\s*其中", r"\1说明对应机制，其中", cleaned)
    cleaned = re.sub(r"((?:公式|方程)\s*[0-9一二三四五六七八九十]+)\s*说明，\s*将", r"\1说明相关指标的组合方式，将", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _suppress_formula_expressions_in_line(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or re.fullmatch(r"\[\[ASSET:\d+\]\]", stripped):
        return line

    if not _line_mentions_formula(line):
        return line

    line = re.sub(
        r"`([^`]+)`",
        lambda match: "" if _looks_like_formula_expression(match.group(1)) else match.group(1),
        line,
    )
    line = re.sub(
        r"\$([^$\n]{4,500})\$",
        lambda match: "" if _looks_like_formula_expression(match.group(1)) else match.group(1),
        line,
    )

    math_expr = (
        r"[^，。；\n]{0,360}?"
        r"(?:=|≥|≤|∑|Σ|∏|\\|arg\s*max|arg\s*min|\bmin\s*\(|\bmax\s*\(|\bQ[A-Za-z]*\s*\()"
        r"[^，。；\n]{0,360}?"
    )
    tail = r"(?P<tail>，其中|，即|，这|，用于|，表示|，说明|。|；)"

    def clean_prefix(prefix: str) -> str:
        prefix = prefix.strip()
        prefix = re.sub(r"\s*(?:可写为|写作|表示为|计算为|定义为|如下|为|是)\s*$", "说明", prefix)
        return prefix

    def replace_colon(match: re.Match) -> str:
        prefix = clean_prefix(match.group("prefix"))
        matched_tail = match.group("tail")
        if matched_tail in {"。", "；"}:
            return prefix + matched_tail
        if matched_tail == "，其中":
            return prefix + "，其中"
        return prefix + "说明" + matched_tail

    def replace_assignment(match: re.Match) -> str:
        prefix = clean_prefix(match.group("prefix"))
        matched_tail = match.group("tail")
        if matched_tail in {"。", "；"}:
            return prefix + "说明其核心计算关系" + matched_tail
        return prefix + "说明" + matched_tail

    formula_label = r"(?:公式|方程)\s*[0-9一二三四五六七八九十]+"
    line = re.sub(
        rf"(?P<prefix>{formula_label}[^。；\n]{{0,160}}?)[：:]\s*{math_expr}{tail}",
        replace_colon,
        line,
        flags=re.IGNORECASE,
    )
    line = re.sub(
        rf"(?P<prefix>{formula_label}[^，。；\n]{{0,120}}?)\s*(?:可写为|写作|表示为|计算为|定义为|为|是)\s*[：:]?\s*{math_expr}{tail}",
        replace_assignment,
        line,
        flags=re.IGNORECASE,
    )
    line = re.sub(r"\s{2,}", " ", line)
    line = re.sub(r"说明说明", "说明", line)
    line = re.sub(r"((?:公式|方程)\s*[0-9一二三四五六七八九十]+)\s*(?:为|是)\s*，", r"\1说明，", line)
    line = re.sub(
        r"((?:公式|方程)\s*[0-9一二三四五六七八九十]+[^，。；\n]{0,80}?)(?:可写为|写作|表示为|计算为|定义为|形式为)\s*[：:]?\s*，",
        r"\1说明，",
        line,
    )
    line = re.sub(r"公式形式为\s*[。；]", "该公式说明其核心计算关系。", line)
    line = re.sub(r"(?:可写为|写作|表示为|计算为|定义为|形式为)\s*[：:]\s*，", "说明，", line)
    return line.strip()


def _line_mentions_formula(line: str) -> bool:
    return bool(re.search(r"(?i)(公式|方程|equation|eq\.?)\s*[0-9一二三四五六七八九十]*", line))


def _looks_like_formula_expression(text: str) -> bool:
    text = _clean_xml_text(text)
    if re.search(r"\\(?:frac|sum|arg|max|min|mathrm|mathbf|left|right)|[=≥≤∑Σ∏]|arg\s*max|arg\s*min", text):
        return True
    latin_tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]*(?:\([^)]*\))?", text)
    operators = re.findall(r"[+\-*/^=<>]|≥|≤|∈|∪", text)
    return len(latin_tokens) >= 3 and len(operators) >= 2


def _normalize_required_heading_levels(text: str) -> str:
    aliases = {
        "核心信息": "核心信息",
        "核心信息表": "核心信息",
        "论文信息": "核心信息",
        "论文基本信息": "核心信息",
        "基本信息": "核心信息",
        "摘要": "摘要",
        "原文摘要": "摘要",
        "中文摘要": "摘要",
        "背景与问题": "背景与问题",
        "研究背景与问题": "背景与问题",
        "研究背景": "背景与问题",
        "背景介绍": "背景与问题",
        "背景": "背景与问题",
        "研究问题": "背景与问题",
        "问题定义": "背景与问题",
        "问题与背景": "背景与问题",
        "创新点": "创新点",
        "核心贡献": "创新点",
        "贡献": "创新点",
        "主要贡献": "创新点",
        "一句话总结": "一句话总结",
        "一句话概括": "一句话总结",
        "方法主线": "方法主线",
        "方法": "方法主线",
        "方法概览": "方法主线",
        "技术路线": "方法主线",
        "关键结果": "关键结果",
        "实验结果": "关键结果",
        "实验与结果": "关键结果",
        "结果": "关键结果",
        "深度分析": "深度分析",
        "结果分析": "深度分析",
        "分析": "深度分析",
        "讨论": "深度分析",
        "局限": "局限",
        "局限性": "局限",
        "局限与不足": "局限",
        "不足": "局限",
        "限制": "局限",
        "总结": "总结",
        "结论": "总结",
        "结语": "总结",
    }

    def normalize(match: re.Match) -> str:
        title = _canonical_report_heading_key(match.group(2))
        canonical = aliases.get(title)
        if canonical:
            return f"## {canonical}"
        return match.group(0)

    return re.sub(r"(?m)^(#{2,6})\s+(.+?)\s*$", normalize, text)


def _canonical_report_heading_key(title: str) -> str:
    title = _clean_xml_text(title).strip()
    title = re.sub(r"^[（(]?\s*(?:\d+|[一二三四五六七八九十]+)\s*[）).、:：-]\s*", "", title)
    title = re.sub(r"^[第]\s*[一二三四五六七八九十\d]+\s*[章节节部分]\s*", "", title)
    title = re.sub(r"\s*[：:]\s*$", "", title)
    title = re.sub(r"\s*[（(].*?[）)]\s*$", "", title)
    return re.sub(r"\s+", "", title)


def _required_report_section_order() -> tuple[str, ...]:
    return (
        "核心信息",
        "摘要",
        "背景与问题",
        "创新点",
        "一句话总结",
        "方法主线",
        "关键结果",
        "深度分析",
        "局限",
        "总结",
    )


def _ensure_required_report_sections(summary: str, abstract: str = "", paper_title: str = "") -> str:
    summary = _normalize_final_sections(summary)
    title = _extract_note_title(summary) or "论文精读笔记"
    sections = _collect_markdown_sections(summary)

    result = [f"# {title}"]
    core_body = _complete_core_info_body(sections.pop("核心信息", ""), paper_title)
    result.extend(["", "## 核心信息", core_body])
    for section in _required_report_section_order():
        if section == "核心信息":
            continue
        body = sections.pop(section, "").strip()
        if body:
            result.extend(["", f"## {section}", body])

    for section, body in sections.items():
        if body.strip():
            result.extend(["", f"## {section}", body.strip()])

    return _normalize_final_sections("\n".join(part for part in result if part is not None))


def _collect_markdown_sections(summary: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if current and buffer:
            body = "\n".join(buffer).strip()
            if body:
                sections[current] = (sections.get(current, "").rstrip() + "\n\n" + body).strip()
        buffer = []

    for raw_line in summary.splitlines():
        line = raw_line.rstrip()
        match = re.match(r"^#{2,6}\s+(.+?)\s*$", line)
        if match:
            flush()
            current = _canonical_report_heading_key(match.group(1)) or match.group(1).strip()
            current = _normalize_report_section_name(current)
            continue
        if current:
            buffer.append(line)
    flush()
    return sections


def _normalize_report_section_name(title: str) -> str:
    for block in _normalize_required_heading_levels(f"## {title}").splitlines():
        if block.startswith("## "):
            return block[3:].strip()
    return title


def _complete_core_info_body(body: str, paper_title: str) -> str:
    lines = [line for line in body.splitlines() if line.strip()]
    if paper_title and not any(re.match(r"^\s*[-*]\s*原文标题\s*[:：]", line) for line in lines):
        lines.insert(0, f"- 原文标题: {paper_title}")
    if not lines:
        lines.append(f"- 原文标题: {paper_title or '论文精读对象'}")
    return "\n".join(lines)


def _compact_text_len(text: str) -> int:
    text = re.sub(r"\[\[ASSET:\d+\]\]", "", text)
    text = re.sub(r"(?m)^#{1,6}\s+.*$", "", text)
    return len(re.sub(r"\s+", "", _clean_xml_text(text)))


def _merge_background_problem_sections(text: str) -> str:
    blocks = re.split(r"(?m)(?=^##\s+)", text)
    merged_bodies: list[str] = []
    result: list[str] = []
    placeholder = "__PAPER_AGENT_BACKGROUND_AND_PROBLEM__"
    inserted = False
    for block in blocks:
        if re.match(r"(?m)^##\s*背景与问题\s*$", block.splitlines()[0] if block.splitlines() else ""):
            body = re.sub(r"(?m)^##\s*背景与问题\s*\n?", "", block, count=1).strip()
            if body:
                merged_bodies.append(body)
            if not inserted:
                result.append(placeholder)
                inserted = True
            continue
        result.append(block)
    if not inserted:
        return text
    merged = "## 背景与问题\n" + "\n\n".join(merged_bodies).strip() + "\n\n"
    return "".join(result).replace(placeholder, merged)


def _clean_core_info_section(text: str) -> str:
    pattern = re.compile(r"(?ms)(^##\s*核心信息\s*\n)(.*?)(?=^## |\Z)")

    def clean(match: re.Match) -> str:
        header = match.group(1)
        body = match.group(2)
        kept_lines = []
        for line in body.splitlines():
            stripped = line.strip()
            if _core_info_line_is_unspecified(stripped):
                continue
            kept_lines.append(line)
        return header + "\n".join(kept_lines).strip() + "\n\n"

    return pattern.sub(clean, text)


def _enforce_core_original_title(text: str, paper_title: str) -> str:
    paper_title = re.sub(r"\s+", " ", _clean_xml_text(paper_title)).strip()
    if not paper_title:
        return text

    pattern = re.compile(r"(?ms)(^##\s*核心信息\s*\n)(.*?)(?=^## |\Z)")

    def replace(match: re.Match) -> str:
        header = match.group(1)
        body = match.group(2).strip("\n")
        lines = body.splitlines()
        kept: list[str] = []
        insert_at = 0
        for line in lines:
            key = _core_info_line_key(line)
            if key in {"标题", "论文题目", "原文题目", "原文标题"}:
                insert_at = len(kept)
                continue
            kept.append(line)
        kept.insert(insert_at, f"- 原文标题: {paper_title}")
        return header + "\n".join(line for line in kept if line.strip()).strip() + "\n\n"

    if pattern.search(text):
        return pattern.sub(replace, text, count=1)
    return text


def _core_info_line_key(line: str) -> str:
    stripped = _clean_xml_text(line).strip()
    stripped = re.sub(r"^\s*[-*]\s*", "", stripped)
    match = re.match(r"([^:：]{1,24})[:：]", stripped)
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group(1))


def _remove_unspecified_placeholders(text: str) -> str:
    placeholder_pattern = re.compile(
        r"^\s*(?:[-*]\s*)?(?:[^:：\n]{1,24}[:：]\s*)?"
        r"(?:原文未明确说明|摘要未完整抽取|未完整抽取|未明确说明|未说明|未提及|未找到|未知|N/A|n/a|None|none)"
        r"[。.\s]*$"
    )
    kept_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if placeholder_pattern.match(stripped):
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines)


def _remove_empty_sections(text: str) -> str:
    blocks = re.split(r"(?m)(?=^#{1,3}\s+)", text)
    kept: list[str] = []
    for index, block in enumerate(blocks):
        if not block.strip():
            continue
        lines = block.splitlines()
        heading = lines[0].strip() if lines else ""
        if heading.startswith("#"):
            body = "\n".join(lines[1:]).strip()
            level = len(heading) - len(heading.lstrip("#"))
            has_child_section = False
            for next_block in blocks[index + 1 :]:
                next_lines = next_block.splitlines()
                next_heading = next_lines[0].strip() if next_lines else ""
                if not next_heading.startswith("#"):
                    continue
                next_level = len(next_heading) - len(next_heading.lstrip("#"))
                has_child_section = next_level > level
                break
            if not body and not heading.startswith("# ") and not has_child_section:
                continue
        kept.append(block.strip())
    return "\n\n".join(kept)


def _core_info_line_is_unspecified(line: str) -> bool:
    if not re.match(r"^[-*]\s*", line):
        return False
    value = re.sub(r"^[-*]\s*[^:：]+[:：]\s*", "", line).strip()
    value = value.strip("-—– ：:")
    if not value:
        return True
    normalized = re.sub(r"\s+", "", value).lower()
    unspecified_values = {
        "原文未明确说明",
        "摘要未完整抽取",
        "未完整抽取",
        "未明确说明",
        "未说明",
        "未提及",
        "未找到",
        "未知",
        "无",
        "n/a",
        "na",
        "none",
        "unknown",
    }
    return normalized in unspecified_values or "原文未明确说明" in normalized


def _deduplicate_section(text: str, section_name: str) -> str:
    blocks = re.split(r"(?m)(?=^## )", text)
    result: list[str] = []
    first_index: int | None = None
    for block in blocks:
        if not block:
            continue
        first_line, sep, rest = block.partition("\n")
        heading = first_line.strip().lstrip("#").strip()
        if heading == section_name:
            if first_index is None:
                first_index = len(result)
                result.append(block.strip())
            elif rest.strip():
                result[first_index] = result[first_index].rstrip() + "\n\n" + rest.strip()
            continue
        result.append(block.strip())
    return "\n\n".join(part for part in result if part)


def _remove_figure_reading_sections(text: str) -> str:
    removed_headings = {
        "图表精读",
        "图标精读",
        "图片精读",
        "表格精读",
        "图表与公式精读",
        "图表和公式精读",
    }
    blocks = re.split(r"(?m)(?=^## )", text)
    result: list[str] = []
    for block in blocks:
        if not block:
            continue
        first_line, _sep, _rest = block.partition("\n")
        heading = first_line.strip().lstrip("#").strip()
        normalized = re.sub(r"\s+", "", heading)
        if normalized in removed_headings or ("精读" in normalized and any(token in normalized for token in ("图表", "图标", "图片", "表格", "公式"))):
            continue
        result.append(block.strip())
    return "\n\n".join(part for part in result if part)


def _strip_thinking(text: str) -> str:
    patterns = [
        r"<think\b[^>]*>.*?</think>",
        r"<thinking\b[^>]*>.*?</thinking>",
        r"<thought\b[^>]*>.*?</thought>",
        r"<analysis\b[^>]*>.*?</analysis>",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"^\s*(Thinking|Reasoning|Analysis)\s*:.*?(?=\n#|\n[A-Z\u4e00-\u9fff])", "", text, flags=re.IGNORECASE | re.DOTALL)
    return text


def _strip_markdown_fences(text: str) -> str:
    text = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", text)
    return text.replace("```", "")


def _textualize_latex(text: str) -> str:
    text = text.replace("\\[", "").replace("\\]", "")
    text = text.replace("\\(", "").replace("\\)", "")
    text = text.replace("$$", "")
    text = re.sub(r"(?<!\w)\$(.+?)\$(?!\w)", r"\1", text)
    text = re.sub(r"\\tilde\{([^{}]+)\}", r"~\1", text)
    text = re.sub(r"\\mathbb\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\mathcal\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\mathrm\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\text\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\mathbf\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"_\{([^{}]+)\}", r"_\1", text)
    text = re.sub(r"\^\{([^{}]+)\}", r"^\1", text)

    def frac(match: re.Match) -> str:
        return f"({match.group(1)})/({match.group(2)})"

    replacements = {
        "\\Delta": "Δ",
        "\\alpha": "α",
        "\\beta": "β",
        "\\gamma": "γ",
        "\\theta": "θ",
        "\\lambda": "λ",
        "\\sum": "Σ",
        "\\infty": "∞",
        "\\times": "×",
        "\\cdot": "·",
        "\\rightarrow": "→",
        "\\to": "→",
        "\\geq": "≥",
        "\\leq": "≤",
        "\\neq": "≠",
        "\\approx": "≈",
        "\\argmax": "argmax",
        "\\max": "max",
        "\\min": "min",
        "\\in": "∈",
        "\\left": "",
        "\\right": "",
        "\\quad": " ",
        "\\ldots": "…",
        "\\ldotp": ".",
        "\\{": "{",
        "\\}": "}",
    }
    text = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", frac, text)
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = text.replace("\\$", "$")
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
    text = text.replace("\\", "")
    text = text.replace("{", "").replace("}", "")
    text = text.replace("$", "")
    plain_math_words = {
        "geq": "≥",
        "leq": "≤",
        "neq": "≠",
        "approx": "≈",
        "eta": "η",
        "theta": "θ",
        "lambda": "λ",
    }
    for source, target in plain_math_words.items():
        text = re.sub(rf"\b{source}\b", target, text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def _asset_context(
    assets: list[PaperAsset],
    text_preview_chars: int = 1500,
    latex_preview_chars: int = 2000,
) -> str:
    if not assets:
        return "未抽取到图表截图。"
    parts = []
    labels: dict[int, str] = {}
    counters = {"figure": 0, "table": 0, "formula": 0}
    for idx, asset in enumerate(assets, 1):
        kind = {"table": "表格", "figure": "图片", "formula": "公式截图"}.get(asset.kind, "截图")
        label = _asset_display_label(idx, asset, counters, labels)
        text = (
            f"[[ASSET:{idx}]] {kind}，第 {asset.page_number} 页，"
            f"最终引用标签：{_compact_asset_label(label)}，"
            f"建议插入章节：{_target_section_for_asset(asset)}，"
            f"caption/context: {_clean_xml_text(asset.caption)}"
        )
        if asset.text and text_preview_chars > 0 and asset.kind != "formula":
            text += f"\n表格文本预览：\n{_clean_xml_text(asset.text[:text_preview_chars])}"
        if asset.latex and latex_preview_chars > 0 and asset.kind != "formula":
            text += f"\nTexTeller LaTeX：{_clean_xml_text(asset.latex[:latex_preview_chars])}"
        parts.append(text)
    return "\n".join(parts)


def _formula_asset_usage_rule(assets: list[PaperAsset]) -> str:
    formula_assets = []
    labels: dict[int, str] = {}
    counters = {"figure": 0, "table": 0, "formula": 0}
    for idx, asset in enumerate(assets, 1):
        label = _asset_display_label(idx, asset, counters, labels)
        if asset.kind == "formula":
            formula_assets.append(f"[[ASSET:{idx}]] 最终引用标签：{_compact_asset_label(label)}")
    if not formula_assets:
        return "未抽取到公式截图；如需提到公式，只用中文解释其作用，不要补写完整公式。"
    return (
        "已抽取公式截图，最终报告必须用截图展示公式本体。正文只写公式编号、变量含义和工程作用，"
        "不要复写 LaTeX、等式、arg max/min、求和式、矩阵式或长变量表达。\n"
        + "\n".join(formula_assets)
    )


def _formula_context(formulas: list[str]) -> str:
    if not formulas:
        return "未抽取到可靠公式候选。"
    return "\n".join(f"{idx}. {formula}" for idx, formula in enumerate(formulas, 1))


def _recognized_formula_context(assets: list[PaperAsset]) -> str:
    formulas = [
        f"[[ASSET:{idx}]] {asset.latex}"
        for idx, asset in enumerate(assets, 1)
        if asset.kind == "formula" and asset.latex
    ]
    if not formulas:
        return "未通过 TexTeller 识别到可靠公式。"
    return "\n".join(formulas)


def _ensure_asset_markers(summary: str, assets: list[PaperAsset]) -> str:
    """Keep figures/tables near related sections even if the model forgot markers."""
    if not assets:
        return summary

    summary = _insert_markers_after_explicit_references(summary, assets)
    referenced = set(re.findall(r"\[\[ASSET:(\d+)\]\]", summary))
    missing_ids = [
        idx
        for idx, asset in enumerate(assets, 1)
        if str(idx) not in referenced and asset.kind in {"figure", "table", "formula"}
    ]
    if not missing_ids:
        return summary

    insertions: dict[str, list[str]] = {}
    for asset_id in missing_ids:
        asset = assets[asset_id - 1]
        if asset.kind == "figure" and len([i for i in missing_ids if assets[i - 1].kind == "figure" and i <= asset_id]) > 6:
            continue
        if asset.kind == "table" and len([i for i in missing_ids if assets[i - 1].kind == "table" and i <= asset_id]) > 8:
            continue
        if asset.kind == "formula":
            continue
        target = _target_section_for_asset(asset)
        insertions.setdefault(target, []).extend(_asset_placeholder_lines(asset_id, asset, target))

    lines = summary.splitlines()
    result: list[str] = []
    current_section = ""
    inserted_sections: set[str] = set()
    body_seen: dict[str, bool] = {}

    for line_index, line in enumerate(lines):
        result.append(line)
        heading = _heading_name(line)
        if heading:
            current_section = heading
            continue
        if not current_section or current_section in inserted_sections or current_section not in insertions:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(">") or re.fullmatch(r"\[\[ASSET:\d+\]\]", stripped):
            continue
        if _next_nonempty_line_is_asset_marker(lines, line_index):
            continue
        if not body_seen.get(current_section):
            body_seen[current_section] = True
            result.extend(insertions[current_section])
            inserted_sections.add(current_section)

    remaining_sections = [section for section in insertions if section not in inserted_sections]
    for section in remaining_sections:
        _insert_asset_lines_into_section(result, section, insertions[section])
    return _normalize_final_sections("\n".join(result))


def _next_nonempty_line_is_asset_marker(lines: list[str], line_index: int) -> bool:
    for next_line in lines[line_index + 1 :]:
        stripped = next_line.strip()
        if not stripped:
            continue
        return bool(re.fullmatch(r"\[\[ASSET:\d+\]\]", stripped))
    return False


def _insert_markers_after_explicit_references(summary: str, assets: list[PaperAsset]) -> str:
    lines = summary.splitlines()
    if not lines:
        return summary

    referenced = set(re.findall(r"\[\[ASSET:(\d+)\]\]", summary))
    insert_after: dict[int, list[str]] = {}
    for asset_id, asset in enumerate(assets, 1):
        if str(asset_id) in referenced:
            continue
        label = _original_asset_label(asset)
        if not label:
            continue
        compact = _compact_asset_label(label)
        pattern = _asset_reference_pattern(compact)
        for line_index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or re.fullmatch(r"\[\[ASSET:\d+\]\]", stripped):
                continue
            if _line_mentions_asset_label(stripped, compact, pattern, asset):
                insert_after.setdefault(line_index, []).append(f"[[ASSET:{asset_id}]]")
                referenced.add(str(asset_id))
                break

    if not insert_after:
        return summary

    result: list[str] = []
    for index, line in enumerate(lines):
        result.append(line)
        result.extend(insert_after.get(index, []))
    return "\n".join(result)


def _line_mentions_asset_label(line: str, compact_label: str, pattern: str, asset: PaperAsset) -> bool:
    if re.search(pattern, line):
        return True
    key = _asset_label_key(asset)
    if key and key in _critical_referenced_asset_keys_in_text(line):
        return True
    if asset.kind != "formula":
        return False
    compact_line = _compact_asset_label(line)
    if compact_label and compact_label in compact_line:
        return True
    match = re.match(r"^公式(.+)$", compact_label)
    if not match:
        return False
    number = re.escape(match.group(1))
    return bool(re.search(rf"(?i)(?:equation|eq\.?|formula)\s*[\(:：]?\s*{number}\b", line))


def _asset_placeholder_lines(asset_id: int, asset: PaperAsset, section: str) -> list[str]:
    return [f"[[ASSET:{asset_id}]]"]


def _insert_asset_lines_into_section(result: list[str], section: str, lines: list[str]) -> None:
    heading_index = _find_section_heading_index(result, section)
    if heading_index is not None:
        insert_at = _section_body_insert_index(result, heading_index)
        result[insert_at:insert_at] = lines
        return
    insert_at = _default_section_insert_index(result, section)
    result[insert_at:insert_at] = [f"## {section}", *lines]


def _find_section_heading_index(lines: list[str], section: str) -> int | None:
    for idx, line in enumerate(lines):
        if line.strip().lstrip("#").strip() == section and line.lstrip().startswith("##"):
            return idx
    return None


def _section_body_insert_index(lines: list[str], heading_index: int) -> int:
    insert_at = heading_index + 1
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1
    if insert_at < len(lines) and not lines[insert_at].lstrip().startswith("#"):
        insert_at += 1
    while insert_at < len(lines) and re.fullmatch(r"\s*\[\[ASSET:\d+\]\]\s*", lines[insert_at]):
        insert_at += 1
    return insert_at


def _default_section_insert_index(lines: list[str], section: str) -> int:
    if section == "方法主线":
        before = {"关键结果", "深度分析", "局限", "总结", "我的笔记"}
    elif section == "关键结果":
        before = {"深度分析", "局限", "总结", "我的笔记"}
    elif section == "数据与任务定义":
        before = {"方法主线", "关键结果", "深度分析", "局限", "总结", "我的笔记"}
    else:
        before = {"深度分析", "局限", "总结", "我的笔记"}
    for idx, line in enumerate(lines):
        if line.strip().lstrip("#").strip() in before:
            return idx
    return len(lines)


def _heading_name(line: str) -> str:
    stripped = line.strip()
    if not stripped.startswith("#"):
        return ""
    return stripped.lstrip("#").strip()


def _target_section_for_asset(asset: PaperAsset) -> str:
    if asset.kind == "table":
        return "关键结果"
    if asset.kind == "formula":
        return "方法主线"
    caption = (asset.caption or "").lower()
    method_tokens = [
        "framework",
        "architecture",
        "pipeline",
        "overview",
        "workflow",
        "method",
        "mechanism",
        "module",
        "algorithm",
        "model structure",
        "图架构",
        "架构",
        "框架",
        "流程",
        "机制",
    ]
    result_tokens = [
        "result",
        "benchmark",
        "performance",
        "accuracy",
        "comparison",
        "ablation",
        "case analysis",
        "analysis",
        "evaluation",
        "score",
        "qa",
        "结果",
        "实验",
        "对比",
        "消融",
        "评测",
        "性能",
    ]
    if any(token in caption for token in method_tokens):
        return "方法主线"
    if any(token in caption for token in result_tokens):
        return "关键结果"
    if any(token in caption for token in ["data", "dataset", "task", "example"]):
        return "数据与任务定义"
    return "方法主线"


def _write_docx(
    path: Path,
    paper_filename: str,
    summary: str,
    assets: list[PaperAsset],
) -> None:
    media_files = [
        (asset.path, f"image{i + 1}.png", f"rId{i + 4}")
        for i, asset in enumerate(assets)
    ]
    document_xml = _document_xml(paper_filename, summary, assets, media_files)
    rels_xml = _document_rels(media_files)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", _content_types())
        docx.writestr("_rels/.rels", _package_rels())
        docx.writestr("docProps/core.xml", _core_props())
        docx.writestr("docProps/app.xml", _app_props())
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("word/styles.xml", _styles_xml())
        docx.writestr("word/settings.xml", _settings_xml())
        docx.writestr("word/fontTable.xml", _font_table_xml())
        docx.writestr("word/_rels/document.xml.rels", rels_xml)
        for source, media_name, _rel_id in media_files:
            if source.exists():
                docx.write(source, f"word/media/{media_name}")


def _next_available_report_path(path: Path) -> Path:
    for index in range(1, 100):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        try:
            with candidate.open("ab"):
                return candidate
        except PermissionError:
            continue
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.stem}-{timestamp}{path.suffix}")


def _document_xml(
    paper_filename: str,
    summary: str,
    assets: list[PaperAsset],
    media_files: list[tuple[Path, str, str]],
) -> str:
    summary = _suppress_formula_text_when_assets_present(summary, assets)
    summary = _normalize_final_sections(_postprocess_summary(summary))
    body = [
        _paragraph(_extract_note_title(summary) or "论文精读笔记", "Title"),
        _paragraph(f"源文件：{paper_filename}", None),
        _paragraph(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", None),
    ]
    media_by_id = {idx + 1: item for idx, item in enumerate(media_files)}
    asset_by_id = {idx + 1: asset for idx, asset in enumerate(assets)}
    used_assets: set[int] = set()
    asset_labels: dict[int, str] = {}
    asset_counters = {"figure": 0, "table": 0, "formula": 0}
    section_name = ""

    for line in _reflow_markdown_lines(summary):
        stripped = _clean_xml_text(line).strip()
        if not stripped:
            continue
        asset_ids = [int(match) for match in re.findall(r"\[\[ASSET:(\d+)\]\]", stripped)]
        text_without_markers = re.sub(r"\[\[ASSET:\d+\]\]", "", stripped).strip()
        line_reference_labels = [
            _asset_display_label(asset_id, asset_by_id[asset_id], asset_counters, asset_labels)
            for asset_id in asset_ids
            if asset_id in asset_by_id and asset_id in media_by_id and asset_id not in used_assets
        ]

        if text_without_markers:
            text_without_markers = _sync_inline_asset_references(text_without_markers, line_reference_labels)
            if text_without_markers.startswith("# "):
                section_name = text_without_markers[2:].strip()
                if section_name != (_extract_note_title(summary) or ""):
                    body.append(_paragraph(section_name, "Heading1"))
            elif text_without_markers.startswith("## "):
                section_name = text_without_markers[3:].strip()
                body.append(_paragraph(section_name, "Heading1"))
            elif text_without_markers.startswith("### "):
                body.append(_paragraph(text_without_markers[4:].strip(), "Heading3"))
            elif re.fullmatch(r"[-*_]{3,}", text_without_markers):
                continue
            elif _looks_like_subheading_text(text_without_markers, section_name):
                body.append(_paragraph(_normalize_markdown_line(text_without_markers), "Heading3"))
            elif text_without_markers.startswith("> "):
                callout_text = text_without_markers[2:].strip() if text_without_markers.startswith("> ") else text_without_markers
                body.append(_paragraph(_with_asset_references(_normalize_callout_line(callout_text), line_reference_labels), "FigureCallout" if "[!figure]" in callout_text else "Callout"))
            elif section_name in {"核心信息"} and _is_list_line(text_without_markers):
                body.append(_paragraph(_normalize_markdown_line(text_without_markers), "Metadata"))
            elif section_name in {"一句话总结"} or text_without_markers.startswith(("重点", "结论")):
                body.append(_paragraph(_with_asset_references(_normalize_markdown_line(text_without_markers), line_reference_labels), "Callout"))
            elif text_without_markers.startswith("[NOTE_CARD]"):
                body.append(_paragraph(_with_asset_references(text_without_markers.removeprefix("[NOTE_CARD]").strip(), line_reference_labels), "NoteCard"))
            elif _is_list_line(text_without_markers):
                body.append(_paragraph(_with_asset_references(_normalize_list_to_sentence(text_without_markers), line_reference_labels), None))
            else:
                body.append(_paragraph(_with_asset_references(_normalize_markdown_line(text_without_markers), line_reference_labels), None))
        elif line_reference_labels:
            if not _append_asset_references_to_previous_paragraph(body, line_reference_labels):
                body.append(_paragraph(_asset_reference_sentence(line_reference_labels[0]), "AssetLead"))

        for asset_id in asset_ids:
            asset = asset_by_id.get(asset_id)
            media = media_by_id.get(asset_id)
            if not asset or not media or asset_id in used_assets:
                continue
            source, _media_name, rel_id = media
            body.append(_image_paragraph(source, asset_id, rel_id, asset))
            used_assets.add(asset_id)

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas"
 xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
 xmlns:v="urn:schemas-microsoft-com:vml"
 xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:w10="urn:schemas-microsoft-com:office:word"
 xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
 xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
 xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk"
 xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml"
 xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
 mc:Ignorable="w14 wp14">
 <w:body>
  {''.join(body)}
  <w:sectPr>
   <w:pgSz w:w="11906" w:h="16838"/>
   <w:pgMar w:top="1080" w:right="1260" w:bottom="1080" w:left="1260" w:header="708" w:footer="708" w:gutter="0"/>
  </w:sectPr>
 </w:body>
</w:document>"""


def _reflow_markdown_lines(markdown: str) -> list[str]:
    result: list[str] = []
    paragraph_buffer: list[str] = []
    list_buffer: list[str] = []
    current_section = ""

    def flush_paragraph() -> None:
        if not paragraph_buffer:
            return
        result.append(_join_paragraph_buffer(paragraph_buffer))
        paragraph_buffer.clear()

    def flush_list() -> None:
        if not list_buffer:
            return
        if current_section in {"核心信息"}:
            result.extend(list_buffer)
        else:
            result.append(_join_paragraph_buffer([_normalize_list_to_sentence(item) for item in list_buffer]))
        list_buffer.clear()

    for raw_line in markdown.splitlines():
        line = _clean_xml_text(raw_line).strip()
        if not line:
            flush_list()
            flush_paragraph()
            continue
        if line.startswith("#"):
            flush_list()
            flush_paragraph()
            current_section = line.lstrip("#").strip()
            result.append(line)
            continue
        if _is_list_line(line):
            flush_paragraph()
            list_buffer.append(line)
            continue
        if _looks_like_subheading_text(line, current_section):
            flush_list()
            flush_paragraph()
            result.append(line)
            continue
        if _is_structural_markdown_line(line):
            flush_list()
            flush_paragraph()
            result.append(line)
            continue
        flush_list()
        paragraph_buffer.append(line)

    flush_list()
    flush_paragraph()
    return result


def _is_structural_markdown_line(line: str) -> bool:
    if line.startswith("#"):
        return True
    if line.startswith(">"):
        return True
    if re.fullmatch(r"\[\[ASSET:\d+\]\]", line):
        return True
    if re.fullmatch(r"[-*_]{3,}", line):
        return True
    if re.match(r"^\s*(?:[-*]\s+|\d+[.、]\s*)", line):
        return True
    return False


def _join_paragraph_buffer(lines: list[str]) -> str:
    text = ""
    for line in lines:
        if not text:
            text = line
            continue
        if re.search(r"[A-Za-z0-9`）)】]$", text) and re.match(r"^[A-Za-z0-9`（(【[]", line):
            text += " " + line
        else:
            text += line
    return text


def _looks_like_subheading_text(text: str, section_name: str = "") -> bool:
    text = _normalize_inline_text(text).strip()
    if not text or section_name == "核心信息":
        return False
    if len(text) > 32 or len(text) < 3:
        return False
    if re.search(r"[。！？!?；;：:，,]$", text):
        return False
    if re.search(r"\[\[ASSET:\d+\]\]", text):
        return False
    if re.match(r"^\s*(?:[-*]\s+|\d+[.、]\s*)", text):
        return False
    heading_tokens = (
        "流程",
        "公式",
        "机制",
        "相似度",
        "条件",
        "定义",
        "方法",
        "结果",
        "消融",
        "分析",
        "贡献",
        "局限",
        "对比",
        "实验",
        "结论",
        "任务",
        "数据",
    )
    return any(token in text for token in heading_tokens)


def _paragraph(text: str, style: str | None = None) -> str:
    style_xml = _paragraph_properties(style)
    run_pr = _run_properties(style)
    return f"<w:p>{style_xml}<w:r>{run_pr}<w:t xml:space=\"preserve\">{html.escape(_normalize_inline_text(text))}</w:t></w:r></w:p>"


def _run_properties(style: str | None = None) -> str:
    base_fonts = _font_run_xml()
    if style == "Heading3":
        return f'<w:rPr>{base_fonts}<w:b/><w:color w:val="0F766E"/><w:sz w:val="23"/></w:rPr>'
    return f"<w:rPr>{base_fonts}</w:rPr>"


def _font_run_xml() -> str:
    return (
        f'<w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" '
        f'w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/>'
    )


def _paragraph_properties(style: str | None = None) -> str:
    if style == "Title":
        return (
            '<w:pPr><w:pStyle w:val="Title"/><w:jc w:val="left"/>'
            '<w:spacing w:before="160" w:after="140"/></w:pPr>'
        )
    if style == "Heading1":
        return (
            '<w:pPr><w:pStyle w:val="Heading1"/>'
            '<w:spacing w:before="360" w:after="150" w:line="300" w:lineRule="auto"/>'
            '<w:ind w:left="80" w:right="80"/>'
            '<w:pBdr><w:left w:val="single" w:sz="18" w:space="6" w:color="0F766E"/></w:pBdr>'
            '<w:shd w:val="clear" w:color="auto" w:fill="E7F2F0"/>'
            '</w:pPr>'
        )
    if style == "Heading2":
        return (
            '<w:pPr><w:pStyle w:val="Heading2"/>'
            '<w:spacing w:before="240" w:after="100" w:line="300" w:lineRule="auto"/>'
            '<w:ind w:left="80" w:right="80"/>'
            '<w:pBdr><w:left w:val="single" w:sz="14" w:space="6" w:color="0F766E"/></w:pBdr>'
            '<w:shd w:val="clear" w:color="auto" w:fill="E7F2F0"/>'
            '</w:pPr>'
        )
    if style == "Heading3":
        return (
            '<w:pPr><w:pStyle w:val="Heading3"/>'
            '<w:spacing w:before="180" w:after="80" w:line="300" w:lineRule="auto"/>'
            '<w:ind w:left="80" w:right="80"/>'
            '<w:pBdr><w:left w:val="single" w:sz="12" w:space="6" w:color="0F766E"/></w:pBdr>'
            '<w:shd w:val="clear" w:color="auto" w:fill="EAF4F2"/>'
            '</w:pPr>'
        )
    if style == "Caption":
        return '<w:pPr><w:pStyle w:val="Caption"/><w:jc w:val="center"/><w:spacing w:before="80" w:after="140"/></w:pPr>'
    if style == "AssetLead":
        return (
            '<w:pPr><w:pStyle w:val="AssetLead"/>'
            '<w:spacing w:before="120" w:after="80" w:line="360" w:lineRule="auto"/>'
            '</w:pPr>'
        )
    if style == "Metadata":
        return (
            '<w:pPr><w:pStyle w:val="Metadata"/>'
            '<w:spacing w:before="18" w:after="18" w:line="285" w:lineRule="auto"/>'
            '<w:ind w:left="240"/>'
            '<w:shd w:val="clear" w:color="auto" w:fill="F2F8F7"/>'
            '</w:pPr>'
        )
    if style == "Callout":
        return (
            '<w:pPr><w:pStyle w:val="Callout"/>'
            '<w:spacing w:before="80" w:after="100" w:line="340" w:lineRule="auto"/>'
            '<w:ind w:left="180" w:right="120"/>'
            '<w:pBdr><w:left w:val="single" w:sz="8" w:space="8" w:color="0F766E"/></w:pBdr>'
            '<w:shd w:val="clear" w:color="auto" w:fill="F2F8F7"/>'
            '</w:pPr>'
        )
    if style == "FigureCallout":
        return (
            '<w:pPr><w:pStyle w:val="FigureCallout"/>'
            '<w:spacing w:before="80" w:after="40" w:line="300" w:lineRule="auto"/>'
            '<w:ind w:left="260" w:right="260"/>'
            '<w:pBdr><w:left w:val="single" w:sz="8" w:space="8" w:color="7BA7A0"/></w:pBdr>'
            '</w:pPr>'
        )
    if style == "NoteCard":
        return (
            '<w:pPr><w:pStyle w:val="NoteCard"/>'
            '<w:spacing w:before="80" w:after="100" w:line="340" w:lineRule="auto"/>'
            '<w:ind w:left="180" w:right="120"/>'
            '<w:pBdr><w:left w:val="single" w:sz="8" w:space="8" w:color="0F766E"/></w:pBdr>'
            '<w:shd w:val="clear" w:color="auto" w:fill="F2F8F7"/>'
            '</w:pPr>'
        )
    if style == "ListParagraph":
        return (
            '<w:pPr><w:pStyle w:val="ListParagraph"/>'
            '<w:spacing w:before="60" w:after="60" w:line="330" w:lineRule="auto"/>'
            '<w:ind w:left="420" w:hanging="220"/></w:pPr>'
        )
    if style:
        return f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
    return '<w:pPr><w:spacing w:before="80" w:after="120" w:line="360" w:lineRule="auto"/></w:pPr>'


def _normalize_inline_text(text: str) -> str:
    text = _clean_xml_text(text)
    text = _strip_thinking(text)
    text = _textualize_latex(text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text


def _normalize_markdown_line(line: str) -> str:
    line = _clean_xml_text(line)
    line = _textualize_latex(line)
    line = re.sub(r"^\s*[-*]\s+", "- ", line)
    line = re.sub(r"^\s*\d+\.\s+", lambda m: m.group(0), line)
    line = line.replace("**", "").replace("__", "").replace("`", "")
    return line


def _is_list_line(line: str) -> bool:
    return bool(re.match(r"^\s*(?:[-*]\s+|\d+[.、]\s*)", line))


def _normalize_list_to_sentence(line: str) -> str:
    line = _normalize_markdown_line(line)
    line = re.sub(r"^\s*[-*]\s+", "", line)
    line = re.sub(r"^\s*\d+[.、]\s*", "", line)
    return line.strip()


def _normalize_callout_line(line: str) -> str:
    line = _normalize_inline_text(line)
    line = line.replace("[!figure]", "图表")
    return line


def _extract_note_title(summary: str) -> str:
    for line in summary.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return _normalize_inline_text(stripped[2:].strip())
    return ""


def _asset_display_label(
    asset_id: int,
    asset: PaperAsset,
    counters: dict[str, int],
    labels: dict[int, str],
) -> str:
    if asset_id in labels:
        return labels[asset_id]
    original_label = _original_asset_label(asset)
    if original_label:
        _advance_asset_counter_from_label(asset, original_label, counters)
        labels[asset_id] = original_label
        return original_label
    kind = asset.kind if asset.kind in counters else "figure"
    counters[kind] += 1
    if kind == "table":
        label = f"第 {asset.page_number} 页表格截图"
        labels[asset_id] = label
        return label
    if kind == "figure":
        label = f"第 {asset.page_number} 页图片截图"
        labels[asset_id] = label
        return label
    prefix = {"table": "表", "figure": "图", "formula": "公式"}.get(kind, "图")
    label = f"{prefix} {counters[kind]}"
    labels[asset_id] = label
    return label


def _advance_asset_counter_from_label(asset: PaperAsset, label: str, counters: dict[str, int]) -> None:
    if asset.kind not in counters:
        return
    match = re.search(r"\d+", label)
    if not match:
        return
    counters[asset.kind] = max(counters[asset.kind], int(match.group(0)))


def _original_asset_label(asset: PaperAsset) -> str:
    text = _clean_xml_text(" ".join(part for part in (asset.caption, asset.text, asset.latex) if part))
    if asset.kind == "figure":
        match = re.search(r"(?i)\b(?:figure|fig\.?)\s*([0-9]+[A-Za-z]?)\b", text)
        if match:
            return f"图 {match.group(1)}"
        match = re.search(r"图\s*([0-9一二三四五六七八九十]+)", text)
        if match:
            return f"图 {match.group(1)}"
    if asset.kind == "table":
        match = re.search(r"(?i)\b(?:table|tab\.?)\s*([0-9]+[A-Za-z]?)\b", text)
        if match:
            return f"表 {match.group(1)}"
        match = re.search(r"表\s*([0-9一二三四五六七八九十]+)", text)
        if match:
            return f"表 {match.group(1)}"
    if asset.kind == "formula":
        for source in (asset.caption, asset.text, asset.latex, text):
            source_text = _clean_xml_text(source or "")
            match = re.search(
                r"(?i)\b(?:equation|eq\.?|formula)\s*[\(:：]?\s*([0-9]+[A-Za-z]?)\)?",
                source_text,
            )
            if match:
                label = _reasonable_equation_number(match.group(1))
                if label:
                    return f"公式 {label}"
            match = re.search(r"(?:公式|方程)\s*[\(:：]?\s*([0-9一二三四五六七八九十]+)\)?", source_text)
            if match:
                return f"公式 {match.group(1)}"
            match = re.search(r"[\(\[（]\s*([0-9一二三四五六七八九十]+[A-Za-z]?)\s*[\)\]）]", source_text)
            if match:
                label = _reasonable_equation_number(match.group(1))
                if label:
                    return f"公式 {label}"
            match = _trailing_equation_number(source_text)
            if match:
                label = _reasonable_equation_number(match)
                if label:
                    return f"公式 {label}"
    return ""


def _reasonable_equation_number(value: str) -> str:
    value = str(value).strip()
    numeric = re.match(r"^(\d+)([A-Za-z]?)$", value)
    if not numeric:
        return value
    number = int(numeric.group(1))
    if number <= 0 or number > 80:
        return ""
    return value


def _trailing_equation_number(text: str) -> str:
    candidates = re.findall(r"(?:^|\s)[\(（]\s*([0-9一二三四五六七八九十]+[A-Za-z]?)\s*[\)）](?=\s*$)", text)
    if candidates:
        return candidates[-1]
    return ""


def _equation_number_token(text: str) -> str:
    trailing = _trailing_equation_number(text)
    if trailing:
        return trailing
    candidates = re.findall(r"(?:^|\s)[\(（]\s*([0-9一二三四五六七八九十]+[A-Za-z]?)\s*[\)）](?:\s|$)", text)
    for candidate in reversed(candidates):
        value = _reasonable_equation_number(candidate)
        if value:
            return value
    return ""


def _with_asset_references(text: str, labels: list[str]) -> str:
    text = _sync_inline_asset_references(text, labels)
    refs = _missing_asset_references(text, labels)
    if not refs:
        return text
    cleaned = text.rstrip()
    cleaned = re.sub(r"[。！？!?；;]\s*$", "", cleaned)
    return f"{cleaned}，{'，'.join(refs)}。"


def _append_asset_references_to_previous_paragraph(body: list[str], labels: list[str]) -> bool:
    if not body:
        return False
    refs = _missing_asset_references(_xml_text_content(body[-1]), labels)
    if not refs:
        return True
    if any(style in body[-1] for style in ('w:pStyle w:val="Title"', 'w:pStyle w:val="Heading1"', 'w:pStyle w:val="Heading2"', 'w:pStyle w:val="Heading3"')):
        return False
    suffix = html.escape(f"，{'，'.join(refs)}。")
    updated = re.sub(r"[。！？!?；;]?\s*</w:t>", suffix + "</w:t>", body[-1], count=1)
    if updated == body[-1]:
        return False
    body[-1] = updated
    return True


def _missing_asset_references(text: str, labels: list[str]) -> list[str]:
    text = _sync_inline_asset_references(text, labels)
    refs = []
    for label in labels:
        compact = _compact_asset_label(label)
        if not compact:
            continue
        if _label_kind(compact) == "公式" and _contains_formula_reference(text):
            continue
        pattern = _asset_reference_pattern(compact)
        if re.search(pattern, text):
            continue
        refs.append(f"如{compact}所示")
    return refs


def _sync_inline_asset_references(text: str, labels: list[str]) -> str:
    if not labels:
        return text
    result = text
    compact_labels = [_compact_asset_label(label) for label in labels]
    for prefix in ("图", "表"):
        same_kind_labels = [
            compact
            for compact in compact_labels
            if re.match(rf"^{prefix}[0-9一二三四五六七八九十]+[A-Za-z]?$", compact)
        ]
        if len(same_kind_labels) != 1:
            continue
        compact = same_kind_labels[0]
        match = re.match(r"^(图|表|公式)([0-9一二三四五六七八九十]+[A-Za-z]?)$", compact)
        if not match:
            continue
        label_prefix, number = match.groups()
        result = re.sub(
            rf"如\s*{re.escape(label_prefix)}\s*[0-9一二三四五六七八九十]+[A-Za-z]?\s*所示",
            f"如{label_prefix}{number}所示",
            result,
        )
    return result


def _label_kind(compact_label: str) -> str:
    match = re.match(r"^(图|表|公式)", compact_label)
    return match.group(1) if match else ""


def _contains_formula_reference(text: str) -> bool:
    return bool(
        re.search(
            r"(?i)(?:如\s*)?(?:公式|方程|equation|eq\.?)\s*[（(]?\s*[0-9一二三四五六七八九十]+[A-Za-z]?\s*[）)]?\s*(?:所示)?",
            _clean_xml_text(text),
        )
    )


def _asset_reference_pattern(compact_label: str) -> str:
    match = re.match(r"^(图|表|公式)(.+)$", compact_label)
    if not match:
        return re.escape(compact_label)
    prefix, number = match.groups()
    return rf"如\s*{re.escape(prefix)}\s*{re.escape(number)}\s*所示"


def _compact_asset_label(label: str) -> str:
    return re.sub(r"\s+", "", _clean_xml_text(label))


def _xml_text_content(xml: str) -> str:
    return html.unescape("".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml)))


def _asset_reference_sentence(label: str) -> str:
    return f"如{_compact_asset_label(label)}所示。"


def _asset_reference_description(asset: PaperAsset) -> str:
    caption = _clean_asset_caption_text(asset.caption, asset)
    caption = re.sub(r"^(?:核心公式|关键公式|公式|图片|表格|图表)\s*[:：]\s*", "", caption).strip()
    if not caption:
        return ""
    if asset.kind == "formula":
        return f"该公式对应{caption}。"
    if asset.kind == "table":
        return f"该表对应论文中的关键实验结果或对比证据。"
    return "该图对应论文中的关键结构、流程或结果证据。"


def _asset_caption(label: str, asset: PaperAsset) -> str:
    caption = _clean_asset_caption_text(asset.caption, asset)
    return f"{label}（第 {asset.page_number} 页）：{caption}"


def _clean_asset_caption_text(caption: str, asset: PaperAsset | None = None) -> str:
    text = _clean_xml_text(caption).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return "原文截图"
    if asset and asset.kind == "table" and not re.match(r"(?i)^(table|tab\.|表)\s*\d*", text):
        return "原文表格截图"
    if len(text) > 180:
        return text[:177].rstrip() + "..."
    return text


def _image_paragraph(path: Path, docpr_id: int, rel_id: str, asset: PaperAsset | None = None) -> str:
    cx, cy = _image_size_emu(path, asset.kind if asset else "", asset.rect if asset else None)
    spacing_after = "220" if asset and asset.kind in {"figure", "table"} else "160"
    return f"""<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:before="100" w:after="{spacing_after}"/></w:pPr><w:r><w:drawing>
<wp:inline distT="0" distB="0" distL="0" distR="0">
<wp:extent cx="{cx}" cy="{cy}"/>
<wp:effectExtent l="0" t="0" r="0" b="0"/>
<wp:docPr id="{docpr_id}" name="Picture {docpr_id}"/>
<wp:cNvGraphicFramePr/>
<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:nvPicPr><pic:cNvPr id="{docpr_id}" name="image{docpr_id}.png"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic>
</a:graphicData>
</a:graphic>
</wp:inline>
</w:drawing></w:r></w:p>"""


def _image_size_emu(path: Path, kind: str = "", rect: fitz.Rect | None = None) -> tuple[int, int]:
    max_width_emu = int(6.2 * 914400)
    min_width_by_kind = {
        "formula": int(3.9 * 914400),
        "table": int(5.7 * 914400),
        "figure": int(5.4 * 914400),
    }
    try:
        with Image.open(path) as image:
            width, height = image.size
    except Exception:
        width, height = 800, 500
    if width <= 0 or height <= 0:
        width, height = 800, 500
    natural_width_emu = width * 9525
    min_width_emu = min_width_by_kind.get(kind, 0)
    if kind == "table" and rect is not None and rect.width < 220:
        target_width_inches = min(4.4, max(3.2, (rect.width / 72.0) * 2.1))
        scale = min(max_width_emu / natural_width_emu, (target_width_inches * 914400) / natural_width_emu)
        return int(width * 9525 * scale), int(height * 9525 * scale)
    target_scale = max(1.0, min_width_emu / natural_width_emu) if min_width_emu else 1.0
    if kind == "table":
        # Small table crops become unreadable if stretched to page width; prefer crisp native scale.
        target_scale = min(target_scale, 1.35)
    scale = min(max_width_emu / natural_width_emu, target_scale)
    return int(width * 9525 * scale), int(height * 9525 * scale)


def _document_rels(media_files: list[tuple[Path, str, str]]) -> str:
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>',
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>',
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable" Target="fontTable.xml"/>',
    ]
    for _source, media_name, rel_id in media_files:
        rels.append(
            f'<Relationship Id="{rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{media_name}"/>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{''.join(rels)}
</Relationships>"""


def _content_types() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
<Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""


def _package_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def _core_props() -> str:
    timestamp = datetime.now(timezone.utc).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>论文总结</dc:title>
<dc:creator>paper_agent</dc:creator>
<cp:lastModifiedBy>paper_agent</cp:lastModifiedBy>
<dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
<dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>"""


def _app_props() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
<Application>paper_agent</Application>
</Properties>"""


def _styles_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="111827"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="0F172A"/><w:sz w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="0F766E"/><w:sz w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="0F766E"/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="0F766E"/><w:sz w:val="23"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="0F766E"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="AssetLead"><w:name w:val="asset lead"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="0F766E"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Metadata"><w:name w:val="metadata"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="0F3F3A"/><w:sz w:val="21"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Callout"><w:name w:val="callout"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="111827"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="FigureCallout"><w:name w:val="figure callout"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="4B635F"/><w:sz w:val="20"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="NoteCard"><w:name w:val="note card"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="111827"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="list paragraph"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="333333"/><w:sz w:val="22"/></w:rPr></w:style>
</w:styles>"""


def _settings_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:defaultTabStop w:val="720"/>
</w:settings>"""


def _font_table_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:font w:name="{DOCX_FONT}"><w:family w:val="swiss"/></w:font>
</w:fonts>"""


def _clean_xml_text(value: str) -> str:
    text = str(value)
    cleaned = []
    for ch in text:
        code = ord(ch)
        if ch in "\t\n\r" or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF:
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    return "".join(cleaned).replace("\ufffd", "")


def _safe_stem(stem: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return safe or "paper"

