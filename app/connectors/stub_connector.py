from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from app.core.models import Channel
from .base import BaseConnector, OnMessageCallback


class StubConnector(BaseConnector):
    def __init__(self, channel: Channel, name: str | None = None) -> None:
        self._channel = channel
        self._name = name or f"stub-{channel.value}"
        self._on_message: Optional[OnMessageCallback] = None
        self._running = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def channel(self) -> Channel:
        return self._channel

    async def start(self, on_message: OnMessageCallback) -> None:
        self._on_message = on_message
        self._running = True
        logger.info(f"{self._name} запущен (ничего не делает)")

    async def stop(self) -> None:
        self._running = False
        logger.info(f"{self._name} остановлен")

    async def send_message(self, chat_id: str, text: str) -> None:
        logger.info(f"[{self._name}] отправка_сообщения chat_id={chat_id} text={text!r}")

    async def simulate_typing(self, chat_id: str, seconds: float) -> None:
        logger.info(f"[{self._name}] имитация_набора chat_id={chat_id} seconds={seconds}")
        await asyncio.sleep(seconds)
