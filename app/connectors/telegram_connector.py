from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
import random

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart
from aiogram.types import Message
from loguru import logger

from app.core.models import IncomingMessage, Channel
from .base import BaseConnector, OnMessageCallback


def _parse_tg_chat_id(chat_id: str) -> tuple[str, Optional[str]]:
    """Возвращает (chat_id, business_connection_id or None).
    Кодируем id бизнес-чатов как: "<chat_id>:<business_connection_id>".
    """
    if ":" in chat_id:
        try:
            cid, bcid = chat_id.split(":", 1)
            return cid, (bcid or None)
        except Exception:
            return chat_id, None
    return chat_id, None


class TelegramConnector(BaseConnector):
    def __init__(self, bot_token: str) -> None:
        self._bot = Bot(token=bot_token)
        self._dp = Dispatcher()
        self._router = Router()
        self._dp.include_router(self._router)
        self._on_message: Optional[OnMessageCallback] = None
        self._polling_task: Optional[asyncio.Task] = None

        # Регистрируем обработчики обычных сообщений
        self._router.message.register(self._handle_message)
        # Регистрируем обработчики Business Mode
        self._router.business_message.register(self._handle_business_message)
        self._router.business_connection.register(self._handle_business_connection)

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
        logger.info("Telegram-коннектор запущен")

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

        # Если aiogram предоставляет business_connection_id у сообщения — включаем его
        bc_id = getattr(message, "business_connection_id", None)
        try:
            if bc_id is None:
                md = message.model_dump()
                bc_id = md.get("business_connection_id")
        except Exception:
            bc_id = None
        stored_chat_id = f"{chat_id}:{bc_id}" if bc_id else chat_id

        # Перехватываем /start
        if text.strip().startswith("/start"):
            try:
                await self.send_message(
                    chat_id=stored_chat_id,
                    text="Добрый день! Это компания по корпоративной доставке питания.\n\nНапишите в чат, что вас интересует. Вам ответит первый освободившийся оператор 🍽️",
                )
            except Exception:
                pass
            return

        incoming = IncomingMessage(
            channel=self.channel,
            chat_id=stored_chat_id,
            user_id=user_id,
            text=text,
            timestamp=datetime.now(timezone.utc),
            raw=message.model_dump(),
        )
        await self._on_message(incoming)

    # Обработка бизнес-сообщений
    async def _handle_business_message(self, message: Message) -> None:
        
        if self._on_message is None:
            return
        text = message.text or message.caption or ""
        chat_id = str(message.chat.id)
        user_id = str(message.from_user.id if message.from_user else message.chat.id)
        # У бизнес-сообщений должен быть business_connection_id
        bc_id = getattr(message, "business_connection_id", None)
        try:
            if bc_id is None:
                md = message.model_dump()
                bc_id = md.get("business_connection_id")
        except Exception:
            bc_id = None
        stored_chat_id = f"{chat_id}:{bc_id}" if bc_id else chat_id
        incoming = IncomingMessage(
            channel=self.channel,
            chat_id=stored_chat_id,
            user_id=user_id,
            text=text,
            timestamp=datetime.now(timezone.utc),
            raw=message.model_dump(),
        )
        await self._on_message(incoming)

    async def _handle_business_connection(self, message: Message) -> None:
        # Пока только логируем изменения бизнес-подключений
        try:
            logger.info("Обновление бизнес-подключения: {}", message.model_dump())
        except Exception:
            logger.info("Получено обновление бизнес-подключения")

    async def send_message(self, chat_id: str, text: str) -> None:
        base_chat_id, bc_id = _parse_tg_chat_id(chat_id)
        if bc_id:
            await self._bot.send_message(chat_id=base_chat_id, text=text, business_connection_id=bc_id)
        else:
            await self._bot.send_message(chat_id=base_chat_id, text=text)

    # Имитируем набор текста
    async def simulate_typing(self, chat_id: str, seconds: float) -> None:
        # Небольшая задержка перед началом набора
        # await asyncio.sleep(random.uniform(1.0, 3.0))
        remaining = max(0.0, float(seconds))
        pulse = 4.5  # обновление статуса "печатает..."
        base_chat_id, bc_id = _parse_tg_chat_id(chat_id)
        while remaining > 0:
            try:
                if bc_id:
                    await self._bot.send_chat_action(chat_id=base_chat_id, action=ChatAction.TYPING, business_connection_id=bc_id)
                else:
                    await self._bot.send_chat_action(chat_id=base_chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            sleep_time = min(pulse, remaining)
            await asyncio.sleep(sleep_time)
            remaining -= sleep_time
