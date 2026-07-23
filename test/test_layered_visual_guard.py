from pathlib import Path
from tempfile import TemporaryDirectory

import fitz
from PIL import Image

from paper_agent.evaluation.visual_validation import precision_recall
from paper_agent.paper_summary import PaperAsset, _visual_asset_guard


def _asset(tmp: Path, kind: str, name: str, size: tuple[int, int], caption: str, text: str = "", rect=None):
    path = tmp / name
    Image.new("RGB", size, "white").save(path)
    return PaperAsset(kind, 1, path, caption, text=text, rect=rect)


def test_layered_visual_guard_has_pass_warn_and_block_fixtures():
    with TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        table = _asset(
            tmp,
            "table",
            "page-001-captioned-table.png",
            (900, 400),
            "Table 1. Results",
            "Method 1.0 2.0 3.0 4.0\nOurs 5.0 6.0 7.0 8.0",
            fitz.Rect(40, 100, 400, 280),
        )
        ambiguous = _asset(
            tmp,
            "table",
            "ambiguous-table.png",
            (600, 300),
            "Table 2. Results",
        )
        bad_figure = _asset(
            tmp,
            "figure",
            "caption-only-figure.png",
            (900, 150),
            "Figure 1. Overview",
        )

        passed = _visual_asset_guard("[[ASSET:1]]", [table], None, "")
        warned = _visual_asset_guard("[[ASSET:1]]", [ambiguous], None, "")
        blocked = _visual_asset_guard("[[ASSET:1]]", [bad_figure], None, "")

    assert passed.status == "passed"
    assert warned.status == "warning"
    assert blocked.status == "failed"
    assert warned.metrics["arbitration_candidates"] == 1
    assert blocked.metrics["outcomes"]["block"] == 1


def test_local_visual_guard_precision_recall_is_reported_by_kind():
    cases = [
        {"kind": "table", "expected_bad": True, "predicted_bad": True},
        {"kind": "table", "expected_bad": True, "predicted_bad": False},
        {"kind": "table", "expected_bad": False, "predicted_bad": False},
        {"kind": "figure", "expected_bad": True, "predicted_bad": True},
        {"kind": "figure", "expected_bad": False, "predicted_bad": True},
    ]

    table_metrics = precision_recall(cases, kind="table")
    figure_metrics = precision_recall(cases, kind="figure")

    assert table_metrics["precision"] == 1.0
    assert table_metrics["recall"] == 0.5
    assert figure_metrics["precision"] == 0.5
    assert figure_metrics["recall"] == 1.0


def test_visual_model_request_contains_page_candidate_and_measurements():
    class Message:
        content = '{"passed": true, "issues": []}'

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    class Completions:
        def __init__(self):
            self.requests = []

        def create(self, **request):
            self.requests.append(request)
            return Response()

    class Chat:
        def __init__(self):
            self.completions = Completions()

    class Client:
        def __init__(self):
            self.chat = Chat()

    with TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        asset = _asset(tmp, "table", "ambiguous-table.png", (600, 300), "Table 2. Results")
        client = Client()
        result = _visual_asset_guard("[[ASSET:1]]", [asset], client, "vision-model")

    request = client.chat.completions.requests[0]
    content = request["messages"][1]["content"]
    assert result.status == "passed"
    assert len([item for item in content if item["type"] == "image_url"]) == 2
    assert "机器测量结果" in content[0]["text"]
    assert request["response_format"]["type"] == "json_schema"
