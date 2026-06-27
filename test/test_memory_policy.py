import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from paper_agent.memory import (
    MemoryPolicy,
    disable_correction_memory,
    list_correction_memories,
    promote_correction_memory,
    record_summary_correction,
)
from paper_agent.memory.correction_memory import _load_correction_memories


def test_memory_policy_filters_low_confidence_and_conflicts():
    with TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / "memory.jsonl"
        record_summary_correction(
            "Paper A",
            "图4写成表2",
            "按原始 caption 类型引用",
            confidence=0.9,
            memory_path=memory_path,
        )
        record_summary_correction(
            "Paper A",
            "图4写成表2",
            "全部改成表格引用",
            confidence=0.9,
            memory_path=memory_path,
        )
        record_summary_correction(
            "Paper A",
            "低置信度规则",
            "不要注入",
            confidence=0.2,
            memory_path=memory_path,
        )

        memories = _load_correction_memories("Paper A", memory_path=memory_path)

    assert len(memories) == 1
    assert memories[0].corrected == "按原始 caption 类型引用"
    assert all(memory.confidence >= MemoryPolicy().min_confidence for memory in memories)


def test_memory_disable_prevents_injection():
    with TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / "memory.jsonl"
        record_summary_correction(
            "Paper A",
            "错误摘要",
            "修正摘要",
            memory_path=memory_path,
        )
        disable_correction_memory(1, memory_path=memory_path)

        memories = _load_correction_memories("Paper A", memory_path=memory_path)
        rows = list_correction_memories(memory_path=memory_path)

    assert memories == []
    assert rows[0]["disabled"] is True


def test_memory_hit_count_and_promotion_policy():
    with TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / "memory.jsonl"
        record_summary_correction(
            "Paper A",
            "公式13写成公式2",
            "公式编号必须保持原文编号",
            category="verification",
            memory_path=memory_path,
        )

        _load_correction_memories("Paper A", memory_path=memory_path)
        _load_correction_memories("Paper A", memory_path=memory_path)
        rows = list_correction_memories(memory_path=memory_path)

        assert rows[0]["hit_count"] == 2
        promote_correction_memory(1, "domain", memory_path=memory_path)
        try:
            promote_correction_memory(1, "global", memory_path=memory_path)
        except ValueError as exc:
            assert "promotion policy" in str(exc)
        else:
            raise AssertionError("global promotion must require evaluation_passed")
        promote_correction_memory(1, "global", memory_path=memory_path, evaluation_passed=True)
        promoted = list_correction_memories(memory_path=memory_path)

    assert [row["scope"] for row in promoted] == ["paper", "domain", "global"]
    assert promoted[-1]["promoted_from"].startswith("paper:")


def test_memory_cli_list_disable_promote():
    from paper_agent.paper_agent import main

    with TemporaryDirectory() as tmp:
        memory_path = Path(tmp) / "memory.jsonl"
        record_summary_correction(
            "Paper A",
            "摘要漏掉右栏",
            "双栏摘要要完整拼接",
            memory_path=memory_path,
        )
        _load_correction_memories("Paper A", memory_path=memory_path)
        _load_correction_memories("Paper A", memory_path=memory_path)

        output = StringIO()
        with redirect_stdout(output):
            assert main(["memory", "list", "--memory-path", str(memory_path)]) == 0
        assert "双栏摘要" in output.getvalue()

        with redirect_stdout(StringIO()):
            assert main(["memory", "promote", "1", "--scope", "domain", "--memory-path", str(memory_path)]) == 0
            assert main(["memory", "disable", "1", "--memory-path", str(memory_path)]) == 0
        payload = json.loads(output.getvalue())

    assert payload["memories"][0]["scope"] == "paper"
