from __future__ import annotations

import os
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    create_channel_id INTEGER NOT NULL,
    category_id INTEGER,
    name_template TEXT NOT NULL DEFAULT '{user}''s Channel',
    default_user_limit INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS temp_channels (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS github_links (
    webhook_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    repo TEXT NOT NULL COLLATE NOCASE,
    linked_by INTEGER NOT NULL,
    last_activity INTEGER NOT NULL,
    warned_at INTEGER,
    UNIQUE (thread_id, repo)
);
"""


@dataclass
class GuildConfig:
    guild_id: int
    create_channel_id: int
    category_id: int | None
    name_template: str
    default_user_limit: int


@dataclass
class GitHubLink:
    webhook_id: int
    guild_id: int
    thread_id: int
    repo: str
    linked_by: int
    last_activity: int
    warned_at: int | None


_LINK_COLUMNS = "webhook_id, guild_id, thread_id, repo, linked_by, last_activity, warned_at"


class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    async def set_guild_config(
        self,
        guild_id: int,
        create_channel_id: int,
        category_id: int | None,
        name_template: str = "{user}'s Channel",
        default_user_limit: int = 0,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO guild_config (guild_id, create_channel_id, category_id, name_template, default_user_limit)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                create_channel_id=excluded.create_channel_id,
                category_id=excluded.category_id,
                name_template=excluded.name_template,
                default_user_limit=excluded.default_user_limit
            """,
            (guild_id, create_channel_id, category_id, name_template, default_user_limit),
        )
        await self.conn.commit()

    async def get_guild_config(self, guild_id: int) -> GuildConfig | None:
        async with self.conn.execute(
            "SELECT guild_id, create_channel_id, category_id, name_template, default_user_limit "
            "FROM guild_config WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return GuildConfig(*row)

    async def delete_guild_config(self, guild_id: int) -> None:
        await self.conn.execute("DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))
        await self.conn.commit()

    async def delete_all_guild_data(self, guild_id: int) -> None:
        await self.conn.execute("DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))
        await self.conn.execute("DELETE FROM temp_channels WHERE guild_id = ?", (guild_id,))
        await self.conn.execute("DELETE FROM github_links WHERE guild_id = ?", (guild_id,))
        await self.conn.commit()

    async def add_temp_channel(self, channel_id: int, guild_id: int, owner_id: int) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO temp_channels (channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
            (channel_id, guild_id, owner_id),
        )
        await self.conn.commit()

    async def remove_temp_channel(self, channel_id: int) -> None:
        await self.conn.execute("DELETE FROM temp_channels WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()

    async def get_temp_channel_owner(self, channel_id: int) -> int | None:
        async with self.conn.execute(
            "SELECT owner_id FROM temp_channels WHERE channel_id = ?", (channel_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_temp_channel_owner(self, channel_id: int, owner_id: int) -> None:
        await self.conn.execute(
            "UPDATE temp_channels SET owner_id = ? WHERE channel_id = ?", (owner_id, channel_id)
        )
        await self.conn.commit()

    async def is_temp_channel(self, channel_id: int) -> bool:
        return await self.get_temp_channel_owner(channel_id) is not None

    async def all_temp_channels(self, guild_id: int) -> list[tuple[int, int]]:
        async with self.conn.execute(
            "SELECT channel_id, owner_id FROM temp_channels WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            return await cursor.fetchall()

    # ---------- GitHub links ----------

    async def add_github_link(
        self, webhook_id: int, guild_id: int, thread_id: int, repo: str, linked_by: int, now: int
    ) -> None:
        await self.conn.execute(
            f"INSERT INTO github_links ({_LINK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (webhook_id, guild_id, thread_id, repo, linked_by, now),
        )
        await self.conn.commit()

    async def remove_github_link(self, webhook_id: int) -> None:
        await self.conn.execute("DELETE FROM github_links WHERE webhook_id = ?", (webhook_id,))
        await self.conn.commit()

    async def _github_links(self, where: str, params: tuple) -> list[GitHubLink]:
        async with self.conn.execute(
            f"SELECT {_LINK_COLUMNS} FROM github_links {where} ORDER BY repo", params
        ) as cursor:
            return [GitHubLink(*row) for row in await cursor.fetchall()]

    async def get_github_link(self, thread_id: int, repo: str) -> GitHubLink | None:
        links = await self._github_links("WHERE thread_id = ? AND repo = ?", (thread_id, repo))
        return links[0] if links else None

    async def github_links_for_thread(self, thread_id: int) -> list[GitHubLink]:
        return await self._github_links("WHERE thread_id = ?", (thread_id,))

    async def github_links_for_guild(self, guild_id: int) -> list[GitHubLink]:
        return await self._github_links("WHERE guild_id = ?", (guild_id,))

    async def all_github_links(self) -> list[GitHubLink]:
        return await self._github_links("", ())

    async def touch_github_link(self, webhook_id: int, now: int) -> bool:
        """Record activity on a link and clear any inactivity warning. Returns False if untracked."""
        cursor = await self.conn.execute(
            "UPDATE github_links SET last_activity = ?, warned_at = NULL WHERE webhook_id = ?",
            (now, webhook_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def touch_github_links_for_thread(self, thread_id: int, now: int) -> None:
        await self.conn.execute(
            "UPDATE github_links SET last_activity = ?, warned_at = NULL WHERE thread_id = ?",
            (now, thread_id),
        )
        await self.conn.commit()

    async def set_github_link_warned(self, webhook_id: int, now: int) -> None:
        await self.conn.execute(
            "UPDATE github_links SET warned_at = ? WHERE webhook_id = ?", (now, webhook_id)
        )
        await self.conn.commit()
