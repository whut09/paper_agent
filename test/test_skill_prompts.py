import json
import subprocess
import shutil
from paper_agent.paper_agent import parse_args
import paper_agent.skill_prompts as skill_prompts
from paper_agent.skill_prompts import (
    PAPER_AGENT_SKILL_ID,
    load_paper_skill_reference,
    paper_agent_skill_root,
    paper_agent_skillbridge_root,
)
from paper_agent.translator import BaseTranslator


class DummyTranslator(BaseTranslator):
    name = "dummy"

    def do_translate(self, text: str) -> str:
        return text


def test_packaged_paper_skill_references_are_available():
    skill_prompts._REFERENCE_CACHE.clear()
    root = paper_agent_skill_root()

    assert root.name == PAPER_AGENT_SKILL_ID
    assert (root / "SKILL.md").exists()
    assert load_paper_skill_reference("summary-system-prompt.md", "direct default") == "direct default"


def test_local_skill_references_are_available(monkeypatch):
    skill_prompts._REFERENCE_CACHE.clear()
    monkeypatch.setenv("PAPER_AGENT_PROMPT_ENGINE", "local")

    assert "DeepPaperNote" in load_paper_skill_reference("summary-system-prompt.md")
    assert "$text" in load_paper_skill_reference("translation-prompt.md")


def test_skillbridge_engine_requires_explicit_opt_in(monkeypatch):
    skill_prompts._REFERENCE_CACHE.clear()
    monkeypatch.setenv("PAPER_AGENT_PROMPT_ENGINE", "skillbridge")
    monkeypatch.delenv("PAPER_AGENT_USE_SKILLBRIDGE_PROMPTS", raising=False)

    assert load_paper_skill_reference("summary-system-prompt.md", "direct default") == "direct default"


def test_skillbridge_root_defaults_to_sibling_repo_or_env(monkeypatch):
    monkeypatch.delenv("PAPER_AGENT_SKILLBRIDGE_ROOT", raising=False)
    root = paper_agent_skillbridge_root()

    assert root is not None
    assert root.name == "agent-skill-bridge"


def test_load_paper_skill_reference_prefers_skillbridge(monkeypatch):
    skill_prompts._REFERENCE_CACHE.clear()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

        class Result:
            returncode = 0
            stdout = json.dumps({"type": "text", "content": "from skillbridge"})

        return Result()

    monkeypatch.setenv("PAPER_AGENT_PROMPT_ENGINE", "skillbridge")
    monkeypatch.setenv("PAPER_AGENT_USE_SKILLBRIDGE_PROMPTS", "true")
    monkeypatch.setenv("PAPER_AGENT_SKILLBRIDGE_ROOT", r"F:\codex\code\agent-skill-bridge")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert load_paper_skill_reference("summary-system-prompt.md") == "from skillbridge"
    assert calls
    command = calls[0][0]
    assert "read" in command
    assert str(paper_agent_skill_root()) in command
    assert "references/summary-system-prompt.md" in command


def test_translator_uses_packaged_skill_prompt_by_default():
    translator = DummyTranslator("en", "zh", None, False)
    message = translator.prompt("Hello {v1}")[0]["content"]

    assert "Source Text:" in message
    assert "Hello {v1}" in message
    assert "zh" in message


def test_summarize_cli_parses_page_ranges():
    args = parse_args(["summarize", "paper.pdf", "--pages", "1,3-4", "--max-assets", "5"])

    assert args.command == "summarize"
    assert args.file == "paper.pdf"
    assert args.pages == [0, 2, 3]
    assert args.max_assets == 5
