"""Generate the README Word summary scrolling demo.

The script renders a local HTML preview with Playwright, captures a set of
scroll positions, and stitches them into a GIF with Pillow.
"""

from __future__ import annotations

import argparse
import base64
import html
import math
import mimetypes
import re
import shutil
import tempfile
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - helper script
    raise SystemExit("Missing dependency: pip install pillow") from exc

try:
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover - helper script
    raise SystemExit(
        "Missing dependency: pip install playwright && python -m playwright install chromium"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCX = ROOT / "paper_agent_files" / "2412.10510v4-summary.docx"
DEFAULT_OUTPUT = ROOT / "assets" / "word-demo.gif"
EDGE_PATHS = [
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]
VIEWPORT = {"width": 1200, "height": 760}
CAPTURE_BOX = {"x": 0, "y": 0, "width": 1200, "height": 760}
FALLBACK_PARAGRAPHS = [
    "DEFAME: Dynamic Evidence-based FAct-checking with Multimodal Experts",
    "源文件：2412.10510v4.pdf",
    "生成时间：2026-05-24 12:47:09",
    "核心信息",
    "- 标题: DEFAME: Dynamic Evidence-based FAct-checking with Multimodal Experts",
    "- 中文标题: 基于动态证据的多模态专家事实核查系统 DEFAME",
    "- 作者: Mark Rothermel 等",
    "- 机构: Technical University of Darmstadt；hessian.AI，Germany",
    "- 领域: 多模态事实核查；开放域信息验证；检索增强多模态推理",
    "摘要",
    "错误信息的泛滥要求事实核查系统同时具备可靠性与可扩展性。本文提出 DEFAME，一种模块化、零样本的多模态大模型管线，用于开放域的文本-图像声明验证。",
    "创新点",
    "DEFAME 的第一个关键创新，是把事实核查从“单轮分类”改造成“证据驱动的动态工作流”。论文不是让大模型直接给真假判断，而是通过规划、检索、总结、推理、裁决和解释的多阶段流程逐步收敛结论。",
    "第二个创新，是把多模态证据真正纳入核查闭环。DEFAME 保留原始图像证据，并引入反向搜图、图像检索和地理定位等工具，让图像与文本证据并列参与决策。",
    "一句话总结",
    "这篇论文真正解决的是：如何用零样本多模态大模型结合外部证据工具，做一个能检索、能推理、能解释的开放域事实核查系统。",
    "研究问题",
    "论文聚焦的痛点非常明确：错误信息传播速度快、规模大，而且越来越多地以图文混合形式出现。只做文本事实核查已经无法覆盖真实需求。",
    "方法主线",
    "DEFAME 可以概括为六个阶段：先规划要执行的动作，再调用工具执行检索，然后把检索结果压缩进报告，接着基于报告展开事实推理，再生成最终判定，最后输出可读解释。",
    "阅读建议",
    "适合先阅读核心信息、摘要和方法主线，再根据总结定位原文中的重点章节与关键图表。",
]


def fallback_blocks() -> list[dict[str, object]]:
    return [{"type": "paragraph", "text": text} for text in FALLBACK_PARAGRAPHS]


def qname(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def paragraph_text(paragraph: ET.Element, namespace: dict[str, str]) -> str:
    return "".join(
        node.text or "" for node in paragraph.findall(".//w:t", namespace)
    ).strip()


def parse_relationships(docx: ZipFile) -> dict[str, str]:
    namespace = {
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships"
    }
    try:
        relationships = ET.fromstring(docx.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}

    mapping: dict[str, str] = {}
    for relationship in relationships.findall("rel:Relationship", namespace):
        rel_id = relationship.get("Id")
        target = relationship.get("Target")
        if rel_id and target:
            mapping[rel_id] = "word/" + target.lstrip("/")
    return mapping


def image_data_url(docx: ZipFile, media_path: str) -> str:
    data = docx.read(media_path)
    mime_type = mimetypes.guess_type(media_path)[0] or "image/png"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def drawing_alt_text(drawing: ET.Element, namespace: dict[str, str]) -> str:
    doc_properties = drawing.find(".//wp:docPr", namespace)
    if doc_properties is None:
        return "论文图表截图"
    return doc_properties.get("descr") or doc_properties.get("name") or "论文图表截图"


def table_rows(table: ET.Element, namespace: dict[str, str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.findall(".//w:tr", namespace):
        cells: list[str] = []
        for cell in row.findall("./w:tc", namespace):
            text = " ".join(
                paragraph_text(paragraph, namespace)
                for paragraph in cell.findall("./w:p", namespace)
            ).strip()
            cells.append(text)
        if any(cells):
            rows.append(cells)
    return rows


def extract_docx_blocks(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return fallback_blocks()

    with ZipFile(path) as docx:
        namespace = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        }
        relationships = parse_relationships(docx)
        document = docx.read("word/document.xml")
        root = ET.fromstring(document)
        body = root.find("w:body", namespace)
        if body is None:
            return fallback_blocks()

        blocks: list[dict[str, object]] = []
        for child in body:
            if child.tag == qname(namespace["w"], "p"):
                text = paragraph_text(child, namespace)
                if text:
                    blocks.append({"type": "paragraph", "text": text})
                for drawing in child.findall(".//w:drawing", namespace):
                    blip = drawing.find(".//a:blip", namespace)
                    if blip is None:
                        continue
                    rel_id = blip.get(qname(namespace["r"], "embed"))
                    media_path = relationships.get(rel_id or "")
                    if not media_path:
                        continue
                    blocks.append(
                        {
                            "type": "image",
                            "src": image_data_url(docx, media_path),
                            "alt": drawing_alt_text(drawing, namespace),
                        }
                    )
            elif child.tag == qname(namespace["w"], "tbl"):
                rows = table_rows(child, namespace)
                if rows:
                    blocks.append({"type": "table", "rows": rows})

    return blocks or fallback_blocks()


def classify_paragraph(text: str, index: int) -> str:
    if index == 0:
        return "title"
    if re.match(r"^(核心信息|摘要|创新点|一句话总结|研究问题|数据与任务定义|方法主线|机制流程|关键公式|实验结果|结论|阅读建议)$", text):
        return "heading"
    if text.startswith("- "):
        return "bullet"
    if re.match(r"^如图\d+所示", text):
        return "caption"
    return "paragraph"


def paragraph_html(text: str, index: int) -> str:
    kind = classify_paragraph(text, index)
    escaped = html.escape(text)
    if kind == "title":
        return f"<h1>{escaped}</h1>"
    if kind == "heading":
        return f"<h2>{escaped}</h2>"
    if kind == "bullet":
        return f"<p class='bullet'><span></span>{html.escape(text[2:])}</p>"
    if kind == "caption":
        return f"<p class='caption'>{escaped}</p>"
    return f"<p>{escaped}</p>"


def table_html(rows: list[list[str]]) -> str:
    row_markup = []
    for index, row in enumerate(rows):
        cell_tag = "th" if index == 0 else "td"
        cells = "".join(f"<{cell_tag}>{html.escape(cell)}</{cell_tag}>" for cell in row)
        row_markup.append(f"<tr>{cells}</tr>")
    return f"<table>{''.join(row_markup)}</table>"


def block_html(block: dict[str, object], paragraph_index: int) -> str:
    block_type = block["type"]
    if block_type == "paragraph":
        return paragraph_html(str(block["text"]), paragraph_index)
    if block_type == "image":
        src = str(block["src"])
        alt = html.escape(str(block.get("alt") or "论文图表截图"))
        return f"<figure class='doc-image'><img src='{src}' alt='{alt}'></figure>"
    if block_type == "table":
        rows = block.get("rows")
        if isinstance(rows, list):
            return table_html(rows)
    return ""


def build_html(blocks: list[dict[str, object]]) -> str:
    rendered_blocks: list[str] = []
    paragraph_index = 0
    image_count = 0
    table_count = 0
    for block in blocks:
        if block["type"] == "image":
            image_count += 1
        if block["type"] == "table":
            table_count += 1
        rendered_blocks.append(block_html(block, paragraph_index))
        if block["type"] == "paragraph":
            paragraph_index += 1

    if image_count == 0 and table_count == 0:
        rendered_blocks.append(
            "<div class='figure'>关键图表与中文解读会保留在 Word 文档中</div>"
        )

    body = "\n".join(rendered_blocks)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PaperAgent Word Demo</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: #e9eef5;
      color: #1f2937;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
    }}
    .app {{
      width: 1200px;
      height: 760px;
      overflow: hidden;
      background:
        linear-gradient(180deg, #f8fafc 0 82px, transparent 82px),
        #e9eef5;
      position: relative;
    }}
    .topbar {{
      height: 70px;
      padding: 14px 30px;
      display: flex;
      align-items: center;
      gap: 16px;
      background: #fff;
      border-bottom: 1px solid #d7dde8;
      box-shadow: 0 8px 22px rgba(31, 41, 55, .08);
    }}
    .word-icon {{
      width: 42px;
      height: 42px;
      border-radius: 8px;
      background: #185abd;
      color: #fff;
      display: grid;
      place-items: center;
      font-weight: 800;
      font-size: 24px;
      box-shadow: inset -8px 0 0 rgba(0, 0, 0, .12);
    }}
    .file-title {{
      font-size: 18px;
      font-weight: 700;
      color: #1d2939;
    }}
    .file-subtitle {{
      margin-top: 4px;
      font-size: 13px;
      color: #667085;
    }}
    .actions {{
      margin-left: auto;
      display: flex;
      gap: 10px;
      align-items: center;
    }}
    .pill {{
      height: 32px;
      padding: 0 13px;
      border-radius: 8px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      font-weight: 700;
      border: 1px solid #c7d7fe;
      color: #155eef;
      background: #eff6ff;
    }}
    .dot {{
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: #12b76a;
    }}
    .workspace {{
      height: 690px;
      padding: 28px 0 64px;
      overflow: hidden;
      position: relative;
    }}
    .page {{
      width: 800px;
      min-height: 1320px;
      margin: 0 auto;
      padding: 70px 76px 90px;
      background: #fff;
      border: 1px solid #d0d5dd;
      box-shadow: 0 22px 46px rgba(31, 41, 55, .18);
      transform: translateY(calc(var(--scroll, 0) * -1px));
      will-change: transform;
    }}
    h1 {{
      margin: 0 0 18px;
      color: #101828;
      font-size: 27px;
      line-height: 1.35;
      letter-spacing: 0;
    }}
    h2 {{
      margin: 28px 0 12px;
      padding-bottom: 7px;
      color: #155eef;
      border-bottom: 2px solid #dbeafe;
      font-size: 19px;
      line-height: 1.35;
      letter-spacing: 0;
    }}
    p {{
      margin: 0 0 13px;
      color: #344054;
      font-size: 16px;
      line-height: 1.78;
      text-align: justify;
    }}
    .bullet {{
      display: flex;
      gap: 10px;
      text-align: left;
    }}
    .bullet span {{
      width: 6px;
      height: 6px;
      margin-top: 12px;
      flex: 0 0 6px;
      border-radius: 999px;
      background: #155eef;
    }}
    .caption {{
      color: #667085;
      font-size: 14px;
      text-align: center;
    }}
    .figure {{
      margin: 18px 0 22px;
      height: 150px;
      border: 1px solid #d7dde8;
      border-radius: 8px;
      background:
        linear-gradient(135deg, rgba(21, 94, 239, .13), transparent 45%),
        linear-gradient(90deg, #f8fafc, #eef4ff);
      display: grid;
      place-items: center;
      color: #475467;
      font-size: 14px;
      font-weight: 700;
    }}
    .doc-image {{
      margin: 18px 0 22px;
      padding: 12px;
      border: 1px solid #d7dde8;
      border-radius: 8px;
      background: #f8fafc;
      text-align: center;
    }}
    .doc-image img {{
      display: block;
      max-width: 100%;
      max-height: 340px;
      margin: 0 auto;
      object-fit: contain;
      border-radius: 4px;
    }}
    table {{
      width: 100%;
      margin: 18px 0 22px;
      border-collapse: collapse;
      color: #344054;
      font-size: 14px;
      line-height: 1.55;
    }}
    th, td {{
      padding: 9px 11px;
      border: 1px solid #d0d5dd;
      vertical-align: top;
      text-align: left;
    }}
    th {{
      color: #1d2939;
      background: #eff6ff;
      font-weight: 800;
    }}
    .footer-shadow {{
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      height: 110px;
      background: linear-gradient(180deg, rgba(233, 238, 245, 0), #e9eef5 70%);
      pointer-events: none;
    }}
    .scrollbar {{
      position: absolute;
      top: 108px;
      right: 38px;
      width: 9px;
      height: 560px;
      border-radius: 999px;
      background: #d7dde8;
      overflow: hidden;
    }}
    .thumb {{
      width: 100%;
      height: 134px;
      border-radius: inherit;
      background: #155eef;
      transform: translateY(calc(var(--thumb, 0) * 1px));
    }}
  </style>
</head>
<body>
  <main class="app" id="app">
    <header class="topbar">
      <div class="word-icon">W</div>
      <div>
        <div class="file-title">2412.10510v4-summary.docx</div>
        <div class="file-subtitle">PaperAgent 生成的可编辑论文总结文档</div>
      </div>
      <div class="actions">
        <div class="pill"><span class="dot"></span>已生成</div>
        <div class="pill">下载 Word</div>
      </div>
    </header>
    <section class="workspace">
      <article class="page" id="page">
        {body}
      </article>
      <div class="scrollbar"><div class="thumb"></div></div>
      <div class="footer-shadow"></div>
    </section>
  </main>
</body>
</html>
"""


def eased_positions(max_scroll: int, frame_count: int) -> list[int]:
    positions: list[int] = []
    for index in range(frame_count):
        t = index / (frame_count - 1)
        eased = 0.5 - math.cos(t * math.pi) / 2
        positions.append(round(max_scroll * eased))
    return positions


def render_frames(html_path: Path, frame_dir: Path, frame_count: int) -> list[Path]:
    frames: list[Path] = []
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception:
            edge_path = next((path for path in EDGE_PATHS if path.exists()), None)
            if edge_path is None:
                raise
            browser = playwright.chromium.launch(executable_path=str(edge_path))
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        page.goto(html_path.as_uri(), wait_until="networkidle")
        page.evaluate(
            """async () => {
                const images = Array.from(document.images);
                await Promise.all(images.map((image) => {
                    if (image.complete) return Promise.resolve();
                    if (image.decode) return image.decode().catch(() => undefined);
                    return new Promise((resolve) => {
                        image.onload = resolve;
                        image.onerror = resolve;
                    });
                }));
            }"""
        )
        max_scroll = int(
            page.evaluate(
                """() => {
                    const sheet = document.querySelector("#page");
                    return Math.max(0, sheet.offsetHeight - 600);
                }"""
            )
        )
        max_thumb = 560 - 134
        for index, scroll in enumerate(eased_positions(max_scroll, frame_count)):
            thumb = 0 if max_scroll == 0 else round((scroll / max_scroll) * max_thumb)
            page.evaluate(
                """({scroll, thumb}) => {
                    document.documentElement.style.setProperty("--scroll", scroll);
                    document.documentElement.style.setProperty("--thumb", thumb);
                }""",
                {"scroll": scroll, "thumb": thumb},
            )
            page.wait_for_timeout(35)
            frame_path = frame_dir / f"frame-{index:03d}.png"
            page.screenshot(path=frame_path, clip=CAPTURE_BOX)
            frames.append(frame_path)
        browser.close()
    return frames


def save_gif(frames: list[Path], output: Path, duration_ms: int) -> None:
    images = [Image.open(frame).convert("P", palette=Image.Palette.ADAPTIVE) for frame in frames]
    output.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        output,
        save_all=True,
        append_images=images[1:],
        duration=duration_ms,
        loop=0,
        optimize=True,
        disposal=2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docx", type=Path, default=DEFAULT_DOCX)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--frames", type=int, default=46)
    parser.add_argument("--duration", type=int, default=80)
    parser.add_argument("--keep-html", action="store_true")
    args = parser.parse_args()

    blocks = extract_docx_blocks(args.docx)
    html_text = build_html(blocks)

    with tempfile.TemporaryDirectory(prefix="paper-agent-word-demo-") as temp:
        temp_dir = Path(temp)
        html_path = temp_dir / "word-demo.html"
        frame_dir = temp_dir / "frames"
        frame_dir.mkdir()
        html_path.write_text(html_text, encoding="utf-8")

        frames = render_frames(html_path, frame_dir, args.frames)
        save_gif(frames, args.output, args.duration)

        if args.keep_html:
            target_html = args.output.with_suffix(".html")
            shutil.copy2(html_path, target_html)
            print(f"HTML preview written to {target_html}")

    print(f"GIF written to {args.output}")


if __name__ == "__main__":
    main()
