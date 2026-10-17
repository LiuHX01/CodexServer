"""Start the container cloud message worker.

Usage: uv run python -m scripts.container_cloud_worker [--token TOKEN] [--endpoint URL]
"""

import argparse
import asyncio

from pydantic import SecretStr

from app.config import Settings
from app.integrations.container_cloud import ContainerCloudWorker, ContainerCloudWorkerConfig
from app.logging_config import configure_logging


def parser():
    result = argparse.ArgumentParser(
        description="Connect to the container cloud relay worker endpoint"
    )
    result.add_argument(
        "--token", help="Worker pairing token; defaults to RELAY_CONTAINER_CLOUD_TOKEN"
    )
    result.add_argument(
        "--endpoint", help="WebSocket endpoint; defaults to RELAY_CONTAINER_CLOUD_ENDPOINT"
    )
    result.add_argument(
        "--management-token", help="management API token; defaults to RELAY_ADMIN_TOKEN"
    )
    result.add_argument(
        "--local", action="store_true", help="Allow ws:// endpoint for local testing"
    )
    result.add_argument("--preset-dir", help="Existing local project directory for test-* probes")
    return result


def build_config(args):
    settings = Settings()
    token = args.token or (
        settings.container_cloud_token.get_secret_value() if settings.container_cloud_token else ""
    )
    endpoint = args.endpoint or settings.container_cloud_endpoint
    if not token:
        raise SystemExit("缺少 token：请传入 --token 或设置 RELAY_CONTAINER_CLOUD_TOKEN")
    if not endpoint:
        raise SystemExit("缺少 endpoint：请传入 --endpoint 或设置 RELAY_CONTAINER_CLOUD_ENDPOINT")
    management_token = args.management_token or (
        settings.container_cloud_management_token.get_secret_value()
        if settings.container_cloud_management_token
        else settings.admin_token.get_secret_value()
    )
    return ContainerCloudWorkerConfig(
        endpoint=endpoint,
        token=SecretStr(token),
        management_token=SecretStr(management_token),
        management_url=settings.container_cloud_management_url,
        reconnect_initial_seconds=settings.container_cloud_reconnect_initial_seconds,
        reconnect_max_seconds=settings.container_cloud_reconnect_max_seconds,
        connect_timeout_seconds=settings.container_cloud_connect_timeout_seconds,
        management_timeout_seconds=settings.container_cloud_management_timeout_seconds,
        local_mode=args.local or settings.container_cloud_local_mode,
        preset_dir=args.preset_dir or settings.container_cloud_preset_dir,
        result_poll_seconds=settings.container_cloud_result_poll_seconds,
        result_timeout_seconds=settings.container_cloud_result_timeout_seconds,
    )


async def main():
    args = parser().parse_args()
    worker = ContainerCloudWorker(build_config(args))
    await worker.run()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
