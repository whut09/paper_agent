# PaperAgent

PaperAgent 是一个面向论文阅读的本地工具，用来把 PDF/Word 论文解析成结构化内容，并调用兼容 OpenAI 接口的大模型生成中文论文总结，最后导出 Word 文档。项目保留了原 PDF 翻译能力，同时把默认入口聚焦到“论文总结助手”。

## 功能

- 上传本地 PDF、DOC、DOCX，或输入论文链接。
- 抽取论文正文、图表截图、公式等关键素材。
- 调用 `config.json` 中配置的大模型接口生成中文总结。
- 在浏览器中预览论文，并下载生成的 `.docx` 总结文档。
- 支持命令行启动。

## 简单原理

程序启动后会读取配置文件中的模型接口参数。用户提交论文后，PaperAgent 会先用 PDF/文档解析模块提取正文和版面信息，再从页面中截取图、表、公式等素材。随后，程序会把论文正文分块发送给大模型生成分段笔记，再进行合并、润色和结构化整理。最终结果会连同关键图表一起写入 Word 文档，便于后续阅读、归档和二次编辑。

整体流程如下：

```text
论文文件或链接
  -> 文档解析与正文抽取
  -> 图表/公式素材提取
  -> 调用大模型生成总结
  -> 生成 Word 总结文档
```

## 环境要求

- Python 3.11 或 3.12
- 本地已安装项目依赖
- 一个兼容 OpenAI SDK 的接口地址、API Key 和模型名称

当前项目直接使用 Python 命令行启动。

## 安装依赖

建议在项目目录下创建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -U pip
pip install -e .
```

如果只想使用当前系统 Python，也可以直接执行：

```powershell
pip install -e .
```

## 配置说明

仓库中的 [config.json](./config.json) 已经脱敏，提交到 GitHub 时只保留 `xx` 占位：

```json
{
    "CODEX_BASE_URL": "xx",
    "CODEX_API_KEY": "xx",
    "CODEX_MODEL": "xx"
}
```

实际本地使用时，请复制一份本地配置：

```powershell
copy config.json config.local.json
```

然后编辑 `config.local.json`：

```json
{
    "CODEX_BASE_URL": "https://你的接口地址/v1",
    "CODEX_API_KEY": "你的 API Key",
    "CODEX_MODEL": "你的模型名称",
    "ENABLED_SERVICES": [],
    "HIDDEN_GRADIO_DETAILS": true,
    "PAPER_AGENT_LANG_FROM": "English",
    "PAPER_AGENT_LANG_TO": "Simplified Chinese",
    "PAPER_AGENT_VFONT": null,
    "NOTO_FONT_PATH": "/app/SourceHanSerifCN-Regular.ttf",
    "PAPER_AGENT_PROMPT": ""
}
```

`config.local.json` 已加入 `.gitignore`，不会被提交。你当前机器上的真实配置已保存在该文件中，本地启动时直接指定它即可。

## 启动命令

在项目根目录执行：

```powershell
python -m paper_agent -i --config config.local.json
```

浏览器打开：

```text
http://localhost:7860/
```

如果不使用本地配置文件，也可以直接使用脱敏后的 `config.json` 模板，但需要先把其中的 `xx` 改成真实值：

```powershell
python -m paper_agent -i --config config.json
```

## 命令行示例

启动图形界面：

```powershell
python -m paper_agent -i --config config.local.json
```

指定端口启动：

```powershell
python -m paper_agent -i --serverport 7860 --config config.local.json
```

查看版本：

```powershell
python -m paper_agent --version
```

翻译论文：

```powershell
python -m paper_agent example.pdf -s openai --config config.local.json
```

## 提交前检查

提交到 GitHub 前建议确认没有泄露真实密钥：

```powershell
rg -n "sk-[A-Za-z0-9]{20,}" --hidden --glob "!.git/**" --glob "!config.local.json"
```

预期结果应为空，不能出现真实 URL 或真实 Key。

## 致谢

感谢 [guaguastandup/zotero-pdf2zh](https://github.com/guaguastandup/zotero-pdf2zh) 项目提供的启发与参考。

本项目也基于开源社区中大量优秀 PDF 解析、版面分析、文档生成和 Gradio 组件能力构建，在此一并致谢。
