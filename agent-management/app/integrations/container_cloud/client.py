import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from .config import ContainerCloudWorkerConfig
from .protocol import decode, encode, parse_ready

logger = logging.getLogger(__name__)


class ContainerCloudConnection:
    def __init__(self, config: ContainerCloudWorkerConfig, identity: dict[str, Any]):
        self.config = config
        self.identity = identity
        self.socket: ClientConnection | None = None

    @asynccontextmanager
    async def open(self) -> AsyncIterator["ContainerCloudConnection"]:
        async with connect(
            self.config.endpoint,
            open_timeout=self.config.connect_timeout_seconds,
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            self.socket = socket
            await self.send(
                {"type": "hello", "token": self.config.token.get_secret_value(), **self.identity}
            )
            try:
                first = await asyncio.wait_for(socket.recv(), self.config.connect_timeout_seconds)
                ready = parse_ready(decode(first))
                logger.info(
                    "event=container_cloud_ready hostname=%s expires_at=%s",
                    self.identity.get("hostname", "unknown"),
                    ready.expires_at,
                )
                yield self
            finally:
                self.socket = None

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        if self.socket is None:
            raise RuntimeError("容器云连接尚未建立")
        async for raw in self.socket:
            yield decode(raw)

    async def close(self):
        if self.socket is not None:
            await self.socket.close()

    async def send(self, message: dict[str, Any]):
        if self.socket is None:
            raise RuntimeError("容器云连接尚未建立")
        await self.socket.send(encode(message))

    async def result(self, task_id: str, nonce: str | None, result: Any):
        message = {"type": "result", "taskId": task_id, "result": result}
        if nonce is not None:
            message["nonce"] = nonce
        if isinstance(result, dict) and "text" in result:
            message["text"] = result["text"]
        await self.send(message)

    async def error(self, task_id: str, nonce: str | None, code: str, message: str):
        payload = {"code": code, "message": message}
        await self.result(task_id, nonce, {"ok": False, "error": payload})
