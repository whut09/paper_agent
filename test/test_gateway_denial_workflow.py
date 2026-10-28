"""Exercise terminal denials above the API layer, without contacting a provider."""

from concurrent.futures import wait as wait_all
from types import SimpleNamespace

import pytest

import paper_agent.paper_summary as ps
from paper_agent.gui import _format_summary_diagnostics
from paper_agent.harness.context import PaperWorkflowContext


def denied():
    return RuntimeError("HTTP 403 request_check_blocked (request id: regression-403)")


@pytest.mark.parametrize("concurrency", [1, 2])
def test_cached_paper_denial_is_not_retried_and_successful_notes_survive(monkeypatch, tmp_path, concurrency):
    cache = tmp_path / "notes.json"
    ps._write_chunk_notes_cache(cache, ["note 1", "note 2", "note 3", "", "note 5"], 5)
    monkeypatch.setattr(ps, "_chunk_text", lambda *_: ["chunk"] * 5)
    monkeypatch.setattr(ps, "_codex_summary_concurrency", lambda: concurrency)
    calls = []

    def chat(*args, **kwargs):
        calls.append(args[2])
        raise denied()

    monkeypatch.setattr(ps, "_chat", chat)
    with pytest.raises(RuntimeError, match="第 4/5 段") as caught:
        ps._summarize_chunks_with_codex(None, "fake", "paper", [], "中文", cache_path=cache)
    assert len(calls) == 1
    assert "request_check_blocked" in str(caught.value.__cause__)
    assert ps._load_chunk_notes_cache(cache, 5) == ["note 1", "note 2", "note 3", "", "note 5"]


def test_parallel_denial_drains_successes_without_scheduling_more_chunks(monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "_chunk_text", lambda *_: ["chunk"] * 5)
    monkeypatch.setattr(ps, "_codex_summary_concurrency", lambda: 2)
    monkeypatch.setattr(ps, "wait", lambda futures, **_: wait_all(futures))
    cache = tmp_path / "notes.json"
    calls = []

    def chat(_client, _model, prompt, **kwargs):
        idx = next(i for i in range(1, 6) if f"第 {i}/5" in prompt)
        calls.append(idx)
        if idx == 2:
            raise denied()
        return f"note {idx}"

    monkeypatch.setattr(ps, "_chat", chat)
    with pytest.raises(RuntimeError, match="第 2/5 段"):
        ps._summarize_chunks_with_codex(None, "fake", "paper", [], "中文", cache_path=cache)
    assert sorted(calls) == [1, 2]
    assert ps._load_chunk_notes_cache(cache, 5) == ["note 1", "", "", "", ""]


def test_cached_partial_denial_propagates_instead_of_being_swallowed(monkeypatch, tmp_path):
    cache = tmp_path / "notes.json"
    ps._write_chunk_notes_cache(cache, ["note"] * 4, 4)
    monkeypatch.setattr(ps, "_chunk_text", lambda *_: ["chunk"] * 4)
    monkeypatch.setattr(ps, "_codex_summary_concurrency", lambda: 2)
    monkeypatch.setattr(ps, "_chat", lambda *_a, **_k: pytest.fail("notes are cached"))

    def integrate(*args):
        raise denied()

    with pytest.raises(RuntimeError, match="半篇整合被模型网关拒绝"):
        ps._summarize_chunks_with_codex(
            None, "fake", "paper", [], "中文", cache_path=cache, partial_integrator=integrate,
        )


def test_serial_partial_denial_does_not_start_next_group():
    calls = []

    def integrate(*args):
        calls.append(args[0])
        raise denied()

    with pytest.raises(RuntimeError, match="request_check_blocked"):
        ps._run_missing_partial_integrations(
            ps._chunk_integration_group_specs(4), ["note"] * 4, [], None, 4, integrate, None, max_workers=1,
        )
    assert calls == ["前半篇"]


@pytest.mark.parametrize("with_partials", [False, True])
def test_final_integration_denial_never_switches_to_shorter_prompt(monkeypatch, with_partials):
    monkeypatch.setattr(ps, "_coerce_codex_client", lambda c: c)
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        raise denied()

    monkeypatch.setattr(ps, "_chat", chat)
    monkeypatch.setattr(ps, "_fast_integrate_summary_with_codex", lambda *_a, **_k: pytest.fail("must not fall back"))
    partials = [ps._partial_integration_record(s["name"], s["start"], s["end"], 4, "partial")
                for s in ps._chunk_integration_group_specs(4)] if with_partials else []
    with pytest.raises(RuntimeError, match="request_check_blocked"):
        ps._integrate_summary_with_codex(
            None, "fake", ["note"] * 4, [], "中文", "abstract", [], "", "title",
            partial_summaries=partials,
        )
    assert len(calls) == 1


@pytest.mark.parametrize("repair_json", [False, True])
def test_verifier_denial_never_becomes_a_passing_local_fallback(monkeypatch, repair_json):
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        if repair_json and len(calls) == 1:
            return "not valid json"
        raise denied()

    monkeypatch.setattr(ps, "_chat", chat)
    with pytest.raises(RuntimeError, match="request_check_blocked"):
        ps._run_verification_agent(None, "fake", "paper", {"claims": [{"claim": "fact"}]})
    assert len(calls) == 1 + int(repair_json)


def test_stream_event_denial_latches_task_and_blocks_later_requests(monkeypatch):
    calls = []
    client = SimpleNamespace(_paper_wire_api="responses")

    def stream(*args):
        calls.append(1)
        raise denied()

    monkeypatch.setattr(ps, "_responses_stream_content", stream)
    monkeypatch.setattr(ps.time, "sleep", lambda *_: pytest.fail("must not retry"))
    for prompt in ["chunk evidence", "partial integration"]:
        with pytest.raises(RuntimeError, match="request_check_blocked"):
            ps._chat(client, "fake", prompt, max_attempts=4)
    assert calls == [1]


def test_nested_denial_remains_actionable_in_gui():
    context = PaperWorkflowContext(
        input_path="paper.pdf", output_dir=".", pages=None, summary_language="中文", codex_envs={}, max_assets=13,
    )
    try:
        try:
            raise denied()
        except RuntimeError as exc:
            raise RuntimeError(ps._chunk_summary_error_message(4, 5, exc)) from exc
    except RuntimeError as exc:
        result = ps._summary_run_result(context, exc)
    markdown = _format_summary_diagnostics(result)
    assert result.status == "failed"
    assert not result.downloadable
    assert result.reason_codes == ["model_request_blocked"]
    assert "regression-403" in markdown
    assert "系统自动修复" not in markdown


def test_chunk_error_retains_request_id_after_long_gateway_message():
    exc = RuntimeError("HTTP 403 request_check_blocked " + "detail " * 100 + "(request id: long-403)")
    message = ps._chunk_summary_error_message(4, 5, exc)
    assert "request id: long-403" in message
    assert len(message) < 400


def test_other_worker_denial_stops_a_transient_workers_next_attempt(monkeypatch):
    client = SimpleNamespace(_paper_wire_api="responses")
    calls = []

    def stream(*args):
        calls.append(1)
        client._paper_terminal_rejection = denied()
        raise TimeoutError("this request timed out while another was denied")

    monkeypatch.setattr(ps, "_responses_stream_content", stream)
    monkeypatch.setattr(ps.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="request_check_blocked"):
        ps._chat(client, "fake", "paper", max_attempts=4)
    assert calls == [1]


def test_structured_stream_error_preserves_gateway_denial_code(monkeypatch):
    calls = []

    class Stream:
        def __iter__(self):
            yield SimpleNamespace(type="error", code="request_check_blocked", message="Request rejected by preflight safety checks")

        def close(self):
            pass

    def create(**kwargs):
        calls.append(1)
        return Stream()

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(ps.time, "sleep", lambda *_: pytest.fail("must not retry"))
    with pytest.raises(RuntimeError, match="request_check_blocked"):
        ps._chat(client, "fake", "paper", max_attempts=4)
    assert calls == [1]


def test_extract_methods_denial_never_reuses_older_report(monkeypatch):
    context = PaperWorkflowContext(
        input_path="paper.pdf", output_dir=".", pages=None, summary_language="中文", codex_envs={}, max_assets=13,
    )
    context.legacy_summary = "# Paper\n\n" + "\n\n".join(
        f"## {section}\n" + "基于论文原文证据的完整报告内容。" * 20
        for section in ps._required_report_section_order()
    )
    assert not ps._report_draft_contract_errors(context.legacy_summary)
    monkeypatch.setattr(ps, "_ensure_workflow_codex_client", lambda _: (None, SimpleNamespace(model="fake")))

    def integrate(*args):
        raise denied()

    monkeypatch.setattr(ps, "_integrate_summary_with_codex", integrate)
    with pytest.raises(RuntimeError, match="request_check_blocked"):
        ps.ExtractMethods().run(context)
    assert not context.summary
