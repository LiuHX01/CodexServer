import json
import tomllib
from unittest.mock import patch

import pytest

from app.backend import CodexBackend
from app.provider import load_ccswitch


def source(tmp_path, *, extra="", auth=None):
    directory = tmp_path / "ccswitch"
    directory.mkdir()
    (directory / "config.toml").write_text(
        'model="third-party-model"\nmodel_provider="custom"\n'
        '[model_providers.custom]\nname="Gateway"\n'
        'base_url="http://127.0.0.1:1234/v1"\nwire_api="responses"\n' + extra
    )
    if auth is not None:
        (directory / "auth.json").write_text(json.dumps(auth))
    return directory


def test_import_selected_provider_without_copying_secrets_or_sessions(settings, tmp_path):
    directory = source(
        tmp_path,
        extra=(
            'requires_openai_auth=true\nexperimental_bearer_token="private-test-key"\n'
            '[model_providers.custom.http_headers]\nX-Key="private-header"\n'
        ),
    )
    before = (directory / "config.toml").read_bytes()
    settings.codex_auth_mode = "ccswitch"
    settings.codex_provider_config_dir = directory
    with patch("app.backend.InteractiveCodexClient") as constructor:
        CodexBackend(settings)
    config = constructor.call_args.args[0]
    overrides = "\n".join(config.config_overrides)
    assert "private-test-key" not in overrides
    assert "private-header" not in overrides
    assert config.env["CODEXSERVER_PROVIDER_API_KEY"] == "private-test-key"
    assert config.env["CODEX_HOME"] == str(settings.codex_home.resolve())
    imported = tomllib.loads(overrides)
    assert imported["forced_login_method"] == "api"
    assert imported["model_providers"]["custom"]["requires_openai_auth"] is False
    assert settings.codex_model == "third-party-model"
    assert (directory / "config.toml").read_bytes() == before
    assert not (settings.codex_home / "auth.json").exists()


def test_import_api_key_from_auth_file(tmp_path):
    directory = source(tmp_path, auth={"OPENAI_API_KEY": "fixture-api-key"})
    imported = load_ccswitch(directory)
    assert imported.env["CODEXSERVER_PROVIDER_API_KEY"] == "fixture-api-key"
    assert "fixture-api-key" not in repr(imported)


def test_import_environment_key(tmp_path, monkeypatch):
    directory = source(tmp_path, extra='env_key="GATEWAY_KEY"\n')
    monkeypatch.setenv("GATEWAY_KEY", "fixture-env-key")
    assert load_ccswitch(directory).env["CODEXSERVER_PROVIDER_API_KEY"] == "fixture-env-key"
    monkeypatch.delenv("GATEWAY_KEY")
    with pytest.raises(ValueError, match="环境变量"):
        load_ccswitch(directory)


def test_never_reuse_chatgpt_tokens_for_third_party(tmp_path):
    directory = source(tmp_path, auth={"tokens": {"access_token": "chatgpt-token"}})
    with pytest.raises(ValueError, match="不会使用 ChatGPT"):
        load_ccswitch(directory)


def test_reject_incompatible_api_without_echoing_credentials(tmp_path):
    directory = source(tmp_path, extra='experimental_bearer_token="private-test-key"\n')
    path = directory / "config.toml"
    path.write_text(path.read_text().replace('wire_api="responses"', 'wire_api="chat"'))
    with pytest.raises(ValueError, match="Responses API"):
        load_ccswitch(directory)
