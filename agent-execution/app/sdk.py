"""Deferred server requests for the pinned openai-codex SDK.

The SDK answers server requests synchronously on its only reader thread. This
adapter diverts those requests to asyncio, leaving SDK notification routing and
RPC waiters intact. All dependencies on SDK private methods live here; upgrades
must pass tests/test_sdk_transport.py before changing the pinned SDK version.
"""

import asyncio
from collections.abc import Callable

from openai_codex import CodexConfig
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexClient
from openai_codex.models import JsonObject


class DeferredRequestClient(CodexClient):
    def __init__(self, config: CodexConfig, deliver: Callable[[dict], None]):
        super().__init__(config)
        self.deliver = deliver

    def _read_message(self):
        while True:
            message = super()._read_message()
            if "method" in message and "id" in message:
                # Never wait for a person on the SDK reader thread.
                self.deliver(message)
                continue
            if message.get("method") == "serverRequest/resolved":
                # Share the request queue to preserve request/resolution wire order.
                self.deliver(message)
            return message

    def respond(self, wire_id: str | int, *, result=None, error=None):
        message = {"id": wire_id}
        message["error" if error is not None else "result"] = error if error is not None else result
        self._write_message(message)


class InteractiveCodexClient(AsyncCodexClient):
    def __init__(self, config: CodexConfig):
        # AsyncCodexClient has no public sync-client injection hook in 0.162.1.
        self._sync = DeferredRequestClient(config, self._deliver)
        self.control_events: asyncio.Queue[dict] = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None

    def _deliver(self, message: dict):
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.control_events.put_nowait, message)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        await super().start()

    async def respond(
        self,
        wire_id: str | int,
        *,
        result: JsonObject | None = None,
        error: JsonObject | None = None,
    ):
        await self._call_sync(self._sync.respond, wire_id, result=result, error=error)
