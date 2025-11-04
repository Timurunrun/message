from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import aiosqlite
from loguru import logger

from .models import MessageRecord, ToolInvocation, CrmBinding, AIResponseRecord


class Storage:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Storage is not initialized"
        return self._db

    async def initialize(self) -> None:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self.db.execute("PRAGMA journal_mode=WAL;")
        await self.db.execute("PRAGMA synchronous=NORMAL;")
        await self._create_schema()
        logger.info(f"База данных инициализирована по пути {self._db_path}")

    async def _create_schema(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                channel TEXT NOT NULL,
                platform_user_id TEXT NOT NULL,
                platform_chat_id TEXT NOT NULL,
                global_user_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(channel, platform_user_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                global_user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                platform_chat_id TEXT NOT NULL,
                platform_user_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                text TEXT NOT NULL,
                ts INTEGER NOT NULL,
                correlation_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_messages_user_ts ON messages(global_user_id, ts DESC);

            CREATE TABLE IF NOT EXISTS tool_invocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                global_user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                platform_chat_id TEXT NOT NULL,
                platform_user_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL,
                output TEXT NOT NULL,
                ts INTEGER NOT NULL,
                call_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_tool_invocations_user_ts ON tool_invocations(global_user_id, ts DESC);

            CREATE TABLE IF NOT EXISTS ai_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                global_user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                platform_chat_id TEXT NOT NULL,
                platform_user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                provider_message_id TEXT,
                ts INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ai_responses_user_ts ON ai_responses(global_user_id, ts DESC);

            CREATE TABLE IF NOT EXISTS crm_bindings (
                global_user_id TEXT PRIMARY KEY,
                contact_id INTEGER,
                lead_id INTEGER,
                lead_status_id INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        await self.db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def clear_all(self) -> None:
        """Полностью очищает все таблицы пользовательских данных."""
        if self._db is None:
            raise RuntimeError("Storage is not initialized")
        async with self._lock:
            await self.db.executescript(
                """
                DELETE FROM messages;
                DELETE FROM ai_responses;
                DELETE FROM tool_invocations;
                DELETE FROM crm_bindings;
                DELETE FROM contacts;
                """
            )
            await self.db.commit()
        logger.info("База данных очищена по запросу администратора")

    async def upsert_contact(self, channel: str, platform_user_id: str, platform_chat_id: str) -> tuple[str, bool]:
        now = int(datetime.now(timezone.utc).timestamp())
        async with self._lock:
            async with self.db.execute(
                "SELECT global_user_id FROM contacts WHERE channel=? AND platform_user_id=?",
                (channel, platform_user_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                return row[0], False
            global_user_id = str(uuid.uuid4())
            await self.db.execute(
                "INSERT OR REPLACE INTO contacts(channel, platform_user_id, platform_chat_id, global_user_id, created_at) VALUES (?,?,?,?,?)",
                (channel, platform_user_id, platform_chat_id, global_user_id, now),
            )
            await self.db.commit()
            logger.debug(
                f"Создана новая связь контакта: channel={channel} platform_user_id={platform_user_id} -> global_user_id={global_user_id}"
            )
            return global_user_id, True

    async def save_message(self, record: MessageRecord) -> None:
        ts = int(record.timestamp.timestamp())
        await self.db.execute(
            """
            INSERT INTO messages(global_user_id, channel, platform_chat_id, platform_user_id, direction, text, ts, correlation_id)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                record.global_user_id,
                record.channel.value,
                record.chat_id,
                record.user_id,
                record.direction.value,
                record.text,
                ts,
                record.correlation_id,
            ),
        )
        await self.db.commit()

    async def get_recent_messages(self, global_user_id: str, limit: int = 50) -> List[MessageRecord]:
        from .models import Channel, Direction
        result: List[MessageRecord] = []
        async with self.db.execute(
            "SELECT channel, platform_chat_id, platform_user_id, direction, text, ts, correlation_id FROM messages WHERE global_user_id=? ORDER BY ts DESC LIMIT ?",
            (global_user_id, limit),
        ) as cursor:
            async for row in cursor:
                channel, chat_id, user_id, direction, text, ts, corr = row
                result.append(
                    MessageRecord(
                        global_user_id=global_user_id,
                        channel=Channel(channel),
                        chat_id=chat_id,
                        user_id=user_id,
                        direction=Direction(direction),
                        text=text,
                        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                        correlation_id=corr,
                    )
                )
        return result

    async def get_all_messages(self, global_user_id: str) -> List[MessageRecord]:
        """Загрузить все сообщения контакта в хронологическом порядке (от старых к новым)."""
        from .models import Channel, Direction
        result: List[MessageRecord] = []
        async with self.db.execute(
            "SELECT channel, platform_chat_id, platform_user_id, direction, text, ts, correlation_id FROM messages WHERE global_user_id=? ORDER BY ts ASC",
            (global_user_id,),
        ) as cursor:
            async for row in cursor:
                channel, chat_id, user_id, direction, text, ts, corr = row
                result.append(
                    MessageRecord(
                        global_user_id=global_user_id,
                        channel=Channel(channel),
                        chat_id=chat_id,
                        user_id=user_id,
                        direction=Direction(direction),
                        text=text,
                        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                        correlation_id=corr,
                    )
                )
        return result

    async def save_tool_invocation(self, inv: ToolInvocation) -> None:
        ts = int(inv.timestamp.timestamp())
        await self.db.execute(
            """
            INSERT INTO tool_invocations(global_user_id, channel, platform_chat_id, platform_user_id, tool_name, arguments, output, ts, call_id)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                inv.global_user_id,
                inv.channel.value,
                inv.chat_id,
                inv.user_id,
                inv.tool_name,
                inv.arguments,
                inv.output,
                ts,
                inv.call_id,
            ),
        )
        await self.db.commit()

    async def save_ai_response(self, response: AIResponseRecord) -> None:
        ts = int(response.timestamp.timestamp())
        await self.db.execute(
            """
            INSERT INTO ai_responses(global_user_id, channel, platform_chat_id, platform_user_id, text, provider_message_id, ts)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                response.global_user_id,
                response.channel.value,
                response.chat_id,
                response.user_id,
                response.text,
                response.provider_message_id,
                ts,
            ),
        )
        await self.db.commit()

    async def get_tool_invocations(self, global_user_id: str, limit: int = 200) -> List[ToolInvocation]:
        from .models import Channel
        res: List[ToolInvocation] = []
        async with self.db.execute(
            "SELECT channel, platform_chat_id, platform_user_id, tool_name, arguments, output, ts, call_id FROM tool_invocations WHERE global_user_id=? ORDER BY ts ASC LIMIT ?",
            (global_user_id, limit),
        ) as cursor:
            async for row in cursor:
                channel, chat_id, user_id, tool_name, arguments, output, ts, call_id = row
                res.append(
                    ToolInvocation(
                        global_user_id=global_user_id,
                        channel=Channel(channel),
                        chat_id=chat_id,
                        user_id=user_id,
                        tool_name=tool_name,
                        arguments=arguments,
                        output=output,
                        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                        call_id=call_id,
                    )
                )
        return res

    async def get_crm_binding(self, global_user_id: str) -> CrmBinding | None:
        async with self.db.execute(
            "SELECT contact_id, lead_id, lead_status_id, created_at, updated_at FROM crm_bindings WHERE global_user_id=?",
            (global_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        contact_id, lead_id, lead_status_id, created_at, updated_at = row
        return CrmBinding(
            global_user_id=global_user_id,
            contact_id=int(contact_id) if contact_id is not None else None,
            lead_id=int(lead_id) if lead_id is not None else None,
            lead_status_id=int(lead_status_id) if lead_status_id is not None else None,
            created_at=datetime.fromtimestamp(int(created_at), tz=timezone.utc),
            updated_at=datetime.fromtimestamp(int(updated_at), tz=timezone.utc),
        )

    async def set_crm_binding(
        self,
        global_user_id: str,
        *,
        contact_id: int | None = None,
        lead_id: int | None = None,
        lead_status_id: int | None = None,
    ) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        await self.db.execute(
            """
            INSERT INTO crm_bindings(global_user_id, contact_id, lead_id, lead_status_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(global_user_id) DO UPDATE SET
                contact_id = COALESCE(excluded.contact_id, crm_bindings.contact_id),
                lead_id = COALESCE(excluded.lead_id, crm_bindings.lead_id),
                lead_status_id = COALESCE(excluded.lead_status_id, crm_bindings.lead_status_id),
                updated_at = excluded.updated_at
            """,
            (global_user_id, contact_id, lead_id, lead_status_id, now, now),
        )
        await self.db.commit()
