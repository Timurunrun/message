from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .provider import AIAssistant, AIMessage, AIResult, Role


class OpenAIManager(AIAssistant):
    """Адаптер OpenAI Responses API без инструментов."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        verbosity: Optional[str] = None,
    ) -> None:
        from openai import AsyncOpenAI

        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY не задан в окружении и не передан в конструктор")
        self._client = AsyncOpenAI(api_key=self._api_key)
        # Попытка подхватить настройки из system_config.json, если параметры не заданы явно
        cfg_path = Path(__file__).with_name("system_config.json")
        cfg: Dict[str, Any] = {}
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
        openai_cfg = (cfg.get("OpenAI") or {}) if isinstance(cfg, dict) else {}

        self._model = model or openai_cfg.get("model", "gpt-5-mini")
        self._reasoning_effort = reasoning_effort or openai_cfg.get("reasoning_effort", "low")
        self._verbosity = verbosity or openai_cfg.get("verbosity", "low")

    async def generate(self, *, messages: List[AIMessage]) -> AIResult:
        from openai import BadRequestError

        # История сообщений
        history = [
            {"role": m.role.value, "content": m.content, **({"name": m.name} if m.name else {})}
            for m in messages
        ]
        try:
            resp = await self._client.responses.create(
                model=self._model,
                input=history,
                reasoning={"effort": self._reasoning_effort},
                text={"verbosity": self._verbosity},
            )
        except BadRequestError as e:
            raise e

        provider_id: Optional[str] = getattr(resp, "id", None)
        output_text = getattr(resp, "output_text", None)
        if not output_text:
            raise RuntimeError("Модель не вернула output_text")

        return AIResult(text=output_text, provider_message_id=provider_id)
