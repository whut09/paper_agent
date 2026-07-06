from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from paper_agent.gui import download_with_limit


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
        assert sessions[1].proxies == {}
        assert sessions[1].calls == 1
