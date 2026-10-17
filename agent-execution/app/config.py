from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import ApprovalPolicy, ExecutionOptions, ReasoningEffort

AUTH_OVERRIDES = ('forced_login_method="chatgpt"', 'cli_auth_credentials_store="file"')
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # The root file is the shared configuration; the service-local file remains a fallback.
    model_config = SettingsConfigDict(
        env_file=(Path(".env"), PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_token: SecretStr = Field(min_length=24)
    data_dir: Path = Path("data")
    workspace_root: Path = Path("data/workspaces")
    default_cwd: str | None = Field(default=None, min_length=1)
    codex_home: Path = Path("data/codex")
    codex_auth_mode: Literal["chatgpt", "ccswitch"] = "chatgpt"
    codex_provider_config_dir: Path = Path("~/.codex")
    codex_bin: str | None = None
    codex_model: str = "gpt-6.1-sol"
    codex_reasoning_effort: ReasoningEffort = "medium"
    codex_approval_policy: ApprovalPolicy = "never"
    allowed_models: list[str] = Field(default_factory=list)
    allowed_approval_policies: list[ApprovalPolicy] = Field(
        default_factory=lambda: ["never", "on-request", "untrusted"]
    )
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

    def execution_options(self, requested: ExecutionOptions | None) -> ExecutionOptions:
        """Resolve each run independently, within deployment-owned policy limits."""
        defaults = ExecutionOptions(
            model=self.codex_model,
            reasoning_effort=self.codex_reasoning_effort,
            approval_policy=self.codex_approval_policy,
            collaboration_mode="default",
        )
        overrides = requested.model_dump(exclude_none=True) if requested else {}
        options = defaults.model_copy(update=overrides)
        if options.model not in {self.codex_model, *self.allowed_models}:
            raise ValueError("Requested model is not allowed by this deployment")
        if options.approval_policy not in self.allowed_approval_policies:
            raise ValueError("Requested approval policy is not allowed by this deployment")
        return options
