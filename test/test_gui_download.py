from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from paper_agent.gui import download_with_limit


class FakeResponse:
    def __init__(self, chunks, *, headers=None, status_code=200, fail_after_first=False):
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
            path = Path(download_with_limit("https://example.test/paper.pdf", Path(temp_dir), None))

        assert path.read_bytes() == b"abcdef"
        assert session.calls[1]["Range"] == "bytes=3-"
        assert not path.with_name(f"{path.name}.part").exists()
