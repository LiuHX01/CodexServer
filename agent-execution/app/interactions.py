"""Agent-initiated requests and their lifetime, independent of business workflows."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from jsonschema import Draft7Validator, ValidationError
from openai_codex import InvalidParamsError, InvalidRequestError

from .backend import CodexBackend
from .models import AgentRequest, Run
from .store import Store

SCHEMAS = {
    "item/tool/requestUserInput": "ToolRequestUserInputResponse",
    "item/commandExecution/requestApproval": "CommandExecutionRequestApprovalResponse",
    "item/fileChange/requestApproval": "FileChangeRequestApprovalResponse",
    "item/permissions/requestApproval": "PermissionsRequestApprovalResponse",
    "mcpServer/elicitation/request": "McpServerElicitationRequestResponse",
}
VALIDATORS = {
    method: Draft7Validator(
        json.loads((Path(__file__).parent / "protocol" / f"{schema}.json").read_text())
    )
    for method, schema in SCHEMAS.items()
}


class Interactions:
    def __init__(
        self,
        backend: CodexBackend,
        store: Store,
        find_run: Callable[[str], Run | None],
        check_thread: Callable[[str], Awaitable[dict]],
    ):
        self.backend = backend
        self.store = store
        self.find_run = find_run
        self.check_thread = check_thread
        self.lock = asyncio.Lock()
        # Wire IDs belong to one runtime process. They are never persisted or reused.
        self.live: dict[str, tuple[AgentRequest, str | int]] = {}

    async def receive(self, message: dict):
        if "id" not in message:
            await self._resolved(message["params"])
            return
        method, wire_id = message["method"], message["id"]
        if method not in VALIDATORS:
            await self.backend.respond(
                wire_id, error={"code": -32601, "message": f"Unsupported server request: {method}"}
            )
            return
        params = message.get("params", {})
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            await self.backend.respond(
                wire_id, error={"code": -32602, "message": "A threadId is required"}
            )
            return
        try:
            await self.check_thread(thread_id)
        except (HTTPException, InvalidParamsError, InvalidRequestError):
            await self.backend.respond(
                wire_id, error={"code": -32602, "message": "Thread is outside the served workspace"}
            )
            return
        async with self.lock:
            run = self.find_run(thread_id)
            request = AgentRequest(
                id=uuid4().hex,
                method=method,
                thread_id=thread_id,
                turn_id=params.get("turnId"),
                run_id=run.id if run else None,
                params=params,
            )
            if run is None or (run.turn_id and request.turn_id not in (None, run.turn_id)):
                request.status = "expired"
                request.reason = "The originating execution is no longer active"
                await self.store.save_request(request, "request.expired")
                await self.backend.respond(
                    wire_id, error={"code": -32600, "message": request.reason}
                )
                return
            await self.store.save_request(request, "request.pending")
            self.live[request.id] = (request, wire_id)

    async def respond(self, request_id: str, result: dict) -> AgentRequest:
        # Once accepted, finish the wire write and state update even if HTTP disconnects.
        return await asyncio.shield(self._respond(request_id, result))

    async def _respond(self, request_id: str, result: dict) -> AgentRequest:
        async with self.lock:
            record = await self.store.get_request(request_id)
            if record is None:
                raise HTTPException(404, "Agent request not found")
            live = self.live.get(request_id)
            if record.status != "pending" or live is None or live[0].status != "pending":
                raise HTTPException(409, {"code": "request_not_pending", "status": record.status})
            run = self.find_run(record.thread_id)
            if run is None or run.id != record.run_id:
                raise HTTPException(409, {"code": "run_not_active"})
            self._validate_response(record, result)
            request, wire_id = live
            # Hold the lifetime lock until the write finishes. Resolution/expiration
            # cannot overtake it and a second responder cannot send another answer.
            try:
                await self.backend.respond(wire_id, result=result)
            except Exception:
                # A failed write may have reached the peer. Never retry automatically.
                request.status = "expired"
                request.reason = "Response delivery failed; the runtime outcome is unknown"
                await self.store.save_request(request, "request.expired")
                self.live.pop(request_id)
                raise
            request.status = "responded"
            request.response = result
            await self.store.save_request(request, "request.responded")
            return request.model_copy(deep=True)

    @staticmethod
    def _validate_response(request: AgentRequest, result: dict):
        try:
            VALIDATORS[request.method].validate(result)
        except ValidationError as exc:
            raise HTTPException(
                422,
                {"code": "invalid_agent_response", "path": list(exc.path), "message": exc.message},
            ) from exc
        if request.method == "item/tool/requestUserInput":
            question_ids = {question["id"] for question in request.params["questions"]}
            if set(result["answers"]) - question_ids:
                raise HTTPException(422, "Response contains an unknown question ID")
        if request.method == "item/commandExecution/requestApproval":
            available = request.params.get("availableDecisions")
            if available is not None and result["decision"] not in available:
                raise HTTPException(422, "Decision is not available for this request")

    async def _resolved(self, params: dict):
        async with self.lock:
            for request_id, (request, wire_id) in tuple(self.live.items()):
                if wire_id != params.get("requestId") or request.thread_id != params.get(
                    "threadId"
                ):
                    continue
                request.status = "resolved" if request.status == "responded" else "expired"
                if request.status == "expired":
                    request.reason = "Runtime cleared the request before a response was submitted"
                await self.store.save_request(request, f"request.{request.status}")
                self.live.pop(request_id)

    async def expire(self, reason: str, *, run_id: str | None = None):
        async with self.lock:
            for request_id, (request, _) in tuple(self.live.items()):
                if run_id is not None and request.run_id != run_id:
                    continue
                request.status, request.reason = "expired", reason
                await self.store.save_request(request, "request.expired")
                self.live.pop(request_id)
