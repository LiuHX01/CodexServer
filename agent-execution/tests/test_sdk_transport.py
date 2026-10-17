"""Exercise the pinned SDK reader using a real child process, without model calls."""

import asyncio
import sys

import pytest
from openai_codex import CodexConfig, TransportClosedError
from pydantic import BaseModel

from app.sdk import InteractiveCodexClient

PEER = """
import json
import sys

def send(message):
    print(json.dumps(message), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"serverInfo": {"version": "0.162.1"}}})
    elif method == "initialized":
        for wire_id, thread in [(17, "first"), ("18", "second")]:
            send({"id": wire_id, "method": "item/tool/requestUserInput",
                  "params": {"threadId": thread, "turnId": "turn-" + thread,
                             "questions": [{"id": "q", "question": "Continue?"}]}})
        send({"method": "thread/status/changed",
              "params": {"threadId": "second", "status": {"type": "idle"}}})
    elif method == "probe":
        send({"id": message["id"], "result": {"value": "reader still running"}})
    elif method == "turn/steer":
        send({"id": message["id"], "result": {"turnId": message["params"]["expectedTurnId"]}})
    elif method == "exit":
        sys.exit(0)
    elif "id" in message and "method" not in message:
        thread = "first" if message["id"] == 17 else "second"
        send({"method": "serverRequest/resolved",
              "params": {"threadId": thread, "requestId": message["id"]}})
        send({"method": "test/receivedResponse", "params": message})
"""


class Probe(BaseModel):
    value: str


@pytest.fixture
async def sdk_peer(tmp_path):
    peer = tmp_path / "peer.py"
    peer.write_text(PEER)
    client = InteractiveCodexClient(CodexConfig(launch_args_override=(sys.executable, str(peer))))
    await client.start()
    try:
        await asyncio.wait_for(client.initialize(), 5)
        yield client
    finally:
        await client.close()


async def test_pending_requests_do_not_block_rpc_notifications_or_other_replies(sdk_peer):
    client = sdk_peer
    async with asyncio.timeout(5):
        first = await client.control_events.get()
        second = await client.control_events.get()
        assert (first["id"], second["id"]) == (17, "18")
        probe = await client.request("probe", {}, response_model=Probe)
        assert probe.value == "reader still running"
        notification = await client.next_notification()
        assert notification.method == "thread/status/changed"
        assert (
            await client.turn_steer("first", "turn-first", "more context")
        ).turn_id == "turn-first"
        # Answer the second thread while the first is still waiting.
        result = {"answers": {"q": {"answers": ["yes"]}}}
        await client.respond(second["id"], result=result)
        resolved = await client.control_events.get()
        assert resolved["params"] == {"threadId": "second", "requestId": "18"}
        assert (await client.next_notification()).method == "serverRequest/resolved"
        received = await client.next_notification()
        assert received.payload.params["result"] == result
        assert received.payload.params["id"] == "18"
        await client.respond(first["id"], error={"code": -32601, "message": "unsupported"})
        assert (await client.control_events.get())["params"]["requestId"] == 17


async def test_transport_exit_unblocks_sdk_waiters_even_with_pending_questions(sdk_peer):
    async with asyncio.timeout(5):
        await sdk_peer.control_events.get()
        await sdk_peer._call_sync(sdk_peer._sync.notify, "exit", None)
        with pytest.raises(TransportClosedError):
            while True:
                await sdk_peer.next_notification()
