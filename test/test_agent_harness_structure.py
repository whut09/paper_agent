from paper_agent.agents import (
    AGENT_CONTRACTS,
    EXTRACTOR_AGENT_CONTRACT,
    READER_AGENT_CONTRACT,
    REFLECTOR_AGENT_CONTRACT,
    SYNTHESIZER_AGENT_CONTRACT,
    VERIFIER_AGENT_CONTRACT,
    ExtractSections,
    ParsePaper,
    SummarizeContribution,
    ReviseReport,
    VerifyClaims,
)
from paper_agent.harness.policy import GateDecision, GatePolicy
from paper_agent.evaluation.guards import GUARD_SPECS
from paper_agent.evaluation.validators import _parse_verification_result
from pathlib import Path
from tempfile import TemporaryDirectory

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
    from paper_agent.harness.context import PaperWorkflowContext as ContextModuleContext
    from paper_agent.harness.executor import PaperWorkflow as ExecutorModuleWorkflow
    from paper_agent.harness.node import PaperWorkflowNode as NodeModuleWorkflowNode

    assert PaperWorkflow is ExecutorModuleWorkflow
    assert PaperContext is ContextModuleContext
    assert PaperWorkflowContext is ContextModuleContext
    assert PaperWorkflowNode is NodeModuleWorkflowNode
    assert PaperAsset is paper_summary.PaperAsset
    assert VerificationResult is paper_summary.VerificationResult
    assert ParsePaper is paper_summary.ParsePaper
    assert ExtractSections is paper_summary.ExtractSections
    assert SummarizeContribution is paper_summary.SummarizeContribution
    assert VerifyClaims is paper_summary.VerifyClaims
    assert ReviseReport is paper_summary.ReviseReport


def test_paper_summary_does_not_reverse_export_core_harness_classes():
    from paper_agent import paper_summary

    for name in (
        "AgentContract",
        "EXTRACTOR_AGENT_CONTRACT",
        "HarnessNode",
        "NodeResult",
        "PaperAgentRole",
        "PaperContext",
        "PaperWorkflow",
        "PaperWorkflowContext",
        "PaperWorkflowNode",
        "READER_AGENT_CONTRACT",
        "REFLECTOR_AGENT_CONTRACT",
        "SYNTHESIZER_AGENT_CONTRACT",
        "VERIFIER_AGENT_CONTRACT",
    ):
        assert not hasattr(paper_summary, name)


def test_agent_contracts_describe_engineering_boundaries():
    assert set(AGENT_CONTRACTS) == {
        "ReaderAgent",
        "ExtractorAgent",
        "SynthesizerAgent",
        "VerifierAgent",
        "ReflectorAgent",
    }
    assert "PaperSource" in READER_AGENT_CONTRACT.outputs
    assert "EvidenceMap" in EXTRACTOR_AGENT_CONTRACT.outputs
    assert "DraftReport" in SYNTHESIZER_AGENT_CONTRACT.outputs
    assert "VerificationReport" in VERIFIER_AGENT_CONTRACT.outputs
    assert "PromptPatch" in REFLECTOR_AGENT_CONTRACT.outputs
    assert not READER_AGENT_CONTRACT.llm_required
    assert not EXTRACTOR_AGENT_CONTRACT.llm_required
    assert SYNTHESIZER_AGENT_CONTRACT.llm_required
    assert VERIFIER_AGENT_CONTRACT.llm_required


def test_guard_registry_documents_harness_value():
    assert set(GUARD_SPECS) == {
        "Evidence Guard",
        "Asset Guard",
        "Coverage Guard",
        "Format Guard",
        "Citation Guard",
        "Loop Guard",
        "Memory Guard",
    }
    assert GUARD_SPECS["Evidence Guard"].blocking
    assert GUARD_SPECS["Asset Guard"].blocking
    assert "claim" in GUARD_SPECS["Evidence Guard"].implementation
    assert "asset manifest" in GUARD_SPECS["Asset Guard"].implementation


def test_default_workflow_nodes_bind_agent_contracts():
    workflow = PaperWorkflow.default()

    assert workflow.nodes["PreparePaper"].agent_contract is READER_AGENT_CONTRACT
    assert workflow.nodes["ParsePaper"].agent_contract is READER_AGENT_CONTRACT
    assert workflow.nodes["ExtractSections"].agent_contract is EXTRACTOR_AGENT_CONTRACT
    assert workflow.nodes["SummarizeContribution"].agent_contract is SYNTHESIZER_AGENT_CONTRACT
    assert workflow.nodes["ExtractMethods"].agent_contract is SYNTHESIZER_AGENT_CONTRACT
    assert workflow.nodes["VerifyClaims"].agent_contract is VERIFIER_AGENT_CONTRACT
    assert workflow.nodes["ReviseReport"].agent_contract is VERIFIER_AGENT_CONTRACT


def test_gate_policy_covers_pass_warn_revise_block():
    from paper_agent.schemas import VerificationResult

    policy = GatePolicy(max_revision_attempts=2)
    assert policy.decide(VerificationResult(True)) == GateDecision.PASS
    assert policy.decide(
        VerificationResult(
            True,
            soft_warnings=[{"type": "weak_evidence", "reason": "single dataset"}],
        )
    ) == GateDecision.WARN
    needs_revision = VerificationResult(
        False,
        hard_failures=[{"type": "unsupported_core_claim", "reason": "not grounded"}],
        patch_suggestions=[{"operation": "delete_claim", "target": "bad claim"}],
    )
    assert policy.decide(needs_revision, revision_attempts=0) == GateDecision.REVISE
    assert policy.decide(needs_revision, revision_attempts=2) == GateDecision.BLOCK


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
    assert result.agent_trace[-1]["contract"] == ""
    assert result.agent_trace[-1]["run_id"] == result.run_id
    assert result.agent_trace[-1]["input_keys"] == ["chunk_notes"]
    assert result.agent_trace[-1]["output_keys"] == ["draft_report"]
    assert result.agent_trace[-1]["warnings"] == ["soft issue"]
    assert result.agent_trace[-1]["metrics"]["tokens"] == 12
    assert "duration_ms" in result.agent_trace[-1]["metrics"]


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
    assert context.agent_trace[-1]["errors"] == ["boom"]


def test_workflow_revision_loop_rechecks_after_revise():
    from paper_agent.schemas import VerificationResult

    class FakeVerify(PaperWorkflowNode):
        name = "VerifyClaims"
        produces = ["verification_report"]

        def run(self, context: PaperWorkflowContext):
            if context.revision_attempts == 0:
                context.summary = "论文提出了新的数据集 XXX。\n方法使用已有数据集验证。"
                context.verification = VerificationResult(
                    False,
                    hard_failures=[{"type": "unsupported_core_claim", "reason": "not grounded"}],
                    patch_suggestions=[{"operation": "delete_claim", "target": "论文提出了新的数据集 XXX。"}],
                )
            else:
                context.verification = VerificationResult(True)

    class FinalNode(PaperWorkflowNode):
        name = "GenerateReport"
        depends_on = ("ReviseReport",)
        produces = ["docx"]

        def run(self, context: PaperWorkflowContext):
            context.chunk_notes.append("generated")

    context = PaperWorkflowContext(
        input_path="paper.pdf",
        output_dir=Path("."),
        pages=None,
        summary_language="中文",
        codex_envs={},
        max_assets=0,
    )

    result = PaperWorkflow([FakeVerify(), ReviseReport(), FinalNode()]).run(context)

    assert result.revision_attempts == 1
    assert "新的数据集 XXX" not in result.summary
    assert [item["decision"] for item in result.gate_history] == ["revise", "pass"]
    assert result.chunk_notes == ["generated"]


def test_workflow_blocks_after_revision_limit_and_writes_failure_report():
    from paper_agent.schemas import VerificationResult

    class AlwaysFailVerify(PaperWorkflowNode):
        name = "VerifyClaims"
        produces = ["verification_report"]

        def run(self, context: PaperWorkflowContext):
            context.verification = VerificationResult(
                False,
                hard_failures=[{"type": "unsupported_core_claim", "claim": "bad", "reason": "not grounded"}],
            )

    class FinalNode(PaperWorkflowNode):
        name = "GenerateReport"
        depends_on = ("ReviseReport",)
        produces = ["docx"]

        def run(self, context: PaperWorkflowContext):
            context.chunk_notes.append("should-not-run")

    with TemporaryDirectory() as temp_dir:
        context = PaperWorkflowContext(
            input_path="paper.pdf",
            output_dir=Path(temp_dir),
            pages=None,
            summary_language="中文",
            codex_envs={},
            max_assets=0,
        )
        context.output = Path(temp_dir)
        context.paper_name = "paper"

        result = PaperWorkflow([AlwaysFailVerify(), ReviseReport(), FinalNode()]).run(context)

        assert result.gate_decision == "block"
        assert result.revision_attempts == 2
        assert result.chunk_notes == []
        assert result.docx_path is None
        assert result.verification_failed_path is not None
        assert result.verification_failed_path.exists()
        assert "Verifier Agent 未通过" in result.verification_failed_path.read_text(encoding="utf-8")
