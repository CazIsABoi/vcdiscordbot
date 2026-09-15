from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..db import Database

log = logging.getLogger("tempvc")


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return interaction.user.guild_permissions.manage_guild
    return app_commands.check(predicate)


class TempVoice(commands.Cog):
    """Join a 'create-vc' channel to get your own temporary voice channel, VoiceMaster-style."""

    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db

    # ---------- Owner-only guard for control commands ----------

    async def _require_owner(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        if not isinstance(interaction.user, discord.Member) or interaction.user.voice is None:
            await interaction.response.send_message(
                "You need to be in your temporary voice channel to use this.", ephemeral=True
            )
            return None

        channel = interaction.user.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("That's not a voice channel.", ephemeral=True)
            return None

        owner_id = await self.db.get_temp_channel_owner(channel.id)
        if owner_id is None:
            await interaction.response.send_message(
                "You're not in a temporary voice channel.", ephemeral=True
            )
            return None

        if owner_id != interaction.user.id and not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "Only the channel owner can do that.", ephemeral=True
            )
            return None

        return channel

    # ---------- Core: join-to-create ----------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild = member.guild
        config = await self.db.get_guild_config(guild.id)

        # Someone joined the configured "create-vc" channel: spin up a new temp channel.
        if config is not None and after.channel is not None and after.channel.id == config.create_channel_id:
            await self._create_temp_channel(member, config)

        # Someone left a channel: clean it up if it's an empty temp channel.
        if before.channel is not None and before.channel != after.channel:
            if await self.db.is_temp_channel(before.channel.id):
                await self._maybe_delete_channel(before.channel)

    async def _create_temp_channel(self, member: discord.Member, config) -> None:
        guild = member.guild
        category = guild.get_channel(config.category_id) if config.category_id else None
        if category is not None and not isinstance(category, discord.CategoryChannel):
            category = None

        name = config.name_template.format(user=member.display_name)[:100]

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(),
            member: discord.PermissionOverwrite(
                manage_channels=True,
                move_members=True,
                connect=True,
                speak=True,
            ),
        }

        try:
            channel = await guild.create_voice_channel(
                name=name,
                category=category,
                user_limit=config.default_user_limit,
                overwrites=overwrites,
                reason=f"Temporary voice channel for {member} ({member.id})",
            )
            await member.move_to(channel, reason="Moved to their new temporary voice channel")
        except discord.Forbidden:
            log.warning(
                "Missing permissions to create/move into a temp channel in guild %s", guild.id
            )
            return
        except discord.HTTPException:
            log.exception("Failed to create temp voice channel in guild %s", guild.id)
            return

        await self.db.add_temp_channel(channel.id, guild.id, member.id)
        log.info("Created temp channel %s for %s in guild %s", channel.id, member.id, guild.id)

    async def _maybe_delete_channel(self, channel: discord.VoiceChannel) -> None:
        # Re-fetch member count; the cached channel object may be stale right after the event fires.
        fresh = channel.guild.get_channel(channel.id)
        if fresh is None:
            await self.db.remove_temp_channel(channel.id)
            return
        if len(fresh.members) == 0:
            try:
                await fresh.delete(reason="Temporary voice channel is empty")
            except discord.HTTPException:
                log.exception("Failed to delete empty temp channel %s", channel.id)
            await self.db.remove_temp_channel(channel.id)
            log.info("Deleted empty temp channel %s", channel.id)

    # ---------- Setup command ----------

    setup_group = app_commands.Group(
        name="tempvc-setup", description="Configure the temporary voice channel system."
    )

    @setup_group.command(name="init", description="Create the join-to-create channel and category.")
    @app_commands.describe(
        channel_name="Name of the voice channel members join to get a temp channel.",
        category_name="Name of the category to put the join channel and temp channels in.",
        default_user_limit="Default user limit for new temp channels (0 = unlimited).",
    )
    @is_admin()
    async def setup_init(
        self,
        interaction: discord.Interaction,
        channel_name: str = "Create VC",
        category_name: str = "Voice Channels",
        default_user_limit: app_commands.Range[int, 0, 99] = 0,
    ) -> None:
        guild = interaction.guild
        assert guild is not None

        category = discord.utils.get(guild.categories, name=category_name)
        if category is None:
            category = await guild.create_category(category_name, reason="Temp VC setup")

        create_channel = await guild.create_voice_channel(
            channel_name, category=category, reason="Temp VC setup"
        )

        await self.db.set_guild_config(
            guild.id, create_channel.id, category.id, default_user_limit=default_user_limit
        )
        await interaction.response.send_message(
            f"Done. Join {create_channel.mention} to get your own temporary voice channel.",
            ephemeral=True,
        )

    @setup_group.command(name="use-existing", description="Use an existing voice channel as the join-to-create channel.")
    @app_commands.describe(channel="The existing voice channel members should join.")
    @is_admin()
    async def setup_use_existing(
        self, interaction: discord.Interaction, channel: discord.VoiceChannel
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        category_id = channel.category.id if channel.category else None
        await self.db.set_guild_config(guild.id, channel.id, category_id)
        await interaction.response.send_message(
            f"Done. Join {channel.mention} to get your own temporary voice channel.",
            ephemeral=True,
        )

    @setup_group.command(name="disable", description="Disable the temporary voice channel system.")
    @is_admin()
    async def setup_disable(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        await self.db.delete_guild_config(guild.id)
        await interaction.response.send_message("Temporary voice channels disabled.", ephemeral=True)

    # ---------- Owner controls (VoiceMaster-style) ----------

    @app_commands.command(name="lock", description="Lock your temporary voice channel.")
    async def lock(self, interaction: discord.Interaction) -> None:
        channel = await self._require_owner(interaction)
        if channel is None:
            return
        await channel.set_permissions(channel.guild.default_role, connect=False)
        await interaction.response.send_message("Channel locked.", ephemeral=True)

    @app_commands.command(name="unlock", description="Unlock your temporary voice channel.")
    async def unlock(self, interaction: discord.Interaction) -> None:
        channel = await self._require_owner(interaction)
        if channel is None:
            return
        await channel.set_permissions(channel.guild.default_role, connect=True)
        await interaction.response.send_message("Channel unlocked.", ephemeral=True)

    @app_commands.command(name="limit", description="Set the user limit for your temporary voice channel.")
    @app_commands.describe(limit="Max users allowed (0 for unlimited).")
    async def limit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 0, 99]) -> None:
        channel = await self._require_owner(interaction)
        if channel is None:
            return
        await channel.edit(user_limit=limit)
        await interaction.response.send_message(f"User limit set to {limit or 'unlimited'}.", ephemeral=True)

    @app_commands.command(name="rename", description="Rename your temporary voice channel.")
    @app_commands.describe(name="New channel name.")
    async def rename(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 100]) -> None:
        channel = await self._require_owner(interaction)
        if channel is None:
            return
        await channel.edit(name=name)
        await interaction.response.send_message(f"Channel renamed to **{name}**.", ephemeral=True)

    @app_commands.command(name="kick", description="Kick a member from your temporary voice channel.")
    @app_commands.describe(member="The member to kick.")
    async def kick(self, interaction: discord.Interaction, member: discord.Member) -> None:
        channel = await self._require_owner(interaction)
        if channel is None:
            return
        if member.voice is None or member.voice.channel != channel:
            await interaction.response.send_message("That member isn't in your channel.", ephemeral=True)
            return
        await member.move_to(None, reason=f"Kicked from temp channel by {interaction.user}")
        await interaction.response.send_message(f"Kicked {member.mention}.", ephemeral=True)

    @app_commands.command(name="permit", description="Allow a member to join your locked channel.")
    @app_commands.describe(member="The member to permit.")
    async def permit(self, interaction: discord.Interaction, member: discord.Member) -> None:
        channel = await self._require_owner(interaction)
        if channel is None:
            return
        await channel.set_permissions(member, connect=True)
        await interaction.response.send_message(f"{member.mention} can now join.", ephemeral=True)

    @app_commands.command(name="reject", description="Disallow a member from joining your channel.")
    @app_commands.describe(member="The member to reject.")
    async def reject(self, interaction: discord.Interaction, member: discord.Member) -> None:
        channel = await self._require_owner(interaction)
        if channel is None:
            return
        await channel.set_permissions(member, connect=False)
        if member.voice is not None and member.voice.channel == channel:
            await member.move_to(None, reason=f"Rejected from temp channel by {interaction.user}")
        await interaction.response.send_message(f"{member.mention} can no longer join.", ephemeral=True)

    @app_commands.command(name="transfer", description="Transfer ownership of your temporary voice channel.")
    @app_commands.describe(member="The member to become the new owner.")
    async def transfer(self, interaction: discord.Interaction, member: discord.Member) -> None:
        channel = await self._require_owner(interaction)
        if channel is None:
            return
        if member.voice is None or member.voice.channel != channel:
            await interaction.response.send_message(
                "The new owner needs to be in your channel.", ephemeral=True
            )
            return
        await self.db.set_temp_channel_owner(channel.id, member.id)
        await channel.set_permissions(
            member, manage_channels=True, move_members=True, connect=True, speak=True
        )
        await interaction.response.send_message(f"Ownership transferred to {member.mention}.", ephemeral=True)

    @app_commands.command(name="claim", description="Claim ownership of a temporary voice channel whose owner left.")
    async def claim(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.user.voice is None:
            await interaction.response.send_message(
                "You need to be in a temporary voice channel to claim it.", ephemeral=True
            )
            return
        channel = interaction.user.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("That's not a voice channel.", ephemeral=True)
            return

        owner_id = await self.db.get_temp_channel_owner(channel.id)
        if owner_id is None:
            await interaction.response.send_message("This isn't a temporary voice channel.", ephemeral=True)
            return

        owner_present = any(m.id == owner_id for m in channel.members)
        if owner_present:
            await interaction.response.send_message(
                "The current owner is still in the channel.", ephemeral=True
            )
            return

        await self.db.set_temp_channel_owner(channel.id, interaction.user.id)
        await channel.set_permissions(
            interaction.user, manage_channels=True, move_members=True, connect=True, speak=True
        )
        await interaction.response.send_message("You're now the owner of this channel.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    db: Database = bot.db  # type: ignore[attr-defined]
    await bot.add_cog(TempVoice(bot, db))
