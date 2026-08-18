"""The one thing in this system holding a Discord credential.

Multi-tenant: a single gateway connection serves every server the bot is in. Each
inbound message is matched to a bus by channel id, and anything unmatched is
dropped before it is written. That lookup is the isolation boundary.
"""

import asyncio
import logging
import math
import secrets
import time

import discord

from .commands import build_tree

log = logging.getLogger("switchboard.gateway")


class Gateway:
    def __init__(self, settings, db, egress=None, notifier=None):
        self.settings = settings
        self.db = db
        self.egress = egress
        self.notifier = notifier

        # Least privilege, matching the bot's role permissions. message_content
        # is privileged and must also be toggled on in the developer portal —
        # without it every message arrives with empty text and the bus looks
        # alive while carrying nothing.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True

        self.client = discord.Client(intents=intents)
        self.tree = build_tree(self.client, db, settings, egress)

        self._task: asyncio.Task | None = None
        self._ready_since: float | None = None
        self._last_error: str | None = None
        self._messages_seen = 0
        self._messages_dropped = 0
        self._last_message_at: float | None = None
        self._commands_synced = 0
        self._command_scope: str | None = None
        self._synced = False

        self._register_events()

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="discord-gateway")
        log.info("gateway task started")

    async def _run(self) -> None:
        try:
            await self.client.start(self.settings.discord_bot_token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced through /health
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.exception("gateway stopped unexpectedly")

    async def stop(self) -> None:
        if not self.client.is_closed():
            await self.client.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("gateway stopped")

    # ---- events ----------------------------------------------------------

    def _register_events(self) -> None:
        @self.client.event
        async def on_ready() -> None:
            self._ready_since = time.time()
            self._last_error = None
            log.info(
                "connected as %s across %d guild(s)",
                self.client.user,
                len(self.client.guilds),
            )
            # on_ready fires again on every reconnect; syncing once is enough and
            # avoids burning command-registration rate limit on flaps.
            if not self._synced:
                try:
                    # ALWAYS sync globally. A dev-guild-only sync registers the
                    # commands in exactly one server, so the bot can be invited
                    # anywhere and /switchboard simply will not exist there —
                    # which silently breaks the entire multi-tenant premise.
                    synced = await self.tree.sync()
                    self._command_scope = "global"
                    log.info(
                        "synced %d command(s) globally — available in every server "
                        "the bot joins (can take up to an hour to propagate)",
                        len(synced),
                    )

                    # Additionally mirror into the dev guild, where they appear
                    # instantly. Guild commands shadow global ones of the same
                    # name, so this adds no duplicates.
                    dev_guild = self.settings.discord_dev_guild_id
                    if dev_guild:
                        guild = discord.Object(id=dev_guild)
                        self.tree.copy_global_to(guild=guild)
                        await self.tree.sync(guild=guild)
                        self._command_scope = "global + dev guild"
                        log.info("also mirrored to the dev guild for instant testing")

                    self._commands_synced = len(synced)
                    self._synced = True
                except Exception as exc:  # noqa: BLE001
                    self._last_error = f"command sync failed: {exc}"
                    log.exception("command sync failed")

        @self.client.event
        async def on_disconnect() -> None:
            self._ready_since = None
            log.warning("gateway disconnected")

        @self.client.event
        async def on_resumed() -> None:
            self._ready_since = time.time()
            log.info("gateway resumed")

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            # Deliberately no `if message.author.bot: return`. Bots do receive
            # other bots' and webhooks' messages, and that is the entire point.
            channel = message.channel
            parent_id = getattr(channel, "parent_id", None)

            # Threads inherit their parent channel's bus.
            bus = await self.db.bus_for_channel(str(parent_id or channel.id))
            if not bus or not bus["enabled"]:
                self._messages_dropped += 1
                return

            if message.webhook_id is not None:
                author_kind = "agent"
            elif message.author.bot:
                author_kind = "bot"
            else:
                author_kind = "human"

            # What this message is a Discord reply to, if anything.
            ref = message.reference
            reply_to = str(ref.message_id) if ref and ref.message_id else None

            # A human message seeds a conversation. Without this the human's
            # message carries no conversation_id, so every agent that replies
            # mints its own and the discussion fragments into parallel threads
            # that each address the human and never each other. Observed live.
            #
            # But minting unconditionally fragments it the other way: every
            # follow-up the human typed started a fresh exchange, so budgets
            # reset whenever they spoke and the turn limit almost never bound.
            # A literal Discord reply is an unambiguous "I mean this one", so it
            # continues that exchange instead.
            conversation_id = None
            if author_kind == "human":
                if reply_to:
                    found = await self.db.conversation_for_message(
                        bus["bus_id"], reply_to
                    )
                    # A closed exchange is not reopened — agents would take 423s
                    # for a message the human clearly expects an answer to. They
                    # get a fresh one instead.
                    if found and not found["closed"]:
                        conversation_id = found["conversation_id"]
                conversation_id = conversation_id or f"c_{secrets.token_hex(3)}"

            if conversation_id:
                # The mention allowlist for this exchange: whoever spoke, plus
                # anyone they @-mentioned. Their IDs are already in the payload,
                # so no member lookup and no privileged members intent is needed.
                # role distinguishes "the person talking" from "someone they
                # deliberately pulled in". An explicit @ is a summons and agents
                # should answer it by pinging; the author usually just wants a
                # reply in the channel they are already watching.
                mentionable = {
                    str(message.author.id): {
                        "id": str(message.author.id),
                        "name": message.author.display_name,
                        "role": "author",
                    }
                }
                for user in message.mentions:
                    mentionable[str(user.id)] = {
                        "id": str(user.id),
                        "name": user.display_name,
                        "role": "summoned",
                    }
                await self.db.seed_conversation(
                    bus["bus_id"], conversation_id, list(mentionable.values())
                )

            try:
                await self.db.record_observed(
                    bus_id=bus["bus_id"],
                    conversation_id=conversation_id,
                    reply_to=reply_to,
                    discord_id=str(message.id),
                    channel_id=str(parent_id or channel.id),
                    thread_id=str(channel.id) if parent_id else None,
                    author_id=str(message.author.id),
                    author_name=message.author.display_name,
                    author_kind=author_kind,
                    content=message.content or "",
                    created_at=message.created_at.timestamp(),
                )
            except Exception:  # noqa: BLE001 - never let a bad row kill the gateway
                log.exception("failed to record message %s", message.id)
                return

            # Human messages arrive only this way, so this is what wakes agents
            # long-polling for a topic.
            if self.notifier is not None:
                self.notifier.notify(bus["bus_id"])

            self._messages_seen += 1
            self._last_message_at = time.time()
            log.debug(
                "recorded %s from %s (%s) on %s",
                message.id, message.author, author_kind, bus["bus_id"],
            )

    # ---- introspection ---------------------------------------------------

    def snapshot(self) -> dict:
        latency = self.client.latency
        return {
            "ready": self.client.is_ready(),
            "user": str(self.client.user) if self.client.user else None,
            "guilds": len(self.client.guilds) if self.client.is_ready() else 0,
            "commands_synced": self._commands_synced,
            "command_scope": self._command_scope,
            "latency_ms": (
                round(latency * 1000, 1)
                if isinstance(latency, float) and not math.isnan(latency)
                else None
            ),
            "ready_since": self._ready_since,
            "uptime_s": (
                round(time.time() - self._ready_since, 1) if self._ready_since else None
            ),
            # Proves MESSAGE_CONTENT is on and the bot can see traffic.
            "messages_seen": self._messages_seen,
            "messages_dropped": self._messages_dropped,
            "last_message_at": self._last_message_at,
            "last_error": self._last_error,
        }
