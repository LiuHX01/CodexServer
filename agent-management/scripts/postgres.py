"""Project-local PostgreSQL lifecycle; never enables a system service."""

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "postgres"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"], default="start", nargs="?")
    action = parser.parse_args().action
    binary = shutil.which("pg_ctl")
    if binary:
        pg = Path(binary).parent
    else:
        pg = next(
            (
                p
                for p in [
                    Path("/opt/homebrew/opt/postgresql@17/bin"),
                    Path("/usr/lib/postgresql/17/bin"),
                ]
                if (p / "pg_ctl").exists()
            ),
            None,
        )
    if pg is None:
        raise SystemExit("PostgreSQL binaries missing. Install PostgreSQL 17 or use compose.yaml.")
    DATA.parent.mkdir(exist_ok=True)

    def run(command, **kwargs):
        return subprocess.run([str(pg / command[0]), *command[1:]], **kwargs)

    if action == "start":
        if not (DATA / "PG_VERSION").exists():
            run(
                [
                    "initdb",
                    "-D",
                    str(DATA),
                    "-U",
                    "relay",
                    "-A",
                    "trust",
                    "--encoding=UTF8",
                    "--locale=C",
                ],
                check=True,
            )
        if run(["pg_ctl", "-D", str(DATA), "status"], capture_output=True).returncode != 0:
            run(
                [
                    "pg_ctl",
                    "-D",
                    str(DATA),
                    "-l",
                    str(DATA.parent / "postgres.log"),
                    "-o",
                    "-h 127.0.0.1 -p 55432",
                    "-w",
                    "start",
                ],
                check=True,
            )
        result = run(
            [
                "psql",
                "-h",
                "127.0.0.1",
                "-p",
                "55432",
                "-U",
                "relay",
                "-d",
                "postgres",
                "-tAc",
                "SELECT 1 FROM pg_database WHERE datname='agentrelay'",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip() != "1":
            run(
                ["createdb", "-h", "127.0.0.1", "-p", "55432", "-U", "relay", "agentrelay"],
                check=True,
            )
        print("PostgreSQL ready: 127.0.0.1:55432 / agentrelay (project-local development cluster)")
    else:
        run(
            [
                "pg_ctl",
                "-D",
                str(DATA),
                *(["-m", "fast", "-w"] if action == "stop" else []),
                action,
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
