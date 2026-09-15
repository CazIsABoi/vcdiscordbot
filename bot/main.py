from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from .db import Database

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tempvc.main")

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.voice_states = True

EXTENSIONS = ["bot.cogs.temp_voice"]


class TempVCBot(commands.Bot):
    def __init__(self, db: Database):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.db = db

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        dev_guild_id = os.getenv("DEV_GUILD_ID")
        if dev_guild_id:
            guild = discord.Object(id=int(dev_guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced application commands to guild %s", dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Synced application commands globally")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable is not set.")

    db_path = os.getenv("DATABASE_PATH", "data/tempvc.db")
    db = Database(db_path)
    await db.connect()

    bot = TempVCBot(db)
    try:
        await bot.start(token)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
