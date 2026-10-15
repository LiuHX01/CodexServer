from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AUTH_OVERRIDES = ('forced_login_method="chatgpt"', 'cli_auth_credentials_store="file"')


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_token: SecretStr = Field(min_length=24)
    data_dir: Path = Path("data")
    workspace_root: Path = Path("data/workspaces")
    codex_home: Path = Path("data/codex")
    codex_bin: str | None = None
    codex_model: str = "gpt-6.1-sol"
    codex_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = "medium"
    codex_sandbox: Literal["read-only", "workspace-write"] = "workspace-write"
    max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    max_pending_runs: int = Field(default=100, ge=1, le=10000)
    run_timeout_seconds: float = Field(default=1800, ge=1)
    shutdown_timeout_seconds: float = Field(default=15, ge=1, le=60)
    enable_docs: bool = True

    @field_validator("api_token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value().startswith("replace-"):
            raise ValueError("Replace the example API_TOKEN with a generated secret")
        return value

    def workspace(self, value: str) -> Path:
        root = self.workspace_root.resolve()
        candidate = Path(value)
        candidate = (
            (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
        if not candidate.is_relative_to(root):
            raise ValueError("cwd must be within WORKSPACE_ROOT")
        if not candidate.is_dir():
            raise ValueError("cwd must be an existing directory")
        return candidate
