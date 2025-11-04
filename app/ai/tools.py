from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from app.crm.deps import get_amocrm_service
from app.core.session import SessionContext, get_current_session


@dataclass(frozen=True)
class SendTextRequest:
	text: str
	simulate_typing: bool = True
	correlation_id: Optional[str] = None


@dataclass(frozen=True)
class SendVoiceRequest:
	voice_id: Optional[str] = None
	audio_url: Optional[str] = None
	transcription: Optional[str] = None


@dataclass(frozen=True)
class SendReactionRequest:
	reaction: str
	remove: bool = False


@dataclass(frozen=True)
class MessagingActions:
	send_text: Callable[[SessionContext, SendTextRequest], Awaitable[str]]
	send_voice: Optional[Callable[[SessionContext, SendVoiceRequest], Awaitable[str]]] = None
	send_reaction: Optional[Callable[[SessionContext, SendReactionRequest], Awaitable[str]]] = None


@dataclass(frozen=True)
class Tool:
	name: str
	description: str
	parameters: Dict[str, Any]
	handler: Callable[[Dict[str, Any]], Awaitable[str]]


_TOOLS: Dict[str, Tool] = {}

_AMOCRM_TOOLS_REGISTERED = False
_MESSAGING_ACTIONS: Optional[MessagingActions] = None


def set_messaging_actions(actions: MessagingActions) -> None:
	global _MESSAGING_ACTIONS
	_MESSAGING_ACTIONS = actions


def clear_messaging_actions() -> None:
	global _MESSAGING_ACTIONS
	_MESSAGING_ACTIONS = None


def _get_messaging_actions() -> Optional[MessagingActions]:
	return _MESSAGING_ACTIONS


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


async def _send_text_message_handler(args: Dict[str, Any]) -> str:
	actions = _get_messaging_actions()
	if actions is None or actions.send_text is None:
		return "messaging_not_configured"
	session = get_current_session()
	if session is None:
		return "session_not_found"
	text = args.get("text")
	if not isinstance(text, str) or not text.strip():
		return "invalid_text"
	simulate_typing = args.get("simulate_typing", True)
	if not isinstance(simulate_typing, bool):
		simulate_typing = True
	correlation_id = args.get("correlation_id")
	if correlation_id is not None and not isinstance(correlation_id, str):
		correlation_id = None
	request = SendTextRequest(text=text, simulate_typing=simulate_typing, correlation_id=correlation_id)
	try:
		result = await actions.send_text(session, request)
	except Exception as exc:
		logger.exception("Error in send_text tool: {}", exc)
		return f"tool_error: {exc}"
	return result or "ok"


async def _send_voice_message_handler(args: Dict[str, Any]) -> str:
	actions = _get_messaging_actions()
	session = get_current_session()
	if session is None:
		return "session_not_found"
	if actions is None or actions.send_voice is None:
		return "voice_not_supported"
	voice_id = args.get("voice_id")
	audio_url = args.get("audio_url")
	transcription = args.get("transcription")
	if voice_id is not None and not isinstance(voice_id, str):
		voice_id = None
	if audio_url is not None and not isinstance(audio_url, str):
		audio_url = None
	if transcription is not None and not isinstance(transcription, str):
		transcription = None
	request = SendVoiceRequest(voice_id=voice_id, audio_url=audio_url, transcription=transcription)
	try:
		result = await actions.send_voice(session, request)
	except NotImplementedError:
		return "voice_not_supported"
	except Exception as exc:
		logger.exception("Error in send_voice tool: {}", exc)
		return f"tool_error: {exc}"
	return result or "voice_sent"


async def _send_reaction_handler(args: Dict[str, Any]) -> str:
	actions = _get_messaging_actions()
	session = get_current_session()
	if session is None:
		return "session_not_found"
	reaction = args.get("reaction")
	if not isinstance(reaction, str) or not reaction.strip():
		return "invalid_reaction"
	remove = args.get("remove", False)
	if not isinstance(remove, bool):
		remove = False
	if actions is None or actions.send_reaction is None:
		return "reaction_not_supported"
	request = SendReactionRequest(reaction=reaction, remove=remove)
	try:
		result = await actions.send_reaction(session, request)
	except NotImplementedError:
		return "reaction_not_supported"
	except Exception as exc:
		logger.exception("Error in send_reaction tool: {}", exc)
		return f"tool_error: {exc}"
	return result or "reaction_set"


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

register_tool(
	Tool(
		name="messaging_send_text",
		description="Отправляет текстовое сообщение пользователю в текущем канале общения.",
		parameters={
			"type": "object",
			"properties": {
				"text": {
					"type": "string",
					"description": "Текстовое содержимое сообщения",
					"minLength": 1,
				},
			},
			"required": ["text"],
			"additionalProperties": False,
		},
		handler=_send_text_message_handler,
	)
)

'''
register_tool(
	Tool(
		name="messaging_send_voice",
		description=(
			"Отправляет голосовое сообщение или аудио. Используй, если нужно ответить голосом."
		),
		parameters={
			"type": "object",
			"properties": {
				"voice_id": {
					"type": ["string", "null"],
					"description": "Идентификатор ранее загруженного голосового сообщения",
				},
				"audio_url": {
					"type": ["string", "null"],
					"description": "URL аудиофайла, доступного для скачивания",
				},
				"transcription": {
					"type": ["string", "null"],
					"description": "Краткая расшифровка или содержание голосового ответа",
				},
			},
			"required": ["voice_id", "audio_url", "transcription"],
			"additionalProperties": False,
		},
		handler=_send_voice_message_handler,
	)
)

register_tool(
	Tool(
		name="messaging_send_reaction",
		description=(
			"Ставит или снимает реакцию на последнее сообщение пользователя, если это поддерживает канал."
		),
		parameters={
			"type": "object",
			"properties": {
				"reaction": {
					"type": "string",
					"description": "Код реакции/эмодзи в формате канала",
					"minLength": 1,
				},
				"remove": {
					"type": ["boolean", "null"],
					"description": "Снять реакцию вместо установки",
					"default": False,
				},
			},
			"required": ["reaction", "remove"],
			"additionalProperties": False,
		},
		handler=_send_reaction_handler,
	)
)
'''

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
