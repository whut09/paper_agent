import paper_agent.paper_summary as ps


def _assert_raises_runtime_error(func, expected_text: str | None = None):
    try:
        func()
    except RuntimeError as exc:
        if expected_text is not None:
            assert expected_text in str(exc)
        return
    raise AssertionError("RuntimeError was not raised")


def test_summary_quality_blocks_untranslated_raw_english_report():
    english_sentence = (
        "Image restoration often faces various complex and unknown degradations in real-world scenarios, "
        "such as noise, blurring, compression artifacts and low resolution. Training specific models for "
        "specific degradation may lead to poor generalization. Existing IR agents rely on multimodal large "
        "language models and a time-consuming strategy."
    )
    bad_summary = "# Q-Agent\n\n" + "\n\n".join([english_sentence] * 8)

    _assert_raises_runtime_error(
        lambda: ps._assert_summary_quality(bad_summary),
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
                "Chinese",
                "abstract evidence",
                [],
                "",
                "Test Paper",
            ),
            "timeout",
        )
    finally:
        ps._chat = original_chat


def test_codex_client_factory_returns_chat_client():
    config = ps.CodexConfig(
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model="test-model",
        use_proxy=False,
        proxy="",
    )
    client = ps._create_codex_client(config)

    assert client is not None
    assert hasattr(client, "chat")


def test_chat_rejects_missing_client_with_clear_error():
    _assert_raises_runtime_error(
        lambda: ps._chat(None, "test-model", "hello"),
        "Codex",
    )


def test_chat_retries_empty_content_response():
    class Message:
        def __init__(self, content):
            self.content = content

    class Choice:
        def __init__(self, content):
            self.message = Message(content)

    class Response:
        def __init__(self, content):
            self.choices = [Choice(content)]

    class Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **_request):
            self.calls += 1
            return Response("" if self.calls == 1 else "有效内容")

    class Chat:
        def __init__(self):
            self.completions = Completions()

    class Client:
        def __init__(self):
            self.chat = Chat()

    client = Client()

    assert ps._chat(client, "test-model", "hello", max_attempts=2) == "有效内容"
    assert client.chat.completions.calls == 2
