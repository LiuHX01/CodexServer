import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .config import ContainerCloudWorkerConfig
from .presets import execute_preset
from .protocol import TaskMessage

logger = logging.getLogger(__name__)
Action = Callable[[TaskMessage], Awaitable[dict[str, Any]]]


class ManagementClient:
    """Small allowlisted client for actions exposed by agent-management."""

    def __init__(self, config: ContainerCloudWorkerConfig):
        self.client = httpx.AsyncClient(
            base_url=config.management_url,
            headers={"Authorization": f"Bearer {config.management_token.get_secret_value()}"},
            timeout=config.management_timeout_seconds,
            follow_redirects=False,
        )

    async def close(self):
        await self.client.aclose()

    async def call(self, task: TaskMessage, method: str, path: str, body=None, *, idempotent=True):
        headers = {}
        if idempotent:
            headers["Idempotency-Key"] = task.task_id[:200]
        response = await self.client.request(method, path, json=body, headers=headers)
        try:
            result = response.json()
        except ValueError:
            result = {"text": response.text}
        if not response.is_success:
            detail = (
                result.get("detail", "management 请求失败")
                if isinstance(result, dict)
                else str(result)
            )
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        return {"ok": True, "status": response.status_code, "data": result}


class ManagementActionRegistry:
    """Maps protocol operations to explicit management actions.

    Add a new named action here when the container cloud contract grows. Unknown
    operations are rejected instead of being treated as arbitrary HTTP requests.
    """

    def __init__(self, config: ContainerCloudWorkerConfig):
        self.config = config
        self.client = ManagementClient(config)
        self.actions: dict[str, Action] = {
            "echo": self.echo,
            "agent_probe": self.agent_probe,
            "agent_patch": self.agent_patch,
            "create_session": self.create_session,
            "start_session": self.start_session,
            "archive_session": self.archive_session,
            "steer_run": self.steer_run,
            "interrupt_run": self.interrupt_run,
            "reply_request": self.reply_request,
        }

    async def close(self):
        await self.client.close()

    async def execute(self, task: TaskMessage):
        action = self.actions.get(task.operation)
        if action is None:
            raise KeyError(f"不支持的容器云操作: {task.operation}")
        return await action(task)

    async def echo(self, task: TaskMessage):
        text = task.payload.get("text")
        if not isinstance(text, str) or len(text) > 200:
            raise ValueError("echo.text 必须是长度不超过 200 的字符串")
        if text.startswith("test-"):
            return await execute_preset(task, self.config.preset_dir)
        return {"ok": True, "text": text}

    async def agent_probe(self, task):
        return await self.client.call(
            task, "POST", f"/v1/agents/{self.required(task, 'agent_id')}/probe", idempotent=False
        )

    async def agent_patch(self, task):
        return await self.client.call(
            task, "PATCH", f"/v1/agents/{self.required(task, 'agent_id')}", self.body(task)
        )

    async def create_session(self, task):
        return await self.client.call(
            task, "POST", f"/v1/agents/{self.required(task, 'agent_id')}/sessions", self.body(task)
        )

    async def start_session(self, task):
        return await self.client.call(
            task,
            "POST",
            f"/v1/sessions/{self.required(task, 'session_id')}/messages",
            self.body(task),
        )

    async def archive_session(self, task):
        return await self.client.call(
            task, "POST", f"/v1/sessions/{self.required(task, 'session_id')}/archive", {}
        )

    async def steer_run(self, task):
        return await self.client.call(
            task, "POST", f"/v1/runs/{self.required(task, 'run_id')}/input", self.body(task)
        )

    async def interrupt_run(self, task):
        return await self.client.call(
            task, "POST", f"/v1/runs/{self.required(task, 'run_id')}/interrupt", {}
        )

    async def reply_request(self, task):
        return await self.client.call(
            task,
            "POST",
            f"/v1/requests/{self.required(task, 'request_id')}/response",
            self.body(task),
        )

    @staticmethod
    def required(task: TaskMessage, field: str) -> str:
        value = task.payload.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{task.operation}.{field} 必须是非空字符串")
        return value

    @staticmethod
    def body(task: TaskMessage) -> dict[str, Any]:
        body = task.payload.get("body", {})
        if not isinstance(body, dict):
            raise ValueError(f"{task.operation}.body 必须是 JSON 对象")
        return body
