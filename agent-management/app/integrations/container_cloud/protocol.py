import json
from dataclasses import dataclass
from typing import Any


class ProtocolError(ValueError):
    """The remote worker protocol contains an invalid message."""


@dataclass(frozen=True)
class TaskMessage:
    task_id: str
    nonce: str | None
    operation: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ReadyMessage:
    expires_at: str | int | float | None


def decode(raw: str | bytes) -> dict[str, Any]:
    try:
        message = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProtocolError("容器云消息不是合法 JSON") from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ProtocolError("容器云消息必须是带 type 的 JSON 对象")
    return message


def parse_ready(message: dict[str, Any]) -> ReadyMessage:
    if message.get("type") != "ready":
        raise ProtocolError("连接建立后未收到 ready 消息")
    expires_at = message.get("expiresAt")
    if expires_at is not None and (
        isinstance(expires_at, bool) or not isinstance(expires_at, (str, int, float))
    ):
        raise ProtocolError("ready.expiresAt 必须是字符串或数字")
    return ReadyMessage(expires_at=expires_at)


def parse_task(message: dict[str, Any]) -> TaskMessage:
    if message.get("type") != "task":
        raise ProtocolError("未知的容器云消息类型")
    task_id = message.get("taskId")
    operation = message.get("operation")
    if not isinstance(task_id, str) or not task_id:
        raise ProtocolError("task.taskId 必须是非空字符串")
    if not isinstance(operation, str) or not operation:
        raise ProtocolError("task.operation 必须是非空字符串")
    payload = message.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("task.payload 必须是 JSON 对象")
    # The original demo protocol placed echo text at task.text. Keep accepting
    # that shape while using payload for all new management actions.
    if "text" in message and "text" not in payload:
        payload = {**payload, "text": message["text"]}
    nonce = message.get("nonce")
    if nonce is not None and not isinstance(nonce, str):
        raise ProtocolError("task.nonce 必须是字符串")
    return TaskMessage(task_id=task_id, nonce=nonce, operation=operation, payload=payload)


def encode(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))
