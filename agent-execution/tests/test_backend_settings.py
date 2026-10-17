from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.backend import CodexBackend


@pytest.mark.parametrize("effort", ["medium", "high"])
async def test_turn_uses_configured_model_and_effort(settings, effort):
    settings.codex_reasoning_effort = effort
    backend = CodexBackend(settings)
    backend.client = AsyncMock()
    backend.client.turn_start.return_value = SimpleNamespace(turn=SimpleNamespace(id="turn-1"))
    await backend.start_turn("thread-1", "hello")
    args = backend.client.turn_start.call_args.args
    assert args[2]["effort"] == effort
    assert args[2]["model"] == "gpt-6.1-sol"


async def test_chatgpt_mode_does_not_login_with_environment_api_key(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unused-test-api-key")
    with patch("app.backend.InteractiveCodexClient") as constructor:
        client = AsyncMock()
        constructor.return_value = client
        backend = CodexBackend(settings)
        overrides = constructor.call_args.args[0].config_overrides
        assert 'forced_login_method="chatgpt"' in overrides
        assert 'cli_auth_credentials_store="file"' in overrides
    await backend.start()
    try:
        client.account_login_start.assert_not_awaited()
    finally:
        await backend.close()


async def test_default_model_is_used_for_new_threads(settings):
    backend = CodexBackend(settings)
    backend.client = AsyncMock()
    backend.client.thread_start.return_value = SimpleNamespace(
        thread=SimpleNamespace(id="thread-1")
    )
    await backend.create(str(settings.workspace_root))
    assert backend.client.thread_start.call_args.args[0]["model"] == "gpt-6.1-sol"
