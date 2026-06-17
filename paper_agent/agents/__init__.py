"""Agent node facades."""

from paper_agent.agents.extractor import ExtractSections
from paper_agent.agents.reader import ParsePaper, PreparePaper
from paper_agent.agents.reflector import get_self_improving_prompt_patches, record_summary_correction
from paper_agent.agents.synthesizer import ExtractMethods, GenerateReport, SummarizeContribution
from paper_agent.agents.verifier import VerifyClaims

__all__ = [
    "ExtractMethods",
    "ExtractSections",
    "GenerateReport",
    "ParsePaper",
    "PreparePaper",
    "SummarizeContribution",
    "VerifyClaims",
    "get_self_improving_prompt_patches",
    "record_summary_correction",
]

