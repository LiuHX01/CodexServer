from collections.abc import AsyncIterator
from typing import Protocol


class ProviderError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


class DeliveryUnknown(Exception):
    """A remote mutation may have happened; repeating it is not a safe retry."""


class Adapter(Protocol):
    async def inspect(self) -> tuple[dict, dict]: ...
    async def mutate(self, path: str, payload: dict | None, key: str) -> dict: ...
    async def get(self, path: str) -> dict | list: ...
    def watch(self, thread_id: str, cursor: int) -> AsyncIterator[dict]: ...
    async def close(self): ...
