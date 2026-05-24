import concurrent.futures
import logging
import re
import unicodedata
from asyncio import CancelledError
from enum import Enum
from string import Template
from typing import Dict

import numpy as np
from pdfminer.converter import PDFConverter
from pdfminer.layout import LTChar, LTFigure, LTLine, LTPage
from pdfminer.pdffont import PDFCIDFont, PDFUnicodeNotDefined
from pdfminer.pdfinterp import PDFGraphicState, PDFResourceManager
from pdfminer.utils import apply_matrix_pt, mult_matrix
from pymupdf import Font
from tenacity import retry, wait_fixed

try:
    import tqdm
except ImportError:
    tqdm = None

from paper_agent.translator import (
    AnythingLLMTranslator,
    ArgosTranslator,
    AzureOpenAITranslator,
    AzureTranslator,
    BaseTranslator,
    BingTranslator,
    DeepLTranslator,
    DeepLXTranslator,
    DeepseekTranslator,
    DifyTranslator,
    GeminiTranslator,
    GoogleTranslator,
    GrokTranslator,
    GroqTranslator,
    MiniMaxTranslator,
    ModelScopeTranslator,
    OllamaTranslator,
    OpenAIlikedTranslator,
    OpenAITranslator,
    QwenMtTranslator,
    SiliconTranslator,
    TencentTranslator,
    XinferenceTranslator,
    ZhipuTranslator,
    X302AITranslator,
)

log = logging.getLogger(__name__)


class PDFConverterEx(PDFConverter):
    def __init__(
        self,
        rsrcmgr: PDFResourceManager,
    ) -> None:
        PDFConverter.__init__(self, rsrcmgr, None, "utf-8", 1, None)

    def begin_page(self, page, ctm) -> None:
        # 重载替换 cropbox
        x0, y0, x1, y1 = page.cropbox
        x0, y0 = apply_matrix_pt(ctm, (x0, y0))
        x1, y1 = apply_matrix_pt(ctm, (x1, y1))
        mediabox = (0, 0, abs(x0 - x1), abs(y0 - y1))
        self.cur_item = LTPage(page.pageno, mediabox)

    def end_page(self, page):
        # 重载返回指令流
        return self.receive_layout(self.cur_item)

    def begin_figure(self, name, bbox, matrix) -> None:
        # 重载设置 pageid
        self._stack.append(self.cur_item)
        self.cur_item = LTFigure(name, bbox, mult_matrix(matrix, self.ctm))
        self.cur_item.pageid = self._stack[-1].pageid

    def end_figure(self, _: str) -> None:
        # 重载返回指令流
        fig = self.cur_item
        assert isinstance(self.cur_item, LTFigure), str(type(self.cur_item))
        self.cur_item = self._stack.pop()
        self.cur_item.add(fig)
        return self.receive_layout(fig)

    def render_char(
        self,
        matrix,
        font,
        fontsize: float,
        scaling: float,
        rise: float,
        cid: int,
        ncs,
        graphicstate: PDFGraphicState,
    ) -> float:
        # 重载设置 cid 和 font
        try:
            text = font.to_unichr(cid)
            assert isinstance(text, str), str(type(text))
        except PDFUnicodeNotDefined:
            text = self.handle_undefined_char(font, cid)
        textwidth = font.char_width(cid)
        textdisp = font.char_disp(cid)
        item = LTChar(
            matrix,
            font,
            fontsize,
            scaling,
            rise,
            text,
            textwidth,
            textdisp,
            ncs,
            graphicstate,
        )
        self.cur_item.add(item)
        item.cid = cid  # hack 插入原字符编码
        item.font = font  # hack 插入原字符字体
        return item.adv


class Paragraph:
    def __init__(self, y, x, x0, x1, y0, y1, size, brk, is_table=False, align="left"):
        self.y: float = y  # 初始纵坐标
        self.x: float = x  # 初始横坐标
        self.x0: float = x0  # 左边界
        self.x1: float = x1  # 右边界
        self.y0: float = y0  # 上边界
        self.y1: float = y1  # 下边界
        self.size: float = size  # 字体大小
        self.brk: bool = brk  # 换行标记
        self.is_table: bool = is_table  # 是否来自表格单元格
        self.align: str = align  # 段落对齐方式


# fmt: off
class TranslateConverter(PDFConverterEx):
    def __init__(
        self,
        rsrcmgr,
        vfont: str = None,
        vchar: str = None,
        thread: int = 0,
        layout={},
        lang_in: str = "",
        lang_out: str = "",
        service: str = "",
        noto_name: str = "",
        noto: Font = None,
        envs: Dict = None,
        prompt: Template = None,
        ignore_cache: bool = False,
        cancellation_event=None,
        progress_callback=None,
    ) -> None:
        super().__init__(rsrcmgr)
        self.vfont = vfont
        self.vchar = vchar
        self.thread = thread
        self.layout = layout
        self.noto_name = noto_name
        self.noto = noto
        self.cancellation_event = cancellation_event
        self.translator: BaseTranslator = None
        # e.g. "ollama:gemma2:9b" -> ["ollama", "gemma2:9b"]
        param = service.split(":", 1)
        service_name = param[0]
        service_model = param[1] if len(param) > 1 else None
        if not envs:
            envs = {}
        for translator in [GoogleTranslator, BingTranslator, DeepLTranslator, DeepLXTranslator, OllamaTranslator, XinferenceTranslator, AzureOpenAITranslator,
                           OpenAITranslator, ZhipuTranslator, ModelScopeTranslator, SiliconTranslator, GeminiTranslator, AzureTranslator, TencentTranslator, DifyTranslator, AnythingLLMTranslator, ArgosTranslator, GrokTranslator, GroqTranslator, DeepseekTranslator, MiniMaxTranslator, OpenAIlikedTranslator, QwenMtTranslator, X302AITranslator]:
            if service_name == translator.name:
                self.translator = translator(lang_in, lang_out, service_model, envs=envs, prompt=prompt, ignore_cache=ignore_cache)
        if not self.translator:
            raise ValueError("Unsupported translation service")

    def receive_layout(self, ltpage: LTPage):
        log.info(f"[PAGE] Processing page {ltpage.pageid}, size={ltpage.width:.2f}x{ltpage.height:.2f}")
        # 段落
        sstk: list[str] = []            # 段落文字栈
        pstk: list[Paragraph] = []      # 段落属性栈
        vbkt: int = 0                   # 段落公式括号计数
        # 公式组
        vstk: list[LTChar] = []         # 公式符号组
        vlstk: list[LTLine] = []        # 公式线条组
        vfix: float = 0                 # 公式纵向偏移
        # 公式组栈
        var: list[list[LTChar]] = []    # 公式符号组栈
        varl: list[list[LTLine]] = []   # 公式线条组栈
        varf: list[float] = []          # 公式纵向偏移栈
        vlen: list[float] = []          # 公式宽度栈
        # 全局
        lstk: list[LTLine] = []         # 全局线条栈
        xt: LTChar = None               # 上一个字符
        xt_cls: int = -1                # 上一个字符所属段落，保证无论第一个字符属于哪个类别都可以触发新段落
        vmax: float = ltpage.width / 4  # 行内公式最大宽度
        ops: str = ""                   # 渲染结果

        # 表格处理状态
        in_table_mode = False           # 是否正在处理表格
        table_rows: dict[float, list[LTChar]] = {}       # 表格行内容：table_rows[y] = 行内字符
        table_cell_indices: set[int] = set()  # 记录表格单元格在 sstk 中的索引
        table_progress = None           # tqdm 进度条对象
        table_lines: list[LTLine] = []   # 页面内表格线，用于推断真实单元格边界
        page_lines: list[LTLine] = []    # 页面内所有细线，表格线 mask 漏检时兜底

        layout = self.layout[ltpage.pageid]
        h, w = layout.shape
        for item in ltpage:
            if isinstance(item, LTLine):
                if item.linewidth < 5:
                    page_lines.append(item)
                cx, cy = np.clip(int(item.x0), 0, w - 1), np.clip(int(item.y0), 0, h - 1)
                if layout[cy, cx] < 0:
                    table_lines.append(item)

        def flush_table():
            nonlocal in_table_mode, table_rows, xt, xt_cls
            if not in_table_mode:
                return

            log.debug(f"[TABLE] >>> Exiting table mode, processing {len(table_rows)} rows")
            all_table_chars = [ch for chars in table_rows.values() for ch in chars]
            if not all_table_chars:
                in_table_mode = False
                table_rows = {}
                return

            text_x0 = min(ch.x0 for ch in all_table_chars)
            text_x1 = max(ch.x1 for ch in all_table_chars)
            text_y0 = min(ch.y0 for ch in all_table_chars)
            text_y1 = max(ch.y1 for ch in all_table_chars)
            max_row_size = max(ch.size for ch in all_table_chars)

            def unique_positions(values, tolerance=1.0):
                positions = []
                for value in sorted(values):
                    if not positions or abs(value - positions[-1]) > tolerance:
                        positions.append(value)
                    else:
                        positions[-1] = (positions[-1] + value) / 2
                return positions

            vertical_xs = []
            horizontal_ys = []
            horizontal_spans = []
            candidate_lines = list(table_lines)
            for line in page_lines:
                lx0, lx1 = sorted([line.pts[0][0], line.pts[1][0]])
                ly0, ly1 = sorted([line.pts[0][1], line.pts[1][1]])
                near_x = lx1 >= text_x0 - max_row_size * 3 and lx0 <= text_x1 + max_row_size * 3
                near_y = ly1 >= text_y0 - max_row_size * 3 and ly0 <= text_y1 + max_row_size * 3
                if near_x and near_y and line not in candidate_lines:
                    candidate_lines.append(line)

            for line in candidate_lines:
                lx0, lx1 = sorted([line.pts[0][0], line.pts[1][0]])
                ly0, ly1 = sorted([line.pts[0][1], line.pts[1][1]])
                if abs(lx1 - lx0) < 1.5 and ly1 >= text_y0 - max_row_size * 2 and ly0 <= text_y1 + max_row_size * 2:
                    vertical_xs.append((lx0 + lx1) / 2)
                if abs(ly1 - ly0) < 1.5 and lx1 >= text_x0 - max_row_size * 2 and lx0 <= text_x1 + max_row_size * 2:
                    horizontal_ys.append((ly0 + ly1) / 2)
                    horizontal_spans.append((lx0, lx1))

            if horizontal_spans:
                vertical_xs.extend([
                    min(span[0] for span in horizontal_spans),
                    max(span[1] for span in horizontal_spans),
                ])
            vertical_xs = unique_positions(vertical_xs)
            horizontal_ys = unique_positions(horizontal_ys)
            if len(vertical_xs) >= 2:
                table_x0 = min(vertical_xs)
                table_x1 = max(vertical_xs)
            elif horizontal_spans:
                table_x0 = min(span[0] for span in horizontal_spans)
                table_x1 = max(span[1] for span in horizontal_spans)
            else:
                table_x0 = text_x0
                table_x1 = text_x1

            row_items = []
            for y in sorted(table_rows.keys(), reverse=True):
                chars = sorted(table_rows[y], key=lambda ch: (ch.x0, ch.y0))
                if not chars:
                    continue
                row_size = max(ch.size for ch in chars)
                widths = [max(ch.x1 - ch.x0, 0.01) for ch in chars if ch.get_text() != " "]
                median_width = float(np.median(widths)) if widths else row_size * 0.5
                split_gap = max(row_size * 2.2, median_width * 3.5)
                space_gap = max(row_size * 0.25, median_width * 0.8)

                if len(vertical_xs) >= 3:
                    grid_cells = {}
                    for ch in chars:
                        char_center = ch.x0 + (ch.x1 - ch.x0) / 2
                        if char_center <= vertical_xs[0]:
                            grid_col = 0
                        elif char_center >= vertical_xs[-1]:
                            grid_col = len(vertical_xs) - 2
                        else:
                            grid_col = 0
                            for idx in range(len(vertical_xs) - 1):
                                if vertical_xs[idx] <= char_center <= vertical_xs[idx + 1]:
                                    grid_col = idx
                                    break
                        grid_cells.setdefault(grid_col, []).append(ch)
                    cell_groups = [(col, grid_cells[col]) for col in sorted(grid_cells.keys())]
                else:
                    cell_chars = []
                    cells = []
                    prev = None
                    for ch in chars:
                        gap = ch.x0 - prev.x1 if prev is not None else 0
                        if prev is not None and gap > split_gap and cell_chars:
                            cells.append(cell_chars)
                            cell_chars = []
                        cell_chars.append(ch)
                        prev = ch
                    if cell_chars:
                        cells.append(cell_chars)
                    cell_groups = list(enumerate(cells))

                row_cells = []
                for grid_col, cell in cell_groups:
                    text_parts = []
                    prev = None
                    for ch in cell:
                        gap = ch.x0 - prev.x1 if prev is not None else 0
                        ch_text = ch.get_text()
                        if (
                            prev is not None
                            and gap > space_gap
                            and text_parts
                            and text_parts[-1] != " "
                            and ch_text != " "
                        ):
                            text_parts.append(" ")
                        text_parts.append(ch_text)
                        prev = ch
                    row_cells.append({
                        "text": "".join(text_parts),
                        "col": grid_col,
                        "x0": min(ch.x0 for ch in cell),
                        "x1": max(ch.x1 for ch in cell),
                        "y0": min(ch.y0 for ch in cell),
                        "y1": max(ch.y1 for ch in cell),
                        "size": max(ch.size for ch in cell),
                    })

                if row_cells:
                    row_y0 = min(cell["y0"] for cell in row_cells)
                    row_y1 = max(cell["y1"] for cell in row_cells)
                    row_items.append({
                        "cells": row_cells,
                        "center": (row_y0 + row_y1) / 2,
                        "y0": row_y0,
                        "y1": row_y1,
                        "size": row_size,
                    })

            if not row_items:
                in_table_mode = False
                table_rows = {}
                return

            for row_idx, row in enumerate(row_items):
                prev_center = row_items[row_idx - 1]["center"] if row_idx > 0 else None
                next_center = row_items[row_idx + 1]["center"] if row_idx + 1 < len(row_items) else None
                lower_candidates = [pos for pos in horizontal_ys if pos <= row["center"]]
                upper_candidates = [pos for pos in horizontal_ys if pos >= row["center"]]
                use_horizontal_grid = len(horizontal_ys) >= len(row_items) + 1 or (len(row_items) == 1 and len(horizontal_ys) >= 2)
                if use_horizontal_grid and lower_candidates and upper_candidates and max(lower_candidates) < min(upper_candidates):
                    lower = max(lower_candidates)
                    upper = min(upper_candidates)
                elif prev_center is not None:
                    upper = (prev_center + row["center"]) / 2
                    if next_center is not None:
                        lower = (row["center"] + next_center) / 2
                    else:
                        lower = row["center"] - (prev_center - row["center"]) / 2
                else:
                    upper = row["center"] + (row["center"] - next_center) / 2 if next_center is not None else row["y1"] + row["size"] * 0.45
                    lower = (row["center"] + next_center) / 2 if next_center is not None else row["y0"] - row["size"] * 0.45

                cells = sorted(row["cells"], key=lambda cell: cell["x0"])
                use_vertical_grid = len(vertical_xs) >= 3
                for col_idx, cell in enumerate(cells):
                    prev_cell = cells[col_idx - 1] if col_idx > 0 else None
                    next_cell = cells[col_idx + 1] if col_idx + 1 < len(cells) else None
                    cell_center = (cell["x0"] + cell["x1"]) / 2
                    left_candidates = [pos for pos in vertical_xs if pos <= cell_center]
                    right_candidates = [pos for pos in vertical_xs if pos >= cell_center]
                    if use_vertical_grid and 0 <= cell["col"] < len(vertical_xs) - 1:
                        left = vertical_xs[cell["col"]]
                        right = vertical_xs[cell["col"] + 1]
                    elif use_vertical_grid and left_candidates and right_candidates and max(left_candidates) < min(right_candidates):
                        left = max(left_candidates)
                        right = min(right_candidates)
                    else:
                        left = table_x0 if prev_cell is None else (prev_cell["x1"] + cell["x0"]) / 2
                        right = table_x1 if next_cell is None else (cell["x1"] + next_cell["x0"]) / 2
                    pad_x = min(max(cell["size"] * 0.25, 1.0), max((right - left) * 0.08, 0.0))
                    pad_y = min(max(cell["size"] * 0.12, 0.5), max((upper - lower) * 0.12, 0.0))
                    x0 = left + pad_x
                    x1 = right - pad_x
                    y0 = lower + pad_y
                    y1 = upper - pad_y
                    if x1 <= x0 + cell["size"] * 0.5:
                        x0, x1 = cell["x0"], cell["x1"]
                    if y1 <= y0 + cell["size"] * 0.5:
                        y0, y1 = cell["y0"], cell["y1"]
                    y = max(y1 - cell["size"] * 0.95, y0)
                    table_cell_indices.add(len(sstk))
                    sstk.append(cell["text"])
                    align = "left" if x0 <= table_x0 + max(cell["size"], 2) else "center"
                    pstk.append(Paragraph(y, x0, x0, x1, y0, y1, cell["size"], True, is_table=True, align=align))
                    log.debug(
                        f"[TABLE] Cell: row={row_idx}, col={col_idx}, size={cell['size']:.2f}, "
                        f"bbox=({x0:.2f},{y0:.2f})-({x1:.2f},{y1:.2f}), text='{cell['text'][:30]}{'...' if len(cell['text'])>30 else ''}'"
                    )

            in_table_mode = False
            table_rows = {}
            # 表格结束后重置段落连续性，避免后续正文接到最后一个单元格后面。
            xt = None
            xt_cls = -1

        def vflag(font: str, char: str):    # 匹配公式（和角标）字体
            if isinstance(font, bytes):     # 不一定能 decode，直接转 str
                try:
                    font = font.decode('utf-8')  # 尝试使用 UTF-8 解码
                except UnicodeDecodeError:
                    font = ""
            font = font.split("+")[-1]      # 字体名截断
            if re.match(r"\(cid:", char):
                return True
            # 基于字体名规则的判定
            if self.vfont:
                if re.match(self.vfont, font):
                    return True
            else:
                if re.match(                                            # latex 字体
                    r"(CM[^R]|MS.M|XY|MT|BL|RM|EU|LA|RS|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy|stmary|.*Mono|.*Code|.*Ital|.*Sym|.*Math)",
                    font,
                ):
                    return True
            # 基于字符集规则的判定
            if self.vchar:
                if re.match(self.vchar, char):
                    return True
            else:
                if (
                    char
                    and char != " "                                     # 非空格
                    and (
                        unicodedata.category(char[0])
                        in ["Lm", "Mn", "Sk", "Sm", "Zl", "Zp", "Zs"]   # 文字修饰符、数学符号、分隔符号
                        or ord(char[0]) in range(0x370, 0x400)          # 希腊字母
                    )
                ):
                    return True
            return False

        ############################################################
        # A. 原文档解析
        char_count = 0  # 用于定期检查取消事件
        table_char_count = 0  # 统计表格字符数量
        cls_distribution = {}  # 调试：统计各类别的字符数量
        for child in ltpage:
            if isinstance(child, LTChar):
                char_count += 1
                if char_count % 100 == 0 and self.cancellation_event and self.cancellation_event.is_set():
                    raise CancelledError("task cancelled")
                cur_v = False
                layout = self.layout[ltpage.pageid]
                # ltpage.height 可能是 fig 里面的高度，这里统一用 layout.shape
                h, w = layout.shape
                # 读取当前字符在 layout 中的类别
                cx, cy = np.clip(int(child.x0), 0, w - 1), np.clip(int(child.y0), 0, h - 1)
                cls = layout[cy, cx]
                # 调试：统计类别分布
                if ltpage.pageid == 1:  # 仅统计第2页
                    cls_distribution[cls] = cls_distribution.get(cls, 0) + 1
                    if cls < 0:
                        table_char_count += 1
                # 锚定文档中 bullet 的位置
                if child.get_text() == "•":
                    cls = 0

                # 表格区域处理（cls < 0 表示表格）
                if cls < 0:
                    if not in_table_mode:
                        # 进入表格模式：结束当前段落（如果有）
                        # 当前段落会在后面处理，这里先不处理
                        in_table_mode = True
                        table_rows = {}
                        log.info(f"[TABLE] >>> Entering table mode on page {ltpage.pageid}, first char at y={child.y0:.2f}, size={child.size:.2f}, text='{child.get_text()}'")

                    # 根据 y 坐标聚类到行。列/单元格必须在整行字符收集完后按横向间距切分，
                    # 否则长文本会因为离首字符太远而被误拆成多个伪单元格。
                    row_threshold = child.size * 1.2
                    matched_row = None
                    min_row_distance = float('inf')

                    # 计算字符的垂直中心
                    char_center_y = child.y0 + (child.y1 - child.y0) / 2

                    # 寻找最近的行
                    for y_center in table_rows.keys():
                        distance = abs(char_center_y - y_center)
                        if distance < row_threshold and distance < min_row_distance:
                            matched_row = y_center
                            min_row_distance = distance

                    if matched_row is None:
                        matched_row = char_center_y
                        table_rows[matched_row] = []

                    table_rows[matched_row].append(child)
                    continue  # 跳过后续处理

                # 如果当前在表格模式，但当前字符不是表格区域，则结束表格模式
                if in_table_mode:
                    flush_table()

                # 判定当前字符是否属于公式
                if (                                                                                        # 判定当前字符是否属于公式
                    cls == 0                                                                                # 1. 类别为保留区域
                    or (cls == xt_cls and len(sstk[-1].strip()) > 1 and child.size < pstk[-1].size * 0.79)  # 2. 角标字体，有 0.76 的角标和 0.799 的大写，这里用 0.79 取中，同时考虑首字母放大的情况
                    or vflag(child.fontname, child.get_text())                                              # 3. 公式字体
                    or (child.matrix[0] == 0 and child.matrix[3] == 0)                                      # 4. 垂直字体
                ):
                    cur_v = True
                # 判定括号组是否属于公式
                if not cur_v:
                    if vstk and child.get_text() == "(":
                        cur_v = True
                        vbkt += 1
                    if vbkt and child.get_text() == ")":
                        cur_v = True
                        vbkt -= 1
                if (                                                        # 判定当前公式是否结束
                    not cur_v                                               # 1. 当前字符不属于公式
                    or cls != xt_cls                                        # 2. 当前字符与前一个字符不属于同一段落
                    # or (abs(child.x0 - xt.x0) > vmax and cls != 0)        # 3. 段落内换行，可能是一长串斜体的段落，也可能是段内分式换行，这里设个阈值进行区分
                    # 禁止纯公式（代码）段落换行，直到文字开始再重开文字段落，保证只存在两种情况
                    # A. 纯公式（代码）段落（锚定绝对位置）sstk[-1]=="" -> sstk[-1]=="{v*}"
                    # B. 文字开头段落（排版相对位置）sstk[-1]!=""
                    or (sstk[-1] != "" and abs(child.x0 - xt.x0) > vmax)    # 因为 cls==xt_cls==0 一定有 sstk[-1]==""，所以这里不需要再判定 cls!=0
                ):
                    if vstk:
                        if (                                                # 根据公式右侧的文字修正公式的纵向偏移
                            not cur_v                                       # 1. 当前字符不属于公式
                            and cls == xt_cls                               # 2. 当前字符与前一个字符属于同一段落
                            and child.x0 > max([vch.x0 for vch in vstk])    # 3. 当前字符在公式右侧
                        ):
                            vfix = vstk[0].y0 - child.y0
                        if sstk[-1] == "":
                            xt_cls = -1 # 禁止纯公式段落（sstk[-1]=="{v*}"）的后续连接，但是要考虑新字符和后续字符的连接，所以这里修改的是上个字符的类别
                        sstk[-1] += f"{{v{len(var)}}}"
                        var.append(vstk)
                        varl.append(vlstk)
                        varf.append(vfix)
                        vstk = []
                        vlstk = []
                        vfix = 0
                # 当前字符不属于公式或当前字符是公式的第一个字符
                if not vstk:
                    if cls == xt_cls:               # 当前字符与前一个字符属于同一段落
                        line_return = child.x1 < xt.x0
                        large_horizontal_gap = child.x0 - xt.x1 > max(child.size * 4, 24)
                        vertical_step = abs(child.y0 - xt.y0)
                        near_paragraph_left = child.x0 <= pstk[-1].x0 + max(child.size * 1.5, 8)
                        ends_sentence = sstk[-1].rstrip().endswith(("。", ".", "；", ";", "！", "!", "？", "?", "：", ":"))
                        starts_new_item = (
                            (line_return or large_horizontal_gap)
                            and sstk[-1].strip()
                            and (near_paragraph_left or large_horizontal_gap)
                            and (
                                vertical_step > max(child.size, pstk[-1].size) * 1.35
                                or ends_sentence
                                or large_horizontal_gap
                            )
                        )
                        if starts_new_item:
                            sstk.append("")
                            pstk.append(Paragraph(child.y0, child.x0, child.x0, child.x0, child.y0, child.y1, child.size, False))
                        elif child.x0 > xt.x1 + 1:  # 添加行内空格
                            sstk[-1] += " "
                        elif line_return:            # 添加换行空格并标记原文段落存在换行
                            sstk[-1] += " "
                            pstk[-1].brk = True
                    else:                           # 根据当前字符构建一个新的段落
                        sstk.append("")
                        pstk.append(Paragraph(child.y0, child.x0, child.x0, child.x0, child.y0, child.y1, child.size, False))
                if not cur_v:                                               # 文字入栈
                    if (                                                    # 根据当前字符修正段落属性
                        child.size > pstk[-1].size                          # 1. 当前字符比段落字体大
                        or len(sstk[-1].strip()) == 1                       # 2. 当前字符为段落第二个文字（考虑首字母放大的情况）
                    ) and child.get_text() != " ":                          # 3. 当前字符不是空格
                        pstk[-1].y -= child.size - pstk[-1].size            # 修正段落初始纵坐标，假设两个不同大小字符的上边界对齐
                        pstk[-1].size = child.size
                    sstk[-1] += child.get_text()
                else:                                                       # 公式入栈
                    if (                                                    # 根据公式左侧的文字修正公式的纵向偏移
                        not vstk                                            # 1. 当前字符是公式的第一个字符
                        and cls == xt_cls                                   # 2. 当前字符与前一个字符属于同一段落
                        and child.x0 > xt.x0                                # 3. 前一个字符在公式左侧
                    ):
                        vfix = child.y0 - xt.y0
                    vstk.append(child)
                # 更新段落边界，因为段落内换行之后可能是公式开头，所以要在外边处理
                pstk[-1].x0 = min(pstk[-1].x0, child.x0)
                pstk[-1].x1 = max(pstk[-1].x1, child.x1)
                pstk[-1].y0 = min(pstk[-1].y0, child.y0)
                pstk[-1].y1 = max(pstk[-1].y1, child.y1)
                # 更新上一个字符
                xt = child
                xt_cls = cls
            elif isinstance(child, LTFigure):   # 图表
                pass
            elif isinstance(child, LTLine):     # 线条
                layout = self.layout[ltpage.pageid]
                # ltpage.height 可能是 fig 里面的高度，这里统一用 layout.shape
                h, w = layout.shape
                # 读取当前线条在 layout 中的类别
                cx, cy = np.clip(int(child.x0), 0, w - 1), np.clip(int(child.y0), 0, h - 1)
                cls = layout[cy, cx]
                if vstk and cls == xt_cls:      # 公式线条
                    vlstk.append(child)
                else:                           # 全局线条
                    lstk.append(child)
            else:
                pass
        # 页面解析结束，输出统计
        if ltpage.pageid == 1:  # 第2页
            log.info(f"[PAGE2] Parsing complete: total chars={char_count}, table chars={table_char_count}, in_table_mode={in_table_mode}, table_rows={len(table_rows)}")
            # 输出类别分布
            if cls_distribution:
                sorted_cls = sorted(cls_distribution.items(), key=lambda x: x[1], reverse=True)
                cls_info = ", ".join([f"cls={k}:{v}" for k, v in sorted_cls[:10]])
                log.info(f"[PAGE2] Class distribution (top 10): {cls_info}")
        # 处理结尾
        if in_table_mode:
            flush_table()
        if vstk:    # 公式出栈
            sstk[-1] += f"{{v{len(var)}}}"
            var.append(vstk)
            varl.append(vlstk)
            varf.append(vfix)
        log.debug("\n==========[VSTACK]==========\n")
        for id, v in enumerate(var):  # 计算公式宽度
            l = max([vch.x1 for vch in v]) - v[0].x0
            log.debug(f'< {l:.1f} {v[0].x0:.1f} {v[0].y0:.1f} {v[0].cid} {v[0].fontname} {len(varl[id])} > v{id} = {"".join([ch.get_text() for ch in v])}')
            vlen.append(l)

        ############################################################
        # B. 段落翻译
        log.debug("\n==========[SSTACK]==========\n")

        # 初始化表格进度条
        total_table_cells = len(table_cell_indices)
        table_progress = None
        if total_table_cells > 0 and tqdm is not None:
            table_progress = tqdm.tqdm(
                total=total_table_cells,
                desc="Translating table cells",
                unit="cell",
                leave=False,
            )

        @retry(wait=wait_fixed(1))
        def worker(s: str, idx: int):  # 多线程翻译，接收索引参数
            # 检查是否已取消
            if self.cancellation_event and self.cancellation_event.is_set():
                raise CancelledError("task cancelled")
            if not s.strip() or re.match(r"^\{v\d+\}$", s):  # 空白和公式不翻译
                return s
            try:
                new = self.translator.translate(s)
                # 更新表格进度
                if idx in table_cell_indices and table_progress is not None:
                    table_progress.update(1)
                return new
            except BaseException as e:
                # 增强错误日志，添加翻译上下文信息
                log.error(f"Translation failed - lang_in: {self.translator.lang_in}, lang_out: {self.translator.lang_out}, service: {self.translator.name}, model: {getattr(self.translator, 'model', 'N/A')}")
                log.error(f"Failed text (first 100 chars): {s[:100]}{'...' if len(s) > 100 else ''}")
                if log.isEnabledFor(logging.DEBUG):
                    log.exception(e)
                else:
                    log.exception(e, exc_info=False)
                raise e
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.thread
            ) as executor:
                # 提交所有任务
                future_to_index = {executor.submit(worker, s, i): i for i, s in enumerate(sstk)}
                news = [None] * len(sstk)
                for future in concurrent.futures.as_completed(future_to_index):
                    idx = future_to_index[future]
                    try:
                        result = future.result()
                        news[idx] = result
                    except CancelledError:
                        # 任务被取消，取消所有其他任务
                        for f in future_to_index:
                            f.cancel()
                        raise
                    except BaseException as e:
                        # 其他异常，取消所有任务
                        for f in future_to_index:
                            f.cancel()
                        raise
        finally:
            # 关闭表格进度条
            if table_progress is not None:
                table_progress.close()

        ############################################################
        # C. 新文档排版
        def raw_string(fcur: str, cstk: str):  # 编码字符串
            if fcur == self.noto_name:
                return "".join(["%04x" % self.noto.has_glyph(ord(c)) for c in cstk])
            elif isinstance(self.fontmap[fcur], PDFCIDFont):  # 判断编码长度
                return "".join(["%04x" % ord(c) for c in cstk])
            else:
                return "".join(["%02x" % ord(c) for c in cstk])

        # 根据目标语言获取默认行距
        LANG_LINEHEIGHT_MAP = {
            "zh-cn": 1.4, "zh-tw": 1.4, "zh-hans": 1.4, "zh-hant": 1.4, "zh": 1.4,
            "ja": 1.1, "ko": 1.2, "en": 1.2, "de": 1.0, "de-de": 1.0, "ar": 1.0, "ru": 0.8, "uk": 0.8, "ta": 0.8
        }
        default_line_height = LANG_LINEHEIGHT_MAP.get(self.translator.lang_out.lower(), 1.1) # 小语种默认1.1
        _x, _y = 0, 0
        ops_list = []

        def gen_op_txt(font, size, x, y, rtxt):
            return f"/{font} {size:f} Tf 1 0 0 1 {x:f} {y:f} Tm [<{rtxt}>] TJ "

        def gen_op_line(x, y, xlen, ylen, linewidth):
            return f"ET q 1 0 0 1 {x:f} {y:f} cm [] 0 d 0 J {linewidth:f} w 0 0 m {xlen:f} {ylen:f} l S Q BT "

        # 输出所有段落的汇总信息（调试用）
        if log.isEnabledFor(logging.INFO):
            table_cells_info = []
            for idx, p in enumerate(pstk):
                cell_info = f"idx{idx}: y={p.y:.2f}, size={p.size:.2f}, h={p.y1-p.y0:.2f}, text='{sstk[idx][:30]}...'"
                table_cells_info.append(cell_info)
            log.info(f"[LAYOUT] Total paragraphs: {len(pstk)}. Details: {' | '.join(table_cells_info[:10])}")

        for id, new in enumerate(news):
            x: float = pstk[id].x                       # 段落初始横坐标
            y: float = pstk[id].y                       # 段落初始纵坐标
            x0: float = pstk[id].x0                     # 段落左边界
            x1: float = pstk[id].x1                     # 段落右边界
            height: float = pstk[id].y1 - pstk[id].y0   # 段落高度
            size: float = pstk[id].size                 # 段落字体大小
            brk: bool = pstk[id].brk                    # 段落换行标记
            is_table_cell = getattr(pstk[id], "is_table", False) or id in table_cell_indices
            if is_table_cell:
                height = max(height, size * 1.05)
                # 表格行通常很矮，尤其德语等长词语言翻译后容易被提前换行并裁剪。
                # 先按单行缩放尝试放入单元格，只有高度足够时才允许多行换行。
                brk = height >= size * 2.2
                if self.translator.lang_out.lower().startswith("de"):
                    brk = False
            cstk: str = ""                              # 当前文字栈
            fcur: str = None                            # 当前字体 ID
            lidx = 0                                    # 记录换行次数
            tx = x
            fcur_ = fcur
            ptr = 0
            log.debug(f"< {y} {x} {x0} {x1} {size} {brk} > {sstk[id]} | {new}")

            ops_vals: list[dict] = []

            while ptr < len(new):
                vy_regex = re.match(
                    r"\{\s*v([\d\s]+)\}", new[ptr:], re.IGNORECASE
                )  # 匹配 {vn} 公式标记
                mod = 0  # 文字修饰符
                if vy_regex:  # 加载公式
                    ptr += len(vy_regex.group(0))
                    try:
                        vid = int(vy_regex.group(1).replace(" ", ""))
                        adv = vlen[vid]
                    except Exception:
                        continue  # 翻译器可能会自动补个越界的公式标记
                    if var[vid][-1].get_text() and unicodedata.category(var[vid][-1].get_text()[0]) in ["Lm", "Mn", "Sk"]:  # 文字修饰符
                        mod = var[vid][-1].width
                else:  # 加载文字
                    ch = new[ptr]
                    fcur_ = None
                    try:
                        if fcur_ is None and self.fontmap["tiro"].to_unichr(ord(ch)) == ch:
                            fcur_ = "tiro"  # 默认拉丁字体
                    except Exception:
                        pass
                    if fcur_ is None:
                        fcur_ = self.noto_name  # 默认非拉丁字体
                    if fcur_ == self.noto_name: # FIXME: change to CONST
                        adv = self.noto.char_lengths(ch, size)[0]
                    else:
                        adv = self.fontmap[fcur_].char_width(ord(ch)) * size
                    ptr += 1
                if (                                # 输出文字缓冲区
                    fcur_ != fcur                   # 1. 字体更新
                    or vy_regex                     # 2. 插入公式
                    or x + adv > x1 + 0.1 * size    # 3. 到达右边界（可能一整行都被符号化，这里需要考虑浮点误差）
                ):
                    if cstk:
                        ops_vals.append({
                            "type": OpType.TEXT,
                            "font": fcur,
                            "size": size,
                            "x": tx,
                            "end_x": x,
                            "dy": 0,
                            "rtxt": raw_string(fcur, cstk),
                            "lidx": lidx
                        })
                        cstk = ""
                if brk and x + adv > x1 + 0.1 * size:  # 到达右边界且原文段落存在换行
                    x = x0
                    lidx += 1
                if vy_regex:  # 插入公式
                    fix = 0
                    if fcur is not None:  # 段落内公式修正纵向偏移
                        fix = varf[vid]
                    for vch in var[vid]:  # 排版公式字符
                        vc = chr(vch.cid)
                        ops_vals.append({
                            "type": OpType.TEXT,
                            "font": self.fontid[vch.font],
                            "size": vch.size,
                            "x": x + vch.x0 - var[vid][0].x0,
                            "end_x": x + vch.x1 - var[vid][0].x0,
                            "dy": fix + vch.y0 - var[vid][0].y0,
                            "rtxt": raw_string(self.fontid[vch.font], vc),
                            "lidx": lidx
                        })
                        if log.isEnabledFor(logging.DEBUG):
                            lstk.append(LTLine(0.1, (_x, _y), (x + vch.x0 - var[vid][0].x0, fix + y + vch.y0 - var[vid][0].y0)))
                            _x, _y = x + vch.x0 - var[vid][0].x0, fix + y + vch.y0 - var[vid][0].y0
                    for l in varl[vid]:  # 排版公式线条
                        if l.linewidth < 5:  # hack 有的文档会用粗线条当图片背景
                            ops_vals.append({
                                "type": OpType.LINE,
                                "x": l.pts[0][0] + x - var[vid][0].x0,
                                "dy": l.pts[0][1] + fix - var[vid][0].y0,
                                "linewidth": l.linewidth,
                                "xlen": l.pts[1][0] - l.pts[0][0],
                                "ylen": l.pts[1][1] - l.pts[0][1],
                                "lidx": lidx
                            })
                else:  # 插入文字缓冲区
                    if not cstk:  # 单行开头
                        tx = x
                        if x == x0 and ch == " ":  # 消除段落换行空格
                            adv = 0
                        else:
                            cstk += ch
                    else:
                        cstk += ch
                adv -= mod # 文字修饰符
                fcur = fcur_
                x += adv
                if log.isEnabledFor(logging.DEBUG):
                    lstk.append(LTLine(0.1, (_x, _y), (x, y)))
                    _x, _y = x, y
            # 处理结尾
            if cstk:
                ops_vals.append({
                    "type": OpType.TEXT,
                    "font": fcur,
                    "size": size,
                    "x": tx,
                    "end_x": x,
                    "dy": 0,
                    "rtxt": raw_string(fcur, cstk),
                    "lidx": lidx
                })

            line_height = min(default_line_height, 1.15) if is_table_cell else default_line_height

            while (lidx + 1) * size * line_height > height and line_height > 1:
                line_height = max(1, line_height - 0.05)

            render_size = size
            size_scale = 1.0
            if is_table_cell and (lidx + 1) * render_size * line_height > height:
                render_size = max(3.5, height / max((lidx + 1) * line_height, 1))
                size_scale = render_size / size if size else 1.0
                if (lidx + 1) * render_size * line_height > height:
                    line_height = max(0.75, height / max((lidx + 1) * render_size, 1))
            if is_table_cell:
                available_width = max(x1 - x0, size * 0.5)
                for line_idx in range(lidx + 1):
                    line_vals = [vals for vals in ops_vals if vals.get("lidx") == line_idx]
                    if not line_vals:
                        continue
                    line_left = min(vals.get("x", x0) for vals in line_vals)
                    line_right = max(vals.get("end_x", vals.get("x", x0)) for vals in line_vals)
                    line_width = max(line_right - line_left, 0)
                    if line_width > available_width:
                        size_scale = min(size_scale, max(0.05, available_width / line_width))
                render_size = size * size_scale
                if render_size > height * 0.9:
                    size_scale = min(size_scale, max(0.05, height * 0.9 / size))
                    render_size = size * size_scale

            line_offsets = {}
            if is_table_cell:
                align = getattr(pstk[id], "align", "left")
                for line_idx in range(lidx + 1):
                    line_vals = [vals for vals in ops_vals if vals.get("lidx") == line_idx]
                    if not line_vals:
                        continue
                    line_left = min(vals.get("x", x0) for vals in line_vals)
                    line_right = max(vals.get("end_x", vals.get("x", x0)) for vals in line_vals)
                    line_width = max(line_right - line_left, 0) * size_scale
                    scaled_left = x0 + (line_left - x0) * size_scale
                    if align == "center":
                        target_left = x0 + max(available_width - line_width, 0) / 2
                    elif align == "right":
                        target_left = x1 - line_width
                    else:
                        target_left = x0
                    line_offsets[line_idx] = target_left - scaled_left

            draw_y = y
            if is_table_cell:
                draw_y = min(draw_y, pstk[id].y1 - render_size * 0.95)
                draw_y = max(draw_y, pstk[id].y0)

            paragraph_ops = []
            for vals in ops_vals:
                if vals["type"] == OpType.TEXT:
                    vals_size = vals["size"] * size_scale if is_table_cell else vals["size"]
                    vals_dy = vals["dy"] * size_scale if is_table_cell else vals["dy"]
                    vals_x = x0 + (vals["x"] - x0) * size_scale if is_table_cell else vals["x"]
                    vals_x += line_offsets.get(vals["lidx"], 0)
                    paragraph_ops.append(gen_op_txt(vals["font"], vals_size, vals_x, vals_dy + draw_y - vals["lidx"] * render_size * line_height, vals["rtxt"]))
                elif vals["type"] == OpType.LINE:
                    vals_dy = vals["dy"] * size_scale if is_table_cell else vals["dy"]
                    vals_x = x0 + (vals["x"] - x0) * size_scale if is_table_cell else vals["x"]
                    vals_x += line_offsets.get(vals["lidx"], 0)
                    vals_xlen = vals["xlen"] * size_scale if is_table_cell else vals["xlen"]
                    vals_ylen = vals["ylen"] * size_scale if is_table_cell else vals["ylen"]
                    paragraph_ops.append(gen_op_line(vals_x, vals_dy + draw_y - vals["lidx"] * render_size * line_height, vals_xlen, vals_ylen, vals["linewidth"]))

            if is_table_cell and paragraph_ops:
                clip_x = pstk[id].x0
                clip_y = pstk[id].y0
                clip_w = max(pstk[id].x1 - pstk[id].x0, 0.1)
                clip_h = max(pstk[id].y1 - pstk[id].y0, 0.1)
                ops_list.append(f"ET q {clip_x:f} {clip_y:f} {clip_w:f} {clip_h:f} re W n BT ")
                ops_list.extend(paragraph_ops)
                ops_list.append("ET Q BT ")
            else:
                ops_list.extend(paragraph_ops)

        for l in lstk:  # 排版全局线条
            if l.linewidth < 5:  # hack 有的文档会用粗线条当图片背景
                ops_list.append(gen_op_line(l.pts[0][0], l.pts[0][1], l.pts[1][0] - l.pts[0][0], l.pts[1][1] - l.pts[0][1], l.linewidth))

        ops = f"BT {''.join(ops_list)}ET "
        return ops


class OpType(Enum):
    TEXT = "text"
    LINE = "line"
