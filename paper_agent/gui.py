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
        gr.update(value=word_result_html(str(docx_path)), visible=True),
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

    .pa-flow {
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 18px;
        margin: 10px 0 14px;
        border: 1px solid #d7dee9;
        border-radius: 12px;
        background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
        box-shadow: 0 8px 26px rgba(16, 24, 40, 0.06);
    }

    .pa-step {
        flex: 1;
        min-width: 0;
        padding: 14px;
        border-radius: 10px;
        background: #ffffff;
        border: 1px solid #e4e7ec;
        text-align: center;
    }

    .pa-icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 58px;
        height: 58px;
        margin-bottom: 10px;
        border-radius: 14px;
        color: #ffffff;
        font-weight: 800;
        background: #165dff;
    }

    .pa-step-title {
        color: #182230;
        font-weight: 700;
        font-size: 15px;
    }

    .pa-step-note {
        color: #667085;
        font-size: 12px;
        margin-top: 4px;
    }

    .pa-arrow {
        width: 54px;
        height: 3px;
        border-radius: 999px;
        background: linear-gradient(90deg, #165dff 0%, #12b76a 100%);
        position: relative;
        overflow: hidden;
    }

    .pa-arrow::after {
        content: "";
        position: absolute;
        inset: 0;
        width: 24px;
        background: rgba(255, 255, 255, .75);
        animation: paMove 1.2s infinite;
    }

    .pa-running .pa-step {
        animation: paPulse 1.8s infinite;
    }

    .pa-running .pa-parse { animation-delay: .25s; }
    .pa-running .pa-word { animation-delay: .5s; }

    .pa-word-preview {
        width: 190px;
        min-height: 150px;
        border-radius: 12px;
        border: 1px solid #b7e4c7;
        background: #ffffff;
        padding: 14px;
        position: relative;
        box-shadow: inset 0 0 0 4px #ecfdf3;
    }

    .pa-word-head {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 16px;
    }

    .pa-word-badge {
        padding: 4px 7px;
        border-radius: 6px;
        background: #12b76a;
        color: white;
        font-weight: 800;
        font-size: 11px;
    }

    .pa-word-name {
        color: #344054;
        font-weight: 700;
        font-size: 12px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .pa-word-line {
        height: 8px;
        width: 72%;
        margin: 9px 0;
        border-radius: 999px;
        background: #d0d5dd;
    }

    .pa-word-line.pa-wide { width: 92%; }
    .pa-word-line.pa-short { width: 48%; }

    .pa-word-block {
        height: 34px;
        width: 84%;
        margin-top: 14px;
        border-radius: 8px;
        background: #ecfdf3;
        border: 1px solid #75e0a7;
    }

    .pa-check {
        position: absolute;
        right: -12px;
        bottom: -12px;
        display: flex;
        align-items: center;
        justify-content: center;
        width: 42px;
        height: 42px;
        border-radius: 50%;
        background: #12b76a;
        color: white;
        font-size: 28px;
        font-weight: 900;
    }

    .pa-result-title {
        color: #182230;
        font-weight: 800;
        font-size: 18px;
        margin-bottom: 8px;
    }

    .pa-result-note {
        color: #667085;
        font-size: 14px;
    }

    @keyframes paMove {
        from { transform: translateX(-28px); }
        to { transform: translateX(58px); }
    }

    @keyframes paPulse {
        0%, 100% { transform: translateY(0); border-color: #e4e7ec; }
        50% { transform: translateY(-5px); border-color: #165dff; }
    }

    @media (max-width: 760px) {
        .pa-flow { flex-direction: column; align-items: stretch; }
        .pa-arrow { width: 3px; height: 36px; margin: 0 auto; }
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
                    - GUI: PDF论文总结助手<br>
                    - 版本: {__version__}
                """
cancellation_event_map = {}

PROCESSING_ANIMATION_HTML = """
<div class="pa-flow pa-running">
  <div class="pa-step">
    <div class="pa-icon">PDF</div>
    <div class="pa-step-title">输入论文</div>
    <div class="pa-step-note">文件或链接</div>
  </div>
  <div class="pa-arrow"></div>
  <div class="pa-step pa-parse">
    <div class="pa-icon">AI</div>
    <div class="pa-step-title">解析内容</div>
    <div class="pa-step-note">正文 / 图表 / 公式</div>
  </div>
  <div class="pa-arrow"></div>
  <div class="pa-step pa-word">
    <div class="pa-icon">DOCX</div>
    <div class="pa-step-title">生成 Word</div>
    <div class="pa-step-note">正在整理总结...</div>
  </div>
</div>
"""


def word_result_html(docx_path: str) -> str:
    filename = os.path.basename(docx_path) if docx_path else "paper-summary.docx"
    return f"""
<div class="pa-flow pa-done">
  <div class="pa-word-preview">
    <div class="pa-word-head">
      <span class="pa-word-badge">WORD</span>
      <span class="pa-word-name">{filename}</span>
    </div>
    <div class="pa-word-line pa-wide"></div>
    <div class="pa-word-line"></div>
    <div class="pa-word-line pa-short"></div>
    <div class="pa-word-block"></div>
    <div class="pa-check">✓</div>
  </div>
  <div>
    <div class="pa-result-title">论文总结已生成</div>
    <div class="pa-result-note">下载组件中可以获取 Word 文档。</div>
  </div>
</div>
"""


def show_processing_animation():
    return gr.update(value=PROCESSING_ANIMATION_HTML, visible=True)


# The following code creates the GUI
with gr.Blocks(
    title="PDF论文总结助手",
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
            result_animation = gr.HTML(visible=False)
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
        show_processing_animation,
        outputs=[result_animation],
    ).then(
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
            result_animation,
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
