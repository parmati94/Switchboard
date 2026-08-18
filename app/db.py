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

SCHEMA_VERSION = 15

# What agents should call themselves. Separate from voice because the two don't
# always track: a casual room might still want descriptive names, and a working
# one might not.
NAMING_PRESETS = {
    "descriptive": (
        "Pick a name describing your particular role or angle — the lens you argue "
        "from, not your job title."
    ),
    "human": (
        "Pick a name a person might actually use in a group chat: short, a little "
        "character, not a job title. Think of a name you have never seen on a bot."
    ),
    "playful": (
        "Pick an absurd, memorable handle. Silly is the point. Keep it clean, and "
        "make it one nobody else would land on by accident."
    ),
    "crude": (
        "Pick a crude, juvenile handle — the kind of username people picked on the "
        "early internet, before anyone used their real name anywhere. Profanity and "
        "innuendo are welcome and the ruder the funnier. Reach past the first thing that comes to mind: everyone's "
        "first instinct here is a fart or a body part, and the funnier handle is "
        "usually two words that have no business together. "
        "Two limits, and they are about the name only: no slurs, and don't make "
        "your handle a dig at a specific person — it is what you are called, not a "
        "comment on someone. Ribbing people in conversation is a different thing "
        "and is entirely welcome."
    ),
}
DEFAULT_NAMING = "human"

# How they treat each other, which is not the same thing as how they talk.
# Conflating the two meant aggression was only reachable by going casual, and a
# casual room could not be anything but savage.
EDGE_PRESETS = {
    "warm": (
        "Be generous with each other. Build on what someone said rather than "
        "looking for the flaw in it, agree out loud when you agree, and get "
        "interested in the topic rather than in scoring. Tease lightly if at all."
    ),
    "dry": (
        "Matter-of-fact with a light touch. Disagree plainly when you disagree, "
        "but you are not here to land hits. Wit is welcome; needling is not."
    ),
    "sharp": (
        "Disagree bluntly and tease freely. Call out weak reasoning, including a "
        "human's. Keep it aimed at the argument rather than the person, and let a "
        "good point stand when someone makes one."
    ),
    "savage": (
        "Rib everyone, pile on when it is funny, and treat silence as no "
        "protection — anyone in the channel is fair game whether or not they have "
        "spoken. "
        "But ribbing is seasoning, not the meal. Vary it: agree when someone is "
        "right, get genuinely interested when a topic deserves it, and let a good "
        "line from someone else land without immediately topping it. Relentless "
        "attack is exactly as one-note as relentless analysis, and the funniest "
        "person in a group chat is never the one swinging at everything. "
        "Two things stay off regardless: slurs, and being genuinely nasty about "
        "someone who is not here to answer back."
    ),
}
DEFAULT_EDGE = "dry"

# Style has two independent axes. Length alone was not enough: capping characters
# made agents terse without making them human, so a casual question came back
# answered like a consulting deck. Voice is the axis that fixes that, and it has
# to relax the etiquette rules rather than merely add adjectives — "never
# acknowledge anything" makes conversation impossible.
VOICE_PRESETS = {
    "casual": {
        "guidance": (
            "Talk like a person in a group chat, not an analyst. Contractions, "
            "opinions, jokes, blunt disagreement. No headings, no bullet lists, no "
            "hedging, no 'the distinction is structural rather than X'. Say the thing. "
            "Reacting to what someone said with a short agreement or a joke is fine "
            "here — the back-and-forth is the point, not noise, and you do not have to be "
            "adding new information to be worth reading."
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

# Who agents may actually ping. Enforced on the wire via allowed_mentions, so a
# narrower mode is a real control rather than an instruction that can drift.
#
# `conversation` was the only behaviour and it under-delivered badly: the
# allowlist is seeded from a human message, so a conversation an agent opened had
# an empty one and nobody could be pinged in it at all — which is most banter
# threads. `participants` fixes that by treating having spoken in the channel as
# the thing that makes you reachable.
MENTION_MODES = {
    "off": "Agents cannot notify anyone. Mentions still render as text.",
    "conversation": (
        "Only the human who started an exchange, plus anyone they @-mentioned in "
        "it. Exchanges an agent started reach nobody."
    ),
    "participants": (
        "Anyone who has posted in this channel recently, plus the conversation's "
        "own people. Having spoken here is what makes you reachable."
    ),
}
DEFAULT_MENTION_MODE = "participants"

# How long having spoken keeps you reachable. Participation is stickier than
# attention, so this is deliberately short: someone who chimed in last week has
# probably stopped watching, and being pinged by a bot calling them names is a
# poor reintroduction.
PARTICIPANT_WINDOW_DAYS = 7.0

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
    -- Agents cannot read below this seq. Lets a room start fresh without
    -- deleting anything: asking an agent to forget does not work, but refusing
    -- to serve the messages does.
    history_from_seq INTEGER NOT NULL DEFAULT 0,
    -- off | conversation | participants. See MENTION_MODES.
    mentions_mode   TEXT NOT NULL DEFAULT 'participants',
    -- How it reads. Delivered to agents at registration and on every poll, so
    -- the human never has to relay it.
    style_preset   TEXT    NOT NULL DEFAULT 'normal',
    style_voice    TEXT    NOT NULL DEFAULT 'neutral',
    style_naming   TEXT,
    style_edge     TEXT,
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

-- A bus can have several valid bootstrap secrets. The one from /enable is stored
-- hashed and cannot be shown again, so anyone else who wants to onboard an agent
-- needs their own — minted by /switchboard join, attributable, and revocable
-- individually rather than by rotating the bus out from under everybody.
CREATE TABLE IF NOT EXISTS bus_invites (
    invite_id   TEXT PRIMARY KEY,
    bus_id      TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    created_by  TEXT,               -- discord user id
    created_as  TEXT,               -- display name at the time, for the roster
    created_at  REAL NOT NULL,
    revoked_at  REAL,
    -- Optional: binds this invite to one existing identity. Whatever registers
    -- with it becomes that agent rather than choosing a name. Assigning an
    -- identity is an operator decision, so it travels in the credential the
    -- operator minted, not in a field the agent fills in.
    agent_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_invites_secret ON bus_invites(secret_hash);
CREATE INDEX IF NOT EXISTS idx_invites_bus    ON bus_invites(bus_id);
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
    renamed_at  REAL,
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

-- Refusals. Everything the server says no to is raised as an HTTPException and
-- then gone, which means the most revealing behaviour on a bus -- two agents
-- composing the same sentence and one losing the race -- leaves no trace at all.
-- The messages table only ever holds what succeeded.
--
-- A table rather than an in-memory ring buffer specifically because redeploys
-- are frequent, and a restart would otherwise wipe the experiment being watched.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    bus_id          TEXT NOT NULL,
    conversation_id TEXT,
    agent_id        TEXT,
    -- collision (409) | too_long (422) | closed (423) | rate_limited (429)
    kind            TEXT NOT NULL,
    detail          TEXT                    -- JSON, shape varies by kind
);

CREATE INDEX IF NOT EXISTS idx_events_bus ON events(bus_id, id);
CREATE INDEX IF NOT EXISTS idx_events_at  ON events(at);
"""


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_bus_secret() -> str:
    return "sb_boot_" + secrets.token_urlsafe(24)


def new_agent_key() -> str:
    return "sb_live_" + secrets.token_urlsafe(24)


AVATAR_BASE = "https://api.dicebear.com/9.x/"

# What an agent may ask to look like. An allowlist rather than a free URL: we
# build the address ourselves, so registering can never point Discord at
# something arbitrary.
AVATAR_STYLES = (
    "adventurer", "big-smile", "bottts", "croodles", "fun-emoji", "lorelei",
    "micah", "notionists", "open-peeps", "personas", "pixel-art", "shapes",
    "thumbs",
)

# Which faces suit which room. Everyone used to be a bottts robot, so a roster
# of thirty agents looked like one factory. Pools rather than a single style, so
# agents on the same bus still differ from each other.
NAMING_AVATARS = {
    "descriptive": ("shapes", "bottts", "thumbs"),
    "human": ("lorelei", "notionists", "micah", "personas"),
    "playful": ("croodles", "adventurer", "big-smile", "open-peeps"),
    "crude": ("fun-emoji", "bottts", "pixel-art", "thumbs"),
}

AVATAR_BACKGROUNDS = ("b6e3f4", "c0aede", "d1d4f9", "ffd5dc", "ffdfbf",
                      "c8e6c9", "ffe0b2", "e1bee7", "f8bbd0", "b2dfdb")


def _pick(options: tuple, name: str, salt: str = "") -> str:
    """Stable choice from a name. sha256 rather than hash(), which is salted per
    process and would hand an agent a different face after every restart."""
    digest = hashlib.sha256((salt + name).encode()).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


def avatar_style_of(url: str | None) -> str | None:
    """The style in a face we generated, or None if it is not one of ours."""
    if not url or not url.startswith(AVATAR_BASE):
        return None
    style = url[len(AVATAR_BASE):].split("/", 1)[0]
    return style if style in AVATAR_STYLES else None


def default_avatar_url(name: str, naming: str = DEFAULT_NAMING,
                       style: str | None = None) -> str:
    """A deterministic face per agent name.

    Discord fetches avatar URLs from its own servers, so these come from a
    generated-avatar service rather than being self-hosted. Same name and style
    always yield the same face, which is what lets a resumed identity come back
    looking like itself.

    The style follows the bus's naming preset unless the agent asked for one.
    Background varies by name so agents sharing a style still differ.
    """
    pool = NAMING_AVATARS.get(naming, NAMING_AVATARS[DEFAULT_NAMING])
    chosen = style if style in AVATAR_STYLES else _pick(pool, name)
    return (f"{AVATAR_BASE}{chosen}/png?seed={quote(name, safe='')}"
            f"&backgroundColor={_pick(AVATAR_BACKGROUNDS, name, salt='bg:')}")


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
        "history_from_seq": row["history_from_seq"],
        "mentions_enabled": bool(row["mentions_enabled"]),
        "mentions_mode": (row["mentions_mode"] if _has(row, "mentions_mode")
                          else DEFAULT_MENTION_MODE),
        "style": _style_for(row),
        # The raw stored overrides, as distinct from the effective values in
        # "style": max_chars there has already fallen back to the length preset,
        # and guidance there is the composed prose. /switchboard style needs to
        # tell "no override set" from "override happens to match the preset" so
        # it can carry one forward untouched.
        "style_overrides": {
            "max_chars": row["style_max_chars"],
            "guidance": row["style_guidance"],
        },
    }


def _style_for(row: aiosqlite.Row) -> dict:
    length = row["style_preset"] or DEFAULT_STYLE
    voice = row["style_voice"] or DEFAULT_VOICE
    length_base = STYLE_PRESETS.get(length, STYLE_PRESETS[DEFAULT_STYLE])
    voice_base = VOICE_PRESETS.get(voice, VOICE_PRESETS[DEFAULT_VOICE])

    edge = row["style_edge"] or DEFAULT_EDGE
    edge_guidance = EDGE_PRESETS.get(edge, EDGE_PRESETS[DEFAULT_EDGE])

    # Voice leads, because it decides whether this reads like a conversation at
    # all. Edge decides how they treat each other. Length only shapes how much of
    # it there is.
    parts = [voice_base["guidance"], edge_guidance, length_base["guidance"]]
    if row["style_guidance"]:
        parts.append(row["style_guidance"])

    naming = row["style_naming"] or DEFAULT_NAMING
    guidance = " ".join(parts)
    naming_hint = NAMING_PRESETS.get(naming, NAMING_PRESETS[DEFAULT_NAMING])
    # Fingerprint the prose, not the labels: the labels ride every poll anyway.
    rev = hashlib.sha256((guidance + naming_hint).encode()).hexdigest()[:8]
    return {
        "rev": rev,
        "length": length,
        "voice": voice,
        "edge": edge,
        "naming": naming,
        "max_chars": row["style_max_chars"] or length_base["max_chars"],
        "guidance": guidance,
        "naming_hint": naming_hint,
        "relaxed_etiquette": voice_base["relaxes_etiquette"],
    }


# The labels an agent should see on every single poll. They are the reminder —
# an agent seeing "casual · sharp · terse · 360" stays anchored, and the prose is
# elaboration on those four words. Repeating the prose costs 363 tokens a poll,
# 4.3x the message it accompanies, to re-send text the agent already has.
STYLE_LABELS = ("rev", "voice", "edge", "length", "naming", "max_chars",
                "relaxed_etiquette")


def style_summary(style: dict) -> dict:
    return {k: style[k] for k in STYLE_LABELS if k in style}


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
        gone = await self.prune_events()
        if gone:
            log.info("pruned %d old event(s)", gone)
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
            ("style_edge", "TEXT"),
            ("limit_agent_turns", "INTEGER NOT NULL DEFAULT 6"),
            ("history_from_seq", "INTEGER NOT NULL DEFAULT 0"),
            ("mentions_mode", "TEXT NOT NULL DEFAULT 'participants'"),
        ):
            if column not in bus_cols:
                log.info("migrating buses: adding %s", column)
                await self._conn.execute(f"ALTER TABLE buses ADD COLUMN {column} {ddl}")
                if column == "mentions_mode":
                    # mentions_enabled was a boolean. Off stays off; on becomes the
                    # behaviour it used to have, not the new default — nobody's
                    # bus should silently widen who agents can ping.
                    await self._conn.execute(
                        "UPDATE buses SET mentions_mode = "
                        "CASE WHEN mentions_enabled = 0 THEN 'off' ELSE 'conversation' END"
                    )

        agent_cols = await self._columns("agents")
        if "renamed_at" not in agent_cols:
            log.info("migrating agents: adding renamed_at")
            await self._conn.execute("ALTER TABLE agents ADD COLUMN renamed_at REAL")

        invite_cols = await self._columns("bus_invites")
        if "agent_id" not in invite_cols:
            log.info("migrating bus_invites: adding agent_id")
            await self._conn.execute("ALTER TABLE bus_invites ADD COLUMN agent_id TEXT")

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
        """Resolve a bootstrap secret to a bus. Enabled buses only.

        Checks the bus's own secret first, then any invite minted by
        /switchboard join — a bus may have several valid secrets so that people
        can be onboarded, and revoked, one at a time.
        """
        assert self._conn
        h = hash_secret(secret)
        async with self._conn.execute(
            "SELECT * FROM buses WHERE secret_hash = ? AND enabled = 1", (h,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return _row_to_bus(row)

        async with self._conn.execute(
            "SELECT b.* FROM buses b JOIN bus_invites i ON i.bus_id = b.bus_id "
            "WHERE i.secret_hash = ? AND i.revoked_at IS NULL AND b.enabled = 1",
            (h,),
        ) as cur:
            row = await cur.fetchone()
            return _row_to_bus(row) if row else None

    async def create_invite(self, bus_id, secret, created_by, created_as,
                            agent_id: str | None = None) -> str:
        assert self._conn
        invite_id = "inv_" + secrets.token_hex(3)
        await self._conn.execute(
            "INSERT INTO bus_invites "
            "(invite_id, bus_id, secret_hash, created_by, created_as, created_at, agent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invite_id, bus_id, hash_secret(secret), created_by, created_as, time.time(),
             agent_id),
        )
        await self._conn.commit()
        return invite_id

    async def revoke_invites(self, bus_id: str, created_by: str | None = None) -> int:
        """Revoke every invite on a bus, or just one person's."""
        assert self._conn
        sql = "UPDATE bus_invites SET revoked_at = ? WHERE bus_id = ? AND revoked_at IS NULL"
        params = [time.time(), bus_id]
        if created_by:
            sql += " AND created_by = ?"
            params.append(created_by)
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur.rowcount

    async def active_invites(self, bus_id: str) -> list[dict]:
        assert self._conn
        async with self._conn.execute(
            "SELECT invite_id, created_by, created_as, created_at FROM bus_invites "
            "WHERE bus_id = ? AND revoked_at IS NULL ORDER BY created_at",
            (bus_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

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
        edge: str | None = None,
        max_chars: int | None = None,
        guidance: str | None = None,
    ) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE buses SET style_preset = ?, style_voice = ?, style_naming = ?, "
            "style_edge = ?, style_max_chars = ?, style_guidance = ? WHERE bus_id = ?",
            (length, voice, naming, edge, max_chars, guidance, bus_id),
        )
        await self._conn.commit()

    # ---- conversations ---------------------------------------------------

    async def seed_conversation(
        self, bus_id: str, conversation_id: str, mentionable: list[dict]
    ) -> None:
        """Open a conversation with its mention allowlist, from a human message.

        The allowlist accumulates. It used to be replaced on every human message,
        so posting "@alice thoughts?" and then a bare "any update?" quietly
        dropped alice out of the exchange she had been summoned into.
        """
        assert self._conn
        async with self._conn.execute(
            "SELECT mentionable FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()

        merged: dict[str, dict] = {}
        if row and row["mentionable"]:
            try:
                for person in json.loads(row["mentionable"]):
                    merged[str(person["id"])] = person
            except (json.JSONDecodeError, TypeError, KeyError):
                pass
        # Later wins: a person re-summoned is upgraded from author to summoned.
        for person in mentionable:
            merged[str(person["id"])] = person

        await self._conn.execute(
            "INSERT INTO conversations "
            "(conversation_id, bus_id, started_at, mentionable, seeded_by) "
            "VALUES (?, ?, ?, ?, 'human') "
            "ON CONFLICT(conversation_id) DO UPDATE SET "
            "mentionable = excluded.mentionable, seeded_by = 'human'",
            (conversation_id, bus_id, time.time(), json.dumps(list(merged.values()))),
        )
        await self._conn.commit()

    async def set_bus_mentions(self, bus_id: str, mode: str) -> None:
        """Set the mention mode. mentions_enabled is kept in step so a rollback
        to an older image finds the boolean it expects rather than a bus that
        silently stops notifying anyone."""
        assert self._conn
        await self._conn.execute(
            "UPDATE buses SET mentions_mode = ?, mentions_enabled = ? WHERE bus_id = ?",
            (mode, 0 if mode == "off" else 1, bus_id),
        )
        await self._conn.commit()

    async def mentionable_for(self, bus: dict, conversation_mentionable,
                              participants: list[dict] | None = None) -> list[dict]:
        """Who agents may notify, by this bus's mode. One resolver, so what /messages
        advertises and what /say enforces can never disagree.

        An agent that is not told it may ping someone will not try, so the same list
        has to ride the envelope and gate the wire.
        """
        people: dict[str, dict] = {}
        mode = bus.get("mentions_mode") or DEFAULT_MENTION_MODE
        if mode == "off":
            return []

        if isinstance(conversation_mentionable, str):
            try:
                conversation_mentionable = json.loads(conversation_mentionable or "[]")
            except json.JSONDecodeError:
                conversation_mentionable = []
        for person in conversation_mentionable or []:
            try:
                people[str(person["id"])] = person
            except (TypeError, KeyError):
                continue

        if mode == "participants":
            # Conversation people first, so someone deliberately summoned keeps that
            # role rather than being flattened to a generic participant. Callers
            # shaping many messages pass the list in, so it is one query per request.
            if participants is None:
                participants = await self.recent_participants(bus["bus_id"])
            for person in participants:
                people.setdefault(person["id"], person)

        return list(people.values())

    async def recent_participants(
        self, bus_id: str, within_days: float = PARTICIPANT_WINDOW_DAYS
    ) -> list[dict]:
        """Humans who have posted in this bus lately, newest first.

        Reads the ledger rather than Discord, so it needs no privileged members
        intent — author_id is already recorded on every observed message. Only
        humans: agents are webhook identities with no user id and can never be
        notified, whatever anyone writes.
        """
        assert self._conn
        cutoff = time.time() - within_days * 86400
        async with self._conn.execute(
            "SELECT author_id, author_name, MAX(created_at) AS seen FROM messages "
            "WHERE bus_id = ? AND author_kind = 'human' AND author_id IS NOT NULL "
            "AND created_at > ? GROUP BY author_id ORDER BY seen DESC",
            (bus_id, cutoff),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": str(r["author_id"]), "name": r["author_name"], "role": "participant"}
            for r in rows
        ]

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

    async def reset_history(self, bus_id: str) -> dict:
        """Hide everything so far from agents and close what is in flight.

        Nothing is deleted — the ledger keeps it, agents just cannot fetch it.
        """
        assert self._conn
        head = (await self.bus_stats(bus_id))["head_seq"]
        await self._conn.execute(
            "UPDATE buses SET history_from_seq = ? WHERE bus_id = ?", (head, bus_id)
        )
        cur = await self._conn.execute(
            "UPDATE conversations SET closed_at = ?, closed_reason = 'the room was reset' "
            "WHERE bus_id = ? AND closed_at IS NULL",
            (time.time(), bus_id),
        )
        closed = cur.rowcount
        await self._conn.commit()
        return {"history_from_seq": head, "conversations_closed": closed}

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

    async def names_used_recently(self, bus_id: str, within_days: float = 7.0) -> list[str]:
        """Every name used on this bus lately, live or retired.

        The roster only lists active agents, so every retired name was free to be
        picked again — and fresh agents kept landing on the same handful, partly
        because a model given the same instruction makes the same draw. Blocking
        recent reuse forces novelty and keeps the channel history legible: two
        different agents with one name is unreadable weeks later.
        """
        assert self._conn
        cutoff = time.time() - within_days * 86400
        async with self._conn.execute(
            "SELECT agent_id FROM agents WHERE bus_id = ? AND "
            "(revoked_at IS NULL OR revoked_at > ? OR created_at > ?)",
            (bus_id, cutoff, cutoff),
        ) as cur:
            return [r["agent_id"] for r in await cur.fetchall()]

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

    async def rename_agent(
        self, bus_id: str, old_id: str, new_id: str, avatar_url: str | None = None
    ) -> dict | None:
        """Change an agent's name in place, keeping its key and webhook.

        Re-registering under a new name was the only way to do this, and it left
        the old entry rotting on the roster. Messages already posted keep the old
        author name — history is history.
        """
        assert self._conn
        # A revoked agent still occupies (bus_id, agent_id), so renaming onto a
        # retired name hit the primary key and 500'd. A retired name should be
        # reclaimable: its key is already dead and its webhook already deleted,
        # so the row carries nothing worth keeping. Messages keep their author
        # names independently, so no history is lost.
        await self._conn.execute(
            "DELETE FROM agents WHERE bus_id = ? AND agent_id = ? "
            "AND revoked_at IS NOT NULL",
            (bus_id, new_id),
        )
        await self._conn.execute(
            "UPDATE agents SET agent_id = ?, avatar_url = COALESCE(?, avatar_url), "
            "renamed_at = ? WHERE bus_id = ? AND agent_id = ? AND revoked_at IS NULL",
            (new_id, avatar_url, time.time(), bus_id, old_id),
        )
        await self._conn.commit()
        return await self.get_agent(bus_id, new_id)

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
        reply_to: str | None = None,
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
                 author_kind, content, created_at, conversation_id, reply_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                created_at  = excluded.created_at,
                -- /say already knows what an agent replied to; the gateway only
                -- learns it for humans. Never overwrite a known value with NULL.
                reply_to    = COALESCE(excluded.reply_to, messages.reply_to)
            """,
            (bus_id, discord_id, channel_id, thread_id, author_id,
             author_name, author_kind, content, created_at, conversation_id, reply_to),
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
            "WHERE m.bus_id = ? AND m.seq > "
            "max(?, COALESCE((SELECT history_from_seq FROM buses WHERE bus_id = ?), 0))"
        )
        params: list = [bus_id, after, bus_id]
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

    # ---- events ----------------------------------------------------------

    async def record_event(
        self,
        bus_id: str,
        kind: str,
        *,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Note a refusal. Never raises — observability must not break a request.

        Called from the paths that reject a post. Those paths already have a
        correct answer for the agent; a failure to write the audit row is not a
        reason to turn that into a 500.
        """
        try:
            assert self._conn
            await self._conn.execute(
                "INSERT INTO events (at, bus_id, conversation_id, agent_id, kind, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), bus_id, conversation_id, agent_id, kind,
                 json.dumps(detail) if detail else None),
            )
            await self._conn.commit()
        except Exception:  # noqa: BLE001 - the request matters more than the record
            log.warning("failed to record %s event on bus %s", kind, bus_id, exc_info=True)

    async def recent_events(
        self, bus_id: str | None = None, limit: int = 100, kind: str | None = None
    ) -> list[dict]:
        """Newest first. bus_id None reads across every bus, for the operator view."""
        assert self._conn
        clauses, params = [], []
        if bus_id:
            clauses.append("bus_id = ?")
            params.append(bus_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self._conn.execute(
            f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", params
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "at": r["at"],
                "bus_id": r["bus_id"],
                "conversation_id": r["conversation_id"],
                "agent_id": r["agent_id"],
                "kind": r["kind"],
                "detail": json.loads(r["detail"]) if r["detail"] else None,
            }
            for r in rows
        ]

    async def prune_events(self, older_than_days: float = 30.0, keep_max: int = 50_000) -> int:
        """Bound the table. Runs at startup, which is often enough given how
        frequently this redeploys, with a row cap as the backstop for when it is
        not. Returns how many rows went."""
        assert self._conn
        cutoff = time.time() - older_than_days * 86400
        await self._conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        await self._conn.execute(
            "DELETE FROM events WHERE id <= "
            "(SELECT MAX(id) FROM events) - ?", (keep_max,),
        )
        await self._conn.commit()
        async with self._conn.execute("SELECT changes() AS n") as cur:
            return (await cur.fetchone())["n"]

    # ---- identity resumption ---------------------------------------------

    async def identity_for_secret(self, secret: str) -> str | None:
        """The identity an invite binds to, if any. None for an ordinary invite.

        Separate from bus_for_secret so the common path stays one query and the
        binding is only looked up at registration, where it matters.
        """
        assert self._conn
        async with self._conn.execute(
            "SELECT agent_id FROM bus_invites "
            "WHERE secret_hash = ? AND revoked_at IS NULL AND agent_id IS NOT NULL",
            (hash_secret(secret),),
        ) as cur:
            row = await cur.fetchone()
            return row["agent_id"] if row else None

    async def dormant_agents(self, bus_id: str, idle_after: float = 300.0) -> list[dict]:
        """Identities that can be taken up: revoked, or simply gone quiet.

        Revoked agents keep their row — only the key and webhook go — so the cast
        of a bus survives /switchboard revoke and can be recalled later.
        """
        assert self._conn
        now = time.time()
        async with self._conn.execute(
            "SELECT agent_id, created_at, last_seen, revoked_at FROM agents "
            "WHERE bus_id = ? AND (revoked_at IS NOT NULL OR COALESCE(last_seen, 0) < ?) "
            "ORDER BY COALESCE(last_seen, created_at) DESC",
            (bus_id, now - idle_after),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r["agent_id"],
                "last_seen": r["last_seen"],
                "revoked": r["revoked_at"] is not None,
            }
            for r in rows
        ]

    async def set_agent_avatar(self, bus_id: str, agent_id: str, avatar_url: str) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE agents SET avatar_url = ? WHERE bus_id = ? AND agent_id = ?",
            (avatar_url, bus_id, agent_id),
        )
        await self._conn.commit()

    async def sweep_stale_conversations(self) -> int:
        """Close exchanges that ran past their bus's time limit. Returns how many.

        The limits were only ever checked inside /say, so they applied to a
        conversation somebody was still trying to post into and to nothing else.
        An exchange that simply went quiet — agents stopped, a session ended —
        stayed open forever, and `open` on /switchboard status counted abandoned
        exchanges as live ones.

        Silent by design. There is nobody left in these to tell, and announcing
        a sweep would put a burst of closure notices into the channel for
        conversations that ended without anyone noticing.
        """
        assert self._conn
        cur = await self._conn.execute(
            "UPDATE conversations SET closed_at = ?, "
            "closed_reason = 'went quiet past the time limit' "
            "WHERE closed_at IS NULL AND conversation_id IN ("
            "  SELECT c.conversation_id FROM conversations c "
            "  JOIN buses b ON b.bus_id = c.bus_id "
            "  WHERE c.closed_at IS NULL "
            "    AND c.started_at < ? - (b.limit_minutes * 60))",
            (time.time(), time.time()),
        )
        closed = cur.rowcount
        await self._conn.commit()
        return closed

    async def conversation_for_message(self, bus_id: str, discord_id: str) -> dict | None:
        """The exchange a message belongs to, and whether it is still open.

        Used when a human replies in Discord: continuing what they replied to
        beats minting a new exchange, which is what fragmented a discussion every
        time the human spoke.
        """
        assert self._conn
        async with self._conn.execute(
            "SELECT m.conversation_id, c.closed_at FROM messages m "
            "LEFT JOIN conversations c ON c.conversation_id = m.conversation_id "
            "WHERE m.bus_id = ? AND m.discord_id = ? AND m.conversation_id IS NOT NULL",
            (bus_id, discord_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {"conversation_id": row["conversation_id"],
                "closed": row["closed_at"] is not None}

    async def messages_by_agent(self, bus_id: str, agent_id: str, limit: int = 10) -> list[str]:
        """What this identity said before, newest last. Ignores history_from_seq.

        Deliberately not subject to the horizon. A reset hides the old *thread*,
        which is about not polluting a fresh conversation; this answers "who was
        I", which is about the character. Self-authored lines only, so resuming
        an identity never reopens anyone else's history.
        """
        assert self._conn
        async with self._conn.execute(
            "SELECT content FROM messages WHERE bus_id = ? AND author_name = ? "
            "AND content <> '' ORDER BY seq DESC LIMIT ?",
            (bus_id, agent_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [r["content"] for r in reversed(rows)]
