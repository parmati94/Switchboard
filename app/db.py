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
from urllib.parse import quote

import aiosqlite

log = logging.getLogger("switchboard.db")

SCHEMA_VERSION = 8

# What agents should call themselves. Separate from voice because the two don't
# always track: a casual room might still want descriptive names, and a working
# one might not.
NAMING_PRESETS = {
    "descriptive": (
        "Pick a name describing your particular role or angle — schema-critic, "
        "perf-analyst, devils-advocate."
    ),
    "human": (
        "Pick a name a person might actually use in a group chat: short, a little "
        "character, not a job title. Marlow, Quill, Pike."
    ),
    "playful": (
        "Pick an absurd, memorable handle — CaptainSpreadsheet, WaffleIron9000, "
        "TheBeanCounter. Silly is the point. Keep it clean."
    ),
    "crude": (
        "Pick a crude, juvenile, off-colour handle — the kind of thing people used "
        "on gaming forums in 2004. FuckFace007, ButtSoup, DongleWizard. Profanity "
        "and innuendo are welcome and the ruder the funnier. "
        "Two limits, and they are about the name only: no slurs, and don't make "
        "your handle a dig at a specific person — it is what you are called, not a "
        "comment on someone. Ribbing people in conversation is a different thing "
        "and is entirely welcome."
    ),
}
DEFAULT_NAMING = "human"

# Style has two independent axes. Length alone was not enough: capping characters
# made agents terse without making them human, so a casual question came back
# answered like a consulting deck. Voice is the axis that fixes that, and it has
# to relax the etiquette rules rather than merely add adjectives — "never
# acknowledge anything" makes conversation impossible.
VOICE_PRESETS = {
    "casual": {
        "guidance": (
            "Talk like a person in a group chat, not an analyst. Contractions, "
            "opinions, humour, blunt disagreement. No headings, no bullet lists, no "
            "hedging, no 'the distinction is structural rather than X'. Say the thing. "
            "Reacting to what someone said with a short agreement or a joke is fine "
            "here — banter is the point, not noise. Rib the others and the humans in "
            "the room, and pile on when it is funny; people here can give it back. "
            "Keep it off slurs and off anyone who is not present to answer."
        ),
        "naming_hint": (
            "Pick a name a person might use in a group chat — short and a bit of "
            "character, not a job title."
        ),
        "relaxes_etiquette": True,
    },
    "neutral": {
        "guidance": (
            "Write like a person talking, not like a report. Plain, direct, no "
            "headings, no bullet lists unless genuinely listing things. Have opinions "
            "and say them. Avoid consultant register and avoid hedging."
        ),
        "naming_hint": "Pick a short descriptive name.",
        "relaxes_etiquette": False,
    },
    "analytical": {
        "guidance": (
            "Precision over warmth. Cite evidence, distinguish claims from opinion, "
            "structure the answer where structure aids the reader."
        ),
        "naming_hint": "Pick a name describing your particular role or angle.",
        "relaxes_etiquette": False,
    },
}
DEFAULT_VOICE = "neutral"

# How long. `guidance` is advisory and shapes shape; `max_chars` is a hard cap
# that catches drift. Guidance alone gets ignored under pressure; a cap alone can
# only truncate, never make writing read better.
STYLE_PRESETS = {
    "terse": {
        "max_chars": 360,
        "guidance": (
            "Reply in one to three sentences. Conversational, like chat. No headings, "
            "no bullet lists, no bold. Make one point and stop."
        ),
    },
    "normal": {
        "max_chars": 1100,
        "guidance": (
            "Reply in a short paragraph or two of prose. Make one point well rather "
            "than several thinly. Avoid headings and long bullet lists."
        ),
    },
    "detailed": {
        "max_chars": 1900,
        "guidance": (
            "Longer structured answers are welcome. Use headings and lists where they "
            "genuinely aid the reader rather than by reflex."
        ),
    },
}
DEFAULT_STYLE = "normal"

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
    default_budget INTEGER NOT NULL DEFAULT 20,
    -- When a conversation ends. Turns bound cost; minutes rescue you from an
    -- agent that is stuck or merely slow. Whichever trips first wins.
    limit_turns    INTEGER NOT NULL DEFAULT 20,
    limit_minutes  INTEGER NOT NULL DEFAULT 10,
    -- Budget for banter: a conversation no human started.
    limit_agent_turns INTEGER NOT NULL DEFAULT 6,
    -- How it reads. Delivered to agents at registration and on every poll, so
    -- the human never has to relay it.
    style_preset   TEXT    NOT NULL DEFAULT 'normal',
    style_voice    TEXT    NOT NULL DEFAULT 'neutral',
    style_naming   TEXT,
    style_max_chars INTEGER,
    style_guidance TEXT
);

-- Open/closed state per exchange. Turn counts are derived from messages; only
-- closure needs storing.
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    bus_id          TEXT NOT NULL,
    started_at      REAL NOT NULL,
    closed_at       REAL,
    closed_reason   TEXT,
    -- 'human' or 'agent'. Banter among agents is welcome — it is most of the
    -- charm — but it gets a smaller budget than a topic a person actually
    -- raised, so a hello cannot run the room dry before the human arrives.
    seeded_by       TEXT NOT NULL DEFAULT 'agent',
    -- Who agents may actually ping in this exchange: the human who started it
    -- plus anyone they @-mentioned. JSON array of {id, name}. Enforced on the
    -- wire via allowed_mentions, so it is not a rule agents can break.
    mentionable     TEXT
);

CREATE INDEX IF NOT EXISTS idx_conversations_bus ON conversations(bus_id);

CREATE INDEX IF NOT EXISTS idx_buses_secret  ON buses(secret_hash);
CREATE INDEX IF NOT EXISTS idx_buses_channel ON buses(channel_id);

-- One row per agent per bus. Names are unique within a bus, never across buses.
CREATE TABLE IF NOT EXISTS agents (
    bus_id      TEXT NOT NULL,
    agent_id    TEXT NOT NULL,        -- the display name, also the address
    key_hash    TEXT NOT NULL,        -- sha256 of the sb_live_ key
    webhook_id  TEXT,
    webhook_url TEXT,                 -- never leaves this process
    avatar_url  TEXT,
    created_at  REAL NOT NULL,
    last_seen   REAL,
    revoked_at  REAL,
    PRIMARY KEY (bus_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_agents_key ON agents(key_hash);

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


def new_agent_key() -> str:
    return "sb_live_" + secrets.token_urlsafe(24)


def default_avatar_url(name: str) -> str:
    """A deterministic face per agent name.

    Discord fetches avatar URLs from its own servers, so Switchboard cannot serve
    these while PUBLIC_URL is a private address — hence a generated-avatar
    service rather than self-hosting. Same name always yields the same face.
    """
    return f"https://api.dicebear.com/9.x/bottts/png?seed={quote(name, safe='')}"


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
        # Carried on every message in the exchange, so an agent joining late
        # still knows who it may ping.
        "mentionable": json.loads(row["mentionable"]) if _has(row, "mentionable") else [],
    }


def _has(row: aiosqlite.Row, key: str) -> bool:
    try:
        return row[key] is not None
    except (IndexError, KeyError):
        return False


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
        "limit_turns": row["limit_turns"],
        "limit_minutes": row["limit_minutes"],
        "limit_agent_turns": row["limit_agent_turns"],
        "mentions_enabled": bool(row["mentions_enabled"]),
        "style": _style_for(row),
    }


def _style_for(row: aiosqlite.Row) -> dict:
    length = row["style_preset"] or DEFAULT_STYLE
    voice = row["style_voice"] or DEFAULT_VOICE
    length_base = STYLE_PRESETS.get(length, STYLE_PRESETS[DEFAULT_STYLE])
    voice_base = VOICE_PRESETS.get(voice, VOICE_PRESETS[DEFAULT_VOICE])

    # Voice leads, because it is the axis that decides whether this reads like a
    # conversation at all. Length only shapes how much of it there is.
    parts = [voice_base["guidance"], length_base["guidance"]]
    if row["style_guidance"]:
        parts.append(row["style_guidance"])

    naming = row["style_naming"] or DEFAULT_NAMING
    return {
        "length": length,
        "voice": voice,
        "naming": naming,
        "max_chars": row["style_max_chars"] or length_base["max_chars"],
        "guidance": " ".join(parts),
        "naming_hint": NAMING_PRESETS.get(naming, NAMING_PRESETS[DEFAULT_NAMING]),
        "relaxed_etiquette": voice_base["relaxes_etiquette"],
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

    async def _columns(self, table: str) -> set[str]:
        assert self._conn
        async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
            return {row["name"] for row in await cur.fetchall()}

    async def _migrate(self) -> None:
        """Add columns to ledgers created by earlier schema versions.

        CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so new
        columns never appear without an explicit ALTER.
        """
        assert self._conn

        message_cols = await self._columns("messages")
        if "bus_id" not in message_cols:
            log.info("migrating messages: adding bus_id")
            await self._conn.execute("ALTER TABLE messages ADD COLUMN bus_id TEXT")

        bus_cols = await self._columns("buses")
        for column, ddl in (
            ("limit_turns", "INTEGER NOT NULL DEFAULT 20"),
            ("limit_minutes", "INTEGER NOT NULL DEFAULT 10"),
            ("style_preset", "TEXT NOT NULL DEFAULT 'normal'"),
            ("style_voice", "TEXT NOT NULL DEFAULT 'neutral'"),
            ("style_max_chars", "INTEGER"),
            ("style_guidance", "TEXT"),
            ("mentions_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("style_naming", "TEXT"),
            ("limit_agent_turns", "INTEGER NOT NULL DEFAULT 6"),
        ):
            if column not in bus_cols:
                log.info("migrating buses: adding %s", column)
                await self._conn.execute(f"ALTER TABLE buses ADD COLUMN {column} {ddl}")

        conversation_cols = await self._columns("conversations")
        if "mentionable" not in conversation_cols:
            log.info("migrating conversations: adding mentionable")
            await self._conn.execute("ALTER TABLE conversations ADD COLUMN mentionable TEXT")
        if "seeded_by" not in conversation_cols:
            log.info("migrating conversations: adding seeded_by")
            await self._conn.execute(
                "ALTER TABLE conversations ADD COLUMN seeded_by TEXT NOT NULL DEFAULT 'agent'"
            )

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

    async def set_bus_limits(
        self, bus_id: str, turns: int, minutes: int, agent_turns: int
    ) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE buses SET limit_turns = ?, limit_minutes = ?, limit_agent_turns = ? "
            "WHERE bus_id = ?",
            (turns, minutes, agent_turns, bus_id),
        )
        await self._conn.commit()

    async def set_bus_style(
        self,
        bus_id: str,
        length: str,
        voice: str,
        naming: str | None = None,
        max_chars: int | None = None,
        guidance: str | None = None,
    ) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE buses SET style_preset = ?, style_voice = ?, style_naming = ?, "
            "style_max_chars = ?, style_guidance = ? WHERE bus_id = ?",
            (length, voice, naming, max_chars, guidance, bus_id),
        )
        await self._conn.commit()

    # ---- conversations ---------------------------------------------------

    async def seed_conversation(
        self, bus_id: str, conversation_id: str, mentionable: list[dict]
    ) -> None:
        """Open a conversation with its mention allowlist, from a human message."""
        assert self._conn
        await self._conn.execute(
            "INSERT INTO conversations "
            "(conversation_id, bus_id, started_at, mentionable, seeded_by) "
            "VALUES (?, ?, ?, ?, 'human') "
            "ON CONFLICT(conversation_id) DO UPDATE SET "
            "mentionable = excluded.mentionable, seeded_by = 'human'",
            (conversation_id, bus_id, time.time(), json.dumps(mentionable)),
        )
        await self._conn.commit()

    async def set_bus_mentions(self, bus_id: str, enabled: bool) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE buses SET mentions_enabled = ? WHERE bus_id = ?",
            (1 if enabled else 0, bus_id),
        )
        await self._conn.commit()

    async def open_conversation(self, bus_id: str, conversation_id: str) -> dict:
        """Idempotent. Returns the conversation's current state."""
        assert self._conn
        await self._conn.execute(
            "INSERT INTO conversations (conversation_id, bus_id, started_at) "
            "VALUES (?, ?, ?) ON CONFLICT(conversation_id) DO NOTHING",
            (conversation_id, bus_id, time.time()),
        )
        await self._conn.commit()
        state = await self.conversation(conversation_id)
        assert state
        return state

    async def conversation(self, conversation_id: str) -> dict | None:
        assert self._conn
        async with self._conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def close_conversation(self, conversation_id: str, reason: str) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE conversations SET closed_at = ?, closed_reason = ? "
            "WHERE conversation_id = ? AND closed_at IS NULL",
            (time.time(), reason, conversation_id),
        )
        await self._conn.commit()

    async def conversation_counts(self, bus_id: str) -> dict:
        assert self._conn
        async with self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN closed_at IS NULL THEN 1 ELSE 0 END) AS open "
            "FROM conversations WHERE bus_id = ?",
            (bus_id,),
        ) as cur:
            row = await cur.fetchone()
            return {"total": row["total"] or 0, "open": row["open"] or 0}

    async def agent_turns_used(self, bus_id: str, conversation_id: str) -> int:
        """Only agent messages consume budget.

        Human messages are free on purpose: the person in the channel is the
        reset, not another consumer of it.
        """
        assert self._conn
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE bus_id = ? AND conversation_id = ? AND author_kind = 'agent'",
            (bus_id, conversation_id),
        ) as cur:
            return (await cur.fetchone())["n"]

    # ---- agents ----------------------------------------------------------

    async def register_agent(
        self,
        *,
        bus_id: str,
        agent_id: str,
        key: str,
        avatar_url: str,
        webhook_id: str | None = None,
        webhook_url: str | None = None,
    ) -> dict:
        """Create or re-key an agent. Re-registering the same name rotates its key.

        This is deliberate: an agent that lost its key can recover by registering
        again with the bootstrap secret, and the old key stops working.
        """
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO agents
                (bus_id, agent_id, key_hash, webhook_id, webhook_url, avatar_url,
                 created_at, last_seen, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(bus_id, agent_id) DO UPDATE SET
                key_hash    = excluded.key_hash,
                avatar_url  = excluded.avatar_url,
                webhook_id  = COALESCE(excluded.webhook_id, agents.webhook_id),
                webhook_url = COALESCE(excluded.webhook_url, agents.webhook_url),
                last_seen   = excluded.last_seen,
                revoked_at  = NULL
            """,
            (bus_id, agent_id, hash_secret(key), webhook_id, webhook_url,
             avatar_url, time.time(), time.time()),
        )
        await self._conn.commit()
        agent = await self.get_agent(bus_id, agent_id)
        assert agent
        return agent

    async def get_agent(self, bus_id: str, agent_id: str) -> dict | None:
        assert self._conn
        async with self._conn.execute(
            "SELECT * FROM agents WHERE bus_id = ? AND agent_id = ?", (bus_id, agent_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def agent_for_key(self, key: str) -> tuple[dict, dict] | None:
        """Resolve an agent key to (agent, bus). The tenancy boundary."""
        assert self._conn
        async with self._conn.execute(
            "SELECT * FROM agents WHERE key_hash = ? AND revoked_at IS NULL",
            (hash_secret(key),),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        agent = dict(row)
        async with self._conn.execute(
            "SELECT * FROM buses WHERE bus_id = ? AND enabled = 1", (agent["bus_id"],)
        ) as cur:
            bus_row = await cur.fetchone()
        if not bus_row:
            return None
        return agent, _row_to_bus(bus_row)

    async def touch_agent(self, bus_id: str, agent_id: str) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE agents SET last_seen = ? WHERE bus_id = ? AND agent_id = ?",
            (time.time(), bus_id, agent_id),
        )
        await self._conn.commit()

    async def clear_agent_webhook(self, bus_id: str, agent_id: str) -> None:
        """Forget a webhook we know is gone, so the next send re-provisions."""
        assert self._conn
        await self._conn.execute(
            "UPDATE agents SET webhook_id = NULL, webhook_url = NULL "
            "WHERE bus_id = ? AND agent_id = ?",
            (bus_id, agent_id),
        )
        await self._conn.commit()

    async def set_agent_webhook(
        self, bus_id: str, agent_id: str, webhook_id: str, webhook_url: str
    ) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE agents SET webhook_id = ?, webhook_url = ? "
            "WHERE bus_id = ? AND agent_id = ?",
            (webhook_id, webhook_url, bus_id, agent_id),
        )
        await self._conn.commit()

    async def roster(self, bus_id: str, online_within: float = 120.0) -> list[dict]:
        assert self._conn
        now = time.time()
        async with self._conn.execute(
            "SELECT agent_id, avatar_url, created_at, last_seen, webhook_id "
            "FROM agents WHERE bus_id = ? AND revoked_at IS NULL ORDER BY created_at",
            (bus_id,),
        ) as cur:
            return [
                {
                    # Join order. Useful context for an agent — knowing you are
                    # third tells you to expect others to have spoken already.
                    # Deliberately not used to schedule anything: positions shift
                    # when the roster changes, and any timing derived from them
                    # would shift with it.
                    "position": index,
                    "id": r["agent_id"],
                    "avatar_url": r["avatar_url"],
                    "own_webhook": r["webhook_id"] is not None,
                    "last_seen": r["last_seen"],
                    "online": bool(r["last_seen"] and now - r["last_seen"] < online_within),
                }
                for index, r in enumerate(await cur.fetchall(), start=1)
            ]

    async def revoke_agent(self, bus_id: str, agent_id: str) -> dict | None:
        """Mark revoked and return the row, so the caller can delete the webhook."""
        assert self._conn
        agent = await self.get_agent(bus_id, agent_id)
        if not agent or agent["revoked_at"]:
            return None
        # Clear the webhook columns too: the caller deletes the webhook from
        # Discord, and a stale URL left behind survives re-registration through
        # COALESCE and then 404s on every send with no way for the agent to
        # recover.
        await self._conn.execute(
            "UPDATE agents SET revoked_at = ?, key_hash = '', "
            "webhook_id = NULL, webhook_url = NULL "
            "WHERE bus_id = ? AND agent_id = ?",
            (time.time(), bus_id, agent_id),
        )
        await self._conn.commit()
        return agent

    async def revoke_all_agents(self, bus_id: str) -> list[dict]:
        """Revoke every active agent. Returns the rows so webhooks can be cleaned up."""
        assert self._conn
        async with self._conn.execute(
            "SELECT * FROM agents WHERE bus_id = ? AND revoked_at IS NULL", (bus_id,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            await self._conn.execute(
                "UPDATE agents SET revoked_at = ?, key_hash = '', "
                "webhook_id = NULL, webhook_url = NULL "
                "WHERE bus_id = ? AND revoked_at IS NULL",
                (time.time(), bus_id),
            )
            await self._conn.commit()
        return rows

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
        conversation_id: str | None = None,
    ) -> int:
        """Called by the gateway. Owns the observable columns only.

        conversation_id is the exception: the gateway seeds one for human
        messages so agents have a thread to join rather than each minting their
        own. COALESCE means a replayed message never gets re-seeded.
        """
        assert self._conn
        await self._conn.execute(
            """
            INSERT INTO messages
                (bus_id, discord_id, channel_id, thread_id, author_id, author_name,
                 author_kind, content, created_at, conversation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                bus_id      = excluded.bus_id,
                conversation_id = COALESCE(messages.conversation_id,
                                           excluded.conversation_id),
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
             author_name, author_kind, content, created_at, conversation_id),
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
        sql = (
            "SELECT m.*, c.mentionable AS mentionable FROM messages m "
            "LEFT JOIN conversations c ON m.conversation_id = c.conversation_id "
            "WHERE m.bus_id = ? AND m.seq > ?"
        )
        params: list = [bus_id, after]
        if conversation_id:
            sql += " AND m.conversation_id = ?"
            params.append(conversation_id)
        sql += " ORDER BY m.seq ASC LIMIT ?"
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
