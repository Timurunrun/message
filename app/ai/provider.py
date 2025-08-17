from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


@dataclass
class AIMessage:
    role: Role
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None


@dataclass
class AIResult:
    text: str
    provider_message_id: Optional[str] = None


class AIAssistant(abc.ABC):
    @abc.abstractmethod
    async def generate(self, *, messages: List[AIMessage]) -> AIResult:
        """Сгенерировать ответ ассистента. Возвращает финальный текст и id провайдера."""
        ...