from paper_agent.agents import ExtractSections, ParsePaper, SummarizeContribution, VerifyClaims
from paper_agent.evaluation.validators import _parse_verification_result
from paper_agent.harness import PaperWorkflow, PaperWorkflowContext, PaperWorkflowNode
from paper_agent.memory import get_self_improving_prompt_patches, record_summary_correction
from paper_agent.schemas import PaperAsset, VerificationResult
from paper_agent.tools.grounding import _build_grounding_map


def test_app_facades_import_without_optional_runtime_dependencies():
    import paper_agent.app.backend
    import paper_agent.app.cli
    import paper_agent.app.gui
    import paper_agent.app.mcp_server

    assert paper_agent.app.backend.__all__ == ["celery_app", "flask_app"]
    assert paper_agent.app.gui.__all__ == ["setup_gui"]


def test_agent_harness_facades_export_core_objects():
    from paper_agent import paper_summary

    assert PaperWorkflow is paper_summary.PaperWorkflow
    assert PaperWorkflowContext is paper_summary.PaperWorkflowContext
    assert PaperWorkflowNode is paper_summary.PaperWorkflowNode
    assert PaperAsset is paper_summary.PaperAsset
    assert VerificationResult is paper_summary.VerificationResult
    assert ParsePaper is paper_summary.ParsePaper
    assert ExtractSections is paper_summary.ExtractSections
    assert SummarizeContribution is paper_summary.SummarizeContribution
    assert VerifyClaims is paper_summary.VerifyClaims


def test_memory_and_evaluation_facades_are_callable():
    assert callable(record_summary_correction)
    assert callable(get_self_improving_prompt_patches)
    assert _parse_verification_result('{"pass": true, "errors": []}').passed


def test_tool_facade_builds_grounding_map():
    grounding = _build_grounding_map(
        "1 Introduction\nThis paper studies agent harnesses for document understanding.\n"
        "2 Method\nThe method uses reader and verifier agents for evidence checks.\n"
    )

    assert "intro" in grounding
    assert "method" in grounding
