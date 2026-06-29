from paper_agent.paper_agent import parse_args
from paper_agent.skill_prompts import PAPER_AGENT_SKILL_ID, load_paper_skill_reference, paper_agent_skill_root
from paper_agent.translator import BaseTranslator


class DummyTranslator(BaseTranslator):
    name = "dummy"

    def do_translate(self, text: str) -> str:
        return text


def test_packaged_paper_skill_references_are_available():
    root = paper_agent_skill_root()

    assert root.name == PAPER_AGENT_SKILL_ID
    assert (root / "SKILL.md").exists()
    assert "DeepPaperNote" in load_paper_skill_reference("summary-system-prompt.md")
    assert "$text" in load_paper_skill_reference("translation-prompt.md")


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
