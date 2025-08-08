from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional
import random

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart
from aiogram.types import Message
from loguru import logger

from app.core.models import IncomingMessage, Channel
from .base import BaseConnector, OnMessageCallback


class TelegramConnector(BaseConnector):
    def __init__(self, bot_token: str) -> None:
        self._bot = Bot(token=bot_token)
        self._dp = Dispatcher()
        self._router = Router()
        self._dp.include_router(self._router)
        self._on_message: Optional[OnMessageCallback] = None
        self._polling_task: Optional[asyncio.Task] = None

        # Регистрируем обработчики
        self._router.message.register(self._handle_message)

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def channel(self) -> Channel:
        return Channel.telegram

    async def start(self, on_message: OnMessageCallback) -> None:
        self._on_message = on_message
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(self._run_polling())
        logger.info("Telegram-коннектор запущен, polling...")

    async def stop(self) -> None:
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        await self._bot.session.close()
        logger.info("Telegram-коннектор остановлен")

    async def _run_polling(self) -> None:
        await self._dp.start_polling(self._bot, allowed_updates=self._dp.resolve_used_update_types())

    async def _handle_message(self, message: Message) -> None:
        if self._on_message is None:
            return
        text = message.text or message.caption or ""
        chat_id = str(message.chat.id)
        user_id = str(message.from_user.id if message.from_user else message.chat.id)
        incoming = IncomingMessage(
            channel=self.channel,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            timestamp=datetime.utcnow(),
            raw=message.model_dump(),
        )
        await self._on_message(incoming)

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._bot.send_message(chat_id=chat_id, text=text)

    async def simulate_typing(self, chat_id: str, seconds: float) -> None:
        # Небольшая задержка перед началом индикации набора
        await asyncio.sleep(random.uniform(1.0, 3.0))
        remaining = max(0.0, float(seconds))
        # Пульсирующая отправка действия, чтобы индикатор не пропадал до момента отправки сообщения
        pulse = 4.5  # Telegram показывает действие примерно 5 секунд; обновляем чуть раньше
        while remaining > 0:
            try:
                await self._bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            sleep_time = min(pulse, remaining)
            await asyncio.sleep(sleep_time)
            remaining -= sleep_time
