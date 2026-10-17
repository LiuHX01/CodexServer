from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import Base
from app.security import Principal

TOKEN = "relay-test-admin-key-with-at-least-24-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ADMIN = Principal("admin", "admin")


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.threads = {}
        self.runs = {}
        self.ambiguous = False
        self.receipts = {}

    async def inspect(self):
        return (
            {"backend_ready": True, "running": 0, "queued": 0, "max_concurrent_runs": 4},
            {
                "instance_id": "source-test",
                "protocol_version": 1,
                "capabilities": {
                    "steering": True,
                    "human_requests": True,
                    "thread_events": True,
                    "idempotency": True,
                },
            },
        )

    async def mutate(self, path, payload, key):
        from app.adapters.base import DeliveryUnknown, ProviderError

        self.calls.append((path, deepcopy(payload), key))
        if key in self.receipts:
            return self.receipts[key]["response"]
        if path == "/v1/threads":
            result = {"id": uuid4().hex, "name": payload["name"], "cwd": "/workspace/test"}
            self.threads[result["id"]] = result
        elif path.endswith("/messages"):
            thread = path.split("/")[3]
            result = {
                "id": uuid4().hex,
                "thread_id": thread,
                "turn_id": uuid4().hex,
                "status": "running",
                "options": payload.get("options", {}),
            }
            self.runs[result["id"]] = result
        elif path.endswith("/cancel"):
            result = {**self.runs[path.split("/")[3]], "cancel_requested": True}
        elif path.endswith("/input"):
            run = self.runs[path.split("/")[3]]
            if run["status"] != "running":
                raise ProviderError(409, "run ended")
            result = {"turnId": run["turn_id"]}
        elif path.endswith("/response"):
            request_id = path.split("/")[3]
            result = {
                "id": request_id,
                "method": "item/tool/requestUserInput",
                "status": "responded",
                "params": {},
                "response": payload["result"],
            }
        else:
            result = {}
        self.receipts[key] = {"status": "completed", "status_code": 200, "response": result}
        if self.ambiguous:
            self.ambiguous = False
            raise DeliveryUnknown("reply lost after remote mutation")
        return result

    async def get(self, path):
        from app.adapters.base import ProviderError

        if path.startswith("/v1/operations/"):
            row = self.receipts.get(path.split("/")[-1])
            if row is None:
                raise ProviderError(404, "missing")
            return row
        if path.startswith("/v1/runs/"):
            return self.runs[path.split("/")[3]]
        if "/requests" in path:
            return []
        return self.threads[path.split("/")[3]]

    async def close(self):
        pass


class FakeRegistry:
    def __init__(self):
        self.adapter = FakeAdapter()

    def open(self, agent):
        return self.adapter


@pytest.fixture
async def database():
    settings = Settings()
    schema = "relay_test_" + uuid4().hex
    admin = create_async_engine(settings.database_url)
    async with admin.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    db = Database(settings.database_url, connect_args={"server_settings": {"search_path": schema}})
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield db
    await db.close()
    async with admin.begin() as connection:
        await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    await admin.dispose()


@pytest.fixture
async def service(database):
    settings = Settings(
        _env_file=None,
        admin_token=TOKEN,
        secret_key="test-secret-" * 4,
        embedded_worker=False,
        access_keys=[
            {
                "principal": "alice",
                "role": "operator",
                "token": "alice-key-with-at-least-24-characters",
            },
            {"principal": "bob", "role": "viewer", "token": "bob-key-with-at-least-24-characters"},
        ],
    )
    registry = FakeRegistry()
    app = create_app(settings, database, registry)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=HEADERS
        ) as client:
            yield client, app.state.worker, registry.adapter, database


async def create_agent(client):
    response = await client.post(
        "/v1/agents",
        json={
            "name": "Test Codex",
            "base_url": "http://provider.test",
            "token": "provider-secret-with-at-least-24-characters",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_session(service):
    client, worker, adapter, db = service
    agent = await create_agent(client)
    response = await client.post(
        f"/v1/agents/{agent['id']}/sessions",
        headers={"Idempotency-Key": uuid4().hex},
        json={"name": "Test conversation"},
    )
    assert response.status_code == 202, response.text
    await worker.dispatcher.dispatch_one()
    return response.json()["session_id"], agent["id"]
