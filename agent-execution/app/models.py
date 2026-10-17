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


ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
ApprovalPolicy = Literal["never", "on-request", "untrusted"]


class ExecutionOptions(StrictModel):
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: ReasoningEffort | None = None
    approval_policy: ApprovalPolicy | None = None
    collaboration_mode: Literal["default", "plan"] | None = None


class TextInput(StrictModel):
    prompt: str = Field(min_length=1, max_length=100000)

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must contain text")
        return value


class Message(TextInput):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": {"prompt": "你好", "stream": False}}
    )
    options: ExecutionOptions | None = None
    stream: bool = Field(
        default=False, strict=True, description="true：本请求返回 SSE；false：返回任务 JSON"
    )


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
    options: ExecutionOptions | None = None


class Event(BaseModel):
    id: int
    event: str
    data: dict[str, Any]
    thread_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    created_at: str | None = None
    version: Literal[1] = 1


RequestStatus = Literal["pending", "responded", "resolved", "expired"]


class AgentRequest(BaseModel):
    id: str
    method: str
    thread_id: str
    turn_id: str | None = None
    run_id: str | None = None
    params: dict[str, Any]
    status: RequestStatus = "pending"
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    response: dict[str, Any] | None = None
    reason: str | None = None


class RequestResponse(StrictModel):
    result: dict[str, Any] = Field(
        description="Runtime 方法对应的响应对象，按 app/protocol 中的 schema 校验。",
        examples=[{"answers": {"question-id": {"answers": ["方案 A"]}}}, {"decision": "accept"}],
    )
