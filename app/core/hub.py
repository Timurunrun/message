from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import List, Optional
from loguru import logger

from .models import IncomingMessage, MessageRecord, Direction, ToolInvocation, AIResponseRecord
from .storage import Storage
from .utils import estimate_typing_seconds
from app.ai import OpenAIManager, AIMessage, Role
from app.connectors.base import BaseConnector
from app.core.models import Channel
from .config import AppConfig
from pathlib import Path
from app.core.session import SessionContext, set_current_session, reset_current_session
from app.crm.service import AmoCRMService, LeadStageContext
from app.ai.tools import (
    MessagingActions,
    SendReactionRequest,
    SendTextRequest,
    SendVoiceRequest,
    clear_messaging_actions,
    set_messaging_actions,
)


class Hub:
    def __init__(
        self,
        storage: Storage,
        connectors: List["BaseConnector"],
        config: AppConfig | None = None,
        crm_service: AmoCRMService | None = None,
    ) -> None:
        self._storage = storage
        self._connectors = connectors
        self._tasks: list[asyncio.Task] = []
        self._crm_service = crm_service

        set_messaging_actions(
            MessagingActions(
                send_text=self._handle_tool_send_text,
                send_voice=self._handle_tool_send_voice,
                send_reaction=self._handle_tool_send_reaction,
            )
        )

        # Инициализируем менеджер ИИ
        try:
            self._assistant = OpenAIManager(
            api_key=getattr(config, "openai_api_key", None),
            model=getattr(config, "ai_model"),
            reasoning_effort=getattr(config, "ai_reasoning_effort"),
            verbosity=getattr(config, "ai_verbosity"),
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
        clear_messaging_actions()

    async def on_incoming_message(self, msg: IncomingMessage) -> None:
        logger.info(f"Входящее сообщение через канал {msg.channel.value} от user={msg.user_id} chat={msg.chat_id}: {msg.text!r}")
        global_user_id, _ = await self._storage.upsert_contact(
            channel=msg.channel.value,
            platform_user_id=msg.user_id,
            platform_chat_id=msg.chat_id,
        )

        session_ctx = SessionContext(
            global_user_id=global_user_id,
            channel=msg.channel,
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            reply_to_message_id=msg.message_id,
        )

        crm_binding = None
        lead_context: Optional[LeadStageContext] = None
        if self._crm_service is not None:
            try:
                crm_binding = await self._crm_service.ensure_contact_and_lead(session=session_ctx, message=msg)
                lead_context = await self._crm_service.get_lead_context(global_user_id)
            except Exception as e:
                logger.error("Ошибка AmoCRM при обработке сообщения: {}", e)
                lead_context = None

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
        system_parts: List[str] = []
        prompt_path = Path(__file__).parents[1] / "ai" / "system_prompt.md"
        if prompt_path.exists():
            system_prompt = prompt_path.read_text(encoding="utf-8")
            system_parts.append(system_prompt)
        else:
            err = f"Не найден system_prompt.md по пути {prompt_path}"
            logger.error(err)
            raise FileNotFoundError(err)

        if self._crm_service is not None:
            crm_info_lines: List[str] = []
            if lead_context is not None:
                crm_info_lines.append("Текущая сделка AmoCRM:")
                deal_name = lead_context.lead_name if lead_context.lead_name else ("—" if lead_context.lead_present else "— (сделка ещё не создана)")
                crm_info_lines.append(f"1) Название сделки: {deal_name}")
                if lead_context.current_stage:
                    stage_name = lead_context.current_stage.get("name", "—")
                    stage_status = lead_context.current_stage.get("status_id")
                    stage_status_str = f"status_id={stage_status}" if stage_status is not None else "status_id=—"
                    crm_info_lines.append(f"   Текущий этап: {stage_name} ({stage_status_str})")
                else:
                    crm_info_lines.append("   Текущий этап: —")
                next_stage = lead_context.next_stage
                if next_stage:
                    next_name = next_stage.get("name", "—")
                    next_status = next_stage.get("status_id")
                    next_status_str = f"status_id={next_status}" if next_status is not None else "status_id=—"
                    crm_info_lines.append(f"2) Следующий этап: {next_name} ({next_status_str})")
                else:
                    crm_info_lines.append("2) Следующий этап: —")
                crm_info_lines.append("3) Вопросы текущего этапа:")
                for question in lead_context.questions:
                    crm_info_lines.append(
                        f"   • {question.name} (id={question.id}, type={question.type})"
                    )
                    crm_info_lines.append(f"     Текущий ответ: {question.answer}")
                    if question.enum_options:
                        crm_info_lines.append("     enum_id варианты:")
                        for option in question.enum_options:
                            crm_info_lines.append(f"       - {option['id']}: {option['value']}")
            elif crm_binding and crm_binding.lead_id:
                crm_info_lines.append("Текущая сделка AmoCRM: информация недоступна")
            else:
                crm_info_lines.append("Сделка ещё не создана или недоступна")
            stage_lines: list[str] = []
            for stage in self._crm_service.stages:
                status = f"{stage.status_id}" if stage.status_id is not None else "—"
                stage_lines.append(f"{stage.index + 1}. {stage.name} (status_id={status})")
            stages_hint = " | ".join(stage_lines)
            crm_info_lines.append(
                f"""
                ## Работа с AmoCRM
                1. После получения ответа обязательно фиксируй его через инструмент `amocrm_update_lead_fields`.
                - Для текстовых и числовых полей передавай значение строкой (например, '10').
                - Для вопросов с типами `select` и `multiselect` передай `enum_id` из списка вариантов для соответствующего вопроса.
                2. Когда ты задашь все вопросы на этапе, переходи к следующему этапу при помощи amocrm_set_lead_stage.
                """
            )
            if stage_lines:
                crm_info_lines.append(f"Этапы воронки: {stages_hint}")
            system_parts.append("\n".join(crm_info_lines))

        if system_parts:
            ai_messages.append(AIMessage(role=Role.system, content="\n\n".join(system_parts)))

        # Собираем историю диалога пользователя
        history: List[MessageRecord] = []
        try:
            history = await self._storage.get_all_messages(global_user_id)
        except Exception:
            history = []

        if history:
            for r in history:
                if r.direction == Direction.inbound:
                    ai_messages.append(AIMessage(role=Role.user, content=r.text))
                else:
                    ai_messages.append(AIMessage(role=Role.assistant, content=r.text))
        else:
            ai_messages.append(AIMessage(role=Role.user, content=msg.text))

        if self._assistant is None:
            ai_text = (
                "Извините, сейчас проходят технические работы. Мы свяжемся с вами позже."
            )
            await self._storage.save_ai_response(
                AIResponseRecord(
                    global_user_id=global_user_id,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    user_id=msg.user_id,
                    text=ai_text,
                    timestamp=datetime.now(timezone.utc),
                    provider_message_id=None,
                )
            )
            await self._handle_tool_send_text(
                session_ctx,
                SendTextRequest(text=ai_text, simulate_typing=True),
            )
        else:
            try:
                t0 = time.perf_counter()
                token = set_current_session(session_ctx)
                try:
                    ai_result = await self._assistant.generate(messages=ai_messages)
                finally:
                    reset_current_session(token)
                latency = time.perf_counter() - t0
                events = getattr(self._assistant, "last_events", []) or []
                ai_text = ai_result.text or self._extract_primary_text_from_events(events) or ""
                # Логируем метрики ответа
                logger.info(
                    "Внутренний ответ ассистента: id={}, latency={:.2f}s, текст={}",
                    getattr(ai_result, "provider_message_id", None),
                    latency,
                    ai_text,
                )
                await self._storage.save_ai_response(
                    AIResponseRecord(
                        global_user_id=global_user_id,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        user_id=msg.user_id,
                        text=ai_text,
                        timestamp=datetime.now(timezone.utc),
                        provider_message_id=getattr(ai_result, "provider_message_id", None),
                    )
                )
                # Сохраняем упрощённую запись об использовании инструмента
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
                fallback_text = "Извините, сейчас проходят технические работы. Мы свяжемся с вами позже."
                await self._storage.save_ai_response(
                    AIResponseRecord(
                        global_user_id=global_user_id,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        user_id=msg.user_id,
                        text=fallback_text,
                        timestamp=datetime.now(timezone.utc),
                        provider_message_id=None,
                    )
                )
                await self._handle_tool_send_text(
                    session_ctx,
                    SendTextRequest(text=fallback_text, simulate_typing=True),
                )

    def _find_connector_for_channel(self, channel: "Channel") -> "BaseConnector | None":
        for c in self._connectors:
            if c.channel == channel:
                return c
        return None

    async def _handle_tool_send_text(self, session: SessionContext, payload: SendTextRequest) -> str:
        connector = self._find_connector_for_channel(session.channel)
        if connector is None:
            logger.error("Не найден коннектор для канала {} при отправке текста", session.channel)
            return "connector_not_available"
        text = payload.text.strip()
        if not text:
            return "invalid_text"
        typing_seconds = estimate_typing_seconds(text)
        if payload.simulate_typing and typing_seconds > 0:
            try:
                await connector.simulate_typing(chat_id=session.chat_id, seconds=typing_seconds)
            except Exception as exc:
                logger.warning("Не удалось имитировать набор: {}", exc)
        try:
            await connector.send_message(
                chat_id=session.chat_id,
                text=text,
                reply_to_message_id=session.reply_to_message_id,
            )
        except Exception as exc:
            logger.exception("Не удалось отправить сообщение через коннектор {}: {}", connector.name, exc)
            return f"send_failed: {exc}"
        await self._storage.save_message(
            MessageRecord(
                global_user_id=session.global_user_id,
                channel=session.channel,
                chat_id=session.chat_id,
                user_id=session.user_id,
                direction=Direction.outbound,
                text=text,
                timestamp=datetime.now(timezone.utc),
                correlation_id=payload.correlation_id,
            )
        )
        logger.info(
            "Отправлено сообщение через tool messaging_send_text в канал {} chat_id={} ({} симв.)",
            session.channel.value,
            session.chat_id,
            len(text),
        )
        return "ok"

    async def _handle_tool_send_voice(self, session: SessionContext, payload: SendVoiceRequest) -> str:
        connector = self._find_connector_for_channel(session.channel)
        if connector is None:
            logger.error("Не найден коннектор для канала {} при отправке голоса", session.channel)
            return "connector_not_available"
        logger.info(
            "Получен запрос на голосовое сообщение (voice_id={}, audio_url={}) для канала {} — функция не реализована",
            payload.voice_id,
            payload.audio_url,
            session.channel.value,
        )
        return "voice_not_supported"

    async def _handle_tool_send_reaction(self, session: SessionContext, payload: SendReactionRequest) -> str:
        connector = self._find_connector_for_channel(session.channel)
        if connector is None:
            logger.error("Не найден коннектор для канала {} при отправке реакции", session.channel)
            return "connector_not_available"
        logger.info(
            "Получен запрос на реакцию '{}' (remove={}) для канала {} — функция не реализована",
            payload.reaction,
            payload.remove,
            session.channel.value,
        )
        return "reaction_not_supported"

    @staticmethod
    def _extract_primary_text_from_events(events: List[dict]) -> str:
        for ev in reversed(events):
            if ev.get("type") != "tool_call":
                continue
            if ev.get("name") != "messaging_send_text":
                continue
            raw_args = ev.get("arguments")
            payload: Optional[dict] = None
            if isinstance(raw_args, dict):
                payload = raw_args
            elif isinstance(raw_args, str):
                try:
                    payload = json.loads(raw_args)
                except Exception:
                    payload = None
            if not payload:
                continue
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                return text
        return ""
