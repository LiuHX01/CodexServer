"""Generate local secrets and register the agent-execution service without exposing its token."""

import asyncio
import secrets
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.models import Agent
from app.security import Security

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    path = ROOT / ".env"
    if not path.exists():
        path.write_text(
            "RELAY_DATABASE_URL=postgresql+asyncpg://relay@127.0.0.1:55432/agentrelay\n"
            f"RELAY_ADMIN_TOKEN={secrets.token_urlsafe(32)}\n"
            f"RELAY_SECRET_KEY={secrets.token_urlsafe(48)}\n"
            "RELAY_EMBEDDED_WORKER=true\n"
        )
        path.chmod(0o600)
    print("Local configuration ready. Run migrations, then --register-codex.")


async def register():
    config = Settings()
    source = ROOT.parent / "agent-execution" / ".env"
    token = dotenv_values(source).get("API_TOKEN")
    if not token:
        raise SystemExit(
            "agent-execution service .env has no API_TOKEN; register from the UI instead."
        )
    db = Database(config.database_url)
    try:
        async with db.transaction() as tx:
            existing = await tx.scalar(
                select(Agent).where(Agent.base_url == "http://127.0.0.1:8000")
            )
            if existing is None:
                tx.add(
                    Agent(
                        name="Local Codex",
                        provider="codex",
                        base_url="http://127.0.0.1:8000",
                        credential=Security(config).encrypt(token),
                    )
                )
        print("Local Codex registered. Credential remains server-side.")
    finally:
        await db.close()


if __name__ == "__main__":
    import sys

    prepare()
    if "--register-codex" in sys.argv:
        asyncio.run(register())
