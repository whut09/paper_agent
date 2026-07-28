# PaperAgent 技术分析

本文档说明 PaperAgent 的核心架构、处理流程、配置方式和本地运行注意事项，便于后续维护与二次开发。

## 1. 项目定位

PaperAgent 是一个本地论文阅读与总结工具。它以 PDF、DOC、DOCX 或在线论文链接为输入，提取论文正文、版面信息、图表和公式等素材，然后调用兼容 OpenAI SDK 的大模型接口生成中文总结，并导出 Word 文档。

项目当前默认以 Python 命令行方式启动：

```powershell
python -m paper_agent -i --config config.local.json
```

浏览器访问：

```text
http://localhost:7860/
```

## 2. 核心模块

| 模块 | 文件 | 作用 |
| --- | --- | --- |
| 命令行入口 | `paper_agent/paper_agent.py`、`paper_agent/__main__.py` | 解析启动参数，进入 GUI、翻译、MCP 等模式 |
| GUI | `paper_agent/gui.py` | 基于 Gradio 提供论文上传、预览、总结和下载入口 |
| 配置管理 | `paper_agent/config.py` | 读取 `config.json` 或 `--config` 指定的配置文件 |
| 论文总结 | `paper_agent/paper_summary.py` | 抽取正文、图表素材，调用大模型生成总结并写入 docx |
| PDF 解析与转换 | `paper_agent/high_level.py`、`paper_agent/converter.py`、`paper_agent/pdfinterp.py` | 处理 PDF 文本、布局和翻译写回 |
| 布局检测 | `paper_agent/doclayout.py` | 使用 ONNX 模型检测页面中的文本、图表、公式等区域 |
| 翻译服务 | `paper_agent/translator.py` | 封装 OpenAI、DeepL、Ollama、ModelScope 等服务 |
| 内核切换 | `paper_agent/kernel/` | 支持 fast 与 precise 两类处理内核 |

## 3. 总结流程

论文总结的主流程位于 `paper_agent/paper_summary.py`，GUI 通过 `paper_agent/gui.py` 调用。

整体流程如下：

```text
用户上传文件或输入链接
  -> 保存到 paper_agent_files
  -> 抽取正文和页面结构
  -> 识别图、表、公式等素材
  -> 将论文正文分块
  -> 调用大模型生成分段笔记
  -> 合并、整理、补全总结
  -> 写入 Word 文档
  -> 返回下载链接
```

其中大模型配置由以下字段控制：

```json
{
    "CODEX_BASE_URL": "https://你的接口地址/v1",
    "CODEX_API_KEY": "你的 API Key",
    "CODEX_MODEL": "你的模型名称"
}
```

`CODEX_BASE_URL` 要求是兼容 OpenAI SDK 的接口地址。`CODEX_API_KEY` 为调用凭据。`CODEX_MODEL` 为模型名称。

## 4. 配置策略

仓库中的 `config.json` 是提交用模板，敏感字段必须保持脱敏：

```json
{
    "CODEX_BASE_URL": "xx",
    "CODEX_API_KEY": "xx",
    "CODEX_MODEL": "xx"
}
```

本地真实配置放在 `config.local.json`，该文件已经加入 `.gitignore`，不会被提交。启动时通过 `--config` 指定：

```powershell
python -m paper_agent -i --config config.local.json
```

这样可以同时满足两个目标：

- GitHub 上不会暴露真实 URL 和 Key。
- 本地仍然可以使用真实配置正常运行。

## 5. PDF 与版面处理

PaperAgent 不依赖传统 OCR 作为主要文本来源。对于可解析 PDF，程序优先读取 PDF 内部文本层和坐标信息；布局检测模型用于识别页面区域，例如正文、标题、图、表和公式。这样可以减少 OCR 错字，同时保留版面结构。

处理逻辑大致分为三层：

1. PDF 解析层：读取文本、字体、坐标、页面对象。
2. 布局检测层：将页面渲染为图像后，使用 DocLayout-YOLO ONNX 模型识别区域。
3. 内容生成层：将抽取到的正文和素材交给大模型，总结后写入 Word。

## 6. GUI 交互

GUI 由 Gradio 构建，主要交互包括：

- 选择文件或输入链接。
- 选择页码范围。
- 设置最多写入 Word 的图表截图数量。
- 生成论文总结。
- 下载生成的 Word 文档。

GUI 会读取 `CODEX_BASE_URL`、`CODEX_API_KEY`、`CODEX_MODEL`，并传入总结模块。

## 7. 命令行入口

查看版本：

```powershell
python -m paper_agent --version
```

启动 GUI：

```powershell
python -m paper_agent -i --config config.local.json
```

指定端口：

```powershell
python -m paper_agent -i --serverport 7860 --config config.local.json
```

翻译文件：

```powershell
python -m paper_agent example.pdf -s openai --config config.local.json
```

## 8. 提交安全检查

提交前建议执行：

```powershell
rg -n "sk-[A-Za-z0-9]{20,}" --hidden --glob "!.git/**" --glob "!config.local.json"
```

预期结果应为空。还需要确认以下文件不会被提交：

- `config.local.json`
- `.gradio/`
- `paper_agent_files/`
- `__pycache__/`
- `*.pyc`
- 本地 PDF、DOCX 等输出文件

## 9. 维护建议

- 保持 `config.json` 为脱敏模板，不写入真实接口信息。
- 修改 GUI 文案后，应运行 `python -m paper_agent --version` 和基础导入检查。
- 修改总结流程后，应重点检查 `paper_agent/paper_summary.py` 中的分块、提示词、docx 写入逻辑。
- 修改 GitHub 提交前，先运行敏感信息扫描。
- 如果 GitHub 推送超时，而浏览器能访问 GitHub，优先检查 Git 是否配置了本机代理。

## 10. 迁移验收层

Prompt 1-6 保持 `paper_agent.harness.workflow.summarize_paper` 为兼容 facade，并按以下独立提交顺序迁移：

1. `4fdc995`：资产候选生成；
2. `a50a30e`：类型化 Finding；
3. `1c0530d`：有界修复状态机；
4. `f37756d`：内容寻址 checkpoint；
5. `95e8eba`：DOCX RenderQA。

`paper_agent/evaluation/acceptance.py` 不参与论文内容生成，只读取 workflow context 和 sidecar，形成 `acceptance.json`。它负责比较 compatibility manifest 与候选池选中 manifest、比较新旧报告章节覆盖率，并汇总耗时、模型调用、有效修复、无效重复修复、hard failure、warning 和最终 QA。五个迁移阶段分别有 `evaluation/migration_golden/` fixture，10 篇真实论文清单位于 `evaluation/representative_papers.json`。

验收状态区分交付可用性和完整渲染认证：最终 RenderQA `pass` 为 `passed`；DOCX 本地结构检查通过、仅渲染器基础设施不可用时为可下载的 `warning`；证据、关键资产或文档结构不完整时才输出 `blocked`，并为每个 blocker 保存 `reason_code` 和 `suggested_actions`。历史 sidecar 缺少 RenderQA 时使用 `qa_not_recorded`，下一步动作是 `rerun_with_render_qa`。

## 11. 高通过率可靠性架构

近期 Agent 研究与开源工程给出的共同结论是：可靠性主要来自短而可验证的闭环，而不是继续增加自由规划 Agent。PaperAgent 采用以下原则：

- [Agentless](https://arxiv.org/abs/2407.01489) 的 `localization -> repair -> validation` 支持将修复限制在受影响资产或 DOCX 节点，避免整篇重新生成。
- [SWE-agent](https://arxiv.org/abs/2405.15793) 强调专用 Agent-Computer Interface；对应到本项目就是类型化 Finding、有限修复动作和明确状态转换，而不是让模型自由描述如何修图。
- [OpenHands](https://arxiv.org/abs/2407.16741) 与 [LangGraph](https://github.com/langchain-ai/langgraph) 强调可恢复状态、事件轨迹和 durable execution；对应到内容寻址 checkpoint 和节点级重试。
- [AI Agents That Matter](https://arxiv.org/abs/2407.01502) 要求同时衡量准确率、成本与可复现性；因此不能只看 Guard pass rate，而应同时跟踪可用文档率、误拦截率、缺陷逃逸率、修复轮数和模型调用成本。
- [Agent Workflow Memory](https://arxiv.org/abs/2409.07429) 与 [DSPy](https://github.com/stanfordnlp/dspy) 支持从成功轨迹中归纳可复用工作流，并按指标优化 prompt/program，避免累积论文名或错误字符串特例。
- [SWE-smith](https://arxiv.org/abs/2504.21798) 强调多样、可执行的评测实例；对应到代表论文集、golden crop 和真实 DOCX 回归。

质量决策采用四级策略：

1. `ACCEPT`：内容、资产和 DOCX 结构完整，渲染检查通过。
2. `WARN`：本地确定性检查通过，但 Word COM、LibreOffice 或网络裁判不可用；允许下载，保留未完整认证状态。
3. `AUTO_REPAIR`：图片超出页高、页面溢出、非关键候选错误等可确定修复问题；只重跑受影响节点并进行一次有界复检。
4. `BLOCK`：缺少图1/图2/表1/表2、未替换 marker、无效 DOCX、关键证据缺失等完整性问题。

验收的主要指标为 `usable_docx_rate`，同时保留 `fully_certified_rate`。后续代表论文评测还应记录 `false_block_rate`、`auto_repair_success_rate`、`critical_defect_escape_rate`、`mean_repair_attempts`、`model_calls_per_success` 和 `elapsed_seconds_per_success`。目标是在 golden fixtures 上保持关键缺陷逃逸率为零，同时降低误拦截和重复修复。

## 12. Git 代理说明

当前机器浏览器通过 `127.0.0.1:7890` 访问 GitHub。Git 需要单独配置代理：

```powershell
git config http.proxy http://127.0.0.1:7890
git config https.proxy http://127.0.0.1:7890
```

如果将来不再使用代理，可以取消：

```powershell
git config --unset http.proxy
git config --unset https.proxy
```
