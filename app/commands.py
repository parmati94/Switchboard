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

import discord
from discord import app_commands

from .db import new_bus_secret
from .egress import ensure_bus_webhook

log = logging.getLogger("switchboard.commands")


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
            "Read the root path first.\n```\n"
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
        state = "enabled" if bus["enabled"] else "disabled"
        speaks = "yes" if bus["webhook_url"] else "no — missing Manage Webhooks"

        # Reading is the failure mode that hides: posting still works without it.
        listens = "unknown"
        me = interaction.guild.me if interaction.guild else None
        if me is not None and isinstance(interaction.channel, discord.TextChannel):
            perms = interaction.channel.permissions_for(me)
            if perms.view_channel and perms.read_message_history:
                listens = "yes"
            else:
                listens = "**NO** — I cannot see this channel; nothing is being recorded"

        await interaction.followup.send(
            f"**Bus `{bus['bus_id']}`** — {state}\n"
            f"Channel: <#{bus['channel_id']}>\n"
            f"Can speak: {speaks}\n"
            f"Can listen: {listens}\n"
            f"Agents: {len(await db.roster(bus['bus_id']))}\n"
            f"Messages recorded: {stats['messages_stored']}\n"
            f"Cursor head: {stats['head_seq']}\n"
            f"Limits: {bus['limit_turns']} turns / {bus['limit_minutes']} min\n"
            f"Style: {bus['style']['preset']} (max {bus['style']['max_chars']} chars)",
            ephemeral=True,
        )

    @group.command(name="rotate", description="Issue a new bootstrap secret")
    async def rotate(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        secret = new_bus_secret()
        await db.rotate_bus_secret(bus["bus_id"], secret)
        base = settings.public_url.rstrip("/")
        await interaction.followup.send(
            f"**New bootstrap secret** for `{bus['bus_id']}`. The previous one no "
            "longer works, so anything using it must be given this:\n"
            f"```\nJoin the bus at {base} — bootstrap secret is {secret}\n"
            "Read the root path first.\n```",
            ephemeral=True,
        )
        log.info("bus %s secret rotated", bus["bus_id"])

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

        lines = []
        for a in agents:
            dot = "🟢" if a["online"] else "⚪"
            hook = "own webhook" if a["own_webhook"] else "shared webhook"
            lines.append(f"{dot} **{a['id']}** — {hook}")
        await interaction.followup.send(
            f"**{len(agents)} agent(s)** on `{bus['bus_id']}`\n" + "\n".join(lines)
            + "\n\n🟢 = seen in the last 2 minutes.",
            ephemeral=True,
        )

    @group.command(name="revoke", description="Revoke an agent's credentials")
    @app_commands.describe(agent="Agent name, as shown in /switchboard roster")
    async def revoke(interaction: discord.Interaction, agent: str) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
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
        deleted = False
        if egress is not None and revoked.get("webhook_url"):
            try:
                await egress.delete_webhook(revoked["webhook_url"])
                deleted = True
            except Exception:  # noqa: BLE001
                log.exception("failed deleting webhook for %r", agent)

        await interaction.followup.send(
            f"**{agent}** revoked. Its key no longer works"
            + (" and its webhook is deleted." if deleted else ".")
            + "\nIt can rejoin with the bootstrap secret unless you `/switchboard rotate` too.",
            ephemeral=True,
        )
        log.info("agent %r revoked on bus %s", agent, bus["bus_id"])

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

    @group.command(name="style", description="Set how agents write on this bus")
    @app_commands.describe(
        preset="terse = chat-length · normal = a short paragraph · detailed = structured",
        max_chars="Optional hard cap override (100-1900)",
        guidance="Optional extra instruction, e.g. 'plain English, no jargon'",
    )
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="terse — one to three sentences", value="terse"),
            app_commands.Choice(name="normal — a short paragraph", value="normal"),
            app_commands.Choice(name="detailed — structured and thorough", value="detailed"),
        ]
    )
    async def style(
        interaction: discord.Interaction,
        preset: app_commands.Choice[str],
        max_chars: app_commands.Range[int, 100, 1900] | None = None,
        guidance: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        bus = await db.bus_for_channel(str(interaction.channel_id))
        if not bus:
            await interaction.followup.send(_no_bus_message(), ephemeral=True)
            return

        await db.set_bus_style(bus["bus_id"], preset.value, max_chars, guidance)
        effective = (await db.bus_for_channel(str(interaction.channel_id)))["style"]
        await interaction.followup.send(
            f"Style on `{bus['bus_id']}` set to **{preset.value}** "
            f"(hard cap {effective['max_chars']} chars).\n"
            f"> {effective['guidance']}\n\n"
            "Agents receive this at registration and again on every poll, so it "
            "applies immediately — including to ones already running. You don't "
            "need to tell them.",
            ephemeral=True,
        )

    tree.add_command(group)
    return tree
