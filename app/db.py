"""The ledger.

Discord is the transport and the human interface; this is the source of truth for
protocol state. Agents never parse metadata out of message text — they receive it
from here.

Everything is scoped by bus_id. A bus is one activated Discord channel, and it is
the tenancy boundary: a missing WHERE clause here leaks another server's
conversation, so bus_id is not optional on any read path.

Messages arrive by two paths that race: the gateway observes a message, and /say
knows the metadata for messages it sent. Both upsert on discord_id and each writes
only its own columns, so whichever lands second merges rather than clobbers.
"""

import hashlib
import json
import logging
import secrets
import time
from pathlib import Path

import aiosqlite

log = logging.getLogger("switchboard.db")

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A bus is one activated Discord channel. The tenancy boundary.
CREATE TABLE IF NOT EXISTS buses (
    bus_id         TEXT PRIMARY KEY,
    guild_id       TEXT NOT NULL,
    channel_id     TEXT NOT NULL UNIQUE,
    guild_name     TEXT,
    channel_name   TEXT,
    webhook_id     TEXT,
    webhook_url    TEXT,               -- never leaves this process
    secret_hash    TEXT NOT NULL,      -- sha256 of the bootstrap secret
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_by     TEXT,               -- discord user id that ran /enable
    created_at     REAL NOT NULL,
    default_budget INTEGER NOT NULL DEFAULT 20
);

CREATE INDEX IF NOT EXISTS idx_buses_secret  ON buses(secret_hash);
CREATE INDEX IF NOT EXISTS idx_buses_channel ON buses(channel_id);

CREATE TABLE IF NOT EXISTS messages (
    -- Monotonic but NOT contiguous: SQLite consumes the AUTOINCREMENT counter
    -- even when an upsert resolves to an UPDATE, so gaps are normal. Never infer
    -- dropped messages from a gap in seq. AUTOINCREMENT (rather than a bare
    -- rowid) is what guarantees seq is never reused, which makes it safe as a
    -- cursor.
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    bus_id          TEXT,
    discord_id      TEXT NOT NULL UNIQUE,
    channel_id      TEXT NOT NULL,
    thread_id       TEXT,
    author_id       TEXT,
    author_name     TEXT NOT NULL,
    author_kind     TEXT NOT NULL,          -- human | agent | bot
    content         TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    conversation_id TEXT,
    depth           INTEGER,
    budget_left     INTEGER,
    reply_to        TEXT,
    kind            TEXT,
    to_agents       TEXT                    -- JSON array
);

CREATE INDEX IF NOT EXISTS idx_messages_bus          ON messages(bus_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(bus_id, conversation_id);
"""


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_bus_secret() -> str:
    return "sb_boot_" + secrets.token_urlsafe(24)


def _row_to_message(row: aiosqlite.Row) -> dict:
    """Shape a ledger row into the envelope agents actually receive."""
    return {
        "seq": row["seq"],
        "id": row["discord_id"],
        "from": row["author_name"],
        "author_kind": row["author_kind"],
        "to": json.loads(row["to_agents"]) if row["to_agents"] else ["*"],
        "conversation_id": row["conversation_id"],
        "depth": row["depth"],
        "budget_left": row["budget_left"],
        "reply_to": row["reply_to"],
        "kind": row["kind"],
        "text": row["content"],
        "created_at": row["created_at"],
    }


def _row_to_bus(row: aiosqlite.Row) -> dict:
    return {
        "bus_id": row["bus_id"],
        "guild_id": row["guild_id"],
        "channel_id": row["channel_id"],
        "guild_name": row["guild_name"],
        "channel_name": row["channel_name"],
        "webhook_id": row["webhook_id"],
        "webhook_url": row["webhook_url"],
        "enabled": bool(row["enabled"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "default_budget": row["default_budget"],
    }


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # WAL keeps the gateway's writes from blocking API reads.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        await self._conn.commit()
        log.info("ledger open at %s (schema v%d)", self.path, SCHEMA_VERSION)

    async def _migrate(self) -> None:
        """v1 (single-tenant) ledgers predate bus_id. Add it rather than reset."""
        assert self._conn
        async with self._conn.execute("PRAGMA table_info(messages)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        if "bus_id" not in columns:
            log.info("migrating messages table: adding bus_id")
            await self._conn.execute("ALTER TABLE messages ADD COLUMN bus_id TEXT")
            await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ---- buses -----------------------------------------------------------

    async def create_bus(
        self,
        *,
        guild_id: str,
        channel_id: str,
        guild_name: str,
        channel_name: str,
        created_by: str,
        secret: str,
    ) -> dict:
        assert self._conn
        bus_id = "b_" + secrets.token_hex(3)
        await self._conn.execute(
            """
            INSERT INTO buses
                (bus_id, guild_id, channel_id, guild_name, channel_name,
                 secret_hash, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                enabled      = 1,
                secret_hash  = excluded.secret_hash,
                guild_name   = excluded.guild_name,
                channel_name = excluded.channel_name
            """,
            (bus_id, guild_id, channel_id, guild_name, channel_name,
             hash_secret(secret), created_by, time.time()),
        )
        await self._conn.commit()
        bus = await self.bus_for_channel(channel_id)
        assert bus
        return bus

    async def bus_for_channel(self, channel_id: str) -> dict | None:
        assert self._conn
        async with self._conn.execute(
            "SELECT * FROM buses WHERE channel_id = ?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
            return _row_to_bus(row) if row else None

    async def bus_for_secret(self, secret: str) -> dict | None:
        """Resolve a bearer token to exactly one bus. Enabled buses only."""
        assert self._conn
        async with self._conn.execute(
            "SELECT * FROM buses WHERE secret_hash = ? AND enabled = 1",
            (hash_secret(secret),),
        ) as cur:
            row = await cur.fetchone()
            return _row_to_bus(row) if row else None

    async def set_bus_webhook(self, bus_id: str, webhook_id: str, webhook_url: str) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE buses SET webhook_id = ?, webhook_url = ? WHERE bus_id = ?",
            (webhook_id, webhook_url, bus_id),
        )
        await self._conn.commit()

    async def set_bus_enabled(self, bus_id: str, enabled: bool) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE buses SET enabled = ? WHERE bus_id = ?", (1 if enabled else 0, bus_id)
        )
        await self._conn.commit()

    async def rotate_bus_secret(self, bus_id: str, secret: str) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE buses SET secret_hash = ? WHERE bus_id = ?",
            (hash_secret(secret), bus_id),
        )
        await self._conn.commit()

    async def enabled_bus_count(self) -> int:
        assert self._conn
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM buses WHERE enabled = 1"
        ) as cur:
            return (await cur.fetchone())["n"]

    # ---- messages --------------------------------------------------------

    async def record_observed(
        self,
        *,
        bus_id: str,
        discord_id: str,
        channel_id: str,
        thread_id: str | None,
        author_id: str | None,
        author_name: str,
        author_kind: str,
        content: str,
        created_at: float,
    ) -> int:
        """Called by the gateway. Owns the observable columns only."""
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO messages
                (bus_id, discord_id, channel_id, thread_id, author_id, author_name,
                 author_kind, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                bus_id      = excluded.bus_id,
                channel_id  = excluded.channel_id,
                thread_id   = excluded.thread_id,
                author_id   = excluded.author_id,
                author_name = excluded.author_name,
                author_kind = excluded.author_kind,
                -- Never let an empty observation destroy text /say already
                -- stored. Discord delivers content-less messages when the
                -- MESSAGE_CONTENT intent is off, and an unconditional
                -- overwrite would silently blank the ledger while everything
                -- still looked healthy.
                content     = CASE WHEN excluded.content <> ''
                                   THEN excluded.content
                                   ELSE messages.content END,
                created_at  = excluded.created_at
            """,
            (bus_id, discord_id, channel_id, thread_id, author_id,
             author_name, author_kind, content, created_at),
        )
        await self._conn.commit()
        return await self._seq_for(discord_id)

    async def record_sent_metadata(
        self,
        *,
        bus_id: str,
        discord_id: str,
        channel_id: str,
        author_name: str,
        content: str,
        conversation_id: str,
        to_agents: list[str],
        depth: int | None = None,
        budget_left: int | None = None,
        reply_to: str | None = None,
        kind: str | None = None,
    ) -> int:
        """Called by /say. Owns the protocol columns only."""
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO messages
                (bus_id, discord_id, channel_id, author_name, author_kind, content,
                 created_at, conversation_id, to_agents, depth, budget_left,
                 reply_to, kind)
            VALUES (?, ?, ?, ?, 'agent', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                to_agents       = excluded.to_agents,
                depth           = excluded.depth,
                budget_left     = excluded.budget_left,
                reply_to        = excluded.reply_to,
                kind            = excluded.kind
            """,
            (bus_id, discord_id, channel_id, author_name, content, time.time(),
             conversation_id, json.dumps(to_agents), depth, budget_left,
             reply_to, kind),
        )
        await self._conn.commit()
        return await self._seq_for(discord_id)

    async def _seq_for(self, discord_id: str) -> int:
        assert self._conn
        async with self._conn.execute(
            "SELECT seq FROM messages WHERE discord_id = ?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
            return row["seq"] if row else 0

    async def messages_after(
        self,
        bus_id: str,
        after: int = 0,
        limit: int = 50,
        conversation_id: str | None = None,
    ) -> list[dict]:
        """bus_id is positional and required — it is the isolation boundary."""
        assert self._conn
        sql = "SELECT * FROM messages WHERE bus_id = ? AND seq > ?"
        params: list = [bus_id, after]
        if conversation_id:
            sql += " AND conversation_id = ?"
            params.append(conversation_id)
        sql += " ORDER BY seq ASC LIMIT ?"
        params.append(min(limit, 200))

        async with self._conn.execute(sql, params) as cur:
            return [_row_to_message(r) for r in await cur.fetchall()]

    async def bus_stats(self, bus_id: str) -> dict:
        assert self._conn
        async with self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(seq), 0) AS head "
            "FROM messages WHERE bus_id = ?",
            (bus_id,),
        ) as cur:
            row = await cur.fetchone()
            return {"messages_stored": row["n"], "head_seq": row["head"]}

    async def global_stats(self) -> dict:
        assert self._conn
        async with self._conn.execute("SELECT COUNT(*) AS n FROM messages") as cur:
            messages = (await cur.fetchone())["n"]
        return {"messages_stored": messages, "buses_enabled": await self.enabled_bus_count()}
