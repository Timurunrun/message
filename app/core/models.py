from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Awaitable, Callable
from datetime import datetime


class Channel(str, Enum):
    telegram = "telegram"
    vk = "vk"
    whatsapp = "whatsapp"
    avito = "avito"
    stub = "stub"


class Direction(str, Enum):
    inbound = "inbound"
    outbound = "outbound"


@dataclass
class VoiceAttachment:
    download: Callable[[], Awaitable[bytes]]
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    duration_seconds: Optional[float] = None
    file_size: Optional[int] = None


@dataclass
class IncomingMessage:
    channel: Channel
    chat_id: str
    user_id: str
    text: str
    timestamp: datetime
    message_id: Optional[str] = None
    raw: Any | None = None
    voice: Optional[VoiceAttachment] = None


@dataclass
class MessageRecord:
    global_user_id: str
    channel: Channel
    chat_id: str
    user_id: str
    direction: Direction
    text: str
    timestamp: datetime
    correlation_id: Optional[str] = None


# Запись использования инструмента моделью

@dataclass
class ToolInvocation:
    global_user_id: str
    channel: Channel
    chat_id: str
    user_id: str
    tool_name: str
    arguments: str
    output: str
    timestamp: datetime
    call_id: Optional[str] = None


@dataclass
class CrmBinding:
	global_user_id: str
	contact_id: Optional[int]
	lead_id: Optional[int]
	lead_status_id: Optional[int]
	created_at: datetime
	updated_at: datetime


@dataclass
class AIResponseRecord:
    global_user_id: str
    channel: Channel
    chat_id: str
    user_id: str
    text: str
    timestamp: datetime
    provider_message_id: Optional[str] = None
