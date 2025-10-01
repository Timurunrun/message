from __future__ import annotations

from typing import Optional

from .service import AmoCRMService


_service: Optional[AmoCRMService] = None


def set_amocrm_service(service: AmoCRMService | None) -> None:
	global _service
	_service = service


def get_amocrm_service() -> Optional[AmoCRMService]:
	return _service
