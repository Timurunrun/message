from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List

from loguru import logger


@dataclass(frozen=True)
class Tool:
	name: str
	description: str
	parameters: Dict[str, Any]
	handler: Callable[[Dict[str, Any]], Awaitable[str]]     # Сам обработчик. Принимает словарь аргументов и возвращает строку.


_TOOLS: Dict[str, Tool] = {}


def register_tool(tool: Tool) -> None:
	if tool.name in _TOOLS:
		raise ValueError(f"Инструмент с именем {tool.name!r} уже зарегистрирован")
	_TOOLS[tool.name] = tool


def get_openai_tools_spec() -> List[Dict[str, Any]]:
	"""Вернуть список инструментов.

	Пример элемента:
	{
		"type": "function",
		"name": "get_weather",
		"description": "...",
		"parameters": {...},
		"strict": True,
	}
	"""
	specs: List[Dict[str, Any]] = []
	for t in _TOOLS.values():
		specs.append(
			{
				"type": "function",
				"name": t.name,
				"description": t.description,
				"parameters": t.parameters,
				"strict": True,
			}
		)
	return specs


async def call_tool(name: str, args: Dict[str, Any]) -> str:
	"""Вызвать зарегистрированный инструмент. Если не найден — вернуть ошибку для модели."""
	tool = _TOOLS.get(name)
	if not tool:
		err = f"Инструмент {name!r} не найден"
		logger.warning(err)
		return err
	try:
		return await tool.handler(args)
	except Exception as e:
		logger.exception("Ошибка выполнения инструмента {}: {}", name, e)
		return f"tool_error: {e}"


# ===== Инструменты =====


async def _test_console_handler(_: Dict[str, Any]) -> str:
	print("тест")
	logger.info("Тестовый инструмент: 'тест' напечатан в консоль")
	return "ok"


register_tool(
	Tool(
		name="test_console_tool",
		description="Печатает слово 'тест' в консоль для проверки работы инструментов",
		parameters={
			"type": "object",
			"properties": {},
			"required": [],
			"additionalProperties": False,
		},
		handler=_test_console_handler,
	)
)