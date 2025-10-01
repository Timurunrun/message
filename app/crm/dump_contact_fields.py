from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv
from loguru import logger


def _normalize_base_url(raw: str) -> str:
    base = raw.strip()
    if not re.match(r"^https?://", base, re.IGNORECASE):
        base = f"https://{base}"
    parsed = httpx.URL(base)
    if parsed.host is None:
        raise ValueError("AMOCRM_BASE_URL должен содержать корректный хост")
    normalized = f"{parsed.scheme}://{parsed.host}"
    if parsed.port:
        normalized = f"{normalized}:{parsed.port}"
    return normalized


async def _fetch_all_fields(base_url: str, token: str) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    fields: list[dict] = []
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=httpx.Timeout(30.0)) as client:
        page = 1
        while True:
            params = {"limit": 50, "page": page}
            response = await client.get("/api/v4/contacts/custom_fields", params=params)
            if response.status_code == 204 or not response.content:
                break
            try:
                payload = response.json()
            except ValueError:
                logger.warning("Не удалось распарсить ответ AmoCRM на странице {}", page)
                break
            chunk = payload.get("_embedded", {}).get("custom_fields") or []
            if not chunk:
                break
            fields.extend(chunk)
            if len(chunk) < 50:
                break
            page += 1
    return fields


async def main() -> None:
    load_dotenv()
    base_url = os.environ.get("AMOCRM_BASE_URL")
    token = os.environ.get("AMOCRM_ACCESS_TOKEN")
    if not base_url or not token:
        raise RuntimeError("AMOCRM_BASE_URL и AMOCRM_ACCESS_TOKEN должны быть заданы в окружении")

    normalized = _normalize_base_url(base_url)
    fields = await _fetch_all_fields(normalized, token)

    target_path = Path(__file__).resolve().parent / "contact_fields.json"
    target_path.write_text(json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Выгружено {} полей в {}", len(fields), target_path)


if __name__ == "__main__":
    asyncio.run(main())
