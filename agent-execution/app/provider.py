"""Import only the selected inference provider, keeping execution data isolated."""

import json
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class RuntimeProvider:
    overrides: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict, repr=False)
    model: str | None = None


def literal(value):
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(literal(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {literal(v)}" for k, v in value.items()) + " }"
    raise ValueError("Provider 配置包含不支持的 TOML 值")


def load_ccswitch(directory: Path) -> RuntimeProvider:
    directory = directory.expanduser().resolve()
    try:
        config = tomllib.loads((directory / "config.toml").read_text())
    except (OSError, ValueError):
        raise ValueError("无法读取 Provider 目录中的 config.toml") from None
    name = config.get("model_provider")
    providers = config.get("model_providers", {})
    provider = providers.get(name) if isinstance(name, str) else None
    if not isinstance(provider, dict):
        raise ValueError("CC Switch 未选择自定义 model_provider，请先切换到第三方供应商")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Provider ID 仅支持字母、数字、下划线和连字符")
    provider = dict(provider)
    if provider.get("wire_api", "responses") != "responses":
        raise ValueError("第三方供应商必须支持 Responses API")
    url = urlsplit(provider.get("base_url", ""))
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Provider base_url 必须是不含凭据及查询参数的 HTTP(S) 地址")
    if "auth" in provider:
        raise ValueError("暂不导入 Provider auth 命令，请使用 env_key 或 API key 配置")

    env = {}
    key = provider.pop("experimental_bearer_token", None)
    env_key = provider.get("env_key")
    if not key and env_key:
        key = os.environ.get(env_key)
        if not key:
            raise ValueError("Provider env_key 对应的环境变量尚未设置")
    if not key:
        auth_file = directory / "auth.json"
        if auth_file.exists():
            try:
                auth = json.loads(auth_file.read_text())
                key = auth.get("OPENAI_API_KEY")
            except (OSError, ValueError, AttributeError):
                raise ValueError("无法读取 Provider auth.json 中的 API key") from None
    if not isinstance(key, str) or not key.strip():
        raise ValueError("未找到第三方 API key；不会使用 ChatGPT 登录凭据代替")
    env["CODEXSERVER_PROVIDER_API_KEY"] = key
    provider["env_key"] = "CODEXSERVER_PROVIDER_API_KEY"
    provider["requires_openai_auth"] = False

    # Keep credentials out of CLI argv and logs, including static auth headers.
    headers = provider.pop("http_headers", {})
    if not isinstance(headers, dict):
        raise ValueError("Provider http_headers 必须是对象")
    env_headers = provider.get("env_http_headers", {}).copy()
    for index, (header, value) in enumerate(headers.items()):
        if not isinstance(value, str):
            raise ValueError("Provider HTTP header 必须是字符串")
        variable = f"CODEXSERVER_PROVIDER_HEADER_{index}"
        env[variable] = value
        env_headers[header] = variable
    if env_headers:
        provider["env_http_headers"] = env_headers

    model = config.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("CC Switch 配置缺少 model")
    overrides = (
        'forced_login_method="api"',
        'cli_auth_credentials_store="file"',
        f"model_provider={literal(name)}",
        f"model_providers.{name}={literal(provider)}",
        f"model={literal(model)}",
    )
    return RuntimeProvider(overrides=overrides, env=env, model=model)


def runtime_provider(settings):
    if settings.codex_auth_mode == "chatgpt":
        from .config import AUTH_OVERRIDES

        return RuntimeProvider(overrides=AUTH_OVERRIDES)
    imported = load_ccswitch(settings.codex_provider_config_dir)
    # The selected CC Switch model is the service default in this mode.
    settings.codex_model = imported.model
    return imported
