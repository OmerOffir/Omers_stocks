
import re
import platform
import sys
import os, sys; sys.path.append(".")
import os, certifi
if platform.system() == "Darwin":
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())
import asyncio
import discord
from discord.ext import commands
from discord_stock.token import discord_tok

TOKEN = f"{discord_tok.dis_1}{discord_tok.dis_2}{discord_tok.dis_3}"  # set your bot token in environment variable

intents = discord.Intents.default()
intents.members = True  # enable in Developer Portal → Bot → Privileged Gateway Intents

bot = commands.Bot(command_prefix="!", intents=intents)
EXTENSIONS = ["private_rooms_cog"]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Slash sync error:", e)


async def main():
    async with bot:
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        await bot.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Please set DISCORD_TOKEN environment variable.")
    asyncio.run(main())
