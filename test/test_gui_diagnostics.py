from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from paper_agent.gui import _format_summary_diagnostics, _summary_callback_response, summarize_file
from paper_agent.schemas.qa import SummaryRunResult


def _result(tmp_path: Path, status: str, *, downloadable: bool, warning: bool = False) -> SummaryRunResult:
    docx = tmp_path / "report.docx"
    trace = tmp_path / "trace.json"
    verification = tmp_path / "verification.json"
    qa = tmp_path / "qa.json"
    docx.write_bytes(b"docx")
    for path in (trace, verification, qa):
        path.write_text("{}", encoding="utf-8")
    return SummaryRunResult(
        status=status,
        message="diagnostic message",
        current_stage="RenderQA",
        progress=1.0,
        progress_message="done",
        repair_count=2,
        reason_codes=["renderer_unavailable"] if warning else [],
        docx_path=docx,
        trace_path=trace,
        verification_path=verification,
        qa_path=qa,
        downloadable=downloadable,
        warning=warning,
    )


def test_gradio_success_exposes_word_and_diagnostics(tmp_path):
    result = _result(tmp_path, "success", downloadable=True)

    response = _summary_callback_response(result, tmp_path / "paper.pdf")

    assert response[0]["value"] == str(result.docx_path)
    assert response[0]["visible"] is True
    assert len(response[6]["value"]) == 3
    assert "RenderQA" in response[5]["value"]


def test_gradio_warning_is_visible_and_downloadable(tmp_path):
    result = _result(tmp_path, "warning", downloadable=True, warning=True)

    markdown = _format_summary_diagnostics(result)
    response = _summary_callback_response(result, None)

    assert "warning" in markdown
    assert "renderer_unavailable" in markdown
    assert response[0]["visible"] is True


def test_gradio_block_hides_word_but_keeps_diagnostics(tmp_path):
    result = _result(tmp_path, "blocked", downloadable=False)
    result.reason_codes = ["missing_critical_asset"]

    response = _summary_callback_response(result, None)

    assert response[0]["value"] is None
    assert response[0]["visible"] is False
    assert "missing_critical_asset" in response[5]["value"]
    assert response[6]["visible"] is True


def test_gradio_network_timeout_has_stage_and_reason_code():
    result = SummaryRunResult(
        status="timeout",
        message="upstream timed out",
        current_stage="VerifyClaims",
        progress=0.78,
        reason_codes=["verifier_transport_failure"],
        next_actions=["retry_verifier"],
    )

    markdown = _format_summary_diagnostics(result)

    assert "VerifyClaims" in markdown
    assert "78%" in markdown
    assert "verifier_transport_failure" in markdown
    assert "retry_verifier" in markdown


def test_gradio_callback_returns_diagnostics_for_download_timeout():
    state = {"session_id": None}
    with patch("paper_agent.gui.download_with_limit", side_effect=requests.exceptions.ReadTimeout("connection timed out")):
        response = summarize_file(
            "Link",
            None,
            "https://example.test/paper.pdf",
            "All",
            "",
            13,
            "",
            state,
            progress=lambda *args, **kwargs: None,
        )

    assert response[0]["visible"] is False
    assert "network_timeout" in response[5]["value"]
    assert state["session_id"] is None


class _FakeAsyncResult:
    state = "SUCCESS"

    def __init__(self, payload):
        self.payload = payload

    def ready(self):
        return True

    def successful(self):
        return True

    def get(self):
        return self.payload


def test_summary_api_returns_diagnostics_and_blocks_unapproved_docx():
    pytest.importorskip("flask")
    pytest.importorskip("celery")
    from paper_agent.backend import flask_app

    payload = {
        "docx": None,
        "diagnostics": {
            "status": "blocked",
            "current_stage": "RenderQA",
            "reason_codes": ["missing_critical_asset"],
        },
        "sidecars": {"qa.json": b"{}"},
    }
    client = flask_app.test_client()
    with patch("paper_agent.backend.celery_app.AsyncResult", return_value=_FakeAsyncResult(payload)):
        status_response = client.get("/v1/summarize/task-id")
        docx_response = client.get("/v1/summarize/task-id/docx")
        qa_response = client.get("/v1/summarize/task-id/diagnostics/qa.json")

    assert status_response.status_code == 200
    assert status_response.json["diagnostics"]["status"] == "blocked"
    assert docx_response.status_code == 409
    assert qa_response.status_code == 200
    assert qa_response.data == b"{}"


def test_summary_api_success_returns_word_bytes():
    pytest.importorskip("flask")
    pytest.importorskip("celery")
    from paper_agent.backend import flask_app

    payload = {
        "docx": b"docx-bytes",
        "diagnostics": {"status": "success", "reason_codes": []},
        "sidecars": {},
    }
    client = flask_app.test_client()
    with patch("paper_agent.backend.celery_app.AsyncResult", return_value=_FakeAsyncResult(payload)):
        response = client.get("/v1/summarize/task-id/docx")

    assert response.status_code == 200
    assert response.data == b"docx-bytes"
