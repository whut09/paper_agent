"""Agent node facades."""

from paper_agent.agents.contracts import (
    AGENT_CONTRACTS,
    EXTRACTOR_AGENT_CONTRACT,
    READER_AGENT_CONTRACT,
    REFLECTOR_AGENT_CONTRACT,
    SYNTHESIZER_AGENT_CONTRACT,
    VERIFIER_AGENT_CONTRACT,
    AgentContract,
)
from paper_agent.agents.extractor import ExtractSections
from paper_agent.agents.reader import ParsePaper, PreparePaper
from paper_agent.agents.reflector import get_self_improving_prompt_patches, record_summary_correction
from paper_agent.agents.synthesizer import ExtractMethods, GenerateReport, SummarizeContribution
from paper_agent.agents.verifier import VerifyClaims

__all__ = [
    "AGENT_CONTRACTS",
    "AgentContract",
    "EXTRACTOR_AGENT_CONTRACT",
    "ExtractMethods",
    "ExtractSections",
    "GenerateReport",
    "ParsePaper",
    "PreparePaper",
    "READER_AGENT_CONTRACT",
    "REFLECTOR_AGENT_CONTRACT",
    "SYNTHESIZER_AGENT_CONTRACT",
    "SummarizeContribution",
    "VERIFIER_AGENT_CONTRACT",
    "VerifyClaims",
    "get_self_improving_prompt_patches",
    "record_summary_correction",
]
