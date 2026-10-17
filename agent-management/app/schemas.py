from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    token: SecretStr


class AgentCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    base_url: str
    token: SecretStr = Field(min_length=24)
    provider: Literal["codex"] = "codex"

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("请输入不含账号密码的 HTTP 服务地址")
        if url.query or url.fragment:
            raise ValueError("服务地址不能包含查询参数或片段")
        return value.rstrip("/")


class AgentPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    token: SecretStr | None = Field(default=None, min_length=24)


class SessionCreate(Input):
    name: str = Field(default="新会话", min_length=1, max_length=200)
    cwd: str | None = None


class SessionImport(Input):
    thread_id: str = Field(min_length=1, max_length=100)


class Options(Input):
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    approval_policy: Literal["never", "on-request", "untrusted"] | None = None
    collaboration_mode: Literal["default", "plan"] | None = None


class Text(Input):
    prompt: str = Field(min_length=1, max_length=100000)

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("输入不能为空白")
        return value


class Message(Text):
    options: Options | None = None


class Answer(Input):
    result: dict[str, Any]


class Member(Input):
    principal: str = Field(min_length=1, max_length=100)
    permission: Literal["view", "operate"]
