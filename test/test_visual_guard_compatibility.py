import json

from paper_agent.paper_summary import _parse_visual_asset_guard_response


def test_suitable_false_preserves_multiple_objects_as_error():
    payload = {
        "asset_id": 5,
        " suitable": False,
        "issues": [{"severity": "error", "type": "multiple_objects",
                    "description": "Table 5 and Table 6 are mixed in one crop"}],
    }
    result = _parse_visual_asset_guard_response(json.dumps(payload))
    assert result["passed"] is False
    assert result["issues"][0]["severity"] == "error"
    assert result["issues"][0]["type"] == "multiple_objects"
    assert "Table 6" in result["issues"][0]["reason"]


def test_fenced_json_preserves_explicit_rejection():
    payload = '{"passed":false,"issues":[{"severity":"error","type":"cropped","reason":"cut off"}]}'
    result = _parse_visual_asset_guard_response("```json\n" + payload + "\n```")
    assert result["passed"] is False
    assert result["issues"][0]["severity"] == "error"


def test_valid_alias_keeps_all_issues():
    result = _parse_visual_asset_guard_response(json.dumps({
        "valid": False, "issues": [
            {"severity": "error", "type": "cropped", "reason": "cut off"},
            {"severity": "warning", "type": "whitespace", "reason": "margin"},
        ],
    }))
    assert result["passed"] is False
    assert len(result["issues"]) == 2
