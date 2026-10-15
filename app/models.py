from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now() -> str:
    return datetime.now(UTC).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateThread(StrictModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {"name": "新会话"}})
    name: str | None = Field(default=None, min_length=1, max_length=200)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)


class Message(StrictModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": {"prompt": "你好", "stream": False}}
    )
    prompt: str = Field(min_length=1, max_length=100000)
    stream: bool = Field(
        default=False, strict=True, description="true：本请求返回 SSE；false：返回任务 JSON"
    )

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must contain text")
        return value


RunStatus = Literal["queued", "running", "completed", "failed", "interrupted"]
TERMINAL = frozenset({"completed", "failed", "interrupted"})


class Run(BaseModel):
    id: str
    thread_id: str
    status: RunStatus = "queued"
    turn_id: str | None = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    final_response: str | None = None
    error: str | None = None
    cancel_requested: bool = False


class Event(BaseModel):
    id: int
    event: str
    data: dict[str, Any]
