"""Feedback memory and prompt patch facades."""

from paper_agent.memory.correction_memory import (
    MemoryPolicy,
    disable_correction_memory,
    list_correction_memories,
    promote_correction_memory,
    record_summary_correction,
)
from paper_agent.memory.prompt_patch import get_self_improving_prompt_patches

__all__ = [
    "MemoryPolicy",
    "disable_correction_memory",
    "get_self_improving_prompt_patches",
    "list_correction_memories",
    "promote_correction_memory",
    "record_summary_correction",
]
