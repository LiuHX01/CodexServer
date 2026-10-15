"""Exercise a running service using curl and real Codex turns.

Run with: uv run python -m scripts.http_smoke
Only the test's own conversations are changed; the bearer token is never printed.
"""

import argparse
import asyncio
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.config import Settings

TERMINAL = {"completed", "failed", "interrupted"}


class Smoke:
    def __init__(self, base_url: str, settings: Settings):
        self.base = base_url.rstrip("/")
        self.token = settings.api_token.get_secret_value()
        self.curl = shutil.which("curl.exe") or shutil.which("curl")
        if not self.curl:
            raise RuntimeError("curl is not installed")
        self.stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.report = {"base_url": self.base, "started_at": self.stamp, "checks": []}
        self.created: list[str] = []
        self.run_ids: list[str] = []

    def record(self, name: str, **result):
        self.report["checks"].append({"check": name, **result})
        print(json.dumps({"check": name, **result}, ensure_ascii=False), flush=True)

    async def request(self, method, path, body=None, expected=200, raw=False, after=None):
        # curl reads sensitive headers from stdin, keeping them out of process arguments.
        config = [
            "url = " + json.dumps(self.base + path),
            "request = " + json.dumps(method),
            "header = " + json.dumps("Authorization: Bearer " + self.token),
        ]
        if after is not None:
            config.append("header = " + json.dumps(f"Last-Event-ID: {after}"))
        if body is not None:
            config += [
                'header = "Content-Type: application/json; charset=utf-8"',
                "data = " + json.dumps(json.dumps(body, ensure_ascii=False), ensure_ascii=False),
            ]
        process = await asyncio.create_subprocess_exec(
            self.curl,
            "--silent",
            "--show-error",
            "--no-buffer",
            "--max-time",
            "180",
            "--write-out",
            "\n%{http_code}",
            "--config",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(("\n".join(config) + "\n").encode())
        if process.returncode:
            raise RuntimeError(f"curl failed: {stderr.decode(errors='replace')}")
        text, status = stdout.decode("utf-8").rsplit("\n", 1)
        if int(status) != expected:
            raise RuntimeError(f"{method} {path}: expected {expected}, received {status}: {text}")
        return text if raw else json.loads(text)

    async def create(self, label):
        thread = await self.request(
            "POST",
            "/v1/threads",
            {"name": f"curl-smoke-{self.stamp}-{label}"},
            expected=201,
        )
        self.created.append(thread["id"])
        return thread["id"]

    async def send(self, thread, prompt):
        run = await self.request(
            "POST",
            f"/v1/threads/{thread}/messages",
            {"prompt": prompt},
            expected=202,
        )
        self.run_ids.append(run["id"])
        return run

    async def wait(self, run_id):
        async with asyncio.timeout(180):
            while True:
                run = await self.request("GET", f"/v1/runs/{run_id}")
                if run["status"] in TERMINAL:
                    return run
                await asyncio.sleep(0.25)

    async def successful(self, run_id, expected):
        run = await self.wait(run_id)
        self.record(
            "model_reply",
            run_id=run_id,
            status=run["status"],
            reply=run["final_response"],
            error=run["error"],
        )
        if run["status"] != "completed" or (run["final_response"] or "").strip() != expected:
            raise RuntimeError(f"Unexpected model result for {run_id}: {run['status']}")
        return run

    async def exercise(self):
        assert (await self.request("GET", "/healthz"))["status"] == "ok"
        status = await self.request("GET", "/v1/status")
        assert status["backend_ready"]
        self.record("service_ready", max_concurrent_runs=status["max_concurrent_runs"])
        docs = await self.request("GET", "/docs", raw=True)
        assert "/docs-assets/swagger-ui-bundle.js" in docs
        await self.request("GET", "/docs-assets/swagger-ui-bundle.js", raw=True)
        self.record("local_docs_assets", passed=True)
        schema = await self.request("GET", "/openapi.json")
        assert "/v1/threads" in schema["paths"]

        a, b = await asyncio.gather(self.create("A"), self.create("B"))
        tokens = ["ORCHID-" + uuid4().hex[:8], "MAPLE-" + uuid4().hex[:8]]
        runs = await asyncio.gather(
            *(
                self.send(
                    thread,
                    f"Remember this token: {token}. Do not run tools. "
                    f"Reply with exactly {token} and nothing else.",
                )
                for thread, token in zip((a, b), tokens, strict=True)
            )
        )
        streams = [
            asyncio.create_task(
                self.request(
                    "GET",
                    f"/v1/runs/{run['id']}/events",
                    raw=True,
                )
            )
            for run in runs
        ]
        conflict = await self.request(
            "POST",
            f"/v1/threads/{a}/messages",
            {"prompt": "Do not run tools. Reply OK."},
            expected=409,
        )
        self.record("same_thread_conflict", http_status=409, detail=conflict["detail"])
        async with asyncio.timeout(180):
            overlap = False
            while True:
                current = await asyncio.gather(
                    *(self.request("GET", f"/v1/runs/{run['id']}") for run in runs)
                )
                if all(r["status"] == "running" and r["turn_id"] for r in current):
                    overlap = True
                if all(r["status"] in TERMINAL for r in current):
                    break
                await asyncio.sleep(0.25)
        self.record("parallel_execution", observed_overlapping_codex_turns=overlap)
        await asyncio.gather(
            *(self.successful(run["id"], token) for run, token in zip(runs, tokens, strict=True))
        )
        for i, stream in enumerate(streams):
            text = await stream
            assert "event: item/agentMessage/delta" in text
            assert "event: run.finished" in text
            assert tokens[i] in text and tokens[1 - i] not in text
            ids = [int(line[4:]) for line in text.splitlines() if line.startswith("id: ")]
            replay = await self.request(
                "GET",
                f"/v1/runs/{runs[i]['id']}/events",
                raw=True,
                after=ids[-1],
            )
            assert replay == ""
            self.record("sse_isolation_and_replay", run_id=runs[i]["id"], events=len(ids))
        history = await self.request("GET", f"/v1/threads/{a}/history")
        assert tokens[0] in json.dumps(history)
        listing = await self.request("GET", "/v1/threads?limit=100")
        assert {a, b} <= {t["id"] for t in listing["data"]}
        self.record("persisted_history_and_listing", passed=True)
        await self.request("POST", f"/v1/threads/{a}/resume")
        recall = await self.send(
            a,
            "What token did I ask you to remember? Do not run tools. Reply only with that token.",
        )
        await self.successful(recall["id"], tokens[0])
        self.record("resume_retains_context", passed=True)
        if not overlap and status["max_concurrent_runs"] >= 2:
            raise RuntimeError("Parallel turns did not overlap during sampling")

    async def cancellation(self):
        thread = await self.create("cancel")
        run = await self.send(
            thread, "Do not run tools. Count from 1 to 10000, putting each number on its own line."
        )
        async with asyncio.timeout(180):
            while True:
                current = await self.request("GET", f"/v1/runs/{run['id']}")
                if current["turn_id"] or current["status"] in TERMINAL:
                    break
                await asyncio.sleep(0.1)
        if current["status"] in TERMINAL:
            raise RuntimeError("Cancellation test turn ended before interrupt could be requested")
        await self.request("POST", f"/v1/runs/{run['id']}/cancel")
        finished = await self.wait(run["id"])
        assert finished["status"] == "interrupted", finished
        assert finished["cancel_requested"]
        self.record("cancel_running_turn", run_id=run["id"], status=finished["status"])

    async def run(self, report_directory: Path, cancel_only: bool = False):
        try:
            if not cancel_only:
                await self.exercise()
            await self.cancellation()
            self.report["passed"] = True
        except Exception as exc:
            self.report["passed"] = False
            self.report["error"] = str(exc)
            self.record("failure", error=str(exc))
        finally:
            for run_id in self.run_ids:
                try:
                    current = await self.request("GET", f"/v1/runs/{run_id}")
                    if current["status"] not in TERMINAL:
                        await self.request("POST", f"/v1/runs/{run_id}/cancel")
                        await self.wait(run_id)
                except Exception as exc:
                    self.record("test_run_cleanup", run_id=run_id, error=str(exc))
            for thread in self.created:
                try:
                    await self.request("POST", f"/v1/threads/{thread}/archive")
                except Exception as exc:
                    self.record("test_thread_cleanup", thread_id=thread, error=str(exc))
            self.report["thread_ids"] = self.created
            await asyncio.to_thread(report_directory.mkdir, parents=True, exist_ok=True)
            path = report_directory / f"http-smoke-{self.stamp}.json"
            await asyncio.to_thread(
                path.write_text,
                json.dumps(self.report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Report: {path.resolve()}", flush=True)
        return self.report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cancel-only", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    started = time.monotonic()
    passed = asyncio.run(Smoke(args.base_url, settings).run(settings.data_dir, args.cancel_only))
    print(f"Passed: {passed}; elapsed: {time.monotonic() - started:.1f}s")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
