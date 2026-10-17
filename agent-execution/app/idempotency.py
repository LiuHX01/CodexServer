"""Persist HTTP mutation receipts so a hub can reconcile ambiguous delivery."""

import hashlib
import hmac
import json

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("idempotency-key")
        if request.method != "POST" or not request.url.path.startswith("/v1/") or not key:
            return await call_next(request)
        token = request.app.state.settings.api_token.get_secret_value()
        expected = f"Bearer {token}".encode()
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected):
            return await call_next(request)
        if len(key) > 200:
            return JSONResponse({"detail": "Idempotency-Key is too long"}, status_code=400)
        body = await request.body()
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            return await call_next(request)
        if isinstance(payload, dict) and payload.get("stream") is True:
            return JSONResponse(
                {"detail": "Use stream=false with Idempotency-Key"}, status_code=422
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                [request.method, request.url.path, payload],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        store = request.app.state.manager.store
        instance = request.headers.get("x-agent-instance")
        if instance and instance != store.instance_id:
            return JSONResponse(
                {"detail": "Agent data instance changed; reconcile the binding"}, status_code=409
            )
        if not await store.reserve_operation(key, fingerprint):
            existing = await store.operation(key)
            if existing["fingerprint"] != fingerprint:
                return JSONResponse(
                    {"detail": "Idempotency-Key belongs to another operation"}, status_code=409
                )
            if existing["status"] == "completed":
                return JSONResponse(
                    existing["response"],
                    status_code=existing["status_code"],
                    headers={"Idempotency-Replayed": "true"},
                )
            return JSONResponse(
                {"detail": "Operation is pending or requires reconciliation"}, status_code=409
            )
        try:
            response = await call_next(request)
            response_body = b"".join([chunk async for chunk in response.body_iterator])
            value = json.loads(response_body)
            if response.status_code >= 500:
                await store.uncertain_operation(key)
            else:
                await store.finish_operation(key, response.status_code, value)
            return JSONResponse(value, status_code=response.status_code)
        except BaseException:
            await store.uncertain_operation(key)
            raise
