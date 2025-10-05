#!/usr/bin/env python3
import re
import platform
import sys
import os, sys; sys.path.append(".")
import os, certifi
if platform.system() == "Darwin":
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())
import time
import requests
import discord
from discord.ext import commands
from discord import app_commands
from typing import Dict, Optional
from discord_stock.token import discord_tok

# --- CONFIG ---
# Strongly recommended: do NOT hardcode secrets. Use env vars.
FINNHUB_TOKEN = "d36ipd9r01qumnp5up8gd36ipd9r01qumnp5up90"
DISCORD_TOKEN = f"{discord_tok.dis_1}{discord_tok.dis_2}{discord_tok.dis_3}"
CHANNEL_ID = "1423996530641076315" 

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # needed to read plain tickers in messages

TICKER_REGEX = re.compile(r"\$?([A-Z]{1,5})(?:\b|$)")

class FinnhubError(Exception):
    pass

def weighted_score(counts: Dict[str, int]) -> float:
    weights = {"strongBuy": 2, "buy": 1, "hold": 0, "sell": -1, "strongSell": -2}
    total = sum(counts.get(k, 0) for k in weights)
    if total == 0:
        return 0.0
    score = sum(weights[k] * counts.get(k, 0) for k in weights) / float(total)
    return round(score, 3)

def score_to_label(score: float) -> str:
    if score >= 1.0:
        return "Strong Buy"
    if score >= 0.5:
        return "Buy"
    if score > -0.5:
        return "Hold"
    if score > -1.0:
        return "Sell"
    return "Strong Sell"

def label_to_color(label: str) -> int:
    COLORS = {
        "Strong Buy": 0x0ECb81,  # bright green
        "Buy":        0x20C997,  # green
        "Hold":       0xFFC107,  # amber
        "Sell":       0xE03131,  # red
        "Strong Sell":0xC92A2A,  # dark red
    }
    return COLORS.get(label, 0x99A2AD)

def decorate_title(symbol: str, label: str) -> str:
    if label == "Strong Buy":
        return f"{symbol} — ⭐ Strong Buy"
    if label == "Strong Sell":
        return f"{symbol} — ⚠️ Strong Sell"
    if label == "Buy":
        return f"{symbol} — ✅ Buy"
    if label == "Sell":
        return f"{symbol} — ❌ Sell"
    return f"{symbol} — ⏸ Hold"

def fetch_latest_reco(symbol: str, token: Optional[str] = None, retries: int = 2) -> Dict:
    token = token or FINNHUB_TOKEN
    if not token or token == "REPLACE_ME_OR_SET_ENV":
        raise FinnhubError("Missing FINNHUB_TOKEN (set env var FINNHUB_TOKEN).")

    url = "https://finnhub.io/api/v1/stock/recommendation"
    params = {"symbol": symbol.upper(), "token": token}

    last_err: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                raise FinnhubError("Rate limited by Finnhub (HTTP 429)")
            if not r.ok:
                raise FinnhubError(f"HTTP {r.status_code}: {r.text}")
            data = r.json()
            if not isinstance(data, list) or not data:
                raise FinnhubError("No recommendation data")
            return data[0]  # newest first
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(1.5 * (2 ** i))
    assert last_err is not None
    raise last_err

def fetch_price_target(symbol: str, token: Optional[str] = None) -> Optional[Dict]:
    token = token or FINNHUB_TOKEN
    if not token or token == "REPLACE_ME_OR_SET_ENV":
        return None
    url = "https://finnhub.io/api/v1/stock/price-target"
    params = {"symbol": symbol.upper(), "token": token}
    try:
        r = requests.get(url, params=params, timeout=10)
        if not r.ok:
            return None
        data = r.json()
        if "targetMean" in data and data.get("targetMean") is not None:
            return data
    except Exception:
        pass
    return None

def build_embed(symbol: str, latest: Dict, pt: Optional[Dict]) -> discord.Embed:
    counts = {
        "strongBuy": int(latest.get("strongBuy", 0)),
        "buy": int(latest.get("buy", 0)),
        "hold": int(latest.get("hold", 0)),
        "sell": int(latest.get("sell", 0)),
        "strongSell": int(latest.get("strongSell", 0)),
    }
    score = weighted_score(counts)
    label = score_to_label(score)

    title = decorate_title(symbol, label)
    color = label_to_color(label)
    period = latest.get("period", "N/A")

    desc_lines = [
        f"**Analyst consensus:** `{label}`",
        f"**Weighted score:** `{score}` (range -2..+2)",
        f"**Period:** `{period}`",
        "",
        f"**Votes** —  SB: `{counts['strongBuy']}`  |  B: `{counts['buy']}`  |  H: `{counts['hold']}`  |  S: `{counts['sell']}`  |  SS: `{counts['strongSell']}`",
    ]

    # 🎯 Add price targets section if available
    if pt:
        mean_t = pt.get("targetMean")
        high_t = pt.get("targetHigh")
        low_t  = pt.get("targetLow")
        updated = pt.get("lastUpdated", "N/A")

        if any(v is not None for v in (mean_t, high_t, low_t)):
            parts = []
            if mean_t is not None:
                parts.append(f"`{float(mean_t):.2f}` (mean)")
            if high_t is not None:
                parts.append(f"High `{float(high_t):.2f}`")
            if low_t is not None:
                parts.append(f"Low `{float(low_t):.2f}`")
            desc_lines += ["", f"🎯 **12-mo Target:** " + " | ".join(parts), f"_Updated: {updated}_"]

        # Only show numeric values if present
        if mean_t and high_t and low_t:
            desc_lines += [
                "",
                f"🎯 **12-mo Target:** `${mean_t}` (mean) | High `${high_t}` | Low `${low_t}`",
                f"_Updated: {updated}_",
            ]

    embed = discord.Embed(
        title=title,
        description="\n".join(desc_lines),
        color=color,
    )
    embed.set_footer(text="Source: Finnhub • /rec <ticker>")
    embed.timestamp = discord.utils.utcnow()

    # Optional thumbnails for extreme ratings
    if label == "Strong Buy":
        embed.set_thumbnail(url="https://emoji.slack-edge.com/T02JZ2LQJ/rocket/2d9c7c2b7.png")
    elif label == "Strong Sell":
        embed.set_thumbnail(url="https://emoji.slack-edge.com/T02JZ2LQJ/skull/3d2e7c5f6.png")

    return embed

async def send_to_channel(bot: commands.Bot, embed: discord.Embed) -> None:
    """Send embed to the configured CHANNEL_ID (env: DISCORD_CHANNEL_ID)."""
    if not CHANNEL_ID:
        print("⚠️ DISCORD_CHANNEL_ID not set; cannot route message.")
        return

    # Try cache first
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)  # API fetch fallback
        except Exception as e:
            print(f"⚠️ Could not fetch channel {CHANNEL_ID}: {e}")
            return

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        print(f"⚠️ Missing permissions to send messages in channel {CHANNEL_ID}")
    except Exception as e:
        print(f"⚠️ Failed to send to channel {CHANNEL_ID}: {e}")

class AnalystRec(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="rec", description="Get analyst recommendation for a ticker")
    @app_commands.describe(ticker="Stock ticker, e.g. AAPL")
    async def rec(self, interaction: discord.Interaction, ticker: str):
        await interaction.response.defer(thinking=True, ephemeral=True)  # ephemeral status
        symbol = ticker.strip().upper()
        try:
            latest = fetch_latest_reco(symbol)
            pt = fetch_price_target(symbol)
            embed = build_embed(symbol, latest, pt)
            # Always send to the configured channel
            await send_to_channel(self.bot, embed)
            if CHANNEL_ID:
                await interaction.followup.send(f"✅ Sent recommendation for **{symbol}** to <#{CHANNEL_ID}>", ephemeral=True)
            else:
                await interaction.followup.send(f"⚠️ DISCORD_CHANNEL_ID not set. Nothing was posted.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages and DMs
        if message.author.bot or not message.guild:
            return
        
        # Optional: only react if the message is in the same configured channel
        # if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        #     return

        m = TICKER_REGEX.fullmatch(message.content.strip())
        if not m:
            return
        symbol = m.group(1).upper()
        try:
            latest = fetch_latest_reco(symbol)
            pt = fetch_price_target(symbol)
            embed = build_embed(symbol, latest, pt)
            await send_to_channel(self.bot, embed)  # route to target channel
        except Exception as e:
            try:
                await message.channel.send(f"Error: {e}")
            except Exception:
                pass

class AnalystBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)

    async def setup_hook(self) -> None:
        await self.add_cog(AnalystRec(self))
        try:
            await self.tree.sync()
        except Exception as e:
            print(f"Slash command sync warning: {e}")

def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN.")
    bot = AnalystBot()
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
