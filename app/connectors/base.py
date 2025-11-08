from __future__ import annotations

import abc
from typing import Awaitable, Callable, Optional

from app.core.models import IncomingMessage, Channel


OnMessageCallback = Callable[[IncomingMessage], Awaitable[None]]


class BaseConnector(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str:
        ...

    @property
    @abc.abstractmethod
    def channel(self) -> Channel:
        ...

    @abc.abstractmethod
    async def start(self, on_message: OnMessageCallback) -> None:
        ...

    @abc.abstractmethod
    async def stop(self) -> None:
        ...

    @abc.abstractmethod
    async def send_message(self, chat_id: str, text: str, reply_to_message_id: Optional[str] = None) -> None:
        ...

    @abc.abstractmethod
    async def simulate_typing(self, chat_id: str, seconds: float) -> None:
        ...
