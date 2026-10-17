import asyncio

from app.integrations.container_cloud.observe import follow_command
from app.integrations.container_cloud.protocol import TaskMessage


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def call(self, task, method, path, **kwargs):
        self.calls.append((method, path))
        return {"data": next(self.responses)}


def receipt(status="queued", **extras):
    return {"id": "command-1", "session_id": "session-1", "status": status, **extras}


def run(status="completed", **extras):
    return {"id": "run-local", "provider_run_id": "native-new", "status": status, **extras}


async def test_follows_receipt_then_only_matching_run_and_final_response():
    client = Client(
        [
            receipt("accepted", result={"id": "native-new"}),
            {"runs": [run(provider_run_id="native-old", final_response="wrong"), run("running")]},
            {"runs": [run(final_response="hello from Codex")]},
        ]
    )
    updates = []

    async def publish(event, data):
        updates.append((event, data))

    await follow_command(
        client, TaskMessage("t", "n", "start_session", {}), receipt(), publish, poll_seconds=0.001
    )
    assert updates[-1][0] == "run_finished"
    assert updates[-1][1]["final_response"] == "hello from Codex"
    assert updates[-1][1]["run_id"] == "run-local"
    assert all(method == "GET" for method, _ in client.calls)
    assert [data["status"] for event, data in updates if event == "run_status"] == [
        "running",
        "completed",
    ]


async def test_unknown_receipt_stops_without_resubmitting():
    client = Client([receipt("unknown", error="receipt missing")])
    updates = []

    async def publish(event, data):
        updates.append((event, data))

    await follow_command(
        client, TaskMessage("t", "n", "start_session", {}), receipt(), publish, poll_seconds=0.001
    )
    assert updates[-1] == (
        "command_ended",
        {
            "command_id": "command-1",
            "status": "unknown",
            "error": "receipt missing",
        },
    )
    assert len(client.calls) == 1


async def test_observation_timeout_does_not_report_run_failed():
    class SlowClient:
        async def call(self, *args, **kwargs):
            await asyncio.Event().wait()

    updates = []

    async def publish(event, data):
        updates.append((event, data))

    await follow_command(
        SlowClient(),
        TaskMessage("t", "n", "start_session", {}),
        receipt(),
        publish,
        poll_seconds=0.001,
        timeout_seconds=0.01,
    )
    assert updates[-1][0] == "observation_error"
    assert updates[-1][1]["code"] == "observation_timeout"
    assert all(event != "run_finished" for event, _ in updates)


async def test_worker_receives_other_tasks_while_following_and_cancels_on_close():
    from pydantic import SecretStr

    from app.integrations.container_cloud import ContainerCloudWorker, ContainerCloudWorkerConfig

    waiting = asyncio.Event()
    cancelled = asyncio.Event()

    class Actions:
        client = None

        async def execute(self, task):
            if task.operation == "start_session":
                return {"ok": True, "data": receipt()}
            return {"ok": True, "text": "hello"}

        async def close(self):
            pass

    class Connection:
        results = []

        async def result(self, *args):
            self.results.append(args)

    config = ContainerCloudWorkerConfig(
        endpoint="ws://localhost:1234",
        token=SecretStr("test"),
        management_token=SecretStr("test"),
        local_mode=True,
    )
    worker = ContainerCloudWorker(config, Actions())

    async def follow(*args):
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker.follow = follow
    connection = Connection()
    try:
        await worker.handle(
            connection,
            {
                "type": "task",
                "taskId": "start",
                "operation": "start_session",
            },
        )
        await waiting.wait()
        await worker.handle(
            connection,
            {
                "type": "task",
                "taskId": "other",
                "operation": "echo",
                "text": "hello",
            },
        )
        assert len(connection.results) == 2
    finally:
        await worker.close()
    assert cancelled.is_set()
    assert not worker.followers
