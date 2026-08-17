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
from .egress import ensure_bus_webhook

log = logging.getLogger("switchboard.commands")

# Sentinel value for "clear the whole roster", surfaced via autocomplete so it
# is one click rather than a name you have to know.
ALL_AGENTS = "*"

# Reused from the project's palette so embeds match the bot's own artwork.
COLOUR_GREEN = 0x2F6B4F
COLOUR_BRASS = 0xB8801F
COLOUR_RED = 0xA83A2E


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
                   f"close at {bus['limit_turns']} turns or {bus['limit_minutes']} min"),
            inline=True,
        )
        embed.add_field(
            name="Style",
            value=(f"**{style['voice']}** · {style['length']}\n"
                   f"{style['max_chars']} char cap"),
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
        cleared = ""
        if clear_agents:
            rows = await db.revoke_all_agents(bus["bus_id"])
            await _cleanup_webhooks(rows)
            cleared = f"\nAlso revoked **{len(rows)} agent(s)** and deleted their webhooks."

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
        turns="Agent messages allowed per conversation (1-200). Bounds cost.",
        minutes="Wall-clock minutes per conversation (1-240). Rescues a stuck exchange.",
    )
    async def limits(
        interaction: discord.Interaction,
        turns: app_commands.Range[int, 1, 200],
        minutes: app_commands.Range[int, 1, 240],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        await db.set_bus_limits(bus["bus_id"], turns, minutes)
        await interaction.followup.send(
            f"Conversations on `{bus['bus_id']}` now close after **{turns} agent turns** "
            f"or **{minutes} minutes**, whichever comes first.\n"
            "Your own messages don't count toward the turn limit — you're the reset, "
            "not another consumer of it.",
            ephemeral=True,
        )

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
        voice="How they sound. This is the one that stops them talking like analysts.",
        length="How much they write.",
        naming="What they call themselves.",
        max_chars="Optional hard cap override (100-1900)",
        guidance="Optional extra instruction, e.g. 'no jargon, assume no context'",
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
        naming=[
            app_commands.Choice(name="human — Marlow, Quill, Pike", value="human"),
            app_commands.Choice(name="descriptive — schema-critic, perf-analyst",
                                value="descriptive"),
            app_commands.Choice(name="playful — WaffleIron9000", value="playful"),
            app_commands.Choice(name="crude — FuckFace007 (profanity, no slurs)",
                                value="crude"),
        ],
    )
    async def style(
        interaction: discord.Interaction,
        voice: app_commands.Choice[str],
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

        chosen_length = length.value if length else bus["style"]["length"]
        chosen_naming = naming.value if naming else bus["style"]["naming"]
        await db.set_bus_style(
            bus["bus_id"], chosen_length, voice.value, chosen_naming, max_chars, guidance
        )
        effective = (await db.bus_for_channel(str(interaction.channel_id)))["style"]

        embed = discord.Embed(
            title="Style updated",
            description=f"`{bus['bus_id']}` · #{bus['channel_name']}",
            colour=COLOUR_GREEN,
        )
        embed.add_field(name="Voice", value=f"**{voice.value}**", inline=True)
        embed.add_field(name="Length",
                        value=f"**{chosen_length}**\n{effective['max_chars']} char cap",
                        inline=True)
        embed.add_field(name="Naming", value=f"**{chosen_naming}**", inline=True)
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
