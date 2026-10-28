from types import SimpleNamespace

import httpx
import openai
import pytest

import paper_agent.paper_summary as ps


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


def client_for(events):
    stream = Stream(events)
    requests = []

    def create(**request):
        requests.append(request)
        return stream

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return client, stream, requests


def test_responses_is_default_and_requires_completed_event():
    client, stream, requests = client_for([
        SimpleNamespace(type="response.output_text.delta", delta="complete report"),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(status="completed")),
    ])
    assert ps._chat(client, "test-model", "paper evidence", system_prompt="editor", max_tokens=5200) == "complete report"
    assert stream.closed
    assert requests[0]["stream"] is True
    assert requests[0]["instructions"] == "editor"
    assert requests[0]["input"] == [{"role": "user", "content": "paper evidence"}]
    assert requests[0]["max_output_tokens"] == 5200
    assert "temperature" not in requests[0]
    assert ps._model_call_count(client) >= 1


@pytest.mark.parametrize("ending", [None, "response.failed", "response.incomplete", "error"])
def test_responses_never_accepts_incomplete_report(ending):
    events = [SimpleNamespace(type="response.output_text.delta", delta="partial report" * 50)]
    if ending:
        events.append(SimpleNamespace(type=ending, message="failed"))
    client, stream, _ = client_for(events)
    with pytest.raises(RuntimeError):
        ps._chat(client, "test-model", "paper evidence", max_attempts=1)
    assert stream.closed


def test_responses_524_does_not_fall_back_to_nonstream_chat():
    response = httpx.Response(524, request=httpx.Request("POST", "https://example.test/v1/responses"))
    calls = []

    def create(**request):
        calls.append(request)
        raise openai.InternalServerError("upstream timeout", response=response, body={})

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(RuntimeError, match="524"):
        ps._chat(client, "test-model", "paper evidence", max_attempts=1)
    assert len(calls) == 1
    assert not hasattr(client, "_paper_wire_api")


def test_unsupported_responses_endpoint_uses_chat_stream(monkeypatch):
    response = httpx.Response(404, request=httpx.Request("POST", "https://example.test/v1/responses"))

    def create(**request):
        raise openai.NotFoundError("endpoint not found", response=response, body={})

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    calls = []
    monkeypatch.setattr(ps, "_chat_stream_content", lambda c, r: calls.append(r) or "streamed report")
    assert ps._chat(client, "test-model", "paper evidence") == "streamed report"
    assert client._paper_wire_api == "chat"
    assert len(calls) == 1


def test_unknown_model_never_falls_back(monkeypatch):
    response = httpx.Response(404, request=httpx.Request("POST", "https://example.test/v1/responses"))

    def create(**request):
        raise openai.NotFoundError("model_not_found", response=response, body={})

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(ps, "_chat_stream_content", lambda *_: pytest.fail("must not fall back"))
    with pytest.raises(RuntimeError, match="model_configuration_error"):
        ps._chat(client, "test-model", "paper evidence", max_attempts=1)


def test_client_factory_preserves_wire_api():
    config = ps.CodexConfig("https://example.test/v1", "test-key", "test-model", wire_api="chat")
    client = ps._create_codex_client(config)
    try:
        assert client._paper_wire_api == "chat"
    finally:
        client.close()


@pytest.mark.parametrize("wire_api", ["responses", "chat", "legacy", "legacy_empty"])
def test_gateway_preflight_denial_is_terminal_and_reports_real_attempt_count(monkeypatch, wire_api):
    response = httpx.Response(403, request=httpx.Request("POST", "https://example.test/v1/responses"))
    error = openai.PermissionDeniedError(
        "request_check_blocked: Request rejected by preflight safety checks (request id: test-403)",
        response=response, body={"code": "request_check_blocked"},
    )
    calls = []

    def denied(*args, **kwargs):
        calls.append(1)
        raise error

    client = SimpleNamespace(_paper_wire_api="legacy" if wire_api == "legacy_empty" else wire_api)
    monkeypatch.setattr(ps, "_responses_stream_content", denied)
    monkeypatch.setattr(ps, "_chat_stream_content", denied)
    monkeypatch.setattr(ps, "_create_chat_completion", lambda *a: "" if wire_api == "legacy_empty" else denied())
    monkeypatch.setattr(ps.time, "sleep", lambda *_: pytest.fail("must not retry denial"))
    with pytest.raises(RuntimeError, match="model_request_blocked") as caught:
        ps._chat(client, "test-model", "paper evidence", max_attempts=4)
    assert len(calls) == 1
    assert "实际请求 1 次" in str(caught.value)
    assert "已重试 4 次" not in str(caught.value)
    assert caught.value.__cause__ is error


def test_nonretryable_status_reports_one_attempt_not_configured_maximum():
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.test/v1/responses"))

    def create(**request):
        raise openai.BadRequestError("invalid input", response=response, body={})

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(RuntimeError, match="实际请求 1 次"):
        ps._chat(client, "test-model", "paper evidence", max_attempts=4)
