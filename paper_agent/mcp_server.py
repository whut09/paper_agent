from mcp.server import Server
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from paper_agent import translate_stream
from paper_agent.converter_docx import convert_to_pdf, is_convertible
from paper_agent.doclayout import ModelInstance
from pathlib import Path

import contextlib
import io
import os
from paper_agent.youcom_search import youcom_search


def create_mcp_app() -> FastMCP:
    mcp = FastMCP("paper_agent")

    @mcp.tool()
    async def translate_pdf(
        file: str, lang_in: str, lang_out: str, ctx: Context
    ) -> str:
        """
        translate given pdf or word document. Argument `file` is absolute path
        of input pdf/doc/docx, `lang_in` and `lang_out` is translate from and
        to language, and should be like google translate lang_code. `lang_in`
        can be `auto` if you can't determine input language.
        """

        _converted_pdf = None
        if is_convertible(file):
            _converted_pdf = convert_to_pdf(file)
            original_name = os.path.splitext(os.path.basename(file))[0]
            file = _converted_pdf
        else:
            original_name = None

        with open(file, "rb") as f:
            file_bytes = f.read()
        await ctx.log(level="info", message=f"start translate {file}")
        with contextlib.redirect_stdout(io.StringIO()):
            doc_mono_bytes, doc_dual_bytes = translate_stream(
                file_bytes,
                lang_in=lang_in,
                lang_out=lang_out,
                service="google",
                model=ModelInstance.value,
                thread=4,
            )
        await ctx.log(level="info", message="translate complete")
        output_path = Path(os.path.dirname(file))
        filename = original_name or os.path.splitext(os.path.basename(file))[0]
        doc_mono = output_path / f"{filename}-mono.pdf"
        doc_dual = output_path / f"{filename}-dual.pdf"
        with open(doc_mono, "wb") as f:
            f.write(doc_mono_bytes)
        with open(doc_dual, "wb") as f:
            f.write(doc_dual_bytes)
        if _converted_pdf:
            try:
                os.unlink(_converted_pdf)
            except OSError:
                pass
        return f"""------------
    translate complete
    mono pdf file: {doc_mono.absolute()}
    dual pdf file: {doc_dual.absolute()}
    """

    @mcp.tool()
    def search_web(query: str, count: int = 5) -> str:
        """Search the live web with You.com and return concise results."""

        return youcom_search(query, count=count)

    return mcp


def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(request.scope, request.receive, request._send) as (
            read_stream,
            write_stream,
        ):
            await mcp_server.run(
                read_stream, write_stream, mcp_server.create_initialization_options()
            )

    return Starlette(
        debug=debug,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )


if __name__ == "__main__":
    import argparse

    mcp = create_mcp_app()
    mcp_server = mcp._mcp_server
    parser = argparse.ArgumentParser(description="Run MCP SSE-based PAPER_AGENT server")

    parser.add_argument(
        "--sse",
        default=False,
        action="store_true",
        help="Run the server with SSE transport or STDIO",
    )
    parser.add_argument(
        "--host", type=str, default="127.0.0.1", required=False, help="Host to bind"
    )
    parser.add_argument(
        "--port", type=int, default=3001, required=False, help="Port to bind"
    )

    args = parser.parse_args()
    if args.sse and args.host and args.port:
        import uvicorn

        starlette_app = create_starlette_app(mcp_server, debug=True)
        uvicorn.run(starlette_app, host=args.host, port=args.port)
    else:
        mcp.run()
