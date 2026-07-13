import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests
import pytest

from paper_agent import sanitize_no_proxy_env
from paper_agent.gui import (
    download_with_limit,
    _configured_proxy_candidates,
    _download_proxy_config,
    _looks_like_dns_failure,
    _start_summary_session,
    cancellation_event_lock,
    cancellation_event_map,
    stop_summary_file,
    summarize_file,
)


class FakeResponse:
    def __init__(
        self, chunks, *, headers=None, status_code=200, fail_after_first=False
    ):
        self.chunks = chunks
        self.headers = headers or {}
        self.status_code = status_code
        self.fail_after_first = fail_after_first

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        for index, chunk in enumerate(self.chunks):
            yield chunk
            if self.fail_after_first and index == 0:
                raise requests.exceptions.ChunkedEncodingError("broken stream")


class FakeSession:
    def __init__(self):
        self.trust_env = True
        self.calls = []

    def get(self, url, stream, timeout, headers=None):
        self.calls.append(headers or {})
        if len(self.calls) == 1:
            return FakeResponse(
                [b"abc"],
                headers={
                    "Content-Disposition": 'attachment; filename="paper.pdf"',
                    "Content-Length": "6",
                },
                fail_after_first=True,
            )
        return FakeResponse(
            [b"def"],
            headers={"Content-Range": "bytes 3-5/6", "Content-Length": "3"},
            status_code=206,
        )


def _clear_summary_sessions():
    with cancellation_event_lock:
        cancellation_event_map.clear()


def test_start_summary_session_cancels_orphaned_session():
    _clear_summary_sessions()
    _, orphan_event = _start_summary_session({"session_id": None})

    new_state = {"session_id": None}
    new_session_id, new_event = _start_summary_session(new_state)

    assert orphan_event.is_set()
    assert not new_event.is_set()
    assert new_state["session_id"] == new_session_id
    _clear_summary_sessions()


def test_stop_summary_file_from_fresh_page_cancels_orphaned_session():
    _clear_summary_sessions()
    _, orphan_event = _start_summary_session({"session_id": None})

    stop_summary_file({"session_id": None})

    assert orphan_event.is_set()
    _clear_summary_sessions()


def test_summarize_file_cleans_session_after_input_error():
    _clear_summary_sessions()
    state = {"session_id": None}

    with pytest.raises(Exception, match="No input"):
        summarize_file(
            "Link",
            None,
            "",
            "All",
            "",
            13,
            "",
            state,
            progress=lambda *args, **kwargs: None,
        )

    assert state["session_id"] is None
    assert not cancellation_event_map


def test_download_with_limit_retries_and_resumes_partial_stream():
    session = FakeSession()
    with TemporaryDirectory() as temp_dir:
        with patch("paper_agent.gui.requests.Session", return_value=session):
            path = Path(
                download_with_limit(
                    "https://example.test/paper.pdf", Path(temp_dir), None
                )
            )

        assert path.read_bytes() == b"abcdef"
        assert session.calls[1]["Range"] == "bytes=3-"
        assert not path.with_name(f"{path.name}.part").exists()


def test_download_with_limit_reuses_existing_local_pdf_before_network():
    with TemporaryDirectory() as temp_dir:
        output = Path(temp_dir)
        local_pdf = output / "Kennerley_Mind_the_Gap_Transferring_Labels_to_Align_Object_Detection_Datasets_CVPR_2026_paper.pdf"
        local_pdf.write_bytes(b"%PDF-1.7\nlocal")

        with (
            patch("paper_agent.gui._local_pdf_search_dirs", return_value=[output]),
            patch("paper_agent.gui._try_curl_download") as curl_download,
            patch("paper_agent.gui._try_parallel_range_download") as parallel_download,
            patch("paper_agent.gui.requests.Session") as session_factory,
        ):
            path = Path(
                download_with_limit(
                    "https://openaccess.thecvf.com/content/CVPR2026/papers/Kennerley_Mind_the_Gap_Transferring_Labels_to_Align_Object_Detection_Datasets_CVPR_2026_paper.pdf",
                    output,
                    None,
                )
            )

        assert path == local_pdf
        assert path.read_bytes() == b"%PDF-1.7\nlocal"
        curl_download.assert_not_called()
        parallel_download.assert_not_called()
        session_factory.assert_not_called()


def test_download_with_limit_reuses_near_matching_browser_download_name():
    with TemporaryDirectory() as temp_dir:
        output = Path(temp_dir)
        local_pdf = output / "Mahalingam Computer Vision with a Superpixelation Camera CVPR 2026 paper (1).pdf"
        local_pdf.write_bytes(b"%PDF-1.7\nbrowser")

        with (
            patch("paper_agent.gui._local_pdf_search_dirs", return_value=[output]),
            patch("paper_agent.gui._try_curl_download") as curl_download,
            patch("paper_agent.gui._try_parallel_range_download") as parallel_download,
            patch("paper_agent.gui.requests.Session") as session_factory,
        ):
            path = Path(
                download_with_limit(
                    "https://openaccess.thecvf.com/content/CVPR2026/papers/Mahalingam_Computer_Vision_with_a_Superpixelation_Camera_CVPR_2026_paper.pdf",
                    output,
                    None,
                )
            )

        assert path.name == "Mahalingam_Computer_Vision_with_a_Superpixelation_Camera_CVPR_2026_paper.pdf"
        assert path.read_bytes() == b"%PDF-1.7\nbrowser"
        assert local_pdf.read_bytes() == b"%PDF-1.7\nbrowser"
        curl_download.assert_not_called()
        parallel_download.assert_not_called()
        session_factory.assert_not_called()


def test_dns_failure_detection_matches_requests_name_resolution_error():
    exc = requests.exceptions.ConnectionError(
        "Failed to resolve 'openaccess.thecvf.com' ([Errno 11001] getaddrinfo failed)"
    )

    assert _looks_like_dns_failure(exc)


def test_sanitize_no_proxy_env_removes_ipv6_entries(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1,::1/128")

    sanitize_no_proxy_env()

    assert "::1" not in os.environ["no_proxy"]
    assert "127.0.0.1" in os.environ["no_proxy"]


def test_configured_proxy_candidates_expand_protocol_variants():
    candidates = _configured_proxy_candidates("172.16.48.193:7897, http://backup.test:8080")
    proxies = [proxy for _mode, proxy in candidates]

    assert "http://172.16.48.193:7897" in proxies
    assert "socks5h://172.16.48.193:7897" in proxies
    assert "socks5://172.16.48.193:7897" in proxies
    assert "http://backup.test:8080" in proxies


def test_download_proxy_config_combines_primary_and_optional_proxies():
    def fake_config(key, default=""):
        if key == "PAPER_AGENT_DOWNLOAD_PROXIES":
            return "http://backup.test:8080 socks5h://backup2.test:1080"
        if key == "PAPER_AGENT_DOWNLOAD_PROXY":
            return "http://primary.test:7890"
        return default

    with patch("paper_agent.gui.get_config_or_env", side_effect=fake_config):
        value = _download_proxy_config()

    assert "http://backup.test:8080" in value
    assert "socks5h://backup2.test:1080" in value
    assert "http://primary.test:7890" in value


def test_download_with_limit_falls_back_after_proxy_timeout():
    class ProxyTimeoutSession:
        def __init__(self):
            self.trust_env = True
            self.proxies = {}
            self.calls = 0

        def get(self, url, stream, timeout, headers=None):
            self.calls += 1
            if self.proxies:
                raise requests.exceptions.ReadTimeout("proxy timed out")
            return FakeResponse(
                [b"ok"],
                headers={
                    "Content-Disposition": 'attachment; filename="paper.pdf"',
                    "Content-Length": "2",
                },
            )

    sessions = []

    def make_session():
        session = ProxyTimeoutSession()
        sessions.append(session)
        return session

    def fake_config(key, default=""):
        if key == "PAPER_AGENT_DOWNLOAD_PROXY":
            return "http://proxy.test:7890"
        return default

    with TemporaryDirectory() as temp_dir:
        with (
            patch("paper_agent.gui._try_curl_download", return_value=None),
            patch("paper_agent.gui._try_parallel_range_download", return_value=None),
            patch("paper_agent.gui._windows_system_proxy", return_value=""),
            patch("paper_agent.gui.get_config_or_env", side_effect=fake_config),
            patch("paper_agent.gui.requests.Session", side_effect=make_session),
        ):
            path = Path(
                download_with_limit(
                    "https://example.test/paper.pdf", Path(temp_dir), None
                )
            )

        assert path.read_bytes() == b"ok"
        assert sessions[0].proxies["https"] == "http://proxy.test:7890"
        assert sessions[0].calls == 3
        assert sessions[-1].proxies == {}
        assert sessions[-1].calls == 1
