"""SSE framing and replay shared by run and thread subscriptions."""

import asyncio
import json
import time
from contextlib import suppress

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from .models import TERMINAL


def replay_cursor(request: Request, after: int) -> int:
    header = request.headers.get("last-event-id")
    if header is None:
        return after
    try:
        value = int(header)
        if value < 0:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(400, "Last-Event-ID must be a nonnegative integer") from exc
    return max(after, value)


def event_response(
    request: Request,
    manager,
    *,
    run_id: str | None = None,
    thread_id: str | None = None,
    after: int = 0,
    follow: bool = True,
):
    store = manager.store

    async def stream():
        cursor, last_ping = after, time.monotonic()
        while True:
            # Query after clearing; the timeout also covers other subscribers
            # clearing this shared wakeup before we begin waiting.
            store.changed.clear()
            batch = await store.events(run_id, cursor, thread_id=thread_id)
            for event in batch:
                cursor = event.id
                # Preserve existing run SSE payloads. Thread streams add correlation metadata.
                data = event.data if run_id is not None else event.model_dump(mode="json")
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.id}\nevent: {event.event}\ndata: {payload}\n\n"
            if await request.is_disconnected() or manager.stopping:
                return
            if batch:
                continue
            if not follow:
                return
            if run_id is not None and (await manager.get(run_id)).status in TERMINAL:
                # Completion can commit between the event query and the state query.
                if await store.events(run_id, cursor, 1):
                    continue
                return
            if time.monotonic() - last_ping >= 15:
                yield ": ping\n\n"
                last_ping = time.monotonic()
            with suppress(TimeoutError):
                await asyncio.wait_for(store.changed.wait(), timeout=1)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if run_id is not None:
        headers["X-Run-ID"] = run_id
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)
