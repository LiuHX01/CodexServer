import asyncio
from contextlib import suppress

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from openai_codex import CodexConfig, InvalidRequestError
from openai_codex.async_client import AsyncCodexClient
from openai_codex.generated.v2_all import ThreadTurnsListResponse

from .config import AUTH_OVERRIDES, Settings


def encode(value):
    return jsonable_encoder(value, by_alias=True)


class CodexBackend:
    """Public low-level SDK API; no custom subprocess or JSON-RPC transport."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncCodexClient(
            CodexConfig(
                codex_bin=settings.codex_bin,
                cwd=str(settings.workspace_root.resolve()),
                env={"CODEX_HOME": str(settings.codex_home.resolve())},
                config_overrides=AUTH_OVERRIDES,
                client_name="codex_http_server",
                client_title="Codex HTTP Server",
                client_version="0.1.0",
            )
        )
        self.healthy = False
        self.drain: asyncio.Task | None = None
        self.drafts: set[str] = set()

    async def start(self):
        self.settings.codex_home.mkdir(parents=True, exist_ok=True)
        self.settings.workspace_root.mkdir(parents=True, exist_ok=True)
        try:
            await self.client.start()
            await self.client.initialize()
            self.healthy = True
            self.drain = asyncio.create_task(self._drain_global_events())
        except BaseException:
            await self.client.close()
            raise

    async def _drain_global_events(self):
        try:
            while True:
                await self.client.next_notification()
        except Exception:
            self.healthy = False

    async def close(self):
        self.healthy = False
        await self.client.close()
        if self.drain:
            self.drain.cancel()
            with suppress(asyncio.CancelledError):
                await self.drain

    def options(self, cwd: str | None = None) -> dict:
        options = {
            "approvalPolicy": "never",
            "sandbox": self.settings.codex_sandbox,
            "model": self.settings.codex_model,
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

    async def resume(self, thread_id: str):
        if thread_id in self.drafts:
            return await self.read(thread_id)
        result = await self.client.thread_resume(
            thread_id, {**self.options(), "excludeTurns": True}
        )
        return encode(result.thread)

    async def rename(self, thread_id: str, name: str):
        return encode(await self.client.thread_set_name(thread_id, name))

    async def archive(self, thread_id: str):
        if thread_id in self.drafts:
            raise HTTPException(409, "An empty thread has no persisted history to archive")
        return encode(await self.client.thread_archive(thread_id))

    async def start_turn(self, thread_id: str, prompt: str):
        params = {
            "approvalPolicy": "never",
            "effort": self.settings.codex_reasoning_effort,
            "model": self.settings.codex_model,
        }
        result = await self.client.turn_start(thread_id, prompt, params)
        self.drafts.discard(thread_id)
        return result.turn.id

    async def events(self, turn_id: str):
        try:
            while True:
                notification = await self.client.next_turn_notification(turn_id)
                data = encode(notification.payload)
                yield notification.method, data
                if notification.method == "turn/completed":
                    return
        finally:
            self.client.unregister_turn_notifications(turn_id)

    async def interrupt(self, thread_id: str, turn_id: str):
        await self.client.turn_interrupt(thread_id, turn_id)
