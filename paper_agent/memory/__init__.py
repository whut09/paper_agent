"""Feedback memory and prompt patch facades."""

from paper_agent.memory.correction_memory import record_summary_correction
from paper_agent.memory.prompt_patch import get_self_improving_prompt_patches

__all__ = ["get_self_improving_prompt_patches", "record_summary_correction"]

