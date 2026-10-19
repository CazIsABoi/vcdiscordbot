from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("tempvc.pins")

MAX_PINS_REACHED = 30003  # Discord error code

# Accepts a bare message ID or a message link (discord.com, ptb., canary.).
MESSAGE_RE = re.compile(r"^(?:https?://(?:\w+\.)?discord(?:app)?\.com/channels/\d+/(\d+)/)?(\d+)/?$")


class ThreadPins(commands.Cog):
    """Lets a thread's creator pin and unpin messages in their own thread."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pin_menu = app_commands.ContextMenu(name="Pin or unpin", callback=self.pin_context)
        self.pin_menu.guild_only = True

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.pin_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.pin_menu.name, type=self.pin_menu.type)

    async def _require_thread_owner(self, interaction: discord.Interaction) -> discord.Thread | None:
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("Run this inside a thread.", ephemeral=True)
            return None
        if thread.owner_id != interaction.user.id:
            await interaction.response.send_message(
                "Only the thread's creator can pin messages with this.", ephemeral=True
            )
            return None
        return thread

    async def _fetch_message(
        self, interaction: discord.Interaction, thread: discord.Thread, value: str
    ) -> discord.Message | None:
        match = MESSAGE_RE.match(value.strip())
        if match is None or (match[1] is not None and int(match[1]) != thread.id):
            await interaction.response.send_message(
                "Give a message ID or link from this thread. Easier: right-click the message → "
                "**Apps → Pin or unpin**.",
                ephemeral=True,
            )
            return None
        try:
            return await thread.fetch_message(int(match[2]))
        except discord.NotFound:
            await interaction.response.send_message("Couldn't find that message in this thread.", ephemeral=True)
            return None

    async def _set_pinned(self, interaction: discord.Interaction, message: discord.Message, pinned: bool) -> None:
        if message.pinned == pinned:
            await interaction.response.send_message(
                f"That message is already {'pinned' if pinned else 'unpinned'}.", ephemeral=True
            )
            return

        reason = f"{'Pinned' if pinned else 'Unpinned'} by thread creator {interaction.user} ({interaction.user.id})"
        try:
            if pinned:
                await message.pin(reason=reason)
            else:
                await message.unpin(reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to pin messages in this thread. Ask an admin to give my role "
                "**Pin Messages** (or **Manage Messages**).",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            if e.code == MAX_PINS_REACHED:
                await interaction.response.send_message(
                    "This thread has hit Discord's pin limit. Unpin something first.", ephemeral=True
                )
                return
            log.exception("Failed to %s message %s", "pin" if pinned else "unpin", message.id)
            await interaction.response.send_message("Something went wrong. Try again.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"{'Pinned' if pinned else 'Unpinned'} {message.jump_url}.", ephemeral=True
        )

    @app_commands.command(name="pin", description="Pin a message in a thread you created.")
    @app_commands.describe(message="The message's link or ID.")
    @app_commands.guild_only()
    async def pin(self, interaction: discord.Interaction, message: str) -> None:
        thread = await self._require_thread_owner(interaction)
        if thread is None:
            return
        target = await self._fetch_message(interaction, thread, message)
        if target is not None:
            await self._set_pinned(interaction, target, True)

    @app_commands.command(name="unpin", description="Unpin a message in a thread you created.")
    @app_commands.describe(message="The message's link or ID.")
    @app_commands.guild_only()
    async def unpin(self, interaction: discord.Interaction, message: str) -> None:
        thread = await self._require_thread_owner(interaction)
        if thread is None:
            return
        target = await self._fetch_message(interaction, thread, message)
        if target is not None:
            await self._set_pinned(interaction, target, False)

    async def pin_context(self, interaction: discord.Interaction, message: discord.Message) -> None:
        thread = await self._require_thread_owner(interaction)
        if thread is None:
            return
        await self._set_pinned(interaction, message, not message.pinned)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ThreadPins(bot))
