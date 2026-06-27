"""Agent contracts and lazy node facades."""

from importlib import import_module
from typing import Any

from paper_agent.agents.contracts import (
    AGENT_CONTRACTS,
    EXTRACTOR_AGENT_CONTRACT,
    READER_AGENT_CONTRACT,
    REFLECTOR_AGENT_CONTRACT,
    SYNTHESIZER_AGENT_CONTRACT,
    VERIFIER_AGENT_CONTRACT,
    AgentContract,
    PaperAgentRole,
)

_LAZY_EXPORTS = {
    "ExtractSections": "paper_agent.agents.extractor",
    "ParsePaper": "paper_agent.agents.reader",
    "PreparePaper": "paper_agent.agents.reader",
    "get_self_improving_prompt_patches": "paper_agent.agents.reflector",
    "record_summary_correction": "paper_agent.agents.reflector",
    "ExtractMethods": "paper_agent.agents.synthesizer",
    "GenerateReport": "paper_agent.agents.synthesizer",
    "SummarizeContribution": "paper_agent.agents.synthesizer",
    "VerifyClaims": "paper_agent.agents.verifier",
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(_LAZY_EXPORTS[name])
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = [
    "AGENT_CONTRACTS",
    "AgentContract",
    "EXTRACTOR_AGENT_CONTRACT",
    "ExtractMethods",
    "ExtractSections",
    "GenerateReport",
    "ParsePaper",
    "PaperAgentRole",
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
