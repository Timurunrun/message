from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from datetime import datetime
from typing import List

import aiosqlite
from loguru import logger

from .models import MessageRecord


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
            """
        )
        await self.db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def upsert_contact(self, channel: str, platform_user_id: str, platform_chat_id: str) -> str:
        now = int(datetime.utcnow().timestamp())
        async with self._lock:
            async with self.db.execute(
                "SELECT global_user_id FROM contacts WHERE channel=? AND platform_user_id=?",
                (channel, platform_user_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                return row[0]
            global_user_id = str(uuid.uuid4())
            await self.db.execute(
                "INSERT OR REPLACE INTO contacts(channel, platform_user_id, platform_chat_id, global_user_id, created_at) VALUES (?,?,?,?,?)",
                (channel, platform_user_id, platform_chat_id, global_user_id, now),
            )
            await self.db.commit()
            logger.debug(
                f"Создана новая связь контакта: channel={channel} platform_user_id={platform_user_id} -> global_user_id={global_user_id}"
            )
            return global_user_id

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
                        timestamp=datetime.utcfromtimestamp(ts),
                        correlation_id=corr,
                    )
                )
        return result
