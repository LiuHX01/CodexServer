"""Prepare the local database and start the hub with its frontend and worker."""

import argparse
import asyncio
import os
import socket
import subprocess
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values

from scripts.bootstrap import ROOT, prepare, register


def database_endpoint(database_url):
    parsed = urlsplit(database_url.replace("+asyncpg", "", 1))
    return parsed.hostname or "127.0.0.1", parsed.port or 5432


def database_is_reachable(database_url):
    host, port = database_endpoint(database_url)
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def management_url_is_configured():
    if "RELAY_CONTAINER_CLOUD_MANAGEMENT_URL" in os.environ:
        return True
    return any(
        dotenv_values(path).get("RELAY_CONTAINER_CLOUD_MANAGEMENT_URL")
        for path in (ROOT / ".env", ROOT.parent / ".env")
        if path.exists()
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register-codex", action="store_true")
    parser.add_argument(
        "--skip-postgres",
        action="store_true",
        help="Use the configured database without starting the project-local PostgreSQL cluster",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--management-url",
        help="Callback URL used by the embedded container-cloud listener",
    )
    args = parser.parse_args()
    os.chdir(ROOT)
    prepare()
    from app.config import Settings

    config = Settings()
    database_host, database_port = database_endpoint(config.database_url)
    local_database = database_host in {"127.0.0.1", "localhost", "::1"} and database_port == 55432
    database_reachable = database_is_reachable(config.database_url)
    if not args.skip_postgres and not database_reachable and local_database:
        print(
            f"PostgreSQL unavailable at {database_host}:{database_port}; "
            "starting project-local cluster."
        )
        subprocess.run([sys.executable, "-m", "scripts.postgres", "start"], check=True)
    elif not args.skip_postgres and database_reachable:
        print(f"PostgreSQL reachable at {database_host}:{database_port}; reusing it.")
    elif not args.skip_postgres:
        print(
            f"Configured PostgreSQL is not reachable at {database_host}:{database_port}; "
            "continuing without starting a local cluster."
        )
    if args.management_url:
        os.environ["RELAY_CONTAINER_CLOUD_MANAGEMENT_URL"] = args.management_url
    elif not management_url_is_configured():
        os.environ["RELAY_CONTAINER_CLOUD_MANAGEMENT_URL"] = f"http://{args.host}:{args.port}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    if args.register_codex:
        asyncio.run(register())
    print(f"AgentRelay: http://{args.host}:{args.port} (Ctrl+C stops API and embedded worker)")
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--no-access-log",
        ],
    )


if __name__ == "__main__":
    main()
