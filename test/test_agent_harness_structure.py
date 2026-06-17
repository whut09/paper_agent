from paper_agent.agents import ExtractSections, ParsePaper, SummarizeContribution, VerifyClaims
from paper_agent.evaluation.validators import _parse_verification_result
from pathlib import Path

from paper_agent.harness import NodeResult, PaperContext, PaperWorkflow, PaperWorkflowContext, PaperWorkflowNode
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
    assert PaperContext is paper_summary.PaperWorkflowContext
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


def test_workflow_records_structured_node_results():
    class LegacyNode(PaperWorkflowNode):
        name = "Legacy"
        produces = ["chunk_notes"]

        def run(self, context: PaperWorkflowContext):
            context.chunk_notes.append("legacy-note")

    class StructuredNode(PaperWorkflowNode):
        name = "Structured"
        depends_on = ("Legacy",)
        requires = ["chunk_notes"]
        produces = ["draft_report"]

        def run(self, context: PaperWorkflowContext):
            context.summary = "draft"
            return NodeResult(status="warning", outputs={"draft_report": "draft"}, warnings=["soft issue"], metrics={"tokens": 12})

    context = PaperWorkflowContext(
        input_path="paper.pdf",
        output_dir=Path("."),
        pages=None,
        summary_language="中文",
        codex_envs={},
        max_assets=0,
    )

    result = PaperWorkflow([LegacyNode(), StructuredNode()]).run(context)

    assert result.node_results["Legacy"].status == "success"
    assert result.node_results["Legacy"].outputs["chunk_notes"] == {"count": 1}
    assert result.node_results["Structured"].status == "warning"
    assert result.node_results["Structured"].metrics["tokens"] == 12
    assert result.agent_trace[-1]["status"] == "warning"


def test_workflow_records_failed_node_result_before_reraising():
    class FailingNode(PaperWorkflowNode):
        name = "Failing"
        produces = ["draft_report"]

        def run(self, context: PaperWorkflowContext):
            raise RuntimeError("boom")

    context = PaperWorkflowContext(
        input_path="paper.pdf",
        output_dir=Path("."),
        pages=None,
        summary_language="中文",
        codex_envs={},
        max_assets=0,
    )

    try:
        PaperWorkflow([FailingNode()]).run(context)
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("Expected failing node to raise")

    assert context.node_results["Failing"].status == "failed"
    assert context.node_results["Failing"].errors == ["boom"]
    assert context.agent_trace[-1]["status"] == "failed"
