from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional
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
class IncomingMessage:
    channel: Channel
    chat_id: str
    user_id: str
    text: str
    timestamp: datetime
    raw: Any | None = None


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
