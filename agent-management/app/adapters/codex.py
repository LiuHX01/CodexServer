import json
from collections.abc import AsyncIterator

import httpx

from .base import DeliveryUnknown, ProviderError


class CodexAdapter:
    def __init__(self, base_url: str, token: str, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(15, read=25),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    @staticmethod
    def decode(response):
        if not response.is_success:
            # Provider errors can include command output; do not leak it into credentials/logs.
            if response.status_code in {401, 403}:
                message = "执行端拒绝访问，请检查凭证或权限"
            elif response.status_code == 409:
                message = "执行端状态已变化或当前操作冲突，请刷新后核对"
            elif response.status_code == 422:
                message = "执行端拒绝请求格式，请按请求协议检查参数"
            else:
                message = f"执行端返回 HTTP {response.status_code}"
            raise ProviderError(response.status_code, message)
        return response.json()

    async def get(self, path):
        return self.decode(await self.client.get(path))

    async def inspect(self):
        status = await self.get("/v1/status")
        try:
            capabilities = await self.get("/v1/capabilities")
        except ProviderError as exc:
            if exc.status != 404:
                raise
            capabilities = {
                "protocol_version": 0,
                "capabilities": {
                    "steering": True,
                    "human_requests": True,
                    "thread_events": True,
                    "idempotency": False,
                },
            }
        return status, capabilities

    async def mutate(self, path, payload, key):
        try:
            response = await self.client.post(path, json=payload, headers={"Idempotency-Key": key})
            # 5xx may occur after a side effect. Reconcile instead of declaring a safe retry.
            if response.status_code >= 500:
                raise DeliveryUnknown("执行端结果待核对")
            return self.decode(response)
        except (httpx.TransportError, ValueError) as exc:
            raise DeliveryUnknown("连接中断，远端操作结果待核对") from exc

    async def watch(self, thread_id, cursor) -> AsyncIterator[dict]:
        async with self.client.stream(
            "GET", f"/v1/threads/{thread_id}/events", params={"after": cursor}
        ) as response:
            if not response.is_success:
                await response.aread()
                self.decode(response)
            fields = {}
            async for line in response.aiter_lines():
                if not line:
                    if "data" in fields:
                        envelope = json.loads(fields["data"])
                        if not isinstance(envelope.get("id"), int) or not envelope.get("event"):
                            raise ValueError("执行端事件缺少标识")
                        yield envelope
                    fields = {}
                elif line.startswith("data:"):
                    fields["data"] = fields.get("data", "") + line[5:].lstrip()

    async def close(self):
        await self.client.aclose()


class AdapterRegistry:
    def __init__(self, security):
        self.security = security
        self.factories = {"codex": CodexAdapter}

    def open(self, agent):
        adapter = self.factories[agent.provider](
            agent.base_url, self.security.decrypt(agent.credential)
        )
        if agent.instance_id and agent.instance_id != "legacy":
            adapter.client.headers["X-Agent-Instance"] = agent.instance_id
        return adapter
