from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import List, Optional
from loguru import logger

from .models import IncomingMessage, MessageRecord, Direction, ToolInvocation
from .storage import Storage
from .utils import estimate_typing_seconds
from app.ai import OpenAIManager, AIMessage, Role
from app.connectors.base import BaseConnector
from app.core.models import Channel
from .config import AppConfig
from pathlib import Path


class Hub:
    def __init__(self, storage: Storage, connectors: List["BaseConnector"], config: AppConfig | None = None) -> None:
        self._storage = storage
        self._connectors = connectors
        self._tasks: list[asyncio.Task] = []

        # Инициализируем ИИ
        try:
            self._assistant = OpenAIManager(
            api_key=getattr(config, "openai_api_key", None),
            model=getattr(config, "ai_model", "gpt-5-mini"),
            reasoning_effort=getattr(config, "ai_reasoning_effort", "minimal"),
            verbosity=getattr(config, "ai_verbosity", "low"),
            )
        except Exception as e:
            logger.error("Не удалось инициализировать ИИ: {}", e)
            self._assistant = None

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

        # Готовим историю сообщений для ИИ
        ai_messages: list[AIMessage] = []
        prompt_path = Path(__file__).parents[1] / "ai" / "system_prompt.md"
        if prompt_path.exists():
            system_prompt = prompt_path.read_text(encoding="utf-8")
            ai_messages.append(AIMessage(role=Role.system, content=system_prompt))
        else:
            err = f"Не найден system_prompt.md по пути {prompt_path}"
            logger.error(err)
            raise FileNotFoundError(err)

        # Собираем историю диалога пользователя
        try:
            history = await self._storage.get_all_messages(global_user_id)
            # Конвертируем историю сообщений с БД в диалог для модели
            for r in history:
                if r.direction == Direction.inbound:
                    ai_messages.append(AIMessage(role=Role.user, content=r.text))
                else:
                    ai_messages.append(AIMessage(role=Role.assistant, content=r.text))
        except Exception:
            pass
        # Добавляем текущее входящее сообщение
        ai_messages.append(AIMessage(role=Role.user, content=msg.text))

        reply_text = ""
        if self._assistant is None:
            reply_text = (
                "Извините, сейчас проходят технические работы. Мы свяжемся с вами позже."
            )
        else:
            try:
                t0 = time.perf_counter()
                ai_result = await self._assistant.generate(messages=ai_messages)
                latency = time.perf_counter() - t0
                reply_text = ai_result.text or "Извините, не удалось сформировать ответ."
                # Логируем метрики ответа
                char_count = len(reply_text)
                typing_seconds = estimate_typing_seconds(reply_text)
                logger.info(
                    "OpenAI ответ: id={}, latency={:.2f}s, длина={} симв., typing≈{:.1f}s",
                    getattr(ai_result, "provider_message_id", None),
                    latency,
                    char_count,
                    typing_seconds,
                )
                # Сохраняем упрощённую запись об использовании инструмента
                events = getattr(self._assistant, "last_events", []) or []
                if events:
                    now = datetime.now(timezone.utc)
                    # Сопоставляем tool_call и tool_output по call_id, формируя одну запись
                    calls: dict[str, dict] = {}
                    for ev in events:
                        if ev.get("type") == "tool_call":
                            calls[ev.get("call_id")] = {
                                "name": ev.get("name"),
                                "arguments": ev.get("arguments"),
                                "output": "",
                            }
                        elif ev.get("type") == "tool_output":
                            cid = ev.get("call_id")
                            rec = calls.setdefault(cid, {"name": "", "arguments": "", "output": ""})
                            rec["output"] = str(ev.get("output") or "")

                    for cid, rec in calls.items():
                        await self._storage.save_tool_invocation(
                            ToolInvocation(
                                global_user_id=global_user_id,
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                user_id=msg.user_id,
                                tool_name=rec.get("name") or "",
                                arguments=rec.get("arguments") or "",
                                output=rec.get("output") or "",
                                timestamp=now,
                                call_id=cid,
                            )
                        )
            except Exception as e:
                logger.exception("Ошибка при генерации ответа ИИ: {}", e)
                reply_text = "Извините, сейчас проходят технические работы. Мы свяжемся с вами позже."

        # Находим коннектор, который получил сообщение (сейчас отвечаем в том же канале)
        connector = self._find_connector_for_channel(msg.channel)
        if connector is None:
            logger.error(f"Не найден коннектор для ответа в канале {msg.channel}")
            return

        typing_seconds = estimate_typing_seconds(reply_text)
        logger.info(
            "Имитация набора перед отправкой: длина={} симв., typing≈{:.1f}s",
            len(reply_text),
            typing_seconds,
        )
        await connector.simulate_typing(chat_id=msg.chat_id, seconds=typing_seconds)

        await connector.send_message(chat_id=msg.chat_id, text=reply_text)
        logger.info(
            "Ответ отправлен в {} chat_id={} ({} симв.)",
            msg.channel.value,
            msg.chat_id,
            len(reply_text),
        )

        await self._storage.save_message(
            MessageRecord(
                global_user_id=global_user_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                direction=Direction.outbound,
                text=reply_text,
                timestamp=datetime.now(timezone.utc),
            )
        )

    def _find_connector_for_channel(self, channel: "Channel") -> "BaseConnector | None":
        for c in self._connectors:
            if c.channel == channel:
                return c
        return None
