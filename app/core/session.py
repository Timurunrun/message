from __future__ import annotations

from dataclasses import dataclass
import contextvars
from typing import Optional

from .models import Channel


@dataclass
class SessionContext:
	global_user_id: str
	channel: Channel
	chat_id: str
	user_id: str
	reply_to_message_id: Optional[str] = None


_current_session: contextvars.ContextVar[SessionContext | None] = contextvars.ContextVar(
	"amocrm_session_context", default=None
)


def set_current_session(ctx: SessionContext | None) -> contextvars.Token:
	return _current_session.set(ctx)


def get_current_session() -> SessionContext | None:
	return _current_session.get()


def reset_current_session(token: contextvars.Token) -> None:
	_current_session.reset(token)
