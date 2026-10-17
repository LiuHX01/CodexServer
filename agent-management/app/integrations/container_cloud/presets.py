"""Short, opt-in local probes carried by ChangeAtlas's legacy echo protocol."""

import asyncio
import json
import logging
import os
import stat
from pathlib import Path

from .protocol import TaskMessage

logger = logging.getLogger(__name__)
COMMANDS = {"test-project", "test-git", "test-create-file"}
MARKER = b"hello from agentrelay preset\n"


async def execute_preset(task: TaskMessage, directory: str | None):
    text = task.payload["text"]
    if text not in COMMANDS:
        raise ValueError(f"未知预设任务：{text}")
    if not directory:
        raise ValueError("预设任务未启用：启动时指定 --preset-dir 或配置目录")
    root = await asyncio.to_thread(resolve_directory, directory)
    details = {"directory": str(root)}
    if text == "test-git":
        # Fixed argv only; Kim text is never interpreted as a shell command.
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--show-toplevel",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=4)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ValueError(stderr.decode(errors="replace")[:500])
        details["git"] = stdout.decode(errors="replace").strip()
    elif text == "test-create-file":
        details.update(await asyncio.to_thread(check_file, root))
    logger.info(
        json.dumps(
            {
                "event": "preset_succeeded",
                "taskId": task.task_id,
                "preset": text,
                "details": details,
            },
            ensure_ascii=False,
        )
    )
    # Only return the original text after the probe succeeds. This is required
    # by the unchanged cloud server's strict echo validation.
    return {"ok": True, "text": text, "details": details}


def resolve_directory(directory):
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("预设任务目录必须是现有目录")
    return root


def check_file(root):
    details = {}
    path = root / ".agentrelay-smoke.txt"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        details["created"] = False
    else:
        with os.fdopen(descriptor, "wb") as file:
            file.write(MARKER)
        details["created"] = True
    # Do not follow a pre-existing symlink or overwrite an existing file.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("测试文件不是普通文件")
        if file.read(len(MARKER) + 1) != MARKER:
            raise ValueError("测试文件已存在且内容不同，未覆盖")
    details["file"] = str(path)
    return details
