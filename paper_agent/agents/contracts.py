"""Agent contracts used by the PaperAgent harness."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PaperAgentRole(str, Enum):
    READER = "Reader"
    EXTRACTOR = "Extractor"
    SYNTHESIZER = "Synthesizer"
    CRITIC = "Critic"


@dataclass(frozen=True)
class AgentContract:
    name: str
    role: str
    responsibilities: list[str]
    inputs: list[str]
    outputs: list[str]
    failure_modes: list[str]
    llm_required: bool = False


READER_AGENT_CONTRACT = AgentContract(
    name="ReaderAgent",
    role=PaperAgentRole.READER.value,
    responsibilities=[
        "读取 PDF / Word / link",
        "标准化本地论文源文件",
        "提取页面正文和原始资产候选",
    ],
    inputs=["source_path", "paper_link", "pages"],
    outputs=["PaperSource", "PageBlock", "RawText", "RawAsset"],
    failure_modes=["无正文", "页码越界", "PDF 损坏", "Word 转换失败", "远程链接下载失败"],
    llm_required=False,
)


EXTRACTOR_AGENT_CONTRACT = AgentContract(
    name="ExtractorAgent",
    role=PaperAgentRole.EXTRACTOR.value,
    responsibilities=[
        "抽取 section、caption、formula 和 asset",
        "构建 Grounding Map",
        "生成 Asset Manifest 和 Paper-to-Knowledge Graph",
    ],
    inputs=["RawText", "PageBlock", "RawAsset"],
    outputs=["EvidenceMap", "AssetManifest", "FormulaList", "KnowledgeGraph"],
    failure_modes=["关键章节缺失", "caption 无法匹配", "图表区域跨界", "公式候选不可读"],
    llm_required=False,
)


SYNTHESIZER_AGENT_CONTRACT = AgentContract(
    name="SynthesizerAgent",
    role=PaperAgentRole.SYNTHESIZER.value,
    responsibilities=[
        "生成结构化中文精读笔记",
        "把证据、图表和 prompt patch 整合为 DraftReport",
        "抽取可校验 ClaimList",
    ],
    inputs=["EvidenceMap", "AssetManifest", "PromptPatch", "FormulaList"],
    outputs=["DraftReport", "ClaimList"],
    failure_modes=["输出格式错误", "asset placeholder 不合法", "摘要或标题改写失真"],
    llm_required=True,
)


VERIFIER_AGENT_CONTRACT = AgentContract(
    name="VerifierAgent",
    role=PaperAgentRole.CRITIC.value,
    responsibilities=[
        "检查 claim grounding",
        "检查 asset 引用和原始编号一致性",
        "检查报告格式与占位符合法性",
    ],
    inputs=["ClaimList", "EvidenceMap", "DraftReport", "AssetManifest"],
    outputs=["VerificationReport", "FixedReport"],
    failure_modes=["核心 claim 无证据", "图表引用错配", "Verifier 输出格式错误", "method claim 章节错配"],
    llm_required=True,
)


REFLECTOR_AGENT_CONTRACT = AgentContract(
    name="ReflectorAgent",
    role="Reflector",
    responsibilities=[
        "接收用户反馈",
        "写入 correction memory",
        "生成 self-improving prompt patch 和 rubric patch",
    ],
    inputs=["UserFeedback", "SummaryCorrection"],
    outputs=["CorrectionMemory", "PromptPatch", "RubricPatch"],
    failure_modes=["反馈为空", "paper_id 无法归一化", "历史修正规则冲突"],
    llm_required=False,
)


AGENT_CONTRACTS = {
    contract.name: contract
    for contract in (
        READER_AGENT_CONTRACT,
        EXTRACTOR_AGENT_CONTRACT,
        SYNTHESIZER_AGENT_CONTRACT,
        VERIFIER_AGENT_CONTRACT,
        REFLECTOR_AGENT_CONTRACT,
    )
}


__all__ = [
    "AGENT_CONTRACTS",
    "AgentContract",
    "EXTRACTOR_AGENT_CONTRACT",
    "PaperAgentRole",
    "READER_AGENT_CONTRACT",
    "REFLECTOR_AGENT_CONTRACT",
    "SYNTHESIZER_AGENT_CONTRACT",
    "VERIFIER_AGENT_CONTRACT",
]
