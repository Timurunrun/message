from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List

from loguru import logger

from app.crm.deps import get_amocrm_service
from app.core.session import get_current_session


@dataclass(frozen=True)
class Tool:
	name: str
	description: str
	parameters: Dict[str, Any]
	handler: Callable[[Dict[str, Any]], Awaitable[str]]


_TOOLS: Dict[str, Tool] = {}

_AMOCRM_TOOLS_REGISTERED = False


def register_tool(tool: Tool) -> None:
	if tool.name in _TOOLS:
		raise ValueError(f"Tool with name {tool.name!r} is already registered")
	_TOOLS[tool.name] = tool


def get_openai_tools_spec() -> List[Dict[str, Any]]:
	"""Return the list of tools.

	Example element:
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
	"""Call a registered tool. If not found — return an error for the model."""
	tool = _TOOLS.get(name)
	if not tool:
		err = f"Tool {name!r} not found"
		logger.warning(err)
		return err
	try:
		return await tool.handler(args)
	except Exception as e:
		logger.exception("Error executing tool {}: {}", name, e)
		return f"tool_error: {e}"


# ===== Tools =====


async def _test_console_handler(_: Dict[str, Any]) -> str:
	print("test")
	logger.info("Test tool: 'test' printed to console")
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


async def _amocrm_update_fields_handler(args: Dict[str, Any]) -> str:
	service = get_amocrm_service()
	if service is None:
		return "amocrm_not_configured"
	session = get_current_session()
	if session is None:
		return "session_not_found"
	answers = args.get("answers")
	if not isinstance(answers, list):
		return "invalid_arguments"
	result = await service.update_lead_fields(global_user_id=session.global_user_id, answers=answers)
	if result != "ok":
		return result
	snapshot = await service.build_stage_snapshot(session.global_user_id)
	return snapshot


async def _amocrm_advance_stage_handler(args: Dict[str, Any]) -> str:
	service = get_amocrm_service()
	if service is None:
		return "amocrm_not_configured"
	session = get_current_session()
	if session is None:
		return "session_not_found"
	stage_id = args.get("stage_id")
	if not isinstance(stage_id, int):
		return "invalid_stage"
	result = await service.change_lead_stage(global_user_id=session.global_user_id, stage_id=stage_id)
	if result != "ok":
		return result
	snapshot = await service.build_stage_snapshot(session.global_user_id)
	return snapshot


def register_amocrm_tools() -> None:
	global _AMOCRM_TOOLS_REGISTERED
	if _AMOCRM_TOOLS_REGISTERED:
		return
	service = get_amocrm_service()
	if service is None:
		raise RuntimeError("AmoCRM service is not initialized")
	question_ids = [q.id for q in service.questions]
	stage_ids = [sid for sid in service.stage_ids if isinstance(sid, int)]
	stage_descriptions = []
	for stage in service.stages:
		if stage.status_id is not None:
			stage_descriptions.append(f"{stage.status_id} — {stage.name}")
	stages_hint = ", ".join(stage_descriptions) if stage_descriptions else None
	register_tool(
		Tool(
			name="amocrm_update_lead_fields",
			description=(
				"Заполняет ответы по вопросам текущего этапа воронки AmoCRM. "
				"Для текстовых и числовых полей передавай строки (например, '10'). "
				"Для select/multiselect указывай enum_id (числовой идентификатор варианта)." 
			),
			parameters={
				"type": "object",
				"properties": {
					"answers": {
						"type": "array",
						"minItems": 1,
						"items": {
							"type": "object",
							"properties": {
								"question_id": {
									"type": "integer",
									"description": "ID вопроса из списка доступных вопросов",
									"enum": question_ids,
								},
								"values": {
									"type": "array",
									"minItems": 1,
									"items": {
										"anyOf": [
											{"type": "string"},
											{"type": "integer"},
										],
									},
									"description": (
										"Ответы на вопрос. Для текстового поля передавай строку. "
										"Для select/multiselect укажи список enum_id (числа)."
									),
								},
							},
							"required": ["question_id", "values"],
							"additionalProperties": False,
						},
					},
				},
				"required": ["answers"],
				"additionalProperties": False,
			},
			handler=_amocrm_update_fields_handler,
		),
	)
	register_tool(
		Tool(
			name="amocrm_set_lead_stage",
			description="Переводит сделку на следующий этап воронки AmoCRM.",
			parameters={
				"type": "object",
				"properties": {
					"stage_id": {
						"type": "integer",
						"description": "ID этапа из допустимого списка" + (f" ({stages_hint})" if stages_hint else ""),
						"enum": stage_ids,
					},
				},
				"required": ["stage_id"],
				"additionalProperties": False,
			},
			handler=_amocrm_advance_stage_handler,
		),
	)
	_AMOCRM_TOOLS_REGISTERED = True
