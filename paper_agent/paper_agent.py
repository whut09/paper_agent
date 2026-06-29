#!/usr/bin/env python3
"""A command line tool for extracting text and images from PDF and
output it to plain text, html, xml or tags.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from string import Template
from typing import List, Optional

from paper_agent import __version__, log
from paper_agent.converter_docx import convert_to_pdf, is_convertible

logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, add_help=True)
    parser.add_argument(
        "files",
        type=str,
        default=None,
        nargs="*",
        help="One or more paths to PDF/Word files.",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"paper_agent v{__version__}",
    )
    parser.add_argument(
        "--debug",
        "-d",
        default=False,
        action="store_true",
        help="Use debug logging level.",
    )
    parse_params = parser.add_argument_group(
        "Parser",
        description="Used during PDF parsing",
    )
    parse_params.add_argument(
        "--pages",
        "-p",
        type=str,
        help="The list of page numbers to parse.",
    )
    parse_params.add_argument(
        "--vfont",
        "-f",
        type=str,
        default="",
        help="The regex to math font name of formula.",
    )
    parse_params.add_argument(
        "--vchar",
        "-c",
        type=str,
        default="",
        help="The regex to math character of formula.",
    )
    parse_params.add_argument(
        "--lang-in",
        "-li",
        type=str,
        default="en",
        help="The code of source language.",
    )
    parse_params.add_argument(
        "--lang-out",
        "-lo",
        type=str,
        default="zh",
        help="The code of target language.",
    )
    parse_params.add_argument(
        "--service",
        "-s",
        type=str,
        default="google",
        help="The service to use for translation.",
    )
    parse_params.add_argument(
        "--output",
        "-o",
        type=str,
        default="",
        help="Output directory for files.",
    )
    parse_params.add_argument(
        "--thread",
        "-t",
        type=int,
        default=4,
        help="The number of threads to execute translation.",
    )
    parse_params.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Interact with GUI.",
    )
    parse_params.add_argument(
        "--share",
        action="store_true",
        help="Enable Gradio Share",
    )
    parse_params.add_argument(
        "--flask",
        action="store_true",
        help="flask",
    )
    parse_params.add_argument(
        "--celery",
        action="store_true",
        help="celery",
    )
    parse_params.add_argument(
        "--authorized",
        type=str,
        nargs="+",
        help="user name and password.",
    )
    parse_params.add_argument(
        "--prompt",
        type=str,
        help="user custom prompt.",
    )

    parse_params.add_argument(
        "--compatible",
        "-cp",
        action="store_true",
        help="Convert the PDF file into PDF/A format to improve compatibility.",
    )

    parse_params.add_argument(
        "--onnx",
        type=str,
        help="custom onnx model path.",
    )

    parse_params.add_argument(
        "--backend",
        type=str,
        choices=["auto", "cpu", "cuda", "dml"],
        default="auto",
        help="ONNX Runtime execution provider: auto, cpu, cuda, dml.",
    )

    parse_params.add_argument(
        "--serverport",
        type=int,
        help="custom WebUI port.",
    )

    parse_params.add_argument(
        "--dir",
        action="store_true",
        help="translate directory.",
    )

    parse_params.add_argument(
        "--config",
        type=str,
        help="config file.",
    )

    parse_params.add_argument(
        "--mode",
        type=str,
        choices=["fast", "precise"],
        default="fast",
        help="Translation mode: fast (v1) or precise (v2, requires paper_agent_next).",
    )

    parse_params.add_argument(
        "--babeldoc",
        default=False,
        action="store_true",
        help="Use experimental backend babeldoc.",
    )

    parse_params.add_argument(
        "--skip-subset-fonts",
        action="store_true",
        help="Skip font subsetting. "
        "This option can improve compatibility "
        "but will increase the size of the output file.",
    )

    parse_params.add_argument(
        "--ignore-cache",
        action="store_true",
        help="Ignore cache and force retranslation.",
    )

    parse_params.add_argument(
        "--mcp", action="store_true", help="Launch paper_agent MCP server in STDIO mode"
    )

    parse_params.add_argument(
        "--sse", action="store_true", help="Launch paper_agent MCP server in SSE mode"
    )

    return parser


def parse_args(args: Optional[List[str]]) -> argparse.Namespace:
    raw_args = sys.argv[1:] if args is None else args
    if raw_args and raw_args[0] == "summarize":
        summarize_parser = argparse.ArgumentParser(description="Generate a grounded PaperAgent Word summary.")
        summarize_parser.add_argument("command", choices=["summarize"])
        summarize_parser.add_argument("file", help="Path to a PDF, DOC, or DOCX paper.")
        summarize_parser.add_argument("--output", "-o", default="paper_agent_files", help="Output directory.")
        summarize_parser.add_argument("--config", type=str, help="config file.")
        summarize_parser.add_argument("--pages", "-p", type=str, help="The list of page numbers to summarize.")
        summarize_parser.add_argument("--summary-language", default="中文", help="Summary language.")
        summarize_parser.add_argument("--max-assets", type=int, default=None, help="Maximum number of figure/table/formula assets.")
        parsed = summarize_parser.parse_args(args=raw_args)
        if parsed.pages:
            parsed.raw_pages = parsed.pages
            parsed.pages = _parse_page_ranges(parsed.pages)
        else:
            parsed.raw_pages = ""
            parsed.pages = None
        return parsed
    if raw_args and raw_args[0] == "eval":
        eval_parser = argparse.ArgumentParser(description="Run PaperAgent evaluation harness.")
        eval_parser.add_argument("command", choices=["eval"])
        eval_parser.add_argument("--cases", default="evaluation/golden_cases")
        return eval_parser.parse_args(args=raw_args)
    if raw_args and raw_args[0] == "memory":
        memory_parser = argparse.ArgumentParser(description="Manage PaperAgent correction memory.")
        memory_parser.add_argument("command", choices=["memory"])
        subparsers = memory_parser.add_subparsers(dest="memory_action", required=True)
        list_parser = subparsers.add_parser("list")
        list_parser.add_argument("--memory-path", default=None)
        list_parser.add_argument("--active-only", action="store_true")
        disable_parser = subparsers.add_parser("disable")
        disable_parser.add_argument("index", type=int)
        disable_parser.add_argument("--memory-path", default=None)
        promote_parser = subparsers.add_parser("promote")
        promote_parser.add_argument("index", type=int)
        promote_parser.add_argument("--scope", choices=["domain", "global"], default="domain")
        promote_parser.add_argument("--evaluation-passed", action="store_true")
        promote_parser.add_argument("--memory-path", default=None)
        return memory_parser.parse_args(args=raw_args)

    parsed_args = create_parser().parse_args(args=raw_args)

    if parsed_args.pages:
        parsed_args.raw_pages = parsed_args.pages
        parsed_args.pages = _parse_page_ranges(parsed_args.pages)

    return parsed_args


def _parse_page_ranges(value: str) -> list[int]:
    pages = []
    for p in value.split(","):
        p = p.strip()
        if not p:
            continue
        if "-" in p:
            start, end = p.split("-")
            pages.extend(range(int(start) - 1, int(end)))
        else:
            pages.append(int(p) - 1)
    return pages


def find_all_files_in_directory(directory_path):
    """
    Recursively search all PDF files in the given directory and return their paths as a list.

    :param directory_path: str, the path to the directory to search
    :return: list of PDF file paths
    """
    # Check if the provided path is a directory
    if not os.path.isdir(directory_path):
        raise ValueError(f"The provided path '{directory_path}' is not a directory.")

    file_paths = []

    # Walk through the directory recursively
    for root, _, files in os.walk(directory_path):
        for file in files:
            # Check if the file is a PDF
            if file.lower().endswith((".pdf", ".doc", ".docx")):
                # Append the full file path to the list
                file_paths.append(os.path.join(root, file))

    return file_paths


def main(args: Optional[List[str]] = None) -> int:
    parsed_args = parse_args(args)

    from rich.logging import RichHandler

    logging.basicConfig(level=logging.INFO, handlers=[RichHandler()])

    # disable httpx, openai, httpcore, http11 logs
    logging.getLogger("httpx").setLevel("CRITICAL")
    logging.getLogger("httpx").propagate = False
    logging.getLogger("openai").setLevel("CRITICAL")
    logging.getLogger("openai").propagate = False
    logging.getLogger("httpcore").setLevel("CRITICAL")
    logging.getLogger("httpcore").propagate = False
    logging.getLogger("http11").setLevel("CRITICAL")
    logging.getLogger("http11").propagate = False

    if getattr(parsed_args, "config", None):
        from paper_agent.config import ConfigManager

        ConfigManager.custome_config(parsed_args.config)

    if getattr(parsed_args, "debug", False):
        log.setLevel(logging.DEBUG)

    if getattr(parsed_args, "command", "") == "eval":
        from paper_agent.evaluation.runner import main as eval_main

        return eval_main(["--cases", parsed_args.cases])

    if getattr(parsed_args, "command", "") == "memory":
        return _memory_cli(parsed_args)

    if getattr(parsed_args, "command", "") == "summarize":
        from paper_agent.config import ConfigManager
        from paper_agent.harness.policy import DEFAULT_MAX_ASSETS
        from paper_agent.harness.workflow import summarize_paper

        output_path = summarize_paper(
            parsed_args.file,
            parsed_args.output,
            pages=parsed_args.pages,
            summary_language=parsed_args.summary_language,
            codex_envs={
                "CODEX_BASE_URL": str(ConfigManager.get("CODEX_BASE_URL", "")),
                "CODEX_API_KEY": str(ConfigManager.get("CODEX_API_KEY", "")),
                "CODEX_MODEL": str(ConfigManager.get("CODEX_MODEL", "")),
                "CODEX_USE_PROXY": str(ConfigManager.get("CODEX_USE_PROXY", "")),
            },
            max_assets=parsed_args.max_assets or DEFAULT_MAX_ASSETS,
        )
        print(output_path)
        return 0

    if parsed_args.interactive:
        from paper_agent.gui import setup_gui

        if parsed_args.serverport:
            setup_gui(
                parsed_args.share, parsed_args.authorized, int(parsed_args.serverport)
            )
        else:
            setup_gui(parsed_args.share, parsed_args.authorized)
        return 0

    from paper_agent.doclayout import ModelInstance, OnnxModel, set_backend

    set_backend(parsed_args.backend)

    if parsed_args.onnx:
        ModelInstance.value = OnnxModel(parsed_args.onnx)
    else:
        ModelInstance.value = OnnxModel.load_available()

    if parsed_args.flask:
        from paper_agent.backend import flask_app

        flask_app.run(port=11008)
        return 0

    if parsed_args.celery:
        from paper_agent.backend import celery_app

        celery_app.start(argv=sys.argv[2:])
        return 0

    if parsed_args.prompt:
        try:
            with open(parsed_args.prompt, "r", encoding="utf-8") as file:
                content = file.read()
            parsed_args.prompt = Template(content)
        except Exception:
            raise ValueError("prompt error.")

    if parsed_args.mcp:
        logging.getLogger("mcp").setLevel(logging.ERROR)
        from paper_agent.mcp_server import create_mcp_app, create_starlette_app

        mcp = create_mcp_app()
        if parsed_args.sse:
            import uvicorn

            starlette_app = create_starlette_app(mcp._mcp_server)
            uvicorn.run(starlette_app)
            return 0
        mcp.run()
        return 0

    print(parsed_args)

    if parsed_args.babeldoc:
        return yadt_main(parsed_args)

    # Unified kernel routing — both fast and precise modes go through the registry
    from paper_agent.kernel import KernelRegistry
    from paper_agent.kernel.protocol import TranslateRequest

    KernelRegistry.switch(parsed_args.mode)  # "fast" or "precise"
    kernel = KernelRegistry.get()

    if parsed_args.dir:
        parsed_args.files = find_all_files_in_directory(parsed_args.files[0])

    # Extract prompt text (may be a Template object from file reading above)
    prompt_text = None
    if parsed_args.prompt:
        prompt_text = (
            parsed_args.prompt.template
            if hasattr(parsed_args.prompt, "template")
            else parsed_args.prompt
        )

    request = TranslateRequest(
        files=parsed_args.files,
        output=parsed_args.output,
        pages=parsed_args.pages,
        lang_in=parsed_args.lang_in,
        lang_out=parsed_args.lang_out,
        service=parsed_args.service,
        thread=parsed_args.thread,
        vfont=parsed_args.vfont,
        vchar=parsed_args.vchar,
        envs={},
        prompt=prompt_text,
        skip_subset_fonts=parsed_args.skip_subset_fonts,
        ignore_cache=parsed_args.ignore_cache,
        compatible=parsed_args.compatible,
        debug=parsed_args.debug,
    )
    kernel.translate(request)
    return 0


def yadt_main(parsed_args) -> int:
    from babeldoc.high_level import async_translate as yadt_translate
    from babeldoc.high_level import init as yadt_init
    from babeldoc.main import create_progress_handler
    from babeldoc.translation_config import TranslationConfig as YadtConfig
    from paper_agent.high_level import download_remote_fonts

    if parsed_args.dir:
        untranlate_file = find_all_files_in_directory(parsed_args.files[0])
    else:
        untranlate_file = parsed_args.files
    lang_in = parsed_args.lang_in
    lang_out = parsed_args.lang_out
    ignore_cache = parsed_args.ignore_cache
    outputdir = None
    if parsed_args.output:
        outputdir = parsed_args.output

    # yadt require init before translate
    yadt_init()
    font_path = download_remote_fonts(lang_out.lower())

    param = parsed_args.service.split(":", 1)
    service_name = param[0]
    service_model = param[1] if len(param) > 1 else None

    envs = {}
    prompt = []

    if parsed_args.prompt:
        try:
            with open(parsed_args.prompt, "r", encoding="utf-8") as file:
                content = file.read()
            prompt = Template(content)
        except Exception:
            raise ValueError("prompt error.")

    from paper_agent.translator import (
        AzureOpenAITranslator,
        GoogleTranslator,
        BingTranslator,
        DeepLTranslator,
        DeepLXTranslator,
        OllamaTranslator,
        OpenAITranslator,
        ZhipuTranslator,
        ModelScopeTranslator,
        SiliconTranslator,
        GeminiTranslator,
        AzureTranslator,
        TencentTranslator,
        DifyTranslator,
        AnythingLLMTranslator,
        XinferenceTranslator,
        ArgosTranslator,
        GrokTranslator,
        GroqTranslator,
        DeepseekTranslator,
        OpenAIlikedTranslator,
        QwenMtTranslator,
        X302AITranslator,
    )

    for translator in [
        GoogleTranslator,
        BingTranslator,
        DeepLTranslator,
        DeepLXTranslator,
        OllamaTranslator,
        XinferenceTranslator,
        AzureOpenAITranslator,
        OpenAITranslator,
        ZhipuTranslator,
        ModelScopeTranslator,
        SiliconTranslator,
        GeminiTranslator,
        AzureTranslator,
        TencentTranslator,
        DifyTranslator,
        AnythingLLMTranslator,
        ArgosTranslator,
        GrokTranslator,
        GroqTranslator,
        DeepseekTranslator,
        OpenAIlikedTranslator,
        QwenMtTranslator,
        X302AITranslator,
    ]:
        if service_name == translator.name:
            translator = translator(
                lang_in,
                lang_out,
                service_model,
                envs=envs,
                prompt=prompt,
                ignore_cache=ignore_cache,
            )
            break
    else:
        raise ValueError("Unsupported translation service")
    import asyncio

    for file in untranlate_file:
        file = file.strip("\"'")
        _converted_pdf = None
        if is_convertible(file):
            _converted_pdf = convert_to_pdf(file)
            file = _converted_pdf
        yadt_config = YadtConfig(
            input_file=file,
            font=font_path,
            pages=",".join((str(x) for x in getattr(parsed_args, "raw_pages", []))),
            output_dir=outputdir,
            doc_layout_model=None,
            translator=translator,
            debug=parsed_args.debug,
            lang_in=lang_in,
            lang_out=lang_out,
            no_dual=False,
            no_mono=False,
            qps=parsed_args.thread,
        )

        async def yadt_translate_coro(yadt_config):
            progress_context, progress_handler = create_progress_handler(yadt_config)
            # 开始翻译
            with progress_context:
                async for event in yadt_translate(yadt_config):
                    progress_handler(event)
                    if yadt_config.debug:
                        logger.debug(event)
                    if event["type"] == "finish":
                        result = event["translate_result"]
                        logger.info("Translation Result:")
                        logger.info(f"  Original PDF: {result.original_pdf_path}")
                        logger.info(f"  Time Cost: {result.total_seconds:.2f}s")
                        logger.info(f"  Mono PDF: {result.mono_pdf_path or 'None'}")
                        logger.info(f"  Dual PDF: {result.dual_pdf_path or 'None'}")
                        break

        asyncio.run(yadt_translate_coro(yadt_config))
        if _converted_pdf:
            try:
                os.unlink(_converted_pdf)
            except OSError:
                pass
    return 0


def _memory_cli(parsed_args: argparse.Namespace) -> int:
    import json

    from paper_agent.memory import (
        disable_correction_memory,
        list_correction_memories,
        promote_correction_memory,
    )

    if parsed_args.memory_action == "list":
        rows = list_correction_memories(
            memory_path=parsed_args.memory_path,
            include_disabled=not parsed_args.active_only,
        )
        print(json.dumps({"memories": rows}, ensure_ascii=False, indent=2))
        return 0
    if parsed_args.memory_action == "disable":
        path = disable_correction_memory(parsed_args.index, memory_path=parsed_args.memory_path)
        print(json.dumps({"state": "disabled", "path": str(path)}, ensure_ascii=False))
        return 0
    if parsed_args.memory_action == "promote":
        path = promote_correction_memory(
            parsed_args.index,
            parsed_args.scope,
            memory_path=parsed_args.memory_path,
            evaluation_passed=parsed_args.evaluation_passed,
        )
        print(json.dumps({"state": "promoted", "path": str(path), "scope": parsed_args.scope}, ensure_ascii=False))
        return 0
    raise ValueError(f"Unsupported memory action: {parsed_args.memory_action}")


if __name__ == "__main__":
    sys.exit(main())
