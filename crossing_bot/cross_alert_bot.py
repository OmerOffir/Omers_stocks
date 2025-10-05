import sys;sys.path.append(".")
import os, certifi
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())
import os
import json
import re
import asyncio
import logging
from typing import Dict, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from discord_stock.token import discord_tok
# -----------------------
# CONFIG
# -----------------------
# Required: set via env var
DISCORD_TOKEN = f"{discord_tok.dis_1}{discord_tok.dis_2}{discord_tok.dis_3}"

# Required: set the channel ID the bot should LISTEN in
LISTEN_CHANNEL_ID = 1406720397893828760  # <-- replace with your channel ID

# Optional: if you want to send confirmations in a different channel, set this.
# If None, the bot replies in the same channel it listened to.
ALERT_CHANNEL_ID = None  # e.g., 987654321098765432

# Where to persist sent alerts (so we don't duplicate)
CACHE_FILE = r"crossing_bot\seen_alerts.json"

# Accept: "TICKER cross 60.0" or "TICKER cross 60.0 up" / "down"
ALERT_REGEX = re.compile(
    r"^\s*([A-Za-z]{1,10})\s+cross\s+([0-9]*\.?[0-9]+)\s*(up|down)?\s*$",
    re.IGNORECASE,
)

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cross-alert-bot")

# -----------------------
# Persistence helpers
# -----------------------
def load_cache() -> Dict[str, bool]:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load cache: {e!r}")
        return {}

def save_cache(cache: Dict[str, bool]) -> None:
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    os.replace(tmp, CACHE_FILE)

def make_key(ticker: str, level: str, direction: str) -> str:
    # Normalize the tuple into a single cache key
    d = (direction or "").lower()
    t = ticker.upper()
    # Use standardized numeric format so "60" and "60.0" map to the same thing
    try:
        lv = f"{float(level):.4f}".rstrip("0").rstrip(".")
    except:
        lv = level
    return f"{t}|{lv}|{d}"

# -----------------------
# Bot setup
# -----------------------
intents = discord.Intents.default()
intents.message_content = True  # required to read message text
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

seen_alerts = load_cache()

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    try:
        await tree.sync()
        logger.info("Slash commands synced.")
    except Exception as e:
        logger.warning(f"Slash command sync failed: {e!r}")

def parse_alert(msg: str):
    """
    Returns (ticker, level, direction) or None if not matched.
    direction is 'up'|'down'|None
    """
    m = ALERT_REGEX.match(msg)
    if not m:
        return None
    ticker = m.group(1).upper()
    level = m.group(2)
    direction = m.group(3).lower() if m.group(3) else None
    return ticker, level, direction

async def send_confirmation(channel: discord.abc.Messageable, ticker: str, level: str, direction: str | None):
    dir_txt = f" {direction.upper()}" if direction else ""
    # Standardize numeric look
    lvl = f"{float(level):.4f}".rstrip("0").rstrip(".")
    content = f"✅ Alert recorded: **{ticker}** cross **{lvl}**{dir_txt}. (sent once)"
    await channel.send(content)

@bot.event
async def on_message(message: discord.Message):
    # Ignore ourselves and other bots
    if message.author.bot:
        return

    # Only listen in the configured channel
    if message.channel.id != LISTEN_CHANNEL_ID:
        return
    parsed = parse_alert(message.content)
    if not parsed:
        return

    ticker, level, direction = parsed
    key = make_key(ticker, level, direction)

    if seen_alerts.get(key, False):
        # Already sent once; do nothing.
        logger.info(f"Duplicate alert ignored: {key}")
        return

    # Mark as seen and persist
    seen_alerts[key] = True
    save_cache(seen_alerts)

    # Decide where to send confirmation
    dest_channel = message.channel
    if ALERT_CHANNEL_ID and ALERT_CHANNEL_ID != message.channel.id:
        ch = bot.get_channel(ALERT_CHANNEL_ID)
        if ch:
            dest_channel = ch

    await send_confirmation(dest_channel, ticker, level, direction)

# -----------------------
# Admin slash commands
# -----------------------
@tree.command(name="reset_alert", description="Reset a single alert (ticker, level, optional direction).")
@app_commands.describe(ticker="e.g., MAGS", level="e.g., 60 or 60.0", direction="up/down (optional)")
async def reset_alert(interaction: discord.Interaction, ticker: str, level: str, direction: str | None = None):
    key = make_key(ticker, level, direction or "")
    if key in seen_alerts:
        del seen_alerts[key]
        save_cache(seen_alerts)
        await interaction.response.send_message(f"♻️ Reset alert for `{key}`.", ephemeral=True)
    else:
        await interaction.response.send_message(f"ℹ️ No alert found for `{key}`.", ephemeral=True)

@tree.command(name="reset_all_alerts", description="Reset ALL stored alerts (careful!).")
async def reset_all_alerts(interaction: discord.Interaction):
    seen_alerts.clear()
    save_cache(seen_alerts)
    await interaction.response.send_message("🧹 Cleared all stored alerts.", ephemeral=True)

# -----------------------
# Entry
# -----------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Please set DISCORD_TOKEN environment variable.")
    bot.run(DISCORD_TOKEN)
