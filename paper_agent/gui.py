import asyncio
import cgi
import os
import shutil
import socket
import uuid
from asyncio import CancelledError
from pathlib import Path

import gradio as gr
import requests
from gradio_pdf import PDF
import logging

from paper_agent import __version__
from paper_agent.config import ConfigManager
from paper_agent.paper_summary import DEFAULT_MAX_ASSETS, summarize_paper

logger = logging.getLogger(__name__)

# The following variable associate strings with page ranges
page_map = {
    "All": None,
    "First": [0],
    "First 5 pages": list(range(0, 5)),
    "Others": None,
}

# Check if this is a public demo, which has resource limits
flag_demo = False

# Limit resources
if ConfigManager.get("PAPER_AGENT_DEMO"):
    flag_demo = True
    page_map = {
        "First": [0],
        "First 20 pages": list(range(0, 20)),
    }
    client_key = ConfigManager.get("PAPER_AGENT_CLIENT_KEY")
    server_key = ConfigManager.get("PAPER_AGENT_SERVER_KEY")


# Configure about Gradio show keys
hidden_gradio_details: bool = bool(ConfigManager.get("HIDDEN_GRADIO_DETAILS"))


def get_config_or_env(key: str, default: str = "") -> str:
    value = ConfigManager.all().get(key) or os.environ.get(key)
    return str(value) if value else default


# Public demo control
def verify_recaptcha(response):
    """
    This function verifies the reCAPTCHA response.
    """
    recaptcha_url = "https://www.google.com/recaptcha/api/siteverify"
    data = {"secret": server_key, "response": response}
    result = requests.post(recaptcha_url, data=data).json()
    return result.get("success")


def download_with_limit(url: str, save_path: Path, size_limit: int | None) -> str:
    """
    This function downloads a file from a URL and saves it to a specified path.

    Inputs:
        - url: The URL to download the file from
        - save_path: The path to save the file to
        - size_limit: The maximum size of the file to download

    Returns:
        - The path of the downloaded file
    """
    chunk_size = 1024
    total_size = 0
    with requests.get(url, stream=True, timeout=10) as response:
        response.raise_for_status()
        content = response.headers.get("Content-Disposition")
        try:  # filename from header
            _, params = cgi.parse_header(content)
            filename = params["filename"]
        except Exception:  # filename from url
            filename = os.path.basename(url)
        filename = os.path.splitext(os.path.basename(filename))[0] + ".pdf"
        with open(save_path / filename, "wb") as file:
            for chunk in response.iter_content(chunk_size=chunk_size):
                total_size += len(chunk)
                if size_limit and total_size > size_limit:
                    raise gr.Error("Exceeds file size limit")
                file.write(chunk)
    return str(save_path / filename)


def stop_summary_file(state: dict) -> None:
    """
    This function stops the summary process.

    Inputs:
        - state: The state of the summary process

    Returns:- None
    """
    session_id = state["session_id"]
    if session_id is None:
        return
    if session_id in cancellation_event_map:
        logger.info(f"Stopping summary for session {session_id}")
        cancellation_event_map[session_id].set()
        # 清理取消事件，允许下一次总结
        del cancellation_event_map[session_id]
        state["session_id"] = None


def summarize_file(
    file_type,
    file_input,
    link_input,
    page_range,
    page_input,
    max_assets,
    recaptcha_response,
    state,
    progress=gr.Progress(),
):
    session_id = uuid.uuid4()
    state["session_id"] = session_id
    cancellation_event_map[session_id] = asyncio.Event()

    if flag_demo and not verify_recaptcha(recaptcha_response):
        raise gr.Error("reCAPTCHA fail")

    progress(0, desc="Preparing paper...")
    output = Path("paper_agent_files")
    output.mkdir(parents=True, exist_ok=True)

    if file_type == "File":
        if not file_input:
            raise gr.Error("No input")
        file_path = shutil.copy(file_input, output)
    else:
        if not link_input:
            raise gr.Error("No input")
        file_path = download_with_limit(
            link_input,
            output,
            5 * 1024 * 1024 if flag_demo else None,
        )

    if page_range != "Others":
        selected_page = page_map[page_range]
    else:
        selected_page = []
        for p in page_input.split(","):
            p = p.strip()
            if not p:
                continue
            if "-" in p:
                start, end = p.split("-")
                selected_page.extend(range(int(start) - 1, int(end)))
            else:
                selected_page.append(int(p) - 1)

    try:
        max_assets_value = int(max_assets)
    except (TypeError, ValueError):
        max_assets_value = DEFAULT_MAX_ASSETS

    def progress_bar(value: float, desc: str):
        progress(value, desc=desc)

    try:
        docx_path = summarize_paper(
            file_path,
            output,
            pages=selected_page,
            summary_language="中文",
            codex_envs={
                "CODEX_BASE_URL": get_config_or_env("CODEX_BASE_URL"),
                "CODEX_API_KEY": get_config_or_env("CODEX_API_KEY"),
                "CODEX_MODEL": get_config_or_env("CODEX_MODEL"),
            },
            max_assets=max_assets_value,
            progress=progress_bar,
            cancellation_event=cancellation_event_map[session_id],
        )
    except CancelledError:
        raise gr.Error("Summary cancelled")
    finally:
        cancellation_event_map.pop(session_id, None)
        state["session_id"] = None

    preview_path = str(file_path) if str(file_path).lower().endswith(".pdf") else None
    return (
        str(docx_path),
        preview_path,
        gr.update(visible=True),
        gr.update(visible=True),
    )


# Global setup
custom_blue = gr.themes.Color(
    c50="#E8F3FF",
    c100="#BEDAFF",
    c200="#94BFFF",
    c300="#6AA1FF",
    c400="#4080FF",
    c500="#165DFF",  # Primary color
    c600="#0E42D2",
    c700="#0A2BA6",
    c800="#061D79",
    c900="#03114D",
    c950="#020B33",
)

custom_css = """
    .secondary-text {color: #999 !important;}
    footer {visibility: hidden}
    .env-warning {color: #dd5500 !important;}
    .env-success {color: #559900 !important;}

    /* Add dashed border to input-file class */
    .input-file {
        border: 1.2px dashed #165DFF !important;
        border-radius: 6px !important;
    }

    .progress-bar-wrap {
        border-radius: 8px !important;
    }

    .progress-bar {
        border-radius: 8px !important;
    }

    .pdf-canvas canvas {
        width: 100%;
    }

    """

demo_recaptcha = """
    <script src="https://www.google.com/recaptcha/api.js?render=explicit" async defer></script>
    <script type="text/javascript">
        var onVerify = function(token) {
            el=document.getElementById('verify').getElementsByTagName('textarea')[0];
            el.value=token;
            el.dispatchEvent(new Event('input'));
        };
    </script>
    """

tech_details_string = f"""
                    <summary>技术细节</summary>
                    - GUI: 论文总结助手<br>
                    - 版本: {__version__}
                """
cancellation_event_map = {}


# The following code creates the GUI
with gr.Blocks(
    title="论文总结助手",
    theme=gr.themes.Default(
        primary_hue=custom_blue, spacing_size="md", radius_size="lg"
    ),
    css=custom_css,
    head=demo_recaptcha if flag_demo else "",
) as demo:
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("## 文件 | < 5 MB" if flag_demo else "## 文件")
            file_type = gr.Radio(
                choices=[("文件", "File"), ("链接", "Link")],
                label="类型",
                value="File",
            )
            file_input = gr.File(
                label="文件",
                file_count="single",
                file_types=[".pdf", ".doc", ".docx"],
                type="filepath",
                elem_classes=["input-file"],
            )
            link_input = gr.Textbox(
                label="链接",
                visible=False,
                interactive=True,
            )
            gr.Markdown("## 总结选项")
            page_range = gr.Radio(
                choices=[("全部", "All"), ("第一页", "First"), ("前5页", "First 5 pages"), ("其他", "Others")],
                label="页面",
                value="All",
            )

            page_input = gr.Textbox(
                label="页面范围",
                visible=False,
                interactive=True,
            )

            max_assets = gr.Number(
                label="最多写入图表截图数",
                value=DEFAULT_MAX_ASSETS,
                precision=0,
                interactive=True,
            )

            def on_select_filetype(file_type):
                return (
                    gr.update(visible=file_type == "File"),
                    gr.update(visible=file_type == "Link"),
                )

            def on_select_page(choice):
                if choice == "Others":
                    return gr.update(visible=True)
                else:
                    return gr.update(visible=False)

            output_title = gr.Markdown("## 已生成论文总结", visible=False)
            output_file_mono = gr.File(
                label="下载 Word 总结文档", visible=False
            )
            recaptcha_response = gr.Textbox(
                label="reCAPTCHA响应", elem_id="verify", visible=False
            )
            recaptcha_box = gr.HTML('<div id="recaptcha-box"></div>')
            with gr.Row():
                summary_btn = gr.Button("生成论文总结", variant="primary")
                cancellation_btn = gr.Button("取消", variant="secondary")
            gr.Markdown("""
### ⚠️ 使用说明：
- 总结会调用 config.json 中的 CODEX_BASE_URL、CODEX_API_KEY、CODEX_MODEL
- 程序会从 PDF 中抽取正文，并将识别到的图、表和关键公式截图直接写入 Word 文档
- 如果程序运行时间过长或出现错误，请点击 "取消" 按钮，按 F5 刷新页面重新运行
- 如遇到 API 频率限制，系统会自动重试（最多5次，间隔递增）
- 总结完成后可下载 docx 文档
""")
            page_range.select(on_select_page, page_range, page_input)
            file_type.select(
                on_select_filetype,
                file_type,
                [file_input, link_input],
                js=(
                    f"""
                    (a,b)=>{{
                        try{{
                            grecaptcha.render('recaptcha-box',{{
                                'sitekey':'{client_key}',
                                'callback':'onVerify'
                            }});
                        }}catch(error){{}}
                        return [a];
                    }}
                    """
                    if flag_demo
                    else ""
                ),
            )

        with gr.Column(scale=2):
            gr.Markdown("## 预览")
            preview = PDF(label="文档预览", visible=True, height=2000)

    # Event handlers
    file_input.upload(
        lambda x: x,
        inputs=file_input,
        outputs=preview,
        js=(
            f"""
            (a,b)=>{{
                try{{
                    grecaptcha.render('recaptcha-box',{{
                        'sitekey':'{client_key}',
                        'callback':'onVerify'
                    }});
                }}catch(error){{}}
                return [a];
            }}
            """
            if flag_demo
            else ""
        ),
    )

    state = gr.State({"session_id": None})

    summary_btn.click(
        summarize_file,
        inputs=[
            file_type,
            file_input,
            link_input,
            page_range,
            page_input,
            max_assets,
            recaptcha_response,
            state,
        ],
        outputs=[
            output_file_mono,
            preview,
            output_file_mono,
            output_title,
        ],
    ).then(lambda: None, js="()=>{grecaptcha.reset()}" if flag_demo else "")

    cancellation_btn.click(
        stop_summary_file,
        inputs=[state],
    )


def parse_user_passwd(file_path: str) -> tuple:
    """
    Parse the user name and password from the file.

    Inputs:
        - file_path: The file path to read.
    Outputs:
        - tuple_list: The list of tuples of user name and password.
        - content: The content of the file
    """
    tuple_list = []
    content = ""
    if not file_path:
        return tuple_list, content
    if len(file_path) == 2:
        try:
            with open(file_path[1], "r", encoding="utf-8") as file:
                content = file.read()
        except FileNotFoundError:
            print(f"Error: File '{file_path[1]}' not found.")
    try:
        with open(file_path[0], "r", encoding="utf-8") as file:
            tuple_list = [
                tuple(line.strip().split(",")) for line in file if line.strip()
            ]
    except FileNotFoundError:
        print(f"Error: File '{file_path[0]}' not found.")
    return tuple_list, content


def _has_ipv6() -> bool:
    """Check whether the system can bind an IPv6 socket."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.close()
        return True
    except OSError:
        return False


def setup_gui(
    share: bool = False, auth_file: list = ["", ""], server_port=7860
) -> None:
    """
    Setup the GUI with the given parameters.

    Inputs:
        - share: Whether to share the GUI.
        - auth_file: The file path to read the user name and password.

    Outputs:
        - None
    """
    user_list, html = parse_user_passwd(auth_file)

    auth_kwargs = {}
    if len(user_list) > 0:
        auth_kwargs = {"auth": user_list, "auth_message": html}

    if flag_demo:
        demo.launch(server_name="0.0.0.0", max_file_size="5mb", inbrowser=True)
        return

    # Try binding addresses in order: "0.0.0.0" for IPv4, fallback to loopback
    bind_addresses = ["0.0.0.0", "127.0.0.1"]

    for addr in bind_addresses:
        try:
            demo.launch(
                server_name=addr,
                debug=True,
                inbrowser=True,
                share=share,
                server_port=server_port,
                **auth_kwargs,
            )
            return
        except Exception:
            print(
                f"Error launching GUI using {addr}.\n"
                "This may be caused by global mode of proxy software."
            )

    # Last resort: let Gradio create a share link
    demo.launch(
        debug=True,
        inbrowser=True,
        share=True,
        server_port=server_port,
        **auth_kwargs,
    )


# For auto-reloading while developing
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    setup_gui()
