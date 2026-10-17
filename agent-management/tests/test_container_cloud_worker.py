from pydantic import SecretStr

from app.integrations.container_cloud import ContainerCloudWorker, ContainerCloudWorkerConfig
from app.integrations.container_cloud.actions import ManagementActionRegistry
from app.integrations.container_cloud.protocol import parse_ready, parse_task


class FakeConnection:
    def __init__(self):
        self.results = []
        self.errors = []

    async def result(self, task_id, nonce, result):
        self.results.append((task_id, nonce, result))

    async def error(self, task_id, nonce, code, message):
        self.errors.append((task_id, nonce, code, message))


def config():
    return ContainerCloudWorkerConfig(
        endpoint="ws://127.0.0.1:8765/worker",
        token=SecretStr("pairing-token"),
        management_token=SecretStr("management-token"),
        local_mode=True,
    )


def test_ready_protocol_accepts_simulator_numeric_expiry():
    ready = parse_ready(
        {
            "type": "ready",
            "expectedIp": "local-simulator",
            "expiresAt": 1_797_000_000_000,
        }
    )
    assert ready.expires_at == 1_797_000_000_000


def test_ready_protocol_rejects_boolean_expiry():
    try:
        parse_ready({"type": "ready", "expiresAt": True})
    except ValueError as exc:
        assert "expiresAt" in str(exc)
    else:
        raise AssertionError("boolean expiresAt should be rejected")


def test_task_protocol_parses_explicit_payload():
    task = parse_task(
        {
            "type": "task",
            "taskId": "task-1",
            "nonce": "nonce-1",
            "operation": "start_session",
            "payload": {"session_id": "session-1", "body": {"prompt": "hello"}},
        }
    )
    assert task.task_id == "task-1"
    assert task.operation == "start_session"
    assert task.payload["body"] == {"prompt": "hello"}


def test_task_protocol_keeps_legacy_top_level_echo_text():
    task = parse_task(
        {
            "type": "task",
            "taskId": "task-legacy",
            "operation": "echo",
            "text": "hello",
        }
    )
    assert task.payload == {"text": "hello"}


async def test_echo_action_preserves_demo_compatibility():
    registry = ManagementActionRegistry(config())
    try:
        task = parse_task(
            {
                "type": "task",
                "taskId": "task-1",
                "operation": "echo",
                "payload": {"text": "hello"},
            }
        )
        assert await registry.execute(task) == {"ok": True, "text": "hello"}
    finally:
        await registry.close()


async def test_management_action_uses_allowlisted_path_and_task_idempotency():
    registry = ManagementActionRegistry(config())
    calls = []

    async def fake_call(task, method, path, body=None, *, idempotent=True):
        calls.append((task.task_id, method, path, body, idempotent))
        return {"ok": True}

    registry.client.call = fake_call
    try:
        task = parse_task(
            {
                "type": "task",
                "taskId": "task-42",
                "operation": "start_session",
                "payload": {"session_id": "session-1", "body": {"prompt": "hello"}},
            }
        )
        assert await registry.execute(task) == {"ok": True}
    finally:
        await registry.close()

    assert calls == [
        (
            "task-42",
            "POST",
            "/v1/sessions/session-1/messages",
            {"prompt": "hello"},
            True,
        )
    ]


async def test_unknown_action_returns_structured_error():
    registry = ManagementActionRegistry(config())
    worker = ContainerCloudWorker(config(), registry)
    connection = FakeConnection()
    try:
        await worker.handle(
            connection,
            {
                "type": "task",
                "taskId": "task-unknown",
                "nonce": "nonce-1",
                "operation": "delete_everything",
                "payload": {},
            },
        )
    finally:
        await worker.close()

    assert not connection.results
    assert connection.errors == [
        ("task-unknown", "nonce-1", "invalid_task", "不支持的容器云操作: delete_everything")
    ]


async def test_presets_gate_echo_on_real_file_result(tmp_path):
    from dataclasses import replace

    registry = ManagementActionRegistry(replace(config(), preset_dir=str(tmp_path)))
    worker = ContainerCloudWorker(config(), registry)
    connection = FakeConnection()
    message = {
        "type": "task",
        "taskId": "file-probe",
        "nonce": "original-nonce",
        "operation": "echo",
        "text": "test-create-file",
    }
    try:
        await worker.handle(connection, message)
        assert connection.results[0][:2] == ("file-probe", "original-nonce")
        assert connection.results[0][2]["text"] == "test-create-file"
        marker = tmp_path / ".agentrelay-smoke.txt"
        assert marker.read_text() == "hello from agentrelay preset\n"
        await worker.handle(connection, message)
        assert connection.results[-1][2]["details"]["created"] is False
        marker.write_text("user content")
        connection.results.clear()
        await worker.handle(connection, message)
        assert not connection.results
        assert connection.errors
        assert marker.read_text() == "user content"
    finally:
        await worker.close()


async def test_presets_reject_unknown_and_disabled(tmp_path):
    from dataclasses import replace

    for directory, command in [(None, "test-project"), (str(tmp_path), "test-typo")]:
        registry = ManagementActionRegistry(replace(config(), preset_dir=directory))
        worker = ContainerCloudWorker(config(), registry)
        connection = FakeConnection()
        try:
            await worker.handle(
                connection,
                {
                    "type": "task",
                    "taskId": "reject",
                    "operation": "echo",
                    "text": command,
                },
            )
            assert not connection.results
            assert connection.errors
        finally:
            await worker.close()


async def test_presets_check_project_and_git(tmp_path):
    import asyncio
    import subprocess
    from dataclasses import replace

    registry = ManagementActionRegistry(replace(config(), preset_dir=str(tmp_path)))
    try:
        task = parse_task(
            {
                "type": "task",
                "taskId": "probe",
                "operation": "echo",
                "text": "test-project",
            }
        )
        assert (await registry.execute(task))["details"]["directory"] == str(tmp_path.resolve())
        await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(tmp_path)], check=True)
        await asyncio.to_thread(
            subprocess.run,
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        task = parse_task(
            {
                "type": "task",
                "taskId": "git",
                "operation": "echo",
                "text": "test-git",
            }
        )
        assert len((await registry.execute(task))["details"]["git"].splitlines()[-1]) == 40
    finally:
        await registry.close()
