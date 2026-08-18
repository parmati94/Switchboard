"""The control plane — slash commands.

Configuration lives in Discord rather than in env vars, which means Discord's
permission model *is* the auth model: the person allowed to configure a bus is
whoever Discord already says can manage the server. No login, no admin UI.

Every command is gated on Manage Server and replies ephemerally, so bootstrap
secrets are delivered to one person and never enter channel history.

Bots cannot invoke application commands, so this surface is structurally
human-only. Agents use the HTTP API.
"""

import logging
import time

import discord
from discord import app_commands

from .db import new_bus_secret
from .egress import ensure_bus_webhook, send_as_bus

log = logging.getLogger("switchboard.commands")

# Sentinel value for "clear the whole roster", surfaced via autocomplete so it
# is one click rather than a name you have to know.
ALL_AGENTS = "*"

# Reused from the project's palette so embeds match the bot's own artwork.
COLOUR_GREEN = 0x2F6B4F
COLOUR_BRASS = 0xB8801F
COLOUR_RED = 0xA83A2E

# Typed into /switchboard style guidance to remove the bus's extra instruction.
# Discord has no way to send an empty string for an optional text option, so
# there has to be a word that means "clear it".
CLEAR_WORDS = {"none", "clear", "reset", "no additional guidance", "-"}


def resolve_style(current: dict, overrides: dict, *, voice=None, edge=None,
                  length=None, naming=None, max_chars=None, guidance=None) -> dict:
    """Work out the new style from a partial change. Pure, so it is testable.

    Every field is preserve-by-default. This used to live inline in the command
    and only covered three of the six fields: omitting max_chars or guidance
    wrote NULL over them, so changing edge alone silently deleted a channel's
    custom instruction with nothing in the response to say so.
    """
    # A length preset carries its own cap, so choosing one without naming a cap
    # means "use that preset's". Otherwise an override set once would outlive
    # every later length change with no way to drop it.
    if max_chars is not None:
        resolved_max = max_chars
    elif length is not None:
        resolved_max = None
    else:
        resolved_max = overrides["max_chars"]

    if guidance is None:
        resolved_guidance = overrides["guidance"]
    elif guidance.strip().lower() in CLEAR_WORDS:
        resolved_guidance = None
    else:
        resolved_guidance = guidance

    return {
        "length": length or current["length"],
        "voice": voice or current["voice"],
        "naming": naming or current["naming"],
        "edge": edge or current["edge"],
        "max_chars": resolved_max,
        "guidance": resolved_guidance,
    }


def _tick(ok) -> str:
    return "✅" if ok else "❌"


def _no_bus_message() -> str:
    return (
        "No bus in this channel. Run `/switchboard enable` here to activate one."
    )


def build_tree(client: discord.Client, db, settings, egress=None) -> app_commands.CommandTree:
    tree = app_commands.CommandTree(client)

    group = app_commands.Group(
        name="switchboard",
        description="Manage this channel's agent bus",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @group.command(name="enable", description="Activate this channel as an agent bus")
    async def enable(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "Run this in a normal text channel — threads and DMs can't host a bus.",
                ephemeral=True,
            )
            return

        # Verify we can READ before accepting the bus. Webhooks post under their
        # own authority, so a bot with Manage Webhooks but no View Channel will
        # happily send messages it can never see coming back — agents talk past
        # each other and human messages are never recorded, with no error
        # anywhere. Refuse loudly instead of creating a bus that half-works.
        me = interaction.guild.me if interaction.guild else None
        if me is not None:
            perms = channel.permissions_for(me)
            missing = [
                label
                for label, granted in (
                    ("View Channel", perms.view_channel),
                    ("Read Message History", perms.read_message_history),
                    ("Manage Webhooks", perms.manage_webhooks),
                )
                if not granted
            ]
            if missing:
                await interaction.followup.send(
                    "I'm missing permissions in this channel: "
                    + ", ".join(f"**{m}**" for m in missing)
                    + ".\n\nWithout **View Channel** I can post but never see anything: "
                    "agents would talk past each other and your own messages would "
                    "never reach them, with no visible error. Grant these on the "
                    "channel, then run `/switchboard enable` again.",
                    ephemeral=True,
                )
                return

        existing = await db.bus_for_channel(str(channel.id))
        if existing and existing["enabled"]:
            await interaction.followup.send(
                f"This channel is already a bus (`{existing['bus_id']}`).\n"
                "Use `/switchboard status` to inspect it, or `/switchboard rotate` "
                "for a new secret. Re-enabling would invalidate your agents' credentials.",
                ephemeral=True,
            )
            return

        secret = new_bus_secret()
        bus = await db.create_bus(
            guild_id=str(interaction.guild_id),
            channel_id=str(channel.id),
            guild_name=interaction.guild.name if interaction.guild else "",
            channel_name=channel.name,
            created_by=str(interaction.user.id),
            secret=secret,
        )

        try:
            await ensure_bus_webhook(client, db, settings, channel, bus)
        except discord.Forbidden:
            await db.set_bus_enabled(bus["bus_id"], False)
            await interaction.followup.send(
                "I need **Manage Webhooks** in this channel to speak on agents' behalf. "
                "Grant it and run `/switchboard enable` again.",
                ephemeral=True,
            )
            return

        base = settings.public_url.rstrip("/")
        await interaction.followup.send(
            f"**Bus enabled** in {channel.mention} — `{bus['bus_id']}`\n\n"
            "Give an agent this one line and it will onboard itself:\n"
            f"```\nJoin the bus at {base} — bootstrap secret is {secret}\n"
            "Read the root path first, sending the secret as an Authorization: Bearer header.\n```\n"
            "This secret is shown **once**. `/switchboard rotate` issues a new one.",
            ephemeral=True,
        )
        log.info("bus %s enabled in %s/#%s", bus["bus_id"], interaction.guild_id, channel.name)

    async def _dormant_names(interaction: discord.Interaction, current: str):
        """Autocomplete for `as:` — identities on this bus that can be taken up."""
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            return []
        names = await db.dormant_agents(bus["bus_id"])
        matches = [n for n in names if current.lower() in n["id"].lower()]
        return [
            app_commands.Choice(
                name=f"{n['id']} — {'revoked' if n['revoked'] else 'idle'}",
                value=n["id"],
            )
            for n in matches[:25]      # Discord renders at most 25
        ]

    @group.command(name="join",
                   description="Get the line that onboards your own agent to this bus")
    @app_commands.describe(
        identity="Optional: assign an existing identity instead of letting the agent "
                 "pick a name. It resumes that character.",
    )
    @app_commands.rename(identity="as")
    @app_commands.autocomplete(identity=_dormant_names)
    async def join(interaction: discord.Interaction, identity: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return
        if not bus["enabled"]:
            await interaction.followup.send(
                f"Bus `{bus['bus_id']}` is disabled. An admin can re-enable it with "
                "`/switchboard enable`.",
                ephemeral=True,
            )
            return

        # The bus's own secret is stored hashed and cannot be shown again, so mint
        # a fresh one belonging to this person. Several secrets can be valid at
        # once, which is what lets one person be cut off without rotating the bus
        # out from under everybody else.
        # Refuse a binding to an identity that is still live, rather than minting
        # a line that only fails later at registration.
        if identity:
            live = [a["id"] for a in await db.roster(bus["bus_id"])]
            if identity in live:
                await interaction.followup.send(
                    f"**{identity}** is still active on this bus. Stop that agent, or "
                    "run `/switchboard revoke` first, then mint the line.",
                    ephemeral=True,
                )
                return

        secret = new_bus_secret()
        invite_id = await db.create_invite(
            bus["bus_id"], secret,
            str(interaction.user.id), interaction.user.display_name,
            agent_id=identity,
        )
        base = settings.public_url.rstrip("/")
        assigned = (
            f"Whatever you paste this to becomes **{identity}** — it does not choose a "
            "name, and it gets back what it said here before.\n"
            if identity else ""
        )
        await interaction.followup.send(
            f"**Your onboarding line for `{bus['bus_id']}`** — paste this to an agent:\n"
            f"```\nJoin the bus at {base} — bootstrap secret is {secret}\n"
            "Read the root path first, sending the secret as an Authorization: Bearer "
            "header.\n```\n"
            f"{assigned}"
            f"This is yours (`{invite_id}`) and nobody else's — shown once, and only "
            "you can see this message. Run `/switchboard join` again if you lose it; "
            "old ones keep working until revoked.\n"
            "Agents you onboard get their own keys, so they survive this being revoked.",
            ephemeral=True,
        )
        log.info("invite %s minted for %s on bus %s (identity=%s)",
                 invite_id, interaction.user.id, bus["bus_id"], identity or "-")

    @group.command(name="disable", description="Stop relaying in this channel")
    async def disable(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        await db.set_bus_enabled(bus["bus_id"], False)
        await interaction.followup.send(
            f"Bus `{bus['bus_id']}` disabled. History is kept and agent credentials "
            "still exist — `/switchboard enable` resumes without re-onboarding.",
            ephemeral=True,
        )

    @group.command(name="status", description="Show this bus's state")
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        stats = await db.bus_stats(bus["bus_id"])
        agents = await db.roster(bus["bus_id"])
        convos = await db.conversation_counts(bus["bus_id"])
        style = bus["style"]

        can_speak = bool(bus["webhook_url"])

        # Reading is the failure mode that hides: posting still works without it,
        # so a bus can look healthy while recording nothing.
        can_listen = None
        me = interaction.guild.me if interaction.guild else None
        if me is not None and isinstance(interaction.channel, discord.TextChannel):
            perms = interaction.channel.permissions_for(me)
            can_listen = perms.view_channel and perms.read_message_history

        healthy = bus["enabled"] and can_speak and can_listen is not False
        colour = COLOUR_GREEN if healthy else (
            COLOUR_RED if not bus["enabled"] or can_listen is False else COLOUR_BRASS
        )

        embed = discord.Embed(
            title=f"#{bus['channel_name']}",
            description=(
                f"`{bus['bus_id']}` · "
                + ("**enabled**" if bus["enabled"] else "**disabled**")
                + ("" if healthy else "  ⚠️ needs attention")
            ),
            colour=colour,
        )

        embed.add_field(
            name="Capability",
            value=(
                f"{_tick(can_speak)} Speak\n"
                + (f"{_tick(can_listen)} Listen" if can_listen is not None
                   else "❔ Listen")
                + ("" if can_speak else "\n-# needs Manage Webhooks")
                + ("" if can_listen is not False
                   else "\n-# **cannot see this channel — nothing is recorded**")
            ),
            inline=True,
        )
        online = sum(1 for a in agents if a["online"])
        embed.add_field(
            name="Agents",
            value=(f"**{len(agents)}** registered\n{online} active"
                   if agents else "none yet\n-# share the secret"),
            inline=True,
        )
        embed.add_field(
            name="Traffic",
            value=f"**{stats['messages_stored']}** messages\ncursor at {stats['head_seq']}",
            inline=True,
        )

        embed.add_field(
            name="Conversations",
            value=(f"**{convos['open']}** open · {convos['total']} total\n"
                   f"close at {bus['limit_turns']} turns or {bus['limit_minutes']} min\n"
                   f"-# {bus['limit_agent_turns']} turns if no human started it"),
            inline=True,
        )
        embed.add_field(
            name="Style",
            value=(f"**{style['voice']}** · {style['edge']}\n{style['length']}, "
                   f"{style['max_chars']} char cap"),
            inline=True,
        )
        invites = await db.active_invites(bus["bus_id"])
        if invites:
            embed.add_field(
                name="Personal invites",
                value="\n".join(f"`{i['invite_id']}` {i['created_as']}" for i in invites[:8])
                      + ("\n-# …and more" if len(invites) > 8 else ""),
                inline=True,
            )
        embed.add_field(
            name="Mentions",
            value=("**allowed**\n-# starter + who they @"
                   if bus["mentions_enabled"] else "**blocked**\n-# nobody is notified"),
            inline=True,
        )

        if agents:
            embed.add_field(
                name="Roster",
                value="\n".join(
                    f"{'🟢' if a['online'] else '⚪'} `{a['position']}` **{a['id']}**"
                    + ("" if a["own_webhook"] else "  -# shared webhook")
                    for a in agents[:10]
                ) + (f"\n-# …and {len(agents) - 10} more" if len(agents) > 10 else ""),
                inline=False,
            )

        embed.set_footer(text="🟢 seen in the last 2 minutes")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="rotate", description="Issue a new bootstrap secret")
    @app_commands.describe(
        clear_agents="Also revoke every registered agent (default: no)"
    )
    async def rotate(
        interaction: discord.Interaction, clear_agents: bool = False
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        secret = new_bus_secret()
        await db.rotate_bus_secret(bus["bus_id"], secret)

        # Agent keys are independent of the bootstrap secret by design — an
        # agent shouldn't lose its credential because you re-keyed the door.
        # But that means rotating alone leaves the roster untouched, which
        # surprises people, so make clearing a one-flag option.
        killed = await db.revoke_invites(bus["bus_id"])
        cleared = (f"\nAlso revoked **{killed}** personal invite(s) from "
                   "`/switchboard join`." if killed else "")
        if clear_agents:
            rows = await db.revoke_all_agents(bus["bus_id"])
            await _cleanup_webhooks(rows)
            cleared += f"\nAlso revoked **{len(rows)} agent(s)** and deleted their webhooks."

        base = settings.public_url.rstrip("/")
        await interaction.followup.send(
            f"**New bootstrap secret** for `{bus['bus_id']}`. The previous one no "
            "longer works, so anything using it must be given this:\n"
            f"```\nJoin the bus at {base} — bootstrap secret is {secret}\n"
            "Read the root path first, sending the secret as an Authorization: Bearer header.\n```"
            + cleared
            + ("\n\nExisting agents keep their own keys and are unaffected — pass "
               "`clear_agents: True` if you wanted a full reset." if not clear_agents else ""),
            ephemeral=True,
        )
        log.info("bus %s secret rotated (clear_agents=%s)", bus["bus_id"], clear_agents)

    @group.command(name="roster", description="Show agents registered on this bus")
    async def roster(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        agents = await db.roster(bus["bus_id"])
        if not agents:
            await interaction.followup.send(
                f"No agents registered on `{bus['bus_id']}` yet. Give one the bootstrap "
                "secret from `/switchboard enable` and it will onboard itself.",
                ephemeral=True,
            )
            return

        now = time.time()
        stale = sum(1 for a in agents if not a["online"])
        embed = discord.Embed(
            title=f"Agents on #{bus['channel_name']}",
            description=f"`{bus['bus_id']}` · **{len(agents)}** registered, "
                        f"{len(agents) - stale} active",
            colour=COLOUR_GREEN if not stale else COLOUR_BRASS,
        )
        for a in agents[:24]:
            if a["last_seen"]:
                age = now - a["last_seen"]
                seen = f"{age:.0f}s ago" if age < 90 else f"{age / 60:.0f}m ago"
            else:
                seen = "never"
            embed.add_field(
                name=f"{'🟢' if a['online'] else '⚪'} {a['position']}. {a['id']}",
                value=f"last seen {seen}\n"
                      + ("own webhook" if a["own_webhook"] else "shared webhook"),
                inline=True,
            )
        embed.set_footer(
            text="🟢 seen in the last 2 minutes · position is join order"
            + (f" · {stale} look gone — revoke → all agents to clear" if stale else "")
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _cleanup_webhooks(rows: list[dict]) -> int:
        deleted = 0
        for row in rows:
            if egress is not None and row.get("webhook_url"):
                try:
                    await egress.delete_webhook(row["webhook_url"])
                    deleted += 1
                except Exception:  # noqa: BLE001
                    log.exception("failed deleting webhook for %r", row["agent_id"])
        return deleted

    @group.command(name="revoke", description="Revoke one agent, or clear them all")
    @app_commands.describe(agent="Pick an agent, or 'all agents' to clear the roster")
    async def revoke(interaction: discord.Interaction, agent: str) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        if agent == ALL_AGENTS:
            rows = await db.revoke_all_agents(bus["bus_id"])
            if not rows:
                await interaction.followup.send("No active agents to clear.", ephemeral=True)
                return
            deleted = await _cleanup_webhooks(rows)
            names = ", ".join(f"`{r['agent_id']}`" for r in rows)
            await interaction.followup.send(
                f"Cleared **{len(rows)} agent(s)**: {names}\n"
                f"{deleted} webhook(s) deleted. The roster is now empty.\n"
                "They can rejoin with the bootstrap secret — `/switchboard rotate` "
                "as well if you want a clean break.",
                ephemeral=True,
            )
            log.info("all %d agents revoked on bus %s", len(rows), bus["bus_id"])
            return

        revoked = await db.revoke_agent(bus["bus_id"], agent)
        if not revoked:
            await interaction.followup.send(
                f"No active agent named `{agent}` on this bus. "
                "Check `/switchboard roster` for exact names.",
                ephemeral=True,
            )
            return

        # Deleting the webhook is what actually silences them; invalidating the
        # key only stops them asking Switchboard to speak on their behalf.
        deleted = await _cleanup_webhooks([revoked])
        await interaction.followup.send(
            f"**{agent}** revoked. Its key no longer works"
            + (" and its webhook is deleted." if deleted else ".")
            + "\nIt can rejoin with the bootstrap secret unless you `/switchboard rotate` too.",
            ephemeral=True,
        )
        log.info("agent %r revoked on bus %s", agent, bus["bus_id"])

    @revoke.autocomplete("agent")
    async def _revoke_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Saves typing exact names, and puts 'all agents' one click away."""
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            return []
        agents = await db.roster(bus["bus_id"])
        choices = [app_commands.Choice(name=f"★ all agents ({len(agents)})", value=ALL_AGENTS)]
        for a in agents:
            if current.lower() in a["id"].lower():
                mark = "🟢" if a["online"] else "⚪"
                choices.append(app_commands.Choice(name=f"{mark} {a['id']}", value=a["id"]))
        return choices[:25]

    @group.command(name="limits", description="Set when conversations end on this bus")
    @app_commands.describe(
        turns="Agent messages per conversation YOU started (1-200). Bounds cost.",
        minutes="Wall-clock minutes per conversation (1-240). Rescues a stuck exchange.",
        agent_turns="Budget for conversations no human started (0-20). Keep this small.",
    )
    async def limits(
        interaction: discord.Interaction,
        turns: app_commands.Range[int, 1, 200],
        minutes: app_commands.Range[int, 1, 240],
        agent_turns: app_commands.Range[int, 0, 20] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        chosen_agent_turns = (
            agent_turns if agent_turns is not None else bus["limit_agent_turns"]
        )
        await db.set_bus_limits(bus["bus_id"], turns, minutes, chosen_agent_turns)

        embed = discord.Embed(
            title="Limits updated",
            description=f"`{bus['bus_id']}` · #{bus['channel_name']}",
            colour=COLOUR_GREEN,
        )
        embed.add_field(name="You start a topic",
                        value=f"**{turns}** agent turns\nor **{minutes}** minutes",
                        inline=True)
        embed.add_field(name="Agents start one",
                        value=f"**{chosen_agent_turns}** turns total",
                        inline=True)
        embed.set_footer(
            text="Your messages never count toward a budget — you are the reset. "
                 "Conversations nobody started are capped hard so agents cannot "
                 "chat the room out before you arrive."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="reset",
                   description="Start fresh — agents stop seeing earlier messages")
    async def reset(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        result = await db.reset_history(bus["bus_id"])

        # A visible line in the channel, so the humans reading can see where the
        # old material stopped being fair game too.
        if egress is not None:
            try:
                await send_as_bus(
                    client, db, settings, egress, bus,
                    "— **fresh start** — earlier messages are no longer visible "
                    "to agents.",
                )
            except Exception:  # noqa: BLE001 - the reset itself matters more
                log.warning("reset marker failed", exc_info=True)

        embed = discord.Embed(
            title="Fresh start",
            description=f"`{bus['bus_id']}` · #{bus['channel_name']}",
            colour=COLOUR_GREEN,
        )
        embed.add_field(name="Hidden below", value=f"seq **{result['history_from_seq']}**",
                        inline=True)
        embed.add_field(name="Conversations closed",
                        value=f"**{result['conversations_closed']}**", inline=True)
        embed.set_footer(
            text="Nothing is deleted — the ledger keeps everything, agents just "
                 "cannot fetch it. Agents already running still hold their own "
                 "memory of it; clear or restart them for a true clean slate."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("bus %s history reset to seq %s", bus["bus_id"], result["history_from_seq"])

    @group.command(name="mentions", description="Allow or forbid agents pinging people")
    @app_commands.describe(enabled="Off means agents can never notify anyone here")
    async def mentions(interaction: discord.Interaction, enabled: bool) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        await db.set_bus_mentions(bus["bus_id"], enabled)
        if enabled:
            body = (
                "Agents on this bus **can ping** — but only the person who started a "
                "conversation and anyone that person @-mentioned in it.\n"
                "Everyone else stays silent: a mention of anyone not on that list "
                "still renders in the message but notifies nobody, and `@everyone` "
                "and role pings are always blocked. That is enforced by Discord on "
                "every send, not by asking agents to behave."
            )
        else:
            body = (
                "Agents on this bus **cannot ping anyone**. Mentions will still "
                "render in their messages but will never notify."
            )
        await interaction.followup.send(body, ephemeral=True)

    @group.command(name="style", description="Set how agents write on this bus")
    @app_commands.describe(
        voice="How they sound — the register, not the attitude.",
        edge="How they treat each other. Separate from voice on purpose.",
        length="How much they write.",
        naming="What they call themselves.",
        max_chars="Hard cap override (100-1900). Omit with a new length to use that preset's cap.",
        guidance="Extra instruction, e.g. 'no jargon, assume no context'. Say 'none' to clear.",
    )
    @app_commands.choices(
        voice=[
            app_commands.Choice(name="casual — like a group chat", value="casual"),
            app_commands.Choice(name="neutral — plain and human", value="neutral"),
            app_commands.Choice(name="analytical — precise and structured", value="analytical"),
        ],
        length=[
            app_commands.Choice(name="terse — one to three sentences", value="terse"),
            app_commands.Choice(name="normal — a short paragraph", value="normal"),
            app_commands.Choice(name="detailed — thorough", value="detailed"),
        ],
        edge=[
            app_commands.Choice(name="warm — generous, builds on points", value="warm"),
            app_commands.Choice(name="dry — plain, light wit, no needling", value="dry"),
            app_commands.Choice(name="sharp — blunt, teases, calls out weak reasoning",
                                value="sharp"),
            app_commands.Choice(name="savage — piles on, nobody is safe", value="savage"),
        ],
        naming=[
            app_commands.Choice(name="human — short, a bit of character", value="human"),
            app_commands.Choice(name="descriptive — schema-critic, perf-analyst",
                                value="descriptive"),
            app_commands.Choice(name="playful — absurd and memorable", value="playful"),
            app_commands.Choice(name="crude — profanity welcome, no slurs", value="crude"),
        ],
    )
    async def style(
        interaction: discord.Interaction,
        voice: app_commands.Choice[str] | None = None,
        edge: app_commands.Choice[str] | None = None,
        length: app_commands.Choice[str] | None = None,
        naming: app_commands.Choice[str] | None = None,
        max_chars: app_commands.Range[int, 100, 1900] | None = None,
        guidance: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        if not any((voice, edge, length, naming, max_chars, guidance)):
            await interaction.followup.send(
                "Nothing to change — pass at least one option. "
                "`/switchboard status` shows what this bus is set to.",
                ephemeral=True,
            )
            return

        new = resolve_style(
            bus["style"], bus["style_overrides"],
            voice=voice.value if voice else None,
            edge=edge.value if edge else None,
            length=length.value if length else None,
            naming=naming.value if naming else None,
            max_chars=max_chars,
            guidance=guidance,
        )
        await db.set_bus_style(
            bus["bus_id"], new["length"], new["voice"], new["naming"], new["edge"],
            new["max_chars"], new["guidance"],
        )
        effective = (await db.bus_for_channel(str(interaction.channel_id)))["style"]

        embed = discord.Embed(
            title="Style updated",
            description=f"`{bus['bus_id']}` · #{bus['channel_name']}",
            colour=COLOUR_GREEN,
        )
        # Every omitted field is carried forward, so the embed says which ones
        # this command actually touched. Otherwise a one-field change reads like
        # it just set all six.
        def kept(passed, note: str) -> str:
            return f"\n-# {note}" if passed else f"\n-# {note} · unchanged"

        embed.add_field(name="Voice",
                        value=f"**{effective['voice']}**{kept(voice, 'how they talk')}",
                        inline=True)
        embed.add_field(name="Edge",
                        value=f"**{effective['edge']}**{kept(edge, 'how they treat people')}",
                        inline=True)
        embed.add_field(name="Length",
                        value=f"**{effective['length']}**"
                              f"{kept(length, str(effective['max_chars']) + ' char cap')}",
                        inline=True)
        embed.add_field(name="Naming",
                        value=f"**{effective['naming']}**{kept(naming, 'what they call themselves')}",
                        inline=True)
        embed.add_field(name="What agents are told",
                        value=effective["guidance"][:1020], inline=False)
        embed.add_field(name="…and about names",
                        value=effective["naming_hint"][:1020], inline=False)
        if effective["relaxed_etiquette"]:
            embed.add_field(
                name="Etiquette relaxed",
                value="Short agreements and jokes are allowed on casual — forbidding "
                      "them is what made conversation impossible.",
                inline=False,
            )
        embed.set_footer(
            text="Applies immediately, including to agents already running. "
                 "Naming affects agents that join from now on."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    tree.add_command(group)
    return tree
