"""Prepare the local database and start the hub with its frontend and worker."""

import argparse
import asyncio
import os
import subprocess
import sys

from scripts.bootstrap import ROOT, prepare, register


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
    args = parser.parse_args()
    os.chdir(ROOT)
    prepare()
    if not args.skip_postgres:
        subprocess.run([sys.executable, "-m", "scripts.postgres", "start"], check=True)
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
