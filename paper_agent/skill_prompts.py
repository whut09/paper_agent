from __future__ import annotations

import os
from pathlib import Path


PAPER_AGENT_SKILL_ID = "paper-agent-paper-reading"


def paper_agent_skill_root() -> Path:
    configured = os.environ.get("PAPER_AGENT_SKILL_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent / "skills" / PAPER_AGENT_SKILL_ID


def load_paper_skill_reference(reference_name: str, default: str = "") -> str:
    reference_path = paper_agent_skill_root() / "references" / reference_name
    try:
        text = reference_path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return text or default


__all__ = ["PAPER_AGENT_SKILL_ID", "load_paper_skill_reference", "paper_agent_skill_root"]
