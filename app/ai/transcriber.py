from __future__ import annotations

import os
from io import BytesIO
from typing import Optional

from loguru import logger
from openai import AsyncOpenAI


class SpeechToTextService:
    """Асинхронная служба расшифровки аудио через OpenAI."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY не задан в окружении и не передан в конструктор")
        self._model = model or "gpt-4o-transcribe"
        self._client = AsyncOpenAI(api_key=self._api_key)

    async def transcribe(self, *, audio_bytes: bytes, file_name: Optional[str] = None, mime_type: Optional[str] = None) -> str:
        if not audio_bytes:
            raise ValueError("audio_bytes is empty")

        stream = BytesIO(audio_bytes)
        stream.name = file_name or self._default_file_name(mime_type)

        try:
            response = await self._client.audio.transcriptions.create(
                model=self._model,
                file=stream,
                response_format="text",
                temperature=0,
            )
        except Exception as exc:
            logger.error("Ошибка при расшифровке аудио: {}", exc)
            raise

        if isinstance(response, str):
            return response.strip()
        text = getattr(response, "text", None)
        if isinstance(text, str):
            return text.strip()
        return ""

    @staticmethod
    def _default_file_name(mime_type: Optional[str]) -> str:
        if mime_type == "audio/ogg":
            return "audio.ogg"
        if mime_type == "audio/mpeg":
            return "audio.mp3"
        if mime_type == "audio/mp4":
            return "audio.m4a"
        if mime_type == "audio/wav":
            return "audio.wav"
        return "audio.mp3"
