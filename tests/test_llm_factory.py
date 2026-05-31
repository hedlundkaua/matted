from llm.codex import CodexProvider
from llm.factory import ProviderFactory
from llm.openrouter import OpenRouterProvider


def test_factory_uses_codex_from_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ACTIVE_LLM_PROVIDER=codex\n", encoding="utf-8")
    monkeypatch.delenv("ACTIVE_LLM_PROVIDER", raising=False)

    provider = ProviderFactory.get_provider(root_dir=str(tmp_path))

    assert isinstance(provider, CodexProvider)


def test_factory_uses_openrouter_from_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ACTIVE_LLM_PROVIDER=openrouter\nOPENROUTER_API_KEY=test\n", encoding="utf-8")
    monkeypatch.delenv("ACTIVE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    provider = ProviderFactory.get_provider(root_dir=str(tmp_path))

    assert isinstance(provider, OpenRouterProvider)


def test_factory_supports_openclaude_alias(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ACTIVE_LLM_PROVIDER=openclaude\nOPENROUTER_API_KEY=test\n", encoding="utf-8")
    monkeypatch.delenv("ACTIVE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    provider = ProviderFactory.get_provider(root_dir=str(tmp_path))

    assert isinstance(provider, OpenRouterProvider)


def test_openrouter_stream_enabled_by_default(monkeypatch):
    monkeypatch.delenv("MATTED_OPENROUTER_STREAM", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    provider = OpenRouterProvider()

    assert provider._stream_enabled() is True


def test_openrouter_stream_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MATTED_OPENROUTER_STREAM", "0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    provider = OpenRouterProvider()

    assert provider._stream_enabled() is False


def test_openrouter_extracts_sse_delta(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    provider = OpenRouterProvider()

    token = provider._extract_stream_delta('{"choices":[{"delta":{"content":"hello"}}]}')

    assert token == "hello"
