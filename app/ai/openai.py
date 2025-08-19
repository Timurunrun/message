from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .provider import AIAssistant, AIMessage, AIResult, Role
from .tools import get_openai_tools_spec, call_tool


class OpenAIManager(AIAssistant):
    """Адаптер OpenAI Responses API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        verbosity: Optional[str] = None,
        max_steps: Optional[int] = None,
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
        self._max_steps = max_steps or int(openai_cfg.get("max_steps", 6))

    async def generate(self, *, messages: List[AIMessage]) -> AIResult:
        from openai import BadRequestError
        # История сообщений как стартовый input_list
        input_list: List[Dict[str, Any]] = [
            {"role": m.role.value, "content": m.content, **({"name": m.name} if m.name else {})}
            for m in messages
        ]

        provider_id: Optional[str] = None
        self.last_events = []  # сюда пишем только tool_call и tool_output

        # Многошаговый цикл: модель может чередовать вызовы инструментов и текст
        for _step in range(self._max_steps):
            try:
                resp = await self._client.responses.create(
                    model=self._model,
                    input=input_list,
                    reasoning={"effort": self._reasoning_effort},
                    text={"verbosity": self._verbosity},
                    tools=get_openai_tools_spec(),
                )
            except BadRequestError:
                raise

            provider_id = getattr(resp, "id", provider_id)

            output_items = list(getattr(resp, "output", []) or [])
            # Добавляем output в input для следующего шага — это нужно для вызовов инструментов
            if output_items:
                input_list += output_items

            tool_call_outputs: List[Dict[str, Any]] = []

            # Сбор событий и выполнение инструментов
            for item in output_items:
                if getattr(item, "type", None) == "function_call":
                    name = getattr(item, "name", None)
                    arguments = getattr(item, "arguments", "{}")
                    call_id = getattr(item, "call_id", None)
                    self.last_events.append({"type": "tool_call", "name": name, "arguments": arguments, "call_id": call_id})
                    try:
                        args = json.loads(arguments or "{}")
                    except Exception:
                        args = {}
                    result_str = await call_tool(name or "", args)
                    tool_call_outputs.append({"type": "function_call_output", "call_id": call_id, "output": result_str})
                    self.last_events.append({"type": "tool_output", "call_id": call_id, "output": result_str})

            if tool_call_outputs:
                # Передаём результаты инструментов и продолжаем цикл
                input_list += tool_call_outputs
                continue

            # Если инструментов не было — пробуем забрать финальный текст
            output_text = getattr(resp, "output_text", None)
            if output_text:
                return AIResult(text=output_text, provider_message_id=provider_id)

        # Если дошли сюда — превысили число шагов или ни разу не получили текст
        raise RuntimeError("Модель не вернула output_text")
