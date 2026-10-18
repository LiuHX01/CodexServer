import asyncio
import logging
import signal
import socket
import time
from collections.abc import Callable
from typing import Any

from websockets.exceptions import ConnectionClosed, InvalidURI

from .actions import ManagementActionRegistry
from .client import ContainerCloudConnection
from .config import ContainerCloudWorkerConfig
from .observe import follow_command
from .protocol import ProtocolError, parse_task

logger = logging.getLogger(__name__)


def connection_error_summary(exc: Exception) -> str:
    """Keep reconnect-loop logs useful without dumping library internals or URLs."""
    if isinstance(exc, InvalidURI):
        return "WebSocket 地址无效或被重定向到非 ws/wss 地址，通常是登录态或 endpoint 配置问题"
    if isinstance(exc, ConnectionClosed):
        return f"WebSocket 已关闭 code={exc.code} reason={exc.reason or '-'}"
    detail = str(exc).splitlines()[0].strip()
    if len(detail) > 240:
        detail = f"{detail[:237]}..."
    return f"{type(exc).__name__}: {detail or '无详细信息'}"


class ContainerCloudWorker:
    def __init__(
        self,
        config: ContainerCloudWorkerConfig,
        actions: ManagementActionRegistry | None = None,
        *,
        connection_factory: Callable[..., ContainerCloudConnection] = ContainerCloudConnection,
    ):
        config.validate()
        self.config = config
        self.actions = actions or ManagementActionRegistry(config)
        self.connection_factory = connection_factory
        self.stop_event = asyncio.Event()
        self.identity = self._identity()
        self.active_connection = None
        self.followers = {}

    def _identity(self) -> dict[str, Any]:
        return {"hostname": socket.gethostname(), "localAddressMatchesTarget": True}

    async def run(self):
        attempts = 0
        while not self.stop_event.is_set():
            try:
                connection = self.connection_factory(self.config, self.identity)
                self.active_connection = connection
                async with connection.open():
                    attempts = 0
                    try:
                        async for message in connection.messages():
                            await self.handle(connection, message)
                    finally:
                        await self.cancel_followers()
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                if exc.code in {4001, 4003, 4009}:
                    logger.error("container cloud worker pairing ended with code %s", exc.code)
                    self.stop()
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                logger.error(
                    "event=container_cloud_connection_failed reason=%s",
                    connection_error_summary(exc),
                )
            self.active_connection = None
            if self.stop_event.is_set():
                break
            delay = min(
                self.config.reconnect_initial_seconds * 2**attempts,
                self.config.reconnect_max_seconds,
            )
            attempts += 1
            logger.info("reconnecting container cloud worker in %.1fs", delay)
            try:
                await asyncio.wait_for(self.stop_event.wait(), delay)
            except TimeoutError:
                pass

    async def handle(self, connection, message: dict[str, Any]):
        if message.get("type") == "ack":
            logger.info("container cloud task acknowledged: %s", message.get("taskId"))
            return
        if message.get("type") != "task":
            logger.debug("ignoring non-task container cloud message: %s", message.get("type"))
            return
        started = time.monotonic()
        task_id = message.get("taskId", "unknown")
        operation = message.get("operation", "unknown")
        logger.info("event=task_received task_id=%s operation=%s", task_id, operation)
        try:
            task = parse_task(message)
            result = await self.actions.execute(task)
        except (KeyError, ProtocolError, ValueError) as exc:
            task_id = message.get("taskId", "unknown")
            nonce = message.get("nonce") if isinstance(message.get("nonce"), str) else None
            detail = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            logger.error("container cloud task rejected: taskId=%s reason=%s", task_id, detail)
            await connection.error(task_id, nonce, "invalid_task", detail)
            return
        except Exception as exc:
            logger.exception("container cloud task failed: %s", message.get("taskId"))
            task_id = message.get("taskId", "unknown")
            nonce = message.get("nonce") if isinstance(message.get("nonce"), str) else None
            await connection.error(task_id, nonce, "management_error", str(exc))
            return
        await connection.result(task.task_id, task.nonce, result)
        logger.info(
            "event=task_result_sent task_id=%s operation=%s duration_ms=%d",
            task.task_id,
            task.operation,
            round((time.monotonic() - started) * 1000),
        )
        command = result.get("data") if isinstance(result, dict) else None
        if (
            task.operation
            in {
                "create_session",
                "start_session",
                "archive_session",
                "steer_run",
                "interrupt_run",
                "reply_request",
            }
            and isinstance(command, dict)
            and command.get("id")
            and command.get("session_id")
        ):
            key = (task.task_id, task.nonce)
            old = self.followers.get(key)
            if old is None or old.done():
                follow_operation = result.get("follow_operation")
                if follow_operation:
                    follower = asyncio.create_task(
                        self.follow(
                            connection,
                            task,
                            command,
                            operation=follow_operation,
                        )
                    )
                else:
                    follower = asyncio.create_task(self.follow(connection, task, command))
                self.followers[key] = follower

                def forget(done):
                    if self.followers.get(key) is done:
                        self.followers.pop(key, None)

                follower.add_done_callback(forget)

    async def follow(self, connection, task, command, *, operation=None):
        async def publish(event, data):
            await connection.send(
                {
                    "type": "task_update",
                    "taskId": task.task_id,
                    "nonce": task.nonce,
                    "event": event,
                    "data": data,
                }
            )

        try:
            await follow_command(
                self.actions.client,
                task,
                command,
                publish,
                operation=operation,
                poll_seconds=self.config.result_poll_seconds,
                timeout_seconds=self.config.result_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("container cloud observation failed: %s", task.task_id)
            try:
                await publish(
                    "observation_error", {"code": "observation_failed", "message": str(exc)}
                )
            except Exception:
                logger.warning("unable to send observation error: %s", task.task_id)

    async def cancel_followers(self):
        followers = list(self.followers.values())
        for follower in followers:
            follower.cancel()
        await asyncio.gather(*followers, return_exceptions=True)
        self.followers.clear()

    def stop(self):
        self.stop_event.set()
        if self.active_connection is not None:
            try:
                asyncio.create_task(self.active_connection.close())
            except RuntimeError:
                pass

    async def close(self):
        self.stop()
        await self.cancel_followers()
        await self.actions.close()


async def run_worker(worker: ContainerCloudWorker):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            pass
    try:
        await worker.run()
    finally:
        await worker.close()
