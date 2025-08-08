from __future__ import annotations

import asyncio
from datetime import datetime
from typing import List
from loguru import logger

from .models import IncomingMessage, MessageRecord, Direction
from .storage import Storage
from .utils import estimate_typing_seconds


class Hub:
    def __init__(self, storage: Storage, connectors: List["BaseConnector"]) -> None:
        self._storage = storage
        self._connectors = connectors
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for connector in self._connectors:
            await connector.start(self.on_incoming_message)
        logger.info(f"Запущено коннекторов: {len(self._connectors)}")

    async def stop(self) -> None:
        for connector in self._connectors:
            await connector.stop()
        # Отмена фоновых задач. Задел на будущее
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def on_incoming_message(self, msg: IncomingMessage) -> None:
        logger.info(f"Входящее сообщение через канал {msg.channel.value} от user={msg.user_id} chat={msg.chat_id}: {msg.text!r}")
        global_user_id = await self._storage.upsert_contact(
            channel=msg.channel.value,
            platform_user_id=msg.user_id,
            platform_chat_id=msg.chat_id,
        )

        await self._storage.save_message(
            MessageRecord(
                global_user_id=global_user_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                direction=Direction.inbound,
                text=msg.text,
                timestamp=msg.timestamp,
            )
        )

        # Простой тестовый ответ
        reply_text = (
            "Спасибо! Это тестовый ответ от хаба.\n"
            f"Вы написали: {msg.text[:4000]}"
        )

        # Находим коннектор, который получил сообщение (сейчас отвечаем в том же канале)
        connector = self._find_connector_for_channel(msg.channel)
        if connector is None:
            logger.error(f"Не найден коннектор для ответа в канале {msg.channel}")
            return

        typing_seconds = estimate_typing_seconds(reply_text)
        await connector.simulate_typing(chat_id=msg.chat_id, seconds=typing_seconds)

        await connector.send_message(chat_id=msg.chat_id, text=reply_text)

        await self._storage.save_message(
            MessageRecord(
                global_user_id=global_user_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                direction=Direction.outbound,
                text=reply_text,
                timestamp=datetime.utcnow(),
            )
        )

    def _find_connector_for_channel(self, channel: "Channel") -> "BaseConnector | None":
        for c in self._connectors:
            if c.channel == channel:
                return c
        return None
