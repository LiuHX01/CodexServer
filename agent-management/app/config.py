from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AccessKey(BaseModel):
    principal: str
    role: Literal["admin", "operator", "viewer"]
    token: SecretStr


class Settings(BaseSettings):
    # The root file is the shared configuration; the service-local file remains a fallback.
    model_config = SettingsConfigDict(
        env_prefix="RELAY_",
        env_file=(Path(".env"), Path(__file__).resolve().parents[2] / ".env"),
        extra="ignore",
    )
    database_url: str = "postgresql+asyncpg://relay@127.0.0.1:55432/agentrelay"
    admin_token: SecretStr = Field(min_length=24)
    secret_key: SecretStr = Field(min_length=32)
    access_keys: list[AccessKey] = Field(default_factory=list)
    embedded_worker: bool = True
    poll_seconds: float = Field(default=2, ge=0.1)
    reconcile_seconds: float = Field(default=10, ge=1)
    cookie_secure: bool = False
    data_dir: Path = Path("data")
    container_cloud_endpoint: str | None = None
    container_cloud_token: SecretStr | None = None
    container_cloud_management_token: SecretStr | None = None
    container_cloud_management_url: str = "http://127.0.0.1:8080"
    container_cloud_local_mode: bool = False
    container_cloud_preset_dir: str | None = None
    container_cloud_target_session_id: str | None = None
    container_cloud_result_poll_seconds: float = Field(default=1.0, gt=0)
    container_cloud_result_timeout_seconds: float = Field(default=1800.0, gt=0)
    container_cloud_reconnect_initial_seconds: float = Field(default=1.0, ge=0.1)
    container_cloud_reconnect_max_seconds: float = Field(default=15.0, ge=1)
    container_cloud_connect_timeout_seconds: float = Field(default=10.0, ge=1)
    container_cloud_management_timeout_seconds: float = Field(default=30.0, ge=1)

    def principals(self):
        return [
            AccessKey(principal="admin", role="admin", token=self.admin_token),
            *self.access_keys,
        ]
