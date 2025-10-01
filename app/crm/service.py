from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx
from loguru import logger
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.models import Channel, CrmBinding, IncomingMessage
from app.core.session import SessionContext
from app.core.storage import Storage


class AmoCRMError(RuntimeError):
	"""Ошибка при запросе к AmoCRM."""


@dataclass
class Question:
	id: int
	name: str
	type: str
	enums: List[Dict[str, Any]]


@dataclass
class Stage:
	index: int
	name: str
	status_id: Optional[int]
	questions: List[Question]


@dataclass
class QuestionContext:
	id: int
	name: str
	type: str
	answer: str
	enum_options: List[Dict[str, Any]]


@dataclass
class LeadStageContext:
	lead_present: bool
	lead_name: Optional[str]
	current_stage: Optional[Dict[str, Any]]
	next_stage: Optional[Dict[str, Any]]
	questions: List[QuestionContext]


class AmoCRMService:
	def __init__(
		self,
		*,
		client: httpx.AsyncClient,
		storage: Storage,
		stage_ids: List[int],
		stages: List[Stage],
		contact_fields: Dict[str, Dict[str, Any]],
	) -> None:
		self._client = client
		self._storage = storage
		self._stage_ids = stage_ids
		self._stages = stages
		self._questions = [q for stage in stages for q in stage.questions]
		self._questions_by_id: Dict[int, Question] = {q.id: q for q in self._questions}
		self._stages_by_status: Dict[int, Stage] = {stage.status_id: stage for stage in stages if stage.status_id is not None}
		self._question_stage_index: Dict[int, int] = {
			question.id: stage.index for stage in stages for question in stage.questions
		}
		self._contact_fields: Dict[str, Dict[str, Any]] = contact_fields
		self._binding_locks: Dict[str, asyncio.Lock] = {}

	@property
	def stage_ids(self) -> List[int]:
		return list(self._stage_ids)

	@property
	def stages(self) -> List[Stage]:
		return [Stage(index=stage.index, name=stage.name, status_id=stage.status_id, questions=list(stage.questions)) for stage in self._stages]

	@property
	def questions(self) -> List[Question]:
		return list(self._questions)

	@classmethod
	async def create(cls, *, base_url: str, access_token: str, storage: Storage) -> "AmoCRMService":
		base = base_url.strip()
		if not re.match(r"^https?://", base, re.IGNORECASE):
			base = f"https://{base}"
		parsed = httpx.URL(base)
		if parsed.host is None:
			raise ValueError("AMOCRM_BASE_URL должен содержать корректный хост")
		normalized = f"{parsed.scheme}://{parsed.host}"
		if parsed.port:
			normalized = f"{normalized}:{parsed.port}"

		client = httpx.AsyncClient(
			base_url=normalized,
			timeout=httpx.Timeout(30.0),
			headers={
				"Authorization": f"Bearer {access_token}",
				"Content-Type": "application/json",
				"Accept": "application/json",
			},
		)

		base_dir = Path(__file__).resolve().parent
		questions_path = base_dir / "funnel" / "questions.json"
		stages_path = base_dir / "funnel" / "stages.json"
		contact_fields_path = base_dir / "contact_field_map.json"

		stage_ids: List[int] = []
		stage_name_overrides: Dict[int, str] = {}
		if stages_path.exists():
			try:
				raw_stages = json.loads(stages_path.read_text(encoding="utf-8"))
				if isinstance(raw_stages, list):
					for entry in raw_stages:
						try:
							if isinstance(entry, dict):
								raw_status_id = entry.get("id") or entry.get("status_id")
								if raw_status_id is None:
									continue
								status_id = int(raw_status_id)
								stage_ids.append(status_id)
								name_value = entry.get("name")
								if name_value:
									stage_name_overrides[status_id] = str(name_value)
							else:
								stage_ids.append(int(entry))
						except Exception:
							logger.warning("Не удалось разобрать этап в stages.json: {}", entry)
				else:
					logger.warning("stages.json должен содержать массив этапов")
			except Exception as e:
				logger.error("Не удалось загрузить stages.json: {}", e)
		else:
			logger.warning("Файл stages.json не найден: {}", stages_path)

		stages_data: List[Stage] = []
		if questions_path.exists():
			try:
				raw = json.loads(questions_path.read_text(encoding="utf-8"))
				if isinstance(raw, list) and raw:
					for idx, stage_block in enumerate(raw):
						stage_name = str(stage_block.get("name") or f"Этап {idx + 1}")
						items = stage_block.get("questions") or []
						stage_questions: List[Question] = []
						for item in items:
							try:
								stage_questions.append(
									Question(
										id=int(item.get("id")),
										name=str(item.get("name") or ""),
										type=str(item.get("type") or "text"),
										enums=list(item.get("enums") or []),
									),
								)
							except Exception:
								logger.warning("Не удалось разобрать вопрос в questions.json: {}", item)
						status_id = stage_ids[idx] if idx < len(stage_ids) else None
						stages_data.append(
							Stage(
								index=idx,
								name=stage_name,
								status_id=status_id,
								questions=stage_questions,
							),
						)
				else:
					logger.warning("questions.json должен содержать массив этапов")
			except Exception as e:
				logger.error("Не удалось загрузить questions.json: {}", e)
		else:
			logger.warning("Файл questions.json не найден: {}", questions_path)

		if stage_ids:
			initial_stage_count = len(stages_data)
			if len(stage_ids) > initial_stage_count:
				for idx in range(initial_stage_count, len(stage_ids)):
					status_id = stage_ids[idx]
					default_name = stage_name_overrides.get(status_id) or f"Этап {idx + 1}"
					stages_data.append(
						Stage(
							index=idx,
							name=default_name,
							status_id=status_id,
							questions=[],
						),
					)
			elif len(stage_ids) < initial_stage_count:
				logger.warning(
					"Количество этапов в stages.json ({}) меньше, чем количество этапов в questions.json ({}).",
					len(stage_ids),
					initial_stage_count,
				)

		contact_fields: Dict[str, Dict[str, Any]] = {}
		if contact_fields_path.exists():
			try:
				data = json.loads(contact_fields_path.read_text(encoding="utf-8"))
				if isinstance(data, dict):
					for key, value in data.items():
						if isinstance(value, dict) and value.get("field_id") is not None:
							contact_fields[key] = value
				else:
					logger.warning("contact_field_map.json должен содержать объект с описанием полей")
			except Exception as e:
				logger.error("Не удалось загрузить contact_field_map.json: {}", e)
		else:
			logger.warning("Файл contact_field_map.json не найден: {}", contact_fields_path)

		required_keys = [
			"phone",
			"email",
			"telegram_id",
			"telegram_username",
			"telegram_login",
			"profile_link",
			"whatsapp_group",
		]
		missing_keys = [key for key in required_keys if key not in contact_fields]
		if missing_keys:
			logger.warning("В карте полей AmoCRM отсутствуют ключи: {}", ", ".join(missing_keys))

		return cls(
			client=client,
			storage=storage,
			stage_ids=stage_ids,
			stages=stages_data,
			contact_fields=contact_fields,
		)

	async def close(self) -> None:
		try:
			await self._client.aclose()
		except Exception:
			pass

	def _get_binding_lock(self, global_user_id: str) -> asyncio.Lock:
		lock = self._binding_locks.get(global_user_id)
		if lock is None:
			lock = asyncio.Lock()
			self._binding_locks[global_user_id] = lock
		return lock

	async def ensure_contact_and_lead(
		self,
		*,
		session: SessionContext,
		message: IncomingMessage,
	) -> Optional[CrmBinding]:
		lock = self._get_binding_lock(session.global_user_id)
		async with lock:
			binding = await self._storage.get_crm_binding(session.global_user_id)
			contact_id = binding.contact_id if binding and binding.contact_id else None
			lead_id = binding.lead_id if binding and binding.lead_id else None
			lead_status_id = binding.lead_status_id if binding and binding.lead_status_id else None

			if contact_id is None:
				try:
					contact_id = await self._create_contact(session=session, message=message)
				except Exception as e:
					logger.exception("Не удалось создать контакт в AmoCRM: {}", e)
					return binding
			if lead_id is None and contact_id is not None:
				try:
					lead_id, lead_status_id = await self._create_lead(contact_id=contact_id, session=session)
				except Exception as e:
					logger.exception("Не удалось создать сделку в AmoCRM: {}", e)
					return binding

			await self._storage.set_crm_binding(
				session.global_user_id,
				contact_id=contact_id,
				lead_id=lead_id,
				lead_status_id=lead_status_id,
			)
			return await self._storage.get_crm_binding(session.global_user_id)

	async def update_lead_fields(
		self,
		*,
		global_user_id: str,
		answers: Sequence[Dict[str, Any]],
	) -> str:
		binding = await self._storage.get_crm_binding(global_user_id)
		if not binding or not binding.lead_id:
			return "lead_not_found"

		current_stage_idx = self._current_stage_index(binding)

		skipped_future_questions = False
		fields_payload: List[Dict[str, Any]] = []
		for item in answers:
			question_id_raw = item.get("question_id")
			values = item.get("values") or []
			try:
				question_id = int(question_id_raw)
			except (TypeError, ValueError):
				continue
			question = self._questions_by_id.get(question_id)
			if not question:
				continue
			question_stage_idx = self._question_stage_index.get(question_id)
			if question_stage_idx is not None and question_stage_idx > current_stage_idx:
				skipped_future_questions = True
				logger.info(
					"Пропускаем вопрос {}: этап {} ещё не достигнут (текущий этап {})",
					question_id,
					question_stage_idx,
					current_stage_idx,
				)
				continue
			cf_values = self._build_custom_field_values(question, values)
			if cf_values:
				fields_payload.append({"field_id": question.id, "values": cf_values})

		if not fields_payload:
			if skipped_future_questions:
				return "stage_not_reached"
			return "no_fields"

		await self._request(
			"PATCH",
			f"/api/v4/leads/{binding.lead_id}",
			json={"custom_fields_values": fields_payload},
		)
		return "ok"

	async def change_lead_stage(self, *, global_user_id: str, stage_id: int) -> str:
		binding = await self._storage.get_crm_binding(global_user_id)
		if not binding or not binding.lead_id:
			return "lead_not_found"

		try:
			target_stage_id = int(stage_id)
		except (TypeError, ValueError):
			return "invalid_stage"

		target_index = self._stage_index_from_status(target_stage_id)
		if target_index is None:
			return "invalid_stage"

		current_index = self._stage_index_from_status(binding.lead_status_id)
		if current_index is None:
			current_index = -1

		if target_index > current_index + 1:
			return "stage_out_of_order"
		if current_index >= 0 and target_index < current_index:
			return "stage_regression_not_allowed"

		await self._request(
			"PATCH",
			f"/api/v4/leads/{binding.lead_id}",
			json={"status_id": target_stage_id},
		)
		await self._storage.set_crm_binding(global_user_id, lead_status_id=target_stage_id)
		return "ok"

	async def get_lead_context(self, global_user_id: str) -> LeadStageContext:
		if not self._stages:
			return LeadStageContext(
				lead_present=False,
				lead_name=None,
				current_stage=None,
				next_stage=None,
				questions=[],
			)

		binding = await self._storage.get_crm_binding(global_user_id)
		stage_index = self._current_stage_index(binding)
		current_stage_obj = self._stages[stage_index]
		next_stage_obj = self._stages[stage_index + 1] if stage_index + 1 < len(self._stages) else None

		lead_present = bool(binding and binding.lead_id)
		lead_name: Optional[str] = None
		answers_map: Dict[int, str] = {}
		if lead_present and binding and binding.lead_id:
			lead_data = await self._fetch_lead(binding.lead_id)
			lead_raw_name = lead_data.get("name") if isinstance(lead_data, dict) else None
			lead_name = str(lead_raw_name).strip() if lead_raw_name else None
			answers_map = self._extract_custom_field_values(lead_data)

		questions: List[QuestionContext] = []
		for question in current_stage_obj.questions:
			answer = answers_map.get(question.id) or "—"
			enum_options: List[Dict[str, Any]] = []
			if question.type in {"select", "multiselect"} and question.enums:
				for option in question.enums:
					enum_id = option.get("id")
					value = option.get("value")
					try:
						enum_id_int = int(enum_id)
					except (TypeError, ValueError):
						continue
					enum_options.append({
						"id": enum_id_int,
						"value": str(value) if value not in (None, "") else "—",
					})
			questions.append(
				QuestionContext(
					id=question.id,
					name=question.name,
					type=question.type,
					answer=answer,
					enum_options=enum_options,
				)
			)

		current_stage = {
			"index": current_stage_obj.index,
			"name": current_stage_obj.name,
			"status_id": current_stage_obj.status_id,
		}
		next_stage = (
			{
				"index": next_stage_obj.index,
				"name": next_stage_obj.name,
				"status_id": next_stage_obj.status_id,
			}
			if next_stage_obj
			else None
		)

		return LeadStageContext(
			lead_present=lead_present,
			lead_name=lead_name,
			current_stage=current_stage,
			next_stage=next_stage,
			questions=questions,
		)

	async def build_stage_snapshot(self, global_user_id: str) -> str:
		if not self._stages:
			return "Этапы воронки не настроены"

		context = await self.get_lead_context(global_user_id)
		if context.current_stage is None:
			return "Этапы воронки не настроены"

		stage_name = context.current_stage.get("name", "—")
		lines: List[str] = [f"Этап '{stage_name}' (AmoCRM):"]
		if not context.lead_present:
			lines.append("- сделка ещё не создана")
			for q in context.questions:
				lines.append(f"• {q.name}: —")
			return "\n".join(lines)

		for q in context.questions:
			lines.append(f"• {q.name}: {q.answer if q.answer else '—'}")

		next_stage = context.next_stage
		if next_stage:
			next_status = f" (status_id={next_stage.get('status_id')})" if next_stage.get("status_id") is not None else ""
			lines.append(f"Следующий этап: '{next_stage.get('name')}'{next_status}")

		return "\n".join(lines)

	def _stage_index_from_status(self, status_id: Optional[int]) -> Optional[int]:
		if status_id is None:
			return None
		stage = self._stages_by_status.get(int(status_id))
		if stage is not None:
			return stage.index
		if self._stage_ids:
			try:
				return self._stage_ids.index(int(status_id))
			except ValueError:
				return None
		return None

	def _current_stage_index(self, binding: Optional[CrmBinding]) -> int:
		if not self._stages:
			return 0
		if binding is None or binding.lead_status_id is None:
			return 0
		idx = self._stage_index_from_status(binding.lead_status_id)
		if idx is None:
			return 0
		return min(max(idx, 0), len(self._stages) - 1)

	async def _create_contact(self, *, session: SessionContext, message: IncomingMessage) -> Optional[int]:
		payload = self._build_contact_payload(session=session, message=message)
		if payload is None:
			return None
		response = await self._request("POST", "/api/v4/contacts", json=[payload])
		data = response.json()
		contacts = data.get("_embedded", {}).get("contacts") or []
		if not contacts:
			return None
		return int(contacts[0].get("id"))

	async def _create_lead(self, *, contact_id: int, session: SessionContext) -> tuple[int, Optional[int]]:
		status_id = self._stage_ids[0] if self._stage_ids else None
		payload: Dict[str, Any] = {
			"name": f"Входящее сообщение ИИ-боту из {session.channel.value}",
			"_embedded": {"contacts": [{"id": contact_id, "is_main": True}]},
		}
		if status_id is not None:
			payload["status_id"] = status_id
		response = await self._request("POST", "/api/v4/leads", json=[payload])
		data = response.json()
		leads = data.get("_embedded", {}).get("leads") or []
		if not leads:
			return 0, status_id
		lead = leads[0]
		lead_id = int(lead.get("id"))
		actual_status = int(lead.get("status_id")) if lead.get("status_id") is not None else status_id
		return lead_id, actual_status

	def _build_contact_payload(self, *, session: SessionContext, message: IncomingMessage) -> Optional[Dict[str, Any]]:
		name = self._derive_contact_name(message)
		phones: List[str] = []
		emails: List[str] = []
		custom_values: List[Dict[str, Any]] = []

		if message.channel == Channel.telegram:
			raw = message.raw or {}
			user = raw.get("from_user") or raw.get("from") or {}
			username = user.get("username")
			if username:
				custom_values.append(self._make_cf("telegram_login", f"@{username}"))
				custom_values.append(self._make_cf("telegram_username", username))
				custom_values.append(self._make_cf("profile_link", f"https://t.me/{username}"))
			custom_values.append(self._make_cf("telegram_id", str(message.user_id)))
		elif message.channel == Channel.vk:
			custom_values.append(self._make_cf("profile_link", f"https://vk.com/id{message.user_id}"))
		else:
			if message.user_id and message.channel == Channel.whatsapp:
				phones.append(message.user_id)
				custom_values.append(self._make_cf("whatsapp_group", message.chat_id or message.user_id))

		custom_values = [cf for cf in custom_values if cf]
		payload: Dict[str, Any] = {"name": name or "Без имени"}
		if phones:
			entry = self._make_cf("phone", phones[0])
			if entry:
				payload.setdefault("custom_fields_values", []).append(entry)
		if emails:
			entry = self._make_cf("email", emails[0])
			if entry:
				payload.setdefault("custom_fields_values", []).append(entry)
		for cf in custom_values:
			if cf.get("field_id"):
				payload.setdefault("custom_fields_values", []).append(cf)
		return payload

	def _make_cf(self, key: str, value: str | None) -> Optional[Dict[str, Any]]:
		if not value:
			return None
		config = self._contact_fields.get(key)
		if not config:
			return None
		field_id = config.get("field_id")
		if field_id is None:
			return None
		entry: Dict[str, Any] = {"field_id": int(field_id), "values": [{"value": value}]}
		enum_id = config.get("enum_id")
		if enum_id is not None:
			try:
				entry["values"][0]["enum_id"] = int(enum_id)
			except (TypeError, ValueError):
				logger.warning("Некорректный enum_id для поля {}: {}", key, enum_id)
		return entry

	def _derive_contact_name(self, message: IncomingMessage) -> str:
		if message.channel == Channel.telegram:
			raw = message.raw or {}
			user = raw.get("from_user") or raw.get("from") or {}
			first_name = user.get("first_name")
			last_name = user.get("last_name")
			username = user.get("username")
			parts = [p for p in [first_name, last_name] if p]
			if parts:
				return " ".join(parts)
			if username:
				return f"@{username}"
			return f"Telegram пользователь {message.user_id}"
		if message.channel == Channel.vk:
			return f"VK пользователь {message.user_id}"
		if message.channel == Channel.whatsapp:
			return f"WhatsApp контакт {message.user_id}"
		return f"Клиент {message.user_id}"

	async def _fetch_lead(self, lead_id: int) -> Dict[str, Any]:
		response = await self._request("GET", f"/api/v4/leads/{lead_id}")
		return response.json()

	def _extract_custom_field_values(self, lead_payload: Dict[str, Any]) -> Dict[int, str]:
		result: Dict[int, str] = {}
		cf_values = lead_payload.get("custom_fields_values") or []
		for item in cf_values:
			field_id = item.get("field_id")
			if field_id is None:
				continue
			question = self._questions_by_id.get(int(field_id))
			if not question:
				continue
			values = item.get("values") or []
			result[int(field_id)] = self._render_answer(question, values)
		return result

	def _render_answer(self, question: Question, values: Iterable[Dict[str, Any]]) -> str:
		if question.type in {"text", "textarea", "numeric", "url"}:
			parts = [str(v.get("value")) for v in values if v.get("value") not in (None, "")]
			return ", ".join(parts)
		if question.type in {"select", "multiselect"}:
			lookup: Dict[int, str] = {int(e.get("id")): str(e.get("value")) for e in question.enums if e.get("id") is not None}
			labels: List[str] = []
			for item in values:
				enum_id = item.get("enum_id")
				if enum_id is not None and int(enum_id) in lookup:
					labels.append(lookup[int(enum_id)])
				elif item.get("value"):
					labels.append(str(item.get("value")))
			return ", ".join(labels)
		parts = [str(item.get("value")) for item in values if item.get("value")]
		return ", ".join(parts)

	def _build_custom_field_values(self, question: Question, raw_values: Sequence[Any]) -> List[Dict[str, Any]]:
		if question.type in {"text", "textarea", "numeric", "url"}:
			parts = [str(v) for v in raw_values if str(v).strip()]
			if not parts:
				return []
			return [{"value": "\n".join(parts)}]
		if question.type == "select":
			fallback_value: Optional[str] = None
			for value in raw_values:
				match = self._resolve_enum_id(question, value)
				if match is not None:
					return [{"enum_id": match}]
				if fallback_value is None:
					fallback_value = self._normalize_free_value(value)
			if fallback_value:
				return [{"value": fallback_value}]
			return []
		if question.type == "multiselect":
			enum_values: List[Dict[str, Any]] = []
			free_values: List[Dict[str, Any]] = []
			seen_ids: set[int] = set()
			seen_texts: set[str] = set()
			for value in raw_values:
				match = self._resolve_enum_id(question, value)
				if match is not None and match not in seen_ids:
					seen_ids.add(match)
					enum_values.append({"enum_id": match})
					continue
				normalized = self._normalize_free_value(value)
				if normalized and normalized not in seen_texts:
					seen_texts.add(normalized)
					free_values.append({"value": normalized})
			return enum_values + free_values
		parts = [str(v) for v in raw_values if str(v).strip()]
		if not parts:
			return []
		return [{"value": "\n".join(parts)}]

	def _resolve_enum_id(self, question: Question, raw_value: Any) -> Optional[int]:
		if raw_value is None:
			return None
		# Dict payloads may contain enum identifiers explicitly
		if isinstance(raw_value, dict):
			for key in ("enum_id", "id", "value_id"):
				candidate = raw_value.get(key)
				if candidate is not None:
					match = self._resolve_enum_id(question, candidate)
					if match is not None:
						return match
			# Fallback to the textual value inside словаря
			raw_value = raw_value.get("value")
			if raw_value is None:
				return None
		if isinstance(raw_value, (list, tuple, set)):
			for item in raw_value:
				match = self._resolve_enum_id(question, item)
				if match is not None:
					return match
			return None
		# Handle numeric identifiers (int or numeric strings)
		candidate_id: Optional[int] = None
		if isinstance(raw_value, (int,)):
			candidate_id = int(raw_value)
		elif isinstance(raw_value, float) and raw_value.is_integer():
			candidate_id = int(raw_value)
		else:
			text = str(raw_value).strip()
			if not text:
				return None
			if text.isdigit():
				candidate_id = int(text)
			else:
				text_lower = text.lower()
				for enum in question.enums:
					enum_value = enum.get("value")
					if enum_value is not None and str(enum_value).strip().lower() == text_lower:
						try:
							return int(enum.get("id"))
						except (TypeError, ValueError):
							continue
		if candidate_id is None:
			return None
		for enum in question.enums:
			try:
				enum_id = int(enum.get("id"))
			except (TypeError, ValueError):
				continue
			if enum_id == candidate_id:
				return enum_id
		return None

	def _normalize_free_value(self, raw_value: Any) -> Optional[str]:
		if raw_value is None:
			return None
		if isinstance(raw_value, dict):
			for key in ("value", "text", "label"):
				nested = raw_value.get(key)
				if nested is not None:
					normalized = self._normalize_free_value(nested)
					if normalized:
						return normalized
			return None
		if isinstance(raw_value, (list, tuple, set)):
			for item in raw_value:
				normalized = self._normalize_free_value(item)
				if normalized:
					return normalized
			return None
		text = str(raw_value).strip()
		return text or None

	async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
		async for attempt in AsyncRetrying(
			retry=retry_if_exception_type((AmoCRMError, httpx.HTTPError)),
			stop=stop_after_attempt(3),
			wait=wait_exponential(multiplier=1, min=1, max=8),
			reraise=True,
		):
			with attempt:
				response = await self._client.request(method, url, **kwargs)
				if response.status_code >= 400:
					raise AmoCRMError(f"AmoCRM error {response.status_code}: {response.text}")
				return response
