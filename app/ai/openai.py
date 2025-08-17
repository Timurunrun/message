from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .provider import AIAssistant, AIMessage, AIResult, Role, ToolSpec


class OpenAIManager(AIAssistant):
    """Адаптер OpenAI Responses API с поддержкой tools.
    """

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

    async def generate(self, *, messages: List[AIMessage], tools: List[ToolSpec] | None = None) -> AIResult:
        from openai import BadRequestError

        # Подготовка инструментов
        tool_defs: List[Dict[str, Any]] = []
        exec_map: Dict[str, ToolSpec] = {}
        if tools:
            for t in tools:
                tool_defs.append(
                    {
                        "type": "function",
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    }
                )
                exec_map[t.name] = t

        # История сообщений
        history = [
            {"role": m.role.value, "content": m.content, **({"name": m.name} if m.name else {})}
            for m in messages
        ]

        prev_response_id: Optional[str] = None
        max_rounds = 6

        def _oget(o: Any, key: str, default: Any = None) -> Any:
            if isinstance(o, dict):
                return o.get(key, default)
            return getattr(o, key, default)

        for _ in range(max_rounds):
            try:
                resp = await self._client.responses.create(
                    model=self._model,
                    input=history,
                    reasoning={"effort": self._reasoning_effort},
                    text={"verbosity": self._verbosity},
                    # tools=tool_defs if tool_defs else None,
                    previous_response_id=prev_response_id
                )
            except BadRequestError as e:
                raise e

            prev_response_id = getattr(resp, "id", None)

            # Если модель вернула tool-calls — исполняем
            tool_calls = []
            for out in (getattr(resp, "output", None) or []):
                if _oget(out, "type") == "tool_call":
                    tool_calls.append(out)

            if tool_calls:
                for tc in tool_calls:
                    name = _oget(tc, "name")
                    call_id = _oget(tc, "id") or _oget(tc, "tool_call_id") or name
                    arguments = _oget(tc, "arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except Exception:
                            arguments = {"_raw": arguments}
                    tool = exec_map.get(name)
                    if not tool:
                        tool_result: Any = {"error": f"unknown tool: {name}"}
                    else:
                        tool_result = await tool.executor(arguments or {})

                    history.append(
                        {
                            "role": Role.tool.value,
                            "content": json.dumps(tool_result, ensure_ascii=False),
                            "name": name,
                            "tool_call_id": call_id,
                        }
                    )
                # продолжим ещё один раунд, чтобы модель увидела результаты инструментов
                continue

            # Иначе ожидаем итоговый текст
            output_text = getattr(resp, "output_text", None)
            if not output_text:
                raise RuntimeError("Модель не вернула output_text")

            return AIResult(text=output_text, provider_message_id=prev_response_id)

        # Если слишком много раундов инструментов
        raise RuntimeError("Превышено максимальное число раундов при вызове инструментов")
