from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path


logger = logging.getLogger(__name__)

PAPER_AGENT_SKILL_ID = "paper-agent-paper-reading"


def paper_agent_skill_root() -> Path:
    configured = os.environ.get("PAPER_AGENT_SKILL_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent / "skills" / PAPER_AGENT_SKILL_ID


def paper_agent_skillbridge_root() -> Path | None:
    configured = os.environ.get("PAPER_AGENT_SKILLBRIDGE_ROOT")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        return candidate if candidate.exists() else None

    fallback = Path(__file__).resolve().parents[2] / "agent-skill-bridge"
    return fallback if fallback.exists() else None


def _skillbridge_command() -> tuple[list[str], Path] | None:
    bridge_root = paper_agent_skillbridge_root()
    if bridge_root is None:
        return None

    cli_entrypoint = bridge_root / "packages" / "cli" / "dist" / "index.js"
    if not cli_entrypoint.exists():
        return None

    if shutil.which("pnpm"):
        return (["pnpm", "skillbridge"], bridge_root)
    if shutil.which("node"):
        return (["node", str(cli_entrypoint)], bridge_root)
    return None


def _load_reference_via_skillbridge(reference_name: str) -> str | None:
    skillbridge = _skillbridge_command()
    if skillbridge is None:
        return None

    command, cwd = skillbridge
    skill_root = paper_agent_skill_root()
    skill_reference = f"references/{reference_name}"

    try:
        completed = subprocess.run(
            [*command, "read", str(skill_root), skill_reference, "--json"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as exc:
        logger.debug("SkillBridge prompt load failed: %s", exc)
        return None

    if completed.returncode != 0:
        logger.debug("SkillBridge prompt load returned non-zero exit code: %s", completed.returncode)
        return None

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        logger.debug("SkillBridge prompt load did not return JSON.")
        return None

    if payload.get("type") != "text":
        return None

    content = payload.get("content")
    if isinstance(content, str):
        stripped = content.strip()
        return stripped or None

    return None


def load_paper_skill_reference(reference_name: str, default: str = "") -> str:
    bridge_text = _load_reference_via_skillbridge(reference_name)
    if bridge_text:
        return bridge_text

    reference_path = paper_agent_skill_root() / "references" / reference_name
    try:
        text = reference_path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return text or default


__all__ = [
    "PAPER_AGENT_SKILL_ID",
    "load_paper_skill_reference",
    "paper_agent_skill_root",
    "paper_agent_skillbridge_root",
]
