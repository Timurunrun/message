from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from app.core.models import IncomingMessage, Channel, VoiceAttachment
from .base import BaseConnector, OnMessageCallback


VK_API_VERSION = "5.199"


@dataclass
class VKCommunity:
    token: str
    group_id: int
    token_hash: str  # короткий идентификатор для маршрутизации


class VKConnector(BaseConnector):
    def __init__(self, tokens: List[str]) -> None:
        # Формат: GROUP_ID:TOKEN
        self._communities: List[VKCommunity] = []

        for entry in tokens:
            raw = entry.strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.split(":")]
            if len(parts) != 2 or not parts[0].isdigit():
                logger.error(
                    "Не удалось разобрать строку VK: '{}'. Ожидался формат GROUP_ID:TOKEN.", raw
                )
                continue
            group_id_str, token = parts
            try:
                group_id = int(group_id_str)
            except ValueError:
                logger.error(
                    "Неверный GROUP_ID в строке VK: {}. Ожидалось целое число перед двоеточием.", raw
                )
                continue
            if not token:
                logger.error("Пустой TOKEN в строке VK: {}", raw)
                continue
            token_hash = hashlib.sha1(token.encode("utf-8")).hexdigest()[:8]
            self._communities.append(
                VKCommunity(token=token, group_id=group_id, token_hash=token_hash)
            )

        self._on_message: Optional[OnMessageCallback] = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(40.0))
        self._polling_tasks: List[asyncio.Task] = []
        # Словари для быстрого выбора по хэшу токена или по идентификатору сообщества
        self._by_hash: Dict[str, VKCommunity] = {c.token_hash: c for c in self._communities}
        self._by_group: Dict[int, VKCommunity] = {c.group_id: c for c in self._communities}

    @property
    def name(self) -> str:
        return "vk"

    @property
    def channel(self) -> Channel:
        return Channel.vk

    async def start(self, on_message: OnMessageCallback) -> None:
        self._on_message = on_message
        if not self._communities:
            logger.warning("VK-коннектор запущен без сообществ! Укажите токены API в .env")
            return
        for community in self._communities:
            task = asyncio.create_task(self._run_long_poll(community))
            self._polling_tasks.append(task)
        logger.info("VK-коннектор запущен для {} сообществ(а)", len(self._communities))

    async def stop(self) -> None:
        for task in self._polling_tasks:
            task.cancel()
        for task in self._polling_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._polling_tasks.clear()
        await self._client.aclose()
        logger.info("VK-коннектор остановлен")

    def _encode_chat_id(self, peer_id: int, community: VKCommunity) -> str:
        # Храним в формате peer_id:group_id
        return f"{peer_id}:{community.group_id}"

    def _decode_chat_id(self, chat_id: str) -> Tuple[int, VKCommunity]:
        try:
            peer_str, group_str = chat_id.split(":", 1)
            peer_id = int(peer_str)
            group_id = int(group_str)
        except Exception:
            raise ValueError(f"Invalid VK chat_id format: {chat_id}")
        community = self._by_group.get(group_id)
        if community is None:
            raise ValueError(f"Unknown VK group in chat_id: {chat_id}")
        return peer_id, community

    async def _api_call(self, method: str, community: VKCommunity, params: dict) -> dict:
        url = f"https://api.vk.com/method/{method}"
        data = {
            **params,
            "v": VK_API_VERSION,
            "access_token": community.token,
        }
        resp = await self._client.post(url, data=data)
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            err = payload["error"]
            code = err.get("error_code")
            sub = err.get("error_subcode")
            if code == 15 and sub == 1133:
                logger.error(
                    "Ошибка VK API в {}: {} — у токена нет необходимых прав. Используйте токен доступа для сообщества {} с правами: сообщения + управление (и включите Long Poll в настройках сообщества).",
                    method,
                    err,
                    community.group_id,
                )
            else:
                logger.error("Ошибка VK API в {}: {}", method, err)
            raise RuntimeError(err)
        return payload.get("response", {})

    async def _get_long_poll_server(self, community: VKCommunity) -> Tuple[str, str, str]:
        response = await self._api_call(
            "groups.getLongPollServer",
            community,
            {"group_id": community.group_id},
        )
        server = response["server"]
        key = response["key"]
        ts = response["ts"]
        return server, key, ts

    async def _run_long_poll(self, community: VKCommunity) -> None:
        try:
            server, key, ts = await self._get_long_poll_server(community)
        except Exception as e:
            logger.error(
                "Не удалось запустить Long Poll VK для сообщества {}. Проверьте права токена (токен сообщества с правами сообщения + управление) и настройки Long Poll. Ошибка: {}",
                community.group_id,
                e,
            )
            return
        wait_seconds = 25
        lp_params = {"act": "a_check", "key": key, "wait": str(wait_seconds), "mode": "2", "ts": ts}
        logger.info("Запущен Long Poll VK для сообщества {}", community.group_id)
        while True:
            try:
                r = await self._client.get(server, params=lp_params)
                r.raise_for_status()
                data = r.json()
                if "failed" in data:
                    failed = data.get("failed")
                    if failed == 1:
                        lp_params["ts"] = data.get("ts", lp_params["ts"])  # просто обновляем ts
                        continue
                    elif failed in (2, 3):
                        # требуется новый ключ или новый сервер
                        server, key, ts = await self._get_long_poll_server(community)
                        lp_params = {"act": "a_check", "key": key, "wait": str(wait_seconds), "mode": "2", "ts": ts}
                        continue
                    else:
                        server, key, ts = await self._get_long_poll_server(community)
                        lp_params = {"act": "a_check", "key": key, "wait": str(wait_seconds), "mode": "2", "ts": ts}
                        continue

                updates = data.get("updates", [])
                lp_params["ts"] = data.get("ts", lp_params["ts"])  # продвигаем ts
                for upd in updates:
                    if upd.get("type") == "message_new":
                        obj = upd.get("object", {})
                        msg = obj.get("message") or obj  # для совместимости
                        text = msg.get("text", "")
                        peer_id = msg.get("peer_id")
                        from_id = msg.get("from_id", peer_id)
                        if peer_id is None:
                            continue
                        if self._on_message is None:
                            continue
                        msg_id = msg.get("conversation_message_id")
                        if msg_id is None:
                            msg_id = msg.get("id")
                        voice_payload: Optional[VoiceAttachment] = None
                        attachments = msg.get("attachments") or []
                        for attachment in attachments:
                            if attachment.get("type") != "audio_message":
                                continue
                            audio_message = attachment.get("audio_message") or {}
                            audio_url = audio_message.get("link_mp3") or audio_message.get("link_ogg")
                            if not audio_url:
                                continue

                            async def _download_voice(url: str = audio_url) -> bytes:
                                response = await self._client.get(url)
                                response.raise_for_status()
                                return response.content

                            duration = audio_message.get("duration")
                            duration_seconds: Optional[float] = None
                            if isinstance(duration, (int, float)):
                                duration_seconds = float(duration)
                            doc_id = audio_message.get("id")
                            owner_id = audio_message.get("owner_id")
                            mime_type = None
                            if isinstance(audio_url, str):
                                lower_url = audio_url.lower()
                                if lower_url.endswith(".ogg"):
                                    mime_type = "audio/ogg"
                                elif lower_url.endswith(".mp3"):
                                    mime_type = "audio/mpeg"
                            extension = "mp3" if mime_type != "audio/ogg" else "ogg"
                            filename = (
                                f"vk_voice_{owner_id}_{doc_id}.{extension}"
                                if doc_id is not None and owner_id is not None
                                else f"vk_voice.{extension}"
                            )
                            voice_payload = VoiceAttachment(
                                download=_download_voice,
                                file_name=filename,
                                mime_type=mime_type,
                                duration_seconds=duration_seconds,
                                file_size=audio_message.get("size"),
                            )
                            break
                        incoming = IncomingMessage(
                            channel=self.channel,
                            chat_id=self._encode_chat_id(peer_id=int(peer_id), community=community),
                            user_id=str(from_id),
                            text=text,
                            timestamp=datetime.now(timezone.utc),
                            message_id=str(msg_id) if msg_id is not None else None,
                            raw=upd,
                            voice=voice_payload,
                        )
                        await self._on_message(incoming)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Ошибка Long Poll VK для сообщества {}: {}", community.group_id, e)
                await asyncio.sleep(2.0)
                try:
                    server, key, ts = await self._get_long_poll_server(community)
                    lp_params = {"act": "a_check", "key": key, "wait": str(wait_seconds), "mode": "2", "ts": ts}
                except Exception:
                    await asyncio.sleep(5.0)

    async def send_message(self, chat_id: str, text: str, reply_to_message_id: Optional[str] = None) -> None:
        peer_id, community = self._decode_chat_id(chat_id)
        random_id = random.randint(1, 2**31 - 1)
        params = {"peer_id": peer_id, "random_id": random_id, "message": text}
        if reply_to_message_id:
            try:
                params["reply_to"] = int(reply_to_message_id)
            except ValueError:
                logger.warning(
                    "Некорректный идентификатор сообщения для reply в VK: {}",
                    reply_to_message_id,
                )
        await self._api_call("messages.send", community, params)

    # Имитируем набор текста
    async def simulate_typing(self, chat_id: str, seconds: float) -> None:
        peer_id, community = self._decode_chat_id(chat_id)
        # Небольшая задержка перед началом набора
        # await asyncio.sleep(random.uniform(1.0, 3.0))
        remaining = max(0.0, float(seconds))
        pulse = 4.5     # обновление статуса "печатает..."
        while remaining > 0:
            try:
                await self._api_call("messages.setActivity", community, {"peer_id": peer_id, "type": "typing"})
            except Exception:
                pass
            sleep_time = min(pulse, remaining)
            await asyncio.sleep(sleep_time)
            remaining -= sleep_time
