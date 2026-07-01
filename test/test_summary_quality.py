import paper_agent.paper_summary as ps


def _assert_raises_runtime_error(func, expected_text: str):
    try:
        func()
    except RuntimeError as exc:
        assert expected_text in str(exc)
        return
    raise AssertionError("RuntimeError was not raised")


def test_summary_quality_blocks_untranslated_raw_english_report():
    bad_summary = """# Q-Agent

## 核心信息
- 标题: Q-Agent

## 摘要
Image restoration often faces various complex and unknown degradations in real-world scenarios, such as noise, blurring, compression artifacts and low resolution. Training specific models for specific degradation may lead to poor generalization. Existing IR agents rely on multimodal large language models and a time-consuming strategy.

## 背景与问题
Image restoration often faces various complex and unknown degradations in real-world scenarios, such as noise, blurring, compression artifacts and low resolution. Training specific models for specific degradation may lead to poor generalization. Existing IR agents rely on multimodal large language models and a time-consuming strategy.

## 方法主线
Image restoration often faces various complex and unknown degradations in real-world scenarios, such as noise, blurring, compression artifacts and low resolution. Training specific models for specific degradation may lead to poor generalization.

## 关键结果
Image restoration often faces various complex and unknown degradations in real-world scenarios, such as noise, blurring, compression artifacts and low resolution.
"""

    _assert_raises_runtime_error(
        lambda: ps._assert_summary_quality(bad_summary),
        "中文总结主体疑似直接复制英文原文",
    )


def test_final_integration_stops_instead_of_raw_fallback_when_llm_times_out():
    original_chat = ps._chat
    try:
        ps._chat = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("mock timeout"))

        _assert_raises_runtime_error(
            lambda: ps._integrate_summary_with_codex(
                None,
                "fake",
                ["note one evidence"],
                [],
                "中文",
                "abstract evidence",
                [],
                "",
                "Test Paper",
            ),
            "避免输出不可读",
        )
    finally:
        ps._chat = original_chat
