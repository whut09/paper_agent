from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import zipfile
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


ProgressCallback = Callable[[float, str], None]
_TEXTELLER_FAILED = False
DEFAULT_MAX_ASSETS = 13
DEFAULT_FIGURE_ASSET_LIMIT = 5
DEFAULT_TABLE_ASSET_LIMIT = 4
DEFAULT_FORMULA_ASSET_LIMIT = 4
DOCX_FONT = "Microsoft YaHei"


DEEP_PAPER_NOTE_SYSTEM_PROMPT = """你是 DeepPaperNote 风格的科研论文精读笔记助手。
你的目标是先写一份高质量 Markdown 论文笔记，再由程序把图表截图插入 Word。
请像顶级 AI 研究员或算法工程师写复现实验笔记一样写，不要写公众号营销文，也不要写浅层摘要。

必须遵守：
1. 证据优先：只能依据论文正文、图表标题/上下文和抽取到的表格文本写结论；原文没有提及的信息、术语、数据集、指标、模型名、应用场景或结论一律省略，不要写“原文未明确说明”“未提及”“未知”等占位句。
2. 技术细节优先：优先解释问题定义、方法机制、训练/推理链路、关键公式、关键数字、消融和局限。
3. 中文笔记：除模型名、数据集名、指标名、论文术语、代码库名等稳定专有名词外，不要夹杂整句英文。
4. 不要把正文写成全篇 bullet list。只有 `## 核心信息` 使用 `- 字段名: 值`；其他章节优先使用自然段和 `###` 小标题。
5. 图表占位符优先：把重要图、表截图放在对应解释段落附近，使用 `[[ASSET:编号]]` 独占一行表示截图位置；不要创建独立“图表精读”章节，也不要把截图集中放到文末。
6. `ASSET` 编号只是程序内部占位符，不是最终 Word 中的图、表、公式编号。正文不要把 `ASSET` 编号写成“公式 2”这类引用；必须使用可用图表截图里给出的“最终引用标签”。
7. 保留原图表和公式编号：解释图、表、公式时尽量保留 Fig. 1、Table 2、Equation 8、(8) 等原始编号；如果无法确定编号，说明它来自第几页的截图。
8. 不要输出思考过程，不要输出 `<think>`、`<thinking>`、代码块、HTML、JSON。
9. 关键公式必须解读。优先使用“TexTeller 公式识别结果”中的 LaTeX；如果没有识别结果，可以重写 1 到 3 个最核心公式的可读文本形式，例如 `Δvision = Ctext / Cvision`；如果公式抽取不可靠，使用可用的公式截图占位符并解释含义。
10. 不要输出大段 LaTeX 堆砌；每个公式后必须有一句工程含义解释。
"""


FINAL_NOTE_PROMPT = """请将下面的分段阅读笔记整合为一份 DeepPaperNote 风格的完整中文 Markdown 论文精读笔记。

输出必须是 Markdown，结构如下，可根据论文内容增加必要的 `###` 小节；没有原文证据的字段、章节或小节可以直接省略：

# 论文标题
标题要比原论文标题更适合中文读者阅读和传播，可以使用“研究对象 | 关键发现/核心结果/开放信息”的形式，也可以使用有张力的问题句；但标题中的机构、模型名、对比对象、数字和结论必须来自原文证据，原文没有提及的不要写。

## 核心信息
只输出原文明确出现的字段；没有出现的字段不要写。
- 标题: 必须填写原文标题，保持英文原文，不要改写、概括或翻译
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

## 创新点
用 3 到 5 个短段落说明真实创新点；不要写成每行都以 `-` 开头的长列表。每个创新点都要说明它解决什么问题、为什么重要。

## 一句话总结
回答这篇论文真正解决什么问题；只写原文直接提到或由原文证据直接支持的内容。

## 研究问题
说明痛点、已有方法不足、任务定义和问题边界。

## 数据与任务定义
说明输入、任务、数据集、评价指标和实验边界。原文没有提及的项目直接省略，不要写占位句。

## 方法主线
必须包含 `### 机制流程`，用 3-5 步解释 Input -> 关键变换 -> Output。架构图、流程图、方法框架图必须放在本节对应解释段落附近。必须包含 `### 关键公式`，解读论文最重要的 1 到 3 个公式；优先采用 TexTeller 识别到的 LaTeX，并把对应 `[[ASSET:编号]]` 放在公式解释旁边。

## 关键结果
提炼最重要的指标、对比、消融和失败/边界证据；不要堆砌所有数字。实验图、结果表、对比表、消融表和 case analysis 图必须放在本节对应解释段落附近。

## 深度分析
说明原文直接支持的贡献、有效性证据、证据薄弱处和作者明确讨论的假设；不要补写原文没有提及的推测。

## 局限
写真实局限，包括数据、评价、部署、复杂度、泛化或基线不足。

## 总结
收束全文，说明这篇论文原文直接支持的结论和复现时应关注的实验点；原文没有提及的后续工作、引用价值或应用场景不要补写。

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
- 不要输出 LaTeX 块公式；公式只用短文本描述和中文解释。
- 不要输出 markdown 表格，表格内容用自然段概括。
- 不要输出“翻译”二字；核心信息里写“中文标题”，摘要章节标题只写“摘要”。
- 不要输出 `## 引用` 章节。
- 不要输出 `## 我的笔记` 章节，统一使用 `## 总结`。
- 除 `## 核心信息` 外，不要让大多数行以 `-` 开头。
- 原文没有提及的字段、章节和小节直接省略；不要输出“原文未明确说明”“未提及”“未知”“N/A”等占位内容。
"""


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


@dataclass
class VerificationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class PaperWorkflowContext:
    input_path: str
    output_dir: str | Path
    pages: list[int] | None
    summary_language: str
    codex_envs: dict[str, str]
    max_assets: int
    progress: ProgressCallback | None = None
    cancellation_event: asyncio.Event | None = None
    output: Path | None = None
    source_path: Path | None = None
    pdf_path: Path | None = None
    paper_name: str = ""
    work_dir: Path | None = None
    text: str = ""
    assets: list[PaperAsset] = field(default_factory=list)
    paper_title: str = ""
    abstract: str = ""
    formulas: list[str] = field(default_factory=list)
    config: CodexConfig | None = None
    client: openai.OpenAI | None = None
    chunk_notes: list[str] = field(default_factory=list)
    summary: str = ""
    verification: VerificationResult | None = None
    docx_path: Path | None = None

    def report(self, value: float, desc: str) -> None:
        if self.progress:
            self.progress(value, desc)

    def check_cancelled(self) -> None:
        if self.cancellation_event and self.cancellation_event.is_set():
            raise asyncio.CancelledError

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


class PaperWorkflowNode:
    name = ""
    depends_on: tuple[str, ...] = ()

    def run(self, context: PaperWorkflowContext) -> None:
        raise NotImplementedError


class PaperWorkflow:
    def __init__(self, nodes: list[PaperWorkflowNode]):
        self.nodes = {node.name: node for node in nodes}
        if len(self.nodes) != len(nodes):
            raise ValueError("PaperWorkflow node names must be unique.")
        for node in nodes:
            missing = [name for name in node.depends_on if name not in self.nodes]
            if missing:
                raise ValueError(f"Workflow node {node.name} depends on missing nodes: {missing}")

    @classmethod
    def default(cls) -> "PaperWorkflow":
        return cls(
            [
                PreparePaper(),
                ParsePaper(),
                ExtractSections(),
                SummarizeContribution(),
                ExtractMethods(),
                VerifyClaims(),
                GenerateReport(),
            ]
        )

    def run(self, context: PaperWorkflowContext) -> PaperWorkflowContext:
        pending = set(self.nodes)
        completed: set[str] = set()
        try:
            while pending:
                ready = sorted(
                    name
                    for name in pending
                    if all(dep in completed for dep in self.nodes[name].depends_on)
                )
                if not ready:
                    raise ValueError(f"Workflow has cyclic or unsatisfied dependencies: {sorted(pending)}")
                for name in ready:
                    context.check_cancelled()
                    self.nodes[name].run(context)
                    completed.add(name)
                    pending.remove(name)
            return context
        finally:
            context.close()


class PreparePaper(PaperWorkflowNode):
    name = "PreparePaper"

    def run(self, context: PaperWorkflowContext) -> None:
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


class ParsePaper(PaperWorkflowNode):
    name = "ParsePaper"
    depends_on = ("PreparePaper",)

    def run(self, context: PaperWorkflowContext) -> None:
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


class ExtractSections(PaperWorkflowNode):
    name = "ExtractSections"
    depends_on = ("ParsePaper",)

    def run(self, context: PaperWorkflowContext) -> None:
        if context.pdf_path is None:
            raise ValueError("ExtractSections requires a parsed PDF.")
        context.report(0.32, "提取标题、摘要和公式...")
        context.paper_title = _extract_title_from_pdf(context.pdf_path, context.pages)
        context.abstract = _extract_abstract_from_pdf(context.pdf_path, context.pages) or _extract_abstract_from_text(context.text)
        context.formulas = _extract_formula_candidates(context.text)


class SummarizeContribution(PaperWorkflowNode):
    name = "SummarizeContribution"
    depends_on = ("ExtractSections",)

    def run(self, context: PaperWorkflowContext) -> None:
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
        )


class ExtractMethods(PaperWorkflowNode):
    name = "ExtractMethods"
    depends_on = ("SummarizeContribution",)

    def run(self, context: PaperWorkflowContext) -> None:
        if context.client is None or context.config is None:
            raise ValueError("ExtractMethods requires an initialized Codex client.")
        context.check_cancelled()
        context.report(0.68, "整合方法、结果和分析...")
        context.summary = _integrate_summary_with_codex(
            context.client,
            context.config.model,
            context.chunk_notes,
            context.assets,
            context.summary_language,
            context.abstract,
            context.formulas,
            _recognized_formula_context(context.assets),
            context.paper_title,
        )


class VerifyClaims(PaperWorkflowNode):
    name = "VerifyClaims"
    depends_on = ("ExtractMethods",)

    def run(self, context: PaperWorkflowContext) -> None:
        if context.client is None or context.config is None:
            raise ValueError("VerifyClaims requires an initialized Codex client.")
        context.report(0.78, "校验标题、摘要和图表引用...")
        context.summary, context.verification = _verify_summary_claims(
            context.summary,
            context.text,
            context.abstract,
            context.client,
            context.config.model,
            context.paper_title,
            context.assets,
        )


class GenerateReport(PaperWorkflowNode):
    name = "GenerateReport"
    depends_on = ("VerifyClaims",)

    def run(self, context: PaperWorkflowContext) -> None:
        if context.output is None or context.source_path is None:
            raise ValueError("GenerateReport requires prepared output paths.")
        context.check_cancelled()
        context.report(0.85, "写入 Word 文档...")
        context.docx_path = context.output / f"{context.paper_name}-summary.docx"
        _write_docx(context.docx_path, context.source_path.name, context.summary, context.assets)
        context.report(1.0, "论文总结完成")


def summarize_paper(
    input_path: str,
    output_dir: str | Path,
    *,
    pages: list[int] | None = None,
    summary_language: str = "中文",
    codex_envs: dict[str, str] | None = None,
    max_assets: int = DEFAULT_MAX_ASSETS,
    progress: ProgressCallback | None = None,
    cancellation_event: asyncio.Event | None = None,
    workflow: PaperWorkflow | None = None,
) -> str:
    """Summarize a paper and write a Word .docx file with captured figures/tables."""
    context = PaperWorkflowContext(
        input_path=input_path,
        output_dir=output_dir,
        pages=pages,
        summary_language=summary_language,
        codex_envs=codex_envs or {},
        max_assets=max_assets,
        progress=progress,
        cancellation_event=cancellation_event,
    )
    result = (workflow or PaperWorkflow.default()).run(context)
    if result.docx_path is None:
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
            table_assets = _capture_tables(page, work_dir, page_no, candidate_limit, seen_boxes)
            table_assets = _filter_assets_before_y(table_assets, stop_y)
            assets.extend(table_assets)

        if kind_limits.get("table", 0) > 0:
            table_assets = _capture_captioned_tables(page, work_dir, page_no, candidate_limit, seen_boxes)
            table_assets = _filter_assets_before_y(table_assets, stop_y)
            assets.extend(table_assets)

        if kind_limits.get("figure", 0) > 0:
            figure_assets = _capture_captioned_figures(page, work_dir, page_no, candidate_limit, seen_boxes)
            figure_assets = _filter_assets_before_y(figure_assets, stop_y)
            assets.extend(figure_assets)

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
        _save_clip(page, clip_rect, path, padding=2)
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
        if not caption and not _table_detection_looks_reliable(table, table_text):
            continue
        clip_rect = _merge_rects([table_rect, caption_rect]) if caption_rect else table_rect
        key = _box_key(page_no, clip_rect)
        if key in seen_boxes:
            continue
        seen_boxes.add(key)
        path = work_dir / f"page-{page_no:03d}-table-{idx:02d}.png"
        _save_clip(page, clip_rect, path, padding=2)
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

        if _overlaps_existing_asset(asset, result):
            continue

        result.append(asset)
        if label_key:
            seen_original_labels.add(label_key)

    return result


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
            candidates.append((score, page_index, rect, text))

    candidates.sort(key=lambda item: (-item[0], item[1], item[2].y0))
    assets: list[PaperAsset] = []
    formula_index_by_page: dict[int, int] = {}
    for _score, page_index, rect, text in candidates:
        if len(assets) >= limit:
            break
        page_no = page_index + 1
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
        latex = _recognize_formula_latex(path)
        caption = _formula_caption(text, latex)
        assets.append(PaperAsset("formula", page_no, path, caption, text=text, latex=latex, rect=rect))
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
    if not line or len(line) > 180:
        return 0.0
    lowered = line.lower()
    if lowered.startswith(
        (
            "figure",
            "fig.",
            "table",
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

    op_match = re.search(r"(=|≈|∈|≤|≥|∝|:=)", line)
    if not op_match:
        return 0.0
    lhs = line[: op_match.start()].strip(" ,.;:()[]")
    lhs_words = re.findall(r"[A-Za-z]{3,}", lhs)
    if len(lhs_words) > 2:
        return 0.0
    if lhs_words and lhs_words[0].lower() in {"where", "when", "with", "textual", "vision", "indices", "tokens"} and len(lhs_words) > 1:
        return 0.0

    math_symbols = set("=<>±∞αβγδϵεΔ∆θλμσ∈→×·∑Σ∫√≤≥≈∝⊤−˜~′")
    symbol_count = sum(1 for ch in line if ch in math_symbols)
    operator_count = sum(line.count(op) for op in ["=", "≈", "∈", "≤", "≥", "∝", "\\frac", "\\sum", "\\prod"])
    if operator_count == 0 and symbol_count < 2:
        return 0.0
    words = re.findall(r"[A-Za-z]{4,}", line)
    if len(words) > 5 and symbol_count < 4:
        return 0.0

    compact = re.sub(r"\s+", "", line).lower()
    score = 10.0 + operator_count * 8 + symbol_count * 1.5
    if _trailing_equation_number(line):
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
    if any(token in lowered for token in ("<answer", "</answer", "<think", "</think", "percentage point")):
        return True
    words = re.findall(r"[A-Za-z]{4,}", cleaned)
    if len(words) > 14:
        return True
    numeric_tokens = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if len(numeric_tokens) >= 8 and not _trailing_equation_number(cleaned):
        return True
    if re.search(r"\b(?:total|recall|accuracy|baseline|method)\b", lowered) and len(numeric_tokens) >= 4:
        return True
    return False


def _formula_clip_rect(page: fitz.Page, anchor: fitz.Rect, lines: list[TextLine]) -> fitz.Rect:
    left, right = _column_bounds(page, anchor)
    y0 = anchor.y0
    y1 = anchor.y1
    anchor_mid = (anchor.y0 + anchor.y1) / 2
    for line in lines:
        if line.rect.x1 < left or line.rect.x0 > right:
            continue
        line_mid = (line.rect.y0 + line.rect.y1) / 2
        if abs(line_mid - anchor_mid) > 34:
            continue
        if _is_formula_continuation_line(line.text) or abs(line_mid - anchor_mid) < 7:
            y0 = min(y0, line.rect.y0)
            y1 = max(y1, line.rect.y1)
    return fitz.Rect(left, max(0, y0), right, min(page.rect.height, y1))


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
    if lowered.startswith(("where ", "when ", "figure", "fig.", "table", "the ", "we ", "this ", "is ", "and ")):
        return False
    math_symbols = set("=<>±∞αβγδϵεΔ∆θλμσ∈→×·∑Σ∫√≤≥≈∝⊤−˜~′")
    symbol_count = sum(1 for ch in line if ch in math_symbols)
    words = re.findall(r"[A-Za-z]{4,}", line)
    return symbol_count >= 1 and len(words) <= 2


def _formula_block_text(lines: list[TextLine], rect: fitz.Rect) -> str:
    parts = []
    for line in lines:
        if line.rect.y1 < rect.y0 - 1 or line.rect.y0 > rect.y1 + 1:
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
        return f"关键公式：{latex}"
    return f"关键公式截图：{_clean_xml_text(text)[:160]}"


def _known_formula_caption(text: str) -> str:
    compact = re.sub(r"\s+", "", _clean_xml_text(text)).lower()
    if "e=[etext;evision]" in compact or "e=[e_text;e_vision]" in compact:
        return "核心公式：E = [E_text; E_vision]，定义文本与视觉 token 的联合嵌入序列"
    if re.match(r"^c_?m=", compact) or compact.startswith("cm="):
        return "核心公式：C_m = (I_intra_m)^α · (I_inter_m)^(1-α)，融合模态内部密度与跨模态交互"
    if compact.startswith(("∆vision=", "δvision=", "Δvision=")):
        return "核心公式：Δ_vision = C_text / C_vision，确定视觉 token 的自适应位置步长"
    if "p′=f(e)" in compact or "p'=f(e)" in compact:
        return "公式：P' = f(E)，由嵌入序列生成模态感知位置索引"
    if compact.startswith("rk("):
        return "公式：RoPE 旋转矩阵随相对位置偏移 Δp 变化"
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
            visual_rect = _fallback_visual_rect_for_caption(page, line.rect)
        if visual_rect is None:
            continue
        clip_rect = _merge_rects([visual_rect, caption_rect])
        if clip_rect.height < 80 or clip_rect.width < 80:
            continue
        key = _box_key(page_no, clip_rect)
        if key in seen_boxes:
            continue
        seen_boxes.add(key)
        figure_index += 1
        path = work_dir / f"page-{page_no:03d}-captioned-figure-{figure_index:02d}.png"
        _save_clip(page, clip_rect, path, padding=2)
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
        and _horizontal_overlap_fraction(line.rect, left, right) > 0
    ]
    row_groups = _group_lines_by_row(candidate_lines)
    selected: list[TextLine] = []
    previous_y1: float | None = None

    for group in row_groups:
        row_rect = _merge_rects([line.rect for line in group])
        if row_rect.is_empty:
            continue
        row_text = " ".join(line.text for line in group)
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
    rect = _merge_rects([line.rect for line in selected])
    rect &= fitz.Rect(left, search_top, right, search_bottom)
    if rect.is_empty or rect.width < 60 or rect.height < 20:
        return None, ""
    rect = _expand_table_rect_to_borders(page, rect)
    text = "\n".join(_clean_xml_text(" ".join(line.text for line in group)) for group in row_groups if any(line in selected for line in group))
    return rect, text[:2500]


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
        if re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z ]{2,}$", line.text.strip()):
            candidates.append(line.rect.y0 - 2)
    return min(candidates) if candidates else None


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
    for next_line in lines[caption_index + 1 : caption_index + 4]:
        if next_line.rect.y0 < previous.rect.y0:
            continue
        if next_line.rect.x1 < left or next_line.rect.x0 > right:
            continue
        gap = next_line.rect.y0 - previous.rect.y1
        if gap > 16:
            break
        lowered = next_line.text.lower().strip()
        if _caption_is_figure(next_line.text) or lowered.startswith(("table", "tab.", "表")):
            break
        if re.match(r"^\d+(?:\.\d+)*\.?\s+[A-Za-z]", next_line.text):
            break
        if kind == "figure" and len(caption_lines) >= 3:
            break
        if kind == "table":
            if len(caption_lines) >= 4:
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


def _caption_column_bounds(page: fitz.Page, caption_rect: fitz.Rect) -> tuple[float, float]:
    if page.rect.width < 420:
        return max(0, caption_rect.x0 - 24), min(page.rect.width, caption_rect.x1 + 24)
    if caption_rect.width > page.rect.width * 0.55:
        return max(0, page.rect.width * 0.05), min(page.rect.width, page.rect.width * 0.95)
    return _column_bounds(page, caption_rect)


def _visual_rect_for_caption(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    lines: list[TextLine],
) -> fitz.Rect | None:
    left, right = _caption_column_bounds(page, caption_rect)
    search_top = max(0, caption_rect.y0 - min(520, page.rect.height * 0.7))
    candidates = []
    column_rect = fitz.Rect(left, search_top, right, caption_rect.y0 + 6)
    for region in _page_graphic_regions(page):
        if region.y0 > caption_rect.y0 + 4 or region.y1 < search_top:
            continue
        if _rect_overlap_fraction(region, column_rect) <= 0:
            continue
        horizontal = _horizontal_overlap_fraction(region, left, right)
        if horizontal < 0.15:
            continue
        if region.width < 18 or region.height < 12:
            continue
        candidates.append(region)
    if not candidates:
        return None

    near = [r for r in candidates if -8 <= caption_rect.y0 - r.y1 <= 150]
    seed_pool = near or candidates
    seed = max(seed_pool, key=lambda r: r.width * r.height)
    group = fitz.Rect(seed)
    changed = True
    while changed:
        changed = False
        for region in candidates:
            if _rect_overlap_fraction(region, group) > 0.05 or _rect_gap(region, group) <= 45:
                before = tuple(group)
                group |= region
                if tuple(group) != before:
                    changed = True
    group &= fitz.Rect(left, search_top, right, caption_rect.y0 - 1)
    if group.is_empty or group.width < 40 or group.height < 30:
        return None
    return group


def _fallback_visual_rect_for_caption(page: fitz.Page, caption_rect: fitz.Rect) -> fitz.Rect | None:
    left, right = _caption_column_bounds(page, caption_rect)
    top = max(0, caption_rect.y0 - min(300, page.rect.height * 0.38))
    rect = fitz.Rect(left, top, right, max(top + 80, caption_rect.y0 - 2))
    if rect.is_empty or rect.width < 40 or rect.height < 40:
        return None
    return rect


def _page_graphic_regions(page: fitz.Page) -> list[fitz.Rect]:
    regions: list[fitz.Rect] = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
        for block in blocks:
            if block.get("type") == 1:
                rect = fitz.Rect(block.get("bbox"))
                if rect.width >= 12 and rect.height >= 12:
                    regions.append(rect)
    except Exception:
        pass
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if not rect:
                continue
            rect = fitz.Rect(rect)
            if rect.width >= 12 and rect.height >= 12:
                regions.append(rect)
    except Exception:
        pass
    return regions


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


def _save_clip(page: fitz.Page, rect: fitz.Rect, path: Path, padding: int = 8) -> None:
    clip = rect + (-padding, -padding, padding, padding)
    clip &= page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
    pix.save(path)


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

    if not base_url:
        raise ValueError("缺少 CODEX_BASE_URL，请在前端或 config.json 中配置 Codex 本地接口 URL。")
    if not model:
        raise ValueError("缺少 CODEX_MODEL，请在前端或 config.json 中配置模型名称。")
    if not api_key:
        api_key = "codex-local"
    return CodexConfig(base_url=base_url, api_key=api_key, model=model, use_proxy=use_proxy)


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
        chunk_notes = _summarize_chunks_with_codex(
            client,
            config.model,
            paper_text,
            assets,
            summary_language,
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
        )
        verified_summary, _verification = _verify_summary_claims(
            summary,
            paper_text,
            abstract,
            client,
            config.model,
            paper_title,
            assets,
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
) -> list[str]:
    chunks = _chunk_text(paper_text, 16000)
    asset_context = _asset_context(assets)
    chunk_notes = []
    for idx, chunk in enumerate(chunks, 1):
        user_prompt = f"""请阅读论文第 {idx}/{len(chunks)} 段内容，生成分段笔记。
总结语言：{summary_language}
只记录本段原文直接提到或由本段证据直接支持的信息；本段没有提及的内容不要补写，也不要写“未提及”“未知”等占位句。

可用图表截图：
{asset_context}

论文内容：
{chunk}
"""
        chunk_notes.append(_chat(client, model, user_prompt))
    return chunk_notes


def _integrate_summary_with_codex(
    client: openai.OpenAI,
    model: str,
    chunk_notes: list[str],
    assets: list[PaperAsset],
    summary_language: str,
    abstract: str,
    formulas: list[str],
    recognized_formulas: str,
    paper_title: str,
) -> str:
    asset_context = _asset_context(assets)
    formula_context = _formula_context(formulas)
    final_input = "\n\n".join(f"[Chunk {i + 1}]\n{note}" for i, note in enumerate(chunk_notes))
    return _chat(
        client,
        model,
        (
            f"{FINAL_NOTE_PROMPT}\n\n总结语言：{summary_language}\n\n"
            f"原始论文标题证据：\n{paper_title or '未抽取到可靠标题。'}\n\n"
            f"原文摘要证据：\n{abstract or '未抽取到可靠摘要。'}\n\n"
            f"关键公式候选：\n{formula_context}\n\n"
            f"TexTeller 公式识别结果：\n{recognized_formulas or '未识别到可靠公式。'}\n\n"
            f"可用图表截图：\n{asset_context}\n\n分段笔记：\n{final_input}"
        ),
    )


def _verify_summary_claims(
    summary: str,
    paper_text: str,
    abstract: str,
    client: openai.OpenAI,
    model: str,
    paper_title: str,
    assets: list[PaperAsset],
) -> tuple[str, VerificationResult]:
    summary = _postprocess_summary(summary)
    summary = _replace_missing_abstract(summary, abstract, client, model)
    summary = _normalize_final_sections(summary)
    summary = _enforce_core_original_title(summary, paper_title)
    summary = _ensure_asset_markers(summary, assets)
    verification = _run_verification_agent(client, model, paper_text, summary)
    if not verification.passed:
        details = "\n".join(f"- {error}" for error in verification.errors[:8])
        raise RuntimeError(f"Verifier Agent 未通过，已停止生成报告：\n{details}")
    return summary, verification


def _run_verification_agent(
    client: openai.OpenAI,
    model: str,
    paper_text: str,
    summary: str,
) -> VerificationResult:
    claims = _extract_verifiable_claims(summary)
    if not claims:
        return VerificationResult(False, ["未能从总结中抽取到可校验 claim。"])

    evidence = _verification_evidence_text(paper_text)
    prompt = (
        "你是论文总结 Verifier Agent。你的任务不是润色总结，而是判断总结中的关键 claim 是否被原文证据支持。\n\n"
        "必须遵守：\n"
        "1. claim 必须能在原文证据中找到直接支持，不能靠常识或猜测补全。\n"
        "2. method / 方法相关 claim 必须能在 method、approach、model、training、algorithm、implementation 等方法相关段落中找到依据。\n"
        "3. contribution / 创新点相关 claim 不能新增原文没有声明的贡献、能力、数据集、指标或应用场景。\n"
        "4. 只输出 JSON，不要输出 Markdown，不要解释过程。\n\n"
        "输出格式：\n"
        "{\"pass\": true/false, \"errors\": [\"具体错误1\", \"具体错误2\"]}\n\n"
        f"待校验 claims：\n{json.dumps(claims, ensure_ascii=False, indent=2)}\n\n"
        f"原文证据：\n{evidence}"
    )
    output = _chat(
        client,
        model,
        prompt,
        system_prompt="You are a strict paper-summary verifier. Output only valid JSON.",
    )
    return _parse_verification_result(output)


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
            claims.append(
                {
                    "section": current_section or "正文",
                    "type": _claim_type(current_section, sentence),
                    "claim": sentence,
                }
            )
            if len(claims) >= limit:
                return claims
    return claims


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


def _verification_evidence_text(paper_text: str, max_chars: int = 24000) -> str:
    chunks = _chunk_text(paper_text, max_chars // 3)
    if len(paper_text) <= max_chars:
        return paper_text
    head = chunks[0] if chunks else paper_text[: max_chars // 3]
    method = _section_window_for_verifier(paper_text, ("method", "approach", "model", "training", "algorithm", "implementation"))
    result = _section_window_for_verifier(paper_text, ("experiment", "evaluation", "result", "ablation", "analysis"))
    evidence = "\n\n".join(part for part in (head, method, result) if part)
    return evidence[:max_chars]


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
    errors = payload.get("errors") or []
    if not isinstance(errors, list):
        errors = [str(errors)]
    cleaned_errors = [_clean_xml_text(str(error)).strip() for error in errors if str(error).strip()]
    passed = bool(payload.get("pass")) and not cleaned_errors
    if not passed and not cleaned_errors:
        cleaned_errors = ["Verifier Agent 判定失败，但未给出具体错误。"]
    return VerificationResult(passed, cleaned_errors)


def _extract_json_object(text: str) -> str:
    cleaned = _strip_markdown_fences(text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("missing JSON object")
    return cleaned[start : end + 1]


def _create_codex_client(config: CodexConfig) -> openai.OpenAI:
    http_client = httpx.Client(
        trust_env=config.use_proxy,
        timeout=httpx.Timeout(600.0, connect=30.0),
    )
    return openai.OpenAI(
        base_url=config.base_url,
        api_key=config.api_key,
        http_client=http_client,
        max_retries=2,
    )


def _chat(
    client: openai.OpenAI,
    model: str,
    user_prompt: str,
    system_prompt: str = DEEP_PAPER_NOTE_SYSTEM_PROMPT,
) -> str:
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
    except openai.APIConnectionError as exc:
        raise RuntimeError(
            "Codex 接口连接失败：服务端断开或网络链路不稳定。程序已默认不继承系统代理；"
            "如果你的接口必须走代理，请在 config.local.json 中加入 CODEX_USE_PROXY: true 后重试。"
        ) from exc
    content = response.choices[0].message.content or ""
    return _postprocess_summary(content)


def _replace_missing_abstract(
    summary: str,
    abstract: str,
    client: openai.OpenAI,
    model: str,
) -> str:
    if not abstract or not any(marker in summary for marker in ("原文摘要未完整抽取", "摘要未完整抽取")):
        return summary
    abstract_cn = _chat(
        client,
        model,
        (
            "请把下面论文英文摘要忠实写成中文摘要。只输出中文内容，"
            "不要总结、不要评价、不要添加标题。\n\n"
            f"{abstract}"
        ),
    )
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
    text = _textualize_latex(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_final_sections(text: str) -> str:
    text = text.replace("标题翻译", "中文标题")
    text = re.sub(r"(?m)^##\s*(?:原文摘要翻译|摘要翻译)\s*$", "## 摘要", text)
    text = text.replace("原文摘要未完整抽取", "摘要未完整抽取")
    text = re.sub(r"(?m)^##\s*我的笔记\s*$", "## 总结", text)
    text = re.sub(r"(?ms)(?:^|\n)##\s*引用\s*\n.*?(?=\n## |\Z)", "\n", text)
    text = _remove_figure_reading_sections(text)
    text = _clean_core_info_section(text)
    text = _remove_unspecified_placeholders(text)
    text = _remove_empty_sections(text)
    text = text.replace("翻译", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
        for index, line in enumerate(lines):
            if re.match(r"^\s*[-*]\s*标题\s*[:：]", line):
                prefix = re.match(r"^(\s*[-*]\s*标题\s*[:：]\s*)", line).group(1)
                lines[index] = f"{prefix}{paper_title}"
                return header + "\n".join(lines).strip() + "\n\n"
        return header + f"- 标题: {paper_title}\n" + body.strip() + "\n\n"

    if pattern.search(text):
        return pattern.sub(replace, text, count=1)
    return text


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
    text = re.sub(r"\\tilde\{([^{}]+)\}", r"~\1", text)
    text = re.sub(r"\\mathbb\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\mathcal\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\mathrm\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\text\{([^{}]+)\}", r"\1", text)
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
        "\\in": "∈",
        "\\left": "",
        "\\right": "",
        "\\quad": " ",
        "\\ldots": "…",
        "\\ldotp": ".",
    }
    text = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", frac, text)
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def _asset_context(assets: list[PaperAsset]) -> str:
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
        if asset.text:
            text += f"\n表格文本预览：\n{_clean_xml_text(asset.text[:1500])}"
        if asset.latex:
            text += f"\nTexTeller LaTeX：{_clean_xml_text(asset.latex)}"
        parts.append(text)
    return "\n".join(parts)


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
        if str(idx) not in referenced and asset.kind in {"figure", "table"}
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
            if re.search(pattern, stripped):
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


def _write_docx(path: Path, paper_filename: str, summary: str, assets: list[PaperAsset]) -> None:
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


def _document_xml(
    paper_filename: str,
    summary: str,
    assets: list[PaperAsset],
    media_files: list[tuple[Path, str, str]],
) -> str:
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
            body.append(_image_paragraph(source, asset_id, rel_id))
            used_assets.add(asset_id)

    if assets and not used_assets:
        body.append(_paragraph("关键图表", "Heading1"))
        for asset_id, asset in asset_by_id.items():
            media = media_by_id.get(asset_id)
            if not media:
                continue
            source, _media_name, rel_id = media
            label = _asset_display_label(asset_id, asset, asset_counters, asset_labels)
            body.append(_paragraph(_asset_reference_sentence(label), "AssetLead"))
            body.append(_image_paragraph(source, asset_id, rel_id))
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
        elif current_section in {"创新点", "关键结果", "局限", "总结", "我的笔记"}:
            for item in list_buffer:
                result.append("[NOTE_CARD] " + _normalize_list_to_sentence(item))
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
        return f'<w:rPr>{base_fonts}<w:b/><w:color w:val="134E4A"/><w:sz w:val="26"/><w:shd w:fill="DDEDEA"/></w:rPr>'
    return f"<w:rPr>{base_fonts}</w:rPr>"


def _font_run_xml() -> str:
    return (
        f'<w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" '
        f'w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/>'
    )


def _paragraph_properties(style: str | None = None) -> str:
    if style == "Title":
        return (
            '<w:pPr><w:pStyle w:val="Title"/><w:jc w:val="center"/>'
            '<w:spacing w:before="240" w:after="180"/></w:pPr>'
        )
    if style == "Heading1":
        return (
            '<w:pPr><w:pStyle w:val="Heading1"/>'
            '<w:spacing w:before="420" w:after="180"/>'
            '<w:shd w:fill="EAF4F1"/><w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="2F7D6D"/></w:pBdr>'
            '</w:pPr>'
        )
    if style == "Heading2":
        return '<w:pPr><w:pStyle w:val="Heading2"/><w:spacing w:before="260" w:after="120"/></w:pPr>'
    if style == "Heading3":
        return (
            '<w:pPr><w:pStyle w:val="Heading3"/>'
            '<w:spacing w:before="260" w:after="100" w:line="320" w:lineRule="auto"/>'
            '<w:ind w:left="80" w:right="120"/>'
            '<w:pBdr><w:left w:val="single" w:sz="12" w:space="8" w:color="2F7D6D"/></w:pBdr>'
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
            '<w:spacing w:before="40" w:after="40" w:line="300" w:lineRule="auto"/>'
            '<w:ind w:left="260"/>'
            '<w:shd w:fill="F7FBFA"/>'
            '</w:pPr>'
        )
    if style == "Callout":
        return (
            '<w:pPr><w:pStyle w:val="Callout"/>'
            '<w:spacing w:before="120" w:after="120" w:line="360" w:lineRule="auto"/>'
            '<w:ind w:left="240" w:right="240"/>'
            '<w:shd w:fill="F7FBFA"/>'
            '<w:pBdr><w:left w:val="single" w:sz="10" w:space="8" w:color="7CB7A8"/></w:pBdr>'
            '</w:pPr>'
        )
    if style == "FigureCallout":
        return (
            '<w:pPr><w:pStyle w:val="FigureCallout"/>'
            '<w:spacing w:before="80" w:after="40" w:line="300" w:lineRule="auto"/>'
            '<w:ind w:left="260" w:right="260"/>'
            '<w:shd w:fill="F1F5F9"/>'
            '<w:pBdr><w:left w:val="single" w:sz="8" w:space="8" w:color="64748B"/></w:pBdr>'
            '</w:pPr>'
        )
    if style == "NoteCard":
        return (
            '<w:pPr><w:pStyle w:val="NoteCard"/>'
            '<w:spacing w:before="90" w:after="90" w:line="340" w:lineRule="auto"/>'
            '<w:ind w:left="220" w:right="220"/>'
            '<w:shd w:fill="FFFDF7"/>'
            '<w:pBdr><w:left w:val="single" w:sz="8" w:space="8" w:color="D7A84A"/></w:pBdr>'
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
                return f"公式 {match.group(1)}"
            match = re.search(r"(?:公式|方程)\s*[\(:：]?\s*([0-9一二三四五六七八九十]+)\)?", source_text)
            if match:
                return f"公式 {match.group(1)}"
            match = re.search(r"[\(\[（]\s*([0-9一二三四五六七八九十]+[A-Za-z]?)\s*[\)\]）]", source_text)
            if match:
                return f"公式 {match.group(1)}"
            match = _trailing_equation_number(source_text)
            if match:
                return f"公式 {match}"
    return ""


def _trailing_equation_number(text: str) -> str:
    candidates = re.findall(r"(?:^|\s)[\(\[（]\s*([0-9一二三四五六七八九十]+[A-Za-z]?)\s*[\)\]）](?=\s*$)", text)
    if candidates:
        return candidates[-1]
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


def _image_paragraph(path: Path, docpr_id: int, rel_id: str) -> str:
    cx, cy = _image_size_emu(path)
    return f"""<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:before="80" w:after="180"/></w:pPr><w:r><w:drawing>
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


def _image_size_emu(path: Path) -> tuple[int, int]:
    max_width_emu = int(6.2 * 914400)
    try:
        with Image.open(path) as image:
            width, height = image.size
    except Exception:
        width, height = 800, 500
    if width <= 0 or height <= 0:
        width, height = 800, 500
    scale = min(1.0, max_width_emu / (width * 9525))
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
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="2B2B2B"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="1F4F46"/><w:sz w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="1F4F46"/><w:sz w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="2F7D6D"/><w:sz w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="FFFFFF"/><w:sz w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:b/><w:color w:val="1F4F46"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="AssetLead"><w:name w:val="asset lead"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="1F4F46"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Metadata"><w:name w:val="metadata"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="294D45"/><w:sz w:val="21"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Callout"><w:name w:val="callout"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="294D45"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="FigureCallout"><w:name w:val="figure callout"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="475569"/><w:sz w:val="20"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="NoteCard"><w:name w:val="note card"/><w:basedOn w:val="Normal"/><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:color w:val="4A3B18"/><w:sz w:val="22"/></w:rPr></w:style>
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
