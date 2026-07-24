"""Graph workflow facade."""

from paper_agent.harness.executor import PaperWorkflow
from paper_agent.paper_summary import summarize_paper, summarize_paper_detailed

__all__ = ["PaperWorkflow", "summarize_paper", "summarize_paper_detailed"]
