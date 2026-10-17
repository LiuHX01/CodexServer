from dataclasses import dataclass

from pydantic import SecretStr


@dataclass(frozen=True)
class ContainerCloudWorkerConfig:
    endpoint: str
    token: SecretStr
    management_token: SecretStr
    management_url: str = "http://127.0.0.1:8080"
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 15.0
    connect_timeout_seconds: float = 10.0
    management_timeout_seconds: float = 30.0
    local_mode: bool = False
    result_poll_seconds: float = 1.0
    result_timeout_seconds: float = 1800.0
    preset_dir: str | None = None

    def validate(self):
        if self.result_poll_seconds <= 0 or self.result_timeout_seconds <= 0:
            raise ValueError("结果轮询间隔和超时必须大于 0")
        if not self.endpoint.startswith(("ws://", "wss://")):
            raise ValueError("容器云 Worker endpoint 必须使用 ws:// 或 wss://")
        if self.local_mode and not self.endpoint.startswith("ws://"):
            raise ValueError("本地模式需要使用 ws:// 地址")
        if not self.local_mode and not self.endpoint.startswith("wss://"):
            raise ValueError("生产模式需要使用 wss:// 加密连接")
        if not self.token.get_secret_value():
            raise ValueError("容器云 Worker token 不能为空")
        if not self.management_token.get_secret_value():
            raise ValueError("management API token 不能为空")
