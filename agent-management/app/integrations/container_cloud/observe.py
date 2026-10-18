"""Read management receipts and run mirrors; never re-submit an operation."""

import asyncio


async def follow_command(
    client,
    task,
    command,
    publish,
    *,
    operation=None,
    poll_seconds=1.0,
    timeout_seconds=1800,
):
    async def read(path):
        response = await client.call(task, "GET", path, idempotent=False)
        return response["data"]

    async def observe():
        receipt = command
        previous = None
        while True:
            status = receipt["status"]
            if status != previous:
                await publish(
                    "command_status",
                    {
                        "command_id": receipt["id"],
                        "session_id": receipt["session_id"],
                        "status": status,
                        "error": receipt.get("error"),
                    },
                )
                previous = status
            if status in {"rejected", "unknown"}:
                await publish(
                    "command_ended",
                    {
                        "command_id": receipt["id"],
                        "status": status,
                        "error": receipt.get("error"),
                    },
                )
                return
            if status == "accepted":
                break
            await asyncio.sleep(poll_seconds)
            receipt = await read(f"/v1/commands/{command['id']}")

        if (operation or task.operation) != "start_session":
            await publish(
                "command_ended",
                {
                    "command_id": receipt["id"],
                    "session_id": receipt["session_id"],
                    "status": "accepted",
                    "result": receipt.get("result"),
                },
            )
            return

        # Match the run from this command's receipt, never the session's latest run.
        native_run_id = (receipt.get("result") or {}).get("id")
        if not native_run_id:
            raise ValueError("已受理的命令回执缺少运行 ID，不能确定对应的运行")
        previous = None
        while True:
            session = await read(f"/v1/sessions/{receipt['session_id']}")
            run = next(
                (row for row in session["runs"] if row["provider_run_id"] == native_run_id), None
            )
            if run is not None:
                if run["status"] != previous:
                    await publish(
                        "run_status",
                        {
                            "command_id": receipt["id"],
                            "session_id": receipt["session_id"],
                            "run_id": run["id"],
                            "status": run["status"],
                        },
                    )
                    previous = run["status"]
                if run["status"] in {"completed", "failed", "interrupted"}:
                    await publish(
                        "run_finished",
                        {
                            "command_id": receipt["id"],
                            "session_id": receipt["session_id"],
                            "run_id": run["id"],
                            "status": run["status"],
                            "final_response": run.get("final_response"),
                            "error": run.get("error"),
                        },
                    )
                    return
            await asyncio.sleep(poll_seconds)

    try:
        async with asyncio.timeout(timeout_seconds):
            await observe()
    except TimeoutError:
        await publish(
            "observation_error",
            {
                "code": "observation_timeout",
                "message": "等待回传超时；运行可能仍在继续，请在管理页面查看，不要重复提交任务。",
            },
        )
