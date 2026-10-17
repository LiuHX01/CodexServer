import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from uuid import uuid4

from fastapi import HTTPException
from openai_codex import InvalidParamsError, InvalidRequestError

from .backend import CodexBackend
from .config import Settings
from .interactions import Interactions
from .models import TERMINAL, Message, Run
from .store import Store

logger = logging.getLogger(__name__)


@dataclass
class LiveRun:
    run: Run
    message: Message
    task: asyncio.Task | None = None
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Manager:
    def __init__(self, backend: CodexBackend, store: Store, settings: Settings):
        self.backend, self.store, self.settings = backend, store, settings
        self.slots = asyncio.Semaphore(settings.max_concurrent_runs)
        self.owners: dict[str, str] = {}
        self.live: dict[str, LiveRun] = {}
        self.admitted: set[str] = set()
        self.stopping = False
        self.interactions = Interactions(backend, store, self.active_run, self.check_thread)
        backend.on_notification = self.notification
        backend.on_control = self.interactions.receive
        backend.on_disconnect = self.disconnected

    def active_run(self, thread_id: str) -> Run | None:
        live = self.live.get(self.owners.get(thread_id))
        if live and live.run.status not in TERMINAL and not live.run.cancel_requested:
            return live.run
        return None

    async def notification(self, event: str, data: dict):
        thread_id = data.get("threadId") or data.get("thread", {}).get("id")
        if not thread_id:
            return
        try:
            await self.check_thread(thread_id)
        except (HTTPException, InvalidParamsError, InvalidRequestError):
            return
        await self.store.publish(event, data, thread_id=thread_id, turn_id=data.get("turnId"))

    async def disconnected(self):
        await self.interactions.expire("Codex runtime connection closed")

    def claim(self, thread_id: str, owner: str):
        if self.stopping or not self.backend.healthy:
            raise HTTPException(503, "Codex backend is unavailable")
        if thread_id in self.owners:
            raise HTTPException(409, {"message": "Thread is busy", "owner": self.owners[thread_id]})
        self.owners[thread_id] = owner

    @asynccontextmanager
    async def manage(self, thread_id: str):
        self.claim(thread_id, "management")
        try:
            yield
        finally:
            self.owners.pop(thread_id, None)

    async def check_thread(self, thread_id: str, *, require_idle: bool = False):
        thread = await self.backend.read(thread_id)
        try:
            self.settings.workspace(thread["cwd"])
        except (ValueError, KeyError) as exc:
            raise HTTPException(
                403, "Thread workspace is outside WORKSPACE_ROOT or missing"
            ) from exc
        if require_idle and thread.get("status", {}).get("type") == "active":
            raise HTTPException(409, "Thread has an active Codex turn")
        return thread

    async def submit(self, thread_id: str, message: Message) -> Run:
        # Reservations happen before the first await, including management requests.
        if len(self.admitted) >= self.settings.max_pending_runs:
            raise HTTPException(429, "Run queue is full", headers={"Retry-After": "2"})
        try:
            execution = self.settings.execution_options(message.options)
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from exc
        run = Run(id=uuid4().hex, thread_id=thread_id, options=execution)
        self.claim(thread_id, run.id)
        self.admitted.add(run.id)
        try:
            await self.check_thread(thread_id, require_idle=True)
            await self.store.save(run, "run.queued", run.model_dump(mode="json"))
            live = LiveRun(run, message)
            self.live[run.id] = live
            live.task = asyncio.create_task(self._execute(live), name=f"run-{run.id}")
            # Establish the runner's try/finally before exposing its ID to cancellation.
            await asyncio.sleep(0)
            return run.model_copy()
        except BaseException:
            if run.id not in self.live:
                self.owners.pop(thread_id, None)
                self.admitted.discard(run.id)
            raise

    async def _save(self, live: LiveRun, event: str, data: dict):
        await self.store.save(live.run, event, data)

    async def _execute(self, live: LiveRun):
        run = live.run
        started = time.monotonic()
        try:
            async with self.slots:
                if run.cancel_requested:
                    run.status = "interrupted"
                    return
                run.status = "running"
                await self._save(live, "run.running", run.model_dump(mode="json"))
                logger.info(
                    "event=run_started run_id=%s thread_id=%s model=%s",
                    run.id,
                    run.thread_id,
                    run.options.model if run.options else "default",
                )
                # Enforce server-side sandbox settings even on a persisted thread.
                await self.backend.resume(run.thread_id, run.options)
                if run.cancel_requested:
                    run.status = "interrupted"
                    return
                run.turn_id = await self.backend.start_turn(
                    run.thread_id, live.message.prompt, run.options
                )
                await self._save(live, "run.started", run.model_dump(mode="json"))
                if run.cancel_requested:
                    await self.backend.interrupt(run.thread_id, run.turn_id)
                # Inference/tool execution timeout starts once Codex has allocated the turn.
                async with asyncio.timeout(self.settings.run_timeout_seconds):
                    await self._consume(live)
        except asyncio.CancelledError:
            run.status = "interrupted"
            run.error = "Service stopped" if self.stopping else "Cancelled before execution"
            if run.turn_id:
                with suppress(Exception):
                    async with asyncio.timeout(5):
                        await self.backend.interrupt(run.thread_id, run.turn_id)
        except TimeoutError:
            run.status, run.error = "failed", "Turn exceeded RUN_TIMEOUT_SECONDS"
            if run.turn_id:
                with suppress(Exception):
                    async with asyncio.timeout(5):
                        await self.backend.interrupt(run.thread_id, run.turn_id)
        except Exception:
            logger.exception("Codex run failed: %s", run.id)
            run.status, run.error = "failed", "Codex operation failed; inspect server logs"
            if run.turn_id:
                with suppress(Exception):
                    async with asyncio.timeout(5):
                        await self.backend.interrupt(run.thread_id, run.turn_id)
        finally:
            try:
                async with live.control_lock:
                    await self.interactions.expire("Execution ended", run_id=run.id)
                    await self._save(live, "run.finished", run.model_dump(mode="json"))
            except Exception:
                logger.exception("Unable to persist run completion: %s", run.id)
            finally:
                self.live.pop(run.id, None)
                self.admitted.discard(run.id)
                self.owners.pop(run.thread_id, None)
                logger.info(
                    "event=run_finished run_id=%s thread_id=%s status=%s duration_ms=%d",
                    run.id,
                    run.thread_id,
                    run.status,
                    round((time.monotonic() - started) * 1000),
                )

    async def _consume(self, live: LiveRun):
        run = live.run
        last_message = None
        final_message = None
        stream = self.backend.events(run.turn_id)
        try:
            async for event, data in stream:
                if event == "item/completed" and data.get("item", {}).get("type") == "agentMessage":
                    item = data["item"]
                    last_message = item.get("text")
                    if item.get("phase") == "final_answer":
                        final_message = last_message
                if event == "turn/completed":
                    async with live.control_lock:
                        await self._save(live, event, data)
                        turn = data["turn"]
                        status = turn["status"]
                        run.status = status if status in TERMINAL else "failed"
                        run.error = (turn.get("error") or {}).get("message")
                        run.final_response = (
                            final_message if final_message is not None else last_message
                        )
                    return
                await self._save(live, event, data)
        finally:
            await stream.aclose()
        raise RuntimeError("Codex stream ended without turn/completed")

    async def steer(self, run_id: str, prompt: str):
        live = self.live.get(run_id)
        if live is None:
            await self.get(run_id)
            raise HTTPException(409, {"code": "run_not_active"})
        async with live.control_lock:
            run = live.run
            if run.status != "running" or not run.turn_id or run.cancel_requested:
                raise HTTPException(409, {"code": "run_not_active", "status": run.status})
            result = await self.backend.steer(run.thread_id, run.turn_id, prompt)
            await self._save(
                live,
                "run.input_accepted",
                {"run_id": run.id, "turn_id": run.turn_id, "prompt": prompt},
            )
            return result

    async def get(self, run_id: str) -> Run:
        run = await self.store.get(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        return run

    async def cancel(self, run_id: str):
        live = self.live.get(run_id)
        if live is None:
            return await self.get(run_id)
        async with live.control_lock:
            run = live.run
            if run.status in TERMINAL or run.cancel_requested:
                return run.model_copy()
            run.cancel_requested = True
            await self._save(live, "run.cancel_requested", {"run_id": run.id})
            await self.interactions.expire("Execution cancellation requested", run_id=run.id)
        if run.status == "queued":
            live.task.cancel()
            await live.task
        elif run.turn_id and run.status not in TERMINAL:
            await self.backend.interrupt(run.thread_id, run.turn_id)
        # Cancellation of a running turn is acknowledged by turn/completed, not the RPC reply.
        return run.model_copy()

    async def shutdown(self):
        self.stopping = True
        await self.interactions.expire("Service stopped")
        active = list(self.live.values())
        if not active:
            return

        async def request_cancel(live):
            with suppress(Exception):
                await self.cancel(live.run.id)

        try:
            async with asyncio.timeout(self.settings.shutdown_timeout_seconds):
                await asyncio.gather(*(request_cancel(live) for live in active))
                await asyncio.gather(*(live.task for live in active), return_exceptions=True)
        except TimeoutError:
            for live in active:
                live.task.cancel()
            # Close transport first so blocked SDK worker threads are released.
            await self.backend.close()
            await asyncio.gather(*(live.task for live in active), return_exceptions=True)
