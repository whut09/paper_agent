from pathlib import Path

import pytest

from paper_agent.harness import PaperWorkflow, PaperWorkflowContext, PaperWorkflowNode
from paper_agent.harness.checkpoints import context_state, node_implementation_version, restore_context


def _context(tmp_path: Path, source: Path) -> PaperWorkflowContext:
    return PaperWorkflowContext(
        input_path=str(source),
        output_dir=tmp_path,
        pages=None,
        summary_language="中文",
        codex_envs={},
        max_assets=3,
    )


def test_workflow_resumes_after_interruption(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"stable-pdf")
    calls = {"prepare": 0, "summarize": 0, "report": 0}

    class Prepare(PaperWorkflowNode):
        name = "Prepare"
        produces = ["chunk_notes"]

        def run(self, context):
            calls["prepare"] += 1
            context.chunk_notes = ["parsed"]

    class Summarize(PaperWorkflowNode):
        name = "Summarize"
        depends_on = ("Prepare",)
        requires = ["chunk_notes"]
        produces = ["draft_report"]

        def run(self, context):
            calls["summarize"] += 1
            context.summary = "draft"

    class Report(PaperWorkflowNode):
        name = "Report"
        depends_on = ("Summarize",)
        requires = ["draft_report"]
        produces = ["docx"]

        def __init__(self, fail=False):
            self.fail = fail

        def run(self, context):
            calls["report"] += 1
            if self.fail:
                raise ValueError("interrupted")
            context.docx_path = context.output_dir / "report.docx"
            context.docx_path.write_bytes(b"docx")

    with pytest.raises(ValueError, match="interrupted"):
        PaperWorkflow([Prepare(), Summarize(), Report(fail=True)]).run(_context(tmp_path, source))

    result = PaperWorkflow([Prepare(), Summarize(), Report()]).run(_context(tmp_path, source))

    assert result.docx_path == tmp_path / "report.docx"
    assert calls == {"prepare": 1, "summarize": 1, "report": 2}
    assert {"Prepare", "Summarize"}.issubset(result.restored_nodes)


def test_invalid_checkpoint_reruns_only_descendants(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"stable-pdf")
    calls = {"a": 0, "b": 0, "c": 0}

    def make_workflow():
        class A(PaperWorkflowNode):
            name = "A"
            produces = ["chunk_notes"]

            def run(self, context):
                calls["a"] += 1
                context.chunk_notes = ["a"]

        class B(PaperWorkflowNode):
            name = "B"
            depends_on = ("A",)
            requires = ["chunk_notes"]
            produces = ["draft_report"]

            def run(self, context):
                calls["b"] += 1
                context.summary = "b"

        class C(PaperWorkflowNode):
            name = "C"
            depends_on = ("B",)
            requires = ["draft_report"]
            produces = ["docx"]

            def run(self, context):
                calls["c"] += 1
                context.docx_path = context.output_dir / "report.docx"
                context.docx_path.write_bytes(b"docx")

        return PaperWorkflow([A(), B(), C()])

    make_workflow().run(_context(tmp_path, source))
    checkpoint_root = next((tmp_path / ".paper-agent-checkpoints").iterdir())
    b_checkpoint = next(checkpoint_root.glob("B-*.ckpt"))
    b_checkpoint.write_bytes(b"corrupt")

    result = make_workflow().run(_context(tmp_path, source))

    assert calls == {"a": 1, "b": 2, "c": 2}
    assert "A" in result.restored_nodes
    assert "B" in result.invalidated_nodes


def test_node_implementation_change_invalidates_only_that_node_and_descendants(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"stable-pdf")
    calls = {"prepare": 0, "report_v1": 0, "report_v2": 0}

    class Prepare(PaperWorkflowNode):
        name = "Prepare"
        produces = ["draft_report"]

        def run(self, context):
            calls["prepare"] += 1
            context.summary = "draft"

    class ReportV1(PaperWorkflowNode):
        name = "Report"
        depends_on = ("Prepare",)
        requires = ["draft_report"]
        produces = ["docx"]

        def run(self, context):
            calls["report_v1"] += 1
            context.docx_path = context.output_dir / "report.docx"
            context.docx_path.write_bytes(b"v1")

    class ReportV2(PaperWorkflowNode):
        name = "Report"
        depends_on = ("Prepare",)
        requires = ["draft_report"]
        produces = ["docx"]

        def run(self, context):
            calls["report_v2"] += 1
            context.docx_path = context.output_dir / "report.docx"
            context.docx_path.write_bytes(b"v2")

    PaperWorkflow([Prepare(), ReportV1()]).run(_context(tmp_path, source))
    result = PaperWorkflow([Prepare(), ReportV2()]).run(_context(tmp_path, source))

    assert calls == {"prepare": 1, "report_v1": 1, "report_v2": 1}
    assert "Prepare" in result.restored_nodes
    assert (tmp_path / "report.docx").read_bytes() == b"v2"


def test_node_timeout_is_bounded_and_reported(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"stable-pdf")

    class Slow(PaperWorkflowNode):
        name = "Slow"
        timeout_seconds = 0.01
        max_attempts = 1

        def run(self, context):
            import time

            time.sleep(0.2)

    with pytest.raises(RuntimeError, match="timed out"):
        PaperWorkflow([Slow()]).run(_context(tmp_path, source))


def test_recoverable_node_retries_with_backoff(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"stable-pdf")
    calls = []

    class Flaky(PaperWorkflowNode):
        name = "Flaky"
        max_attempts = 3
        retry_base_delay = 0

        def run(self, context):
            calls.append("call")
            if len(calls) < 3:
                raise ConnectionError("temporary transport failure")
            context.summary = "ok"

    result = PaperWorkflow([Flaky()]).run(_context(tmp_path, source))

    assert result.summary == "ok"
    assert len(calls) == 3
    assert result.node_results["Flaky"].metrics["retry_count"] == 2


def test_checkpoint_identity_and_payload_do_not_contain_secrets(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"stable-pdf")

    class Done(PaperWorkflowNode):
        name = "Done"

        def run(self, context):
            context.summary = "ok"

    context = _context(tmp_path, source)
    context.codex_envs = {
        "CODEX_API_KEY": "super-secret-api-key",
        "CODEX_MODEL": "gpt-test",
        "CODEX_PROXY": "http://proxy-user:proxy-password@localhost:7890/path?token=secret",
    }
    PaperWorkflow([Done()]).run(context)
    checkpoint = next((tmp_path / ".paper-agent-checkpoints").rglob("*.ckpt"))
    raw = checkpoint.read_bytes()

    assert b"super-secret-api-key" not in raw
    assert b"proxy-password" not in raw
    assert b"token=secret" not in raw
    assert b"gpt-test" in raw


def test_checkpoint_does_not_restore_stale_runtime_deadlines(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"stable-pdf")
    previous = _context(tmp_path, source)
    previous.workflow_started_at = 1.0
    previous.workflow_timeout_seconds = 3600.0
    previous.current_stage = "ExtractMethods"
    previous.current_progress = 0.68
    previous.progress_message = "stale progress"

    state = context_state(previous)

    assert "workflow_started_at" not in state
    assert "workflow_timeout_seconds" not in state
    assert "current_stage" not in state
    current = _context(tmp_path, source)
    current.workflow_started_at = 999.0
    restore_context(current, state)
    assert current.workflow_started_at == 999.0


def test_node_implementation_version_includes_module_file_hash(monkeypatch):
    import importlib

    class Node(PaperWorkflowNode):
        name = "Node"

    checkpoint_module = importlib.import_module("paper_agent.harness.checkpoints")
    monkeypatch.setattr(checkpoint_module, "file_sha256", lambda _path: "a" * 64)
    first = node_implementation_version(Node())
    monkeypatch.setattr(checkpoint_module, "file_sha256", lambda _path: "b" * 64)
    second = node_implementation_version(Node())

    assert first != second


def test_restored_terminal_block_does_not_continue_to_report(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"stable-pdf")
    calls = {"report": 0}

    class Verify(PaperWorkflowNode):
        name = "VerifyClaims"

        def run(self, context):
            context.summary = "blocked draft"

    class Revise(PaperWorkflowNode):
        name = "ReviseReport"
        depends_on = ("VerifyClaims",)

        def run(self, context):
            context.gate_decision = "block"

    class Report(PaperWorkflowNode):
        name = "GenerateReport"
        depends_on = ("ReviseReport",)

        def run(self, context):
            calls["report"] += 1

    workflow = PaperWorkflow([Verify(), Revise(), Report()])
    workflow.run(_context(tmp_path, source))
    restored = workflow.run(_context(tmp_path, source))

    assert calls["report"] == 0
    assert "ReviseReport" in restored.restored_nodes
    assert restored.gate_decision == "block"


def test_restored_revise_checkpoint_returns_to_verification_before_report(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"stable-pdf")
    calls = {"verify": 0, "revise": 0, "report": 0}

    class Verify(PaperWorkflowNode):
        name = "VerifyClaims"

        def run(self, context):
            calls["verify"] += 1
            if calls["verify"] == 2:
                raise RuntimeError("interrupted before recheck")
            context.summary = f"verified-{calls['verify']}"

    class Revise(PaperWorkflowNode):
        name = "ReviseReport"
        depends_on = ("VerifyClaims",)

        def run(self, context):
            calls["revise"] += 1
            context.gate_decision = "revise" if calls["revise"] == 1 else "accept"

    class Report(PaperWorkflowNode):
        name = "GenerateReport"
        depends_on = ("ReviseReport",)

        def run(self, context):
            calls["report"] += 1
            context.docx_path = tmp_path / "report.docx"
            context.docx_path.write_bytes(b"docx")

    workflow = PaperWorkflow([Verify(), Revise(), Report()])
    with pytest.raises(RuntimeError, match="interrupted before recheck"):
        workflow.run(_context(tmp_path, source))

    result = workflow.run(_context(tmp_path, source))

    assert calls == {"verify": 3, "revise": 2, "report": 1}
    assert result.gate_decision == "accept"
    assert result.docx_path == tmp_path / "report.docx"
