"""Outbound side — posting to Discord on an agent's behalf.

Each bus owns a webhook that Switchboard provisions itself. There is no
DISCORD_WEBHOOK_URL setting: that would be a second way of naming a channel, and
two sources of truth can disagree — point one at #general while the channel id
says #agents and the bus reads one room while speaking into another.

Phase 3 posts every agent on a bus through that one webhook with a per-message
username override, which already gives distinct identities. Phase 4 mints a
webhook per agent so revocation becomes per-agent too.
"""

import logging

import aiohttp
import discord

log = logging.getLogger("switchboard.egress")

OUTBOUND_LIMIT = 1000

# Discord's hard limit is 2000; the headroom absorbs the breadcrumb footer.
CHUNK_LIMIT = 1900


def chunk_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split on paragraph boundaries, falling back to a hard cut.

    Splitting mid-sentence makes agent output much harder to read, so paragraphs
    are preferred and only genuinely oversized ones get cut blind.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for para in text.split("\n\n"):
        if len(para) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), limit):
                chunks.append(para[i : i + limit])
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > limit:
            chunks.append(current)
            current = para
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


async def ensure_bus_webhook(client, db, settings, channel, bus) -> str:
    """Return a usable webhook URL for this bus, creating one if needed.

    Reuses the webhook we made on a previous boot. Self-healing: if someone
    deletes it in the Discord UI, the next call recreates it. Raises
    discord.Forbidden if the bot lacks Manage Webhooks.
    """
    me = client.user
    for hook in await channel.webhooks():
        # Ownership alone is not enough: agent webhooks are also ours, and
        # adopting one as the bus webhook means revoking that agent silently
        # takes out the bus's ability to post system messages. Match the exact
        # name — agent hooks are "<name> · <agent>".
        if (hook.user and me and hook.user.id == me.id and hook.token
                and hook.name == settings.webhook_name):
            await db.set_bus_webhook(bus["bus_id"], str(hook.id), hook.url)
            log.info("bus %s reusing webhook %s", bus["bus_id"], hook.id)
            return hook.url

    hook = await channel.create_webhook(
        name=settings.webhook_name, reason="Switchboard bus egress"
    )
    await db.set_bus_webhook(bus["bus_id"], str(hook.id), hook.url)
    log.info("bus %s created webhook %s", bus["bus_id"], hook.id)
    return hook.url


# Discord's "Maximum number of webhooks reached (15)" error code.
MAX_WEBHOOKS_CODE = 30007


async def ensure_agent_webhook(client, db, settings, channel, bus, agent_id) -> str | None:
    """Mint a webhook for one agent, or return None to fall back to the bus one.

    Per-agent webhooks are what make revocation per-agent: deleting one webhook
    silences one agent without disturbing the others. Discord caps webhooks at
    15 per channel, so past that we return None and the caller posts through the
    bus webhook with a username override — identities still render distinctly,
    but revoking that agent means rotating rather than deleting.
    """
    existing = await db.get_agent(bus["bus_id"], agent_id)
    if existing and existing.get("webhook_url"):
        return existing["webhook_url"]

    try:
        hook = await channel.create_webhook(
            name=f"{settings.webhook_name} · {agent_id}",
            reason=f"Switchboard agent {agent_id}",
        )
    except discord.HTTPException as exc:
        if exc.code == MAX_WEBHOOKS_CODE:
            log.warning(
                "webhook cap reached on bus %s — agent %r will share the bus webhook",
                bus["bus_id"], agent_id,
            )
            return None
        raise

    await db.set_agent_webhook(bus["bus_id"], agent_id, str(hook.id), hook.url)
    log.info("agent %r on bus %s got webhook %s", agent_id, bus["bus_id"], hook.id)
    return hook.url


async def send_as_bus(client, db, settings, egress, bus, text, username=None):
    """Post a system message, re-provisioning the bus webhook if it has gone.

    Closure notices and reset markers all go through the bus webhook, and a
    webhook deleted in Discord left them failing permanently — nothing recreated
    it outside /switchboard enable.
    """
    async def attempt(url):
        return await egress.send(webhook_url=url, text=text,
                                 username=username or settings.webhook_name)

    try:
        return await attempt(bus["webhook_url"])
    except (discord.NotFound, NoWebhookConfigured):
        channel = client.get_channel(int(bus["channel_id"]))
        if channel is None:
            raise
        log.warning("bus %s webhook gone; re-provisioning", bus["bus_id"])
        await db.set_bus_webhook(bus["bus_id"], None, None)
        url = await ensure_bus_webhook(client, db, settings, channel, dict(bus, webhook_url=None))
        return await attempt(url)


class NoWebhookConfigured(RuntimeError):
    pass


class Egress:
    """Stateless with respect to buses — the webhook URL comes in per call."""

    def __init__(self, settings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        # aiohttp defaults to 100 concurrent connections. Every agent message is
        # an outbound webhook POST, so on a busy bus that cap is the pacing item:
        # a burst drains 100 at a time while the rest queue behind it. Discord's
        # own per-webhook limits are the real ceiling and they are well under
        # this, so a higher cap costs nothing and stops us adding a second one.
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=OUTBOUND_LIMIT)
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def delete_webhook(self, webhook_url: str) -> None:
        """Used by revoke. Silently tolerates an already-deleted webhook."""
        assert self._session
        try:
            await discord.Webhook.from_url(webhook_url, session=self._session).delete()
        except discord.NotFound:
            pass

    async def send(
        self,
        *,
        webhook_url: str | None,
        text: str,
        username: str,
        avatar_url: str | None = None,
        footer: str | None = None,
        mention_user_ids: list[str] | None = None,
    ) -> list[str]:
        """Post as `username`. Returns the Discord message IDs, one per chunk.

        mention_user_ids is enforced by Discord, not by asking agents nicely: any
        other mention in the text still renders, but notifies nobody. @everyone
        and role pings are always off.
        """
        if not webhook_url:
            raise NoWebhookConfigured("this bus has no webhook provisioned")
        assert self._session

        chunks = chunk_text(text)
        if not chunks:
            return []

        webhook = discord.Webhook.from_url(webhook_url, session=self._session)

        ids: list[str] = []
        for index, chunk in enumerate(chunks):
            body = chunk
            # Breadcrumb rides only the final chunk so a split message reads cleanly.
            if footer and index == len(chunks) - 1:
                body = f"{chunk}\n-# {footer}"

            kwargs: dict = {
                "content": body,
                "username": username,
                "wait": True,
                "allowed_mentions": discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=[discord.Object(id=int(i)) for i in (mention_user_ids or [])]
                    or False,
                ),
            }
            if avatar_url:
                kwargs["avatar_url"] = avatar_url

            # discord.py handles 429 backoff internally.
            message = await webhook.send(**kwargs)
            ids.append(str(message.id))

        return ids
