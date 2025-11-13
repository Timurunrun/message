from __future__ import annotations

from pydantic import BaseModel
import os
from typing import List


class AppConfig(BaseModel):
    telegram_bot_token: str | None = None
    vk_tokens: List[str] = []
    db_path: str = "data/messages.db"

    openai_api_key: str | None = None
    ai_model: str | None = None
    ai_reasoning_effort: str | None = None
    ai_verbosity: str | None = None
    ai_transcription_model: str | None = None
    amocrm_base_url: str | None = None
    amocrm_access_token: str | None = None

    @staticmethod
    def load_from_env() -> "AppConfig":
        raw_vk = os.getenv("VK_COMMUNITY_TOKENS", "")
        vk_tokens = [t.strip() for t in raw_vk.split(",") if t.strip()]
        return AppConfig(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            vk_tokens=vk_tokens,
            db_path=os.getenv("DB_PATH", "data/messages.db"),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            ai_model=os.getenv("AI_MODEL", "gpt-5"),
            ai_reasoning_effort=os.getenv("AI_REASONING_EFFORT", "low"),
            ai_verbosity=os.getenv("AI_VERBOSITY", "low"),
            ai_transcription_model=os.getenv("AI_TRANSCRIPTION_MODEL", "gpt-4o-transcribe"),
            amocrm_base_url=os.getenv("AMOCRM_BASE_URL") or None,
            amocrm_access_token=os.getenv("AMOCRM_ACCESS_TOKEN") or None,
        )
