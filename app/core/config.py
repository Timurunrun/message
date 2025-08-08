from __future__ import annotations

from pydantic import BaseModel
import os
from typing import List


class AppConfig(BaseModel):
    telegram_bot_token: str | None = None
    vk_tokens: List[str] = []
    db_path: str = "data/messages.db"

    @staticmethod
    def load_from_env() -> "AppConfig":
        raw_vk = os.getenv("VK_COMMUNITY_TOKENS", "")
        vk_tokens = [t.strip() for t in raw_vk.split(",") if t.strip()]
        return AppConfig(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            vk_tokens=vk_tokens,
            db_path=os.getenv("DB_PATH", "data/messages.db"),
        )
