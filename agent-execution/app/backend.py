import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from openai_codex import CodexConfig, InvalidRequestError
from openai_codex.generated.v2_all import ThreadTurnsListResponse
from openai_codex.models import UnknownNotification

from .config import Settings
from .models import ExecutionOptions
from .provider import runtime_provider
from .sdk import InteractiveCodexClient

logger = logging.getLogger(__name__)


def encode(value):
    return jsonable_encoder(value, by_alias=True)


class CodexBackend:
    """Codex operations, with deferred requests isolated in the SDK adapter."""

    def __init__(self, settings: Settings):
        self.settings = settings
        provider = runtime_provider(settings)
        self.client = InteractiveCodexClient(
            CodexConfig(
                codex_bin=settings.codex_bin,
                cwd=str(settings.workspace_root.resolve()),
                env={"CODEX_HOME": str(settings.codex_home.resolve()), **provider.env},
                config_overrides=provider.overrides,
                client_name="codex_http_server",
                client_title="Codex HTTP Server",
                client_version="0.1.0",
            )
        )
        self.healthy = False
        self.drain: asyncio.Task | None = None
        self.controls: asyncio.Task | None = None
        self.on_notification: Callable[[str, dict], Awaitable[None]] | None = None
        self.on_control: Callable[[dict], Awaitable[None]] | None = None
        self.on_disconnect: Callable[[], Awaitable[None]] | None = None
        self.drafts: set[str] = set()

    async def start(self):
        self.settings.codex_home.mkdir(parents=True, exist_ok=True)
        self.settings.workspace_root.mkdir(parents=True, exist_ok=True)
        try:
            await self.client.start()
            await self.client.initialize()
            self.healthy = True
            self.drain = asyncio.create_task(self._drain_global_events())
            self.controls = asyncio.create_task(self._drain_control_events())
        except BaseException:
            await self.client.close()
            raise

    async def _drain_global_events(self):
        try:
            while True:
                notification = await self.client.next_notification()
                if notification.method != "serverRequest/resolved" and self.on_notification:
                    await self.on_notification(
                        notification.method, self.notification_data(notification)
                    )
        except Exception:
            await self._disconnected()

    async def _drain_control_events(self):
        try:
            while True:
                message = await self.client.control_events.get()
                if self.on_control:
                    await self.on_control(message)
        except Exception:
            await self._disconnected()

    async def _disconnected(self):
        if self.healthy:
            self.healthy = False
            logger.exception("Codex event receiver stopped")
            if self.on_disconnect:
                await self.on_disconnect()

    @staticmethod
    def notification_data(notification):
        payload = notification.payload
        return encode(payload.params if isinstance(payload, UnknownNotification) else payload)

    async def close(self):
        self.healthy = False
        tasks = [task for task in (self.drain, self.controls) if task]
        for task in tasks:
            task.cancel()
        await self.client.close()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    def options(self, cwd: str | None = None, execution: ExecutionOptions | None = None) -> dict:
        execution = execution or self.settings.execution_options(None)
        options = {
            "approvalPolicy": execution.approval_policy,
            "sandbox": self.settings.codex_sandbox,
            "model": execution.model,
        }
        if cwd is not None:
            options["cwd"] = cwd
        return options

    async def create(self, cwd: str):
        result = await self.client.thread_start(self.options(cwd))
        self.drafts.add(result.thread.id)
        return encode(result.thread)

    async def read(self, thread_id: str):
        return encode((await self.client.thread_read(thread_id)).thread)

    async def history(self, thread_id: str, cursor: str | None, limit: int):
        try:
            result = await self.client.request(
                "thread/turns/list",
                {"threadId": thread_id, "cursor": cursor, "limit": limit, "itemsView": "full"},
                response_model=ThreadTurnsListResponse,
            )
        except InvalidRequestError as exc:
            if "is not materialized yet" not in exc.message:
                raise
            return {"data": [], "nextCursor": None, "backwardsCursor": None}
        return encode(result)

    async def list(self, cursor: str | None, limit: int, archived: bool = False):
        # app-server sessions are excluded by the runtime's default interactive-source filter.
        params = {
            "cursor": cursor,
            "limit": limit,
            "archived": archived,
            "sortKey": "updated_at",
            "sourceKinds": ["appServer", "cli", "vscode", "exec"],
        }
        result = encode(await self.client.thread_list(params))
        # Empty threads are live drafts until the runtime persists the first user message.
        drafts = []
        if not archived:
            for thread_id in tuple(self.drafts):
                thread = await self.read(thread_id)
                drafts.append(thread)
        result["drafts"] = drafts
        return result

    async def resume(self, thread_id: str, execution: ExecutionOptions | None = None):
        if thread_id in self.drafts:
            return await self.read(thread_id)
        result = await self.client.thread_resume(
            thread_id, {**self.options(execution=execution), "excludeTurns": True}
        )
        return encode(result.thread)

    async def rename(self, thread_id: str, name: str):
        return encode(await self.client.thread_set_name(thread_id, name))

    async def archive(self, thread_id: str):
        if thread_id in self.drafts:
            raise HTTPException(409, "An empty thread has no persisted history to archive")
        return encode(await self.client.thread_archive(thread_id))

    async def start_turn(
        self, thread_id: str, prompt: str, execution: ExecutionOptions | None = None
    ):
        execution = execution or self.settings.execution_options(None)
        params = {
            "approvalPolicy": execution.approval_policy,
            "effort": execution.reasoning_effort,
            "model": execution.model,
            "collaborationMode": {
                "mode": execution.collaboration_mode or "default",
                "settings": {
                    "model": execution.model,
                    "reasoning_effort": execution.reasoning_effort,
                    "developer_instructions": None,
                },
            },
        }
        result = await self.client.turn_start(thread_id, prompt, params)
        self.drafts.discard(thread_id)
        return result.turn.id

    async def events(self, turn_id: str):
        try:
            while True:
                notification = await self.client.next_turn_notification(turn_id)
                data = self.notification_data(notification)
                yield notification.method, data
                if notification.method == "turn/completed":
                    return
        finally:
            self.client.unregister_turn_notifications(turn_id)

    async def interrupt(self, thread_id: str, turn_id: str):
        await self.client.turn_interrupt(thread_id, turn_id)

    async def steer(self, thread_id: str, turn_id: str, prompt: str):
        return encode(await self.client.turn_steer(thread_id, turn_id, prompt))

    async def respond(self, wire_id, *, result=None, error=None):
        await self.client.respond(wire_id, result=result, error=error)
