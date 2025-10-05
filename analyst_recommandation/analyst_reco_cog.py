# daytradebot/analyst_reco_cog.py
import os
import re
import time
import asyncio
from typing import Dict, Optional, List

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import discord
from discord.ext import commands
from discord import app_commands

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

# ---------- Config (env) ----------
FINNHUB_TOKEN = "d36ipd9r01qumnp5up8gd36ipd9r01qumnp5up90"
CHANNEL_ID = int("1423996530641076315")

# Daily scan time (local to Asia/Jerusalem)
DAILY_HOUR = int(os.getenv("ANALYST_DAILY_HOUR", "16"))     # 16:30 by default
DAILY_MIN  = int(os.getenv("ANALYST_DAILY_MIN", "30"))

# Watchlist file (one ticker per line)
WATCHLIST_FILE = "stocks_watchlist/core.txt"

# Accept $AAPL or AAPL (1–5 uppercase letters)
TICKER_REGEX = re.compile(r"\$?([A-Z]{1,5})(?:\b|$)")

# ---------- HTTP Session with retries ----------
_session = requests.Session()
_session.headers.update({"User-Agent": "AnalystRecoBot/1.0 (+discord)"})
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=2,
            backoff_factor=1.25,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
    ),
)

def http_get(url: str, params: dict, timeout: float = 15.0) -> requests.Response:
    return _session.get(url, params=params, timeout=timeout)

# ---------- Domain helpers ----------
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
    colors = {
        "Strong Buy": 0x0ECB81,
        "Buy":        0x20C997,
        "Hold":       0xFFC107,
        "Sell":       0xE03131,
        "Strong Sell":0xC92A2A,
    }
    return colors.get(label, 0x99A2AD)

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

def fetch_latest_reco(symbol: str, token: Optional[str] = None) -> Dict:
    token = token or FINNHUB_TOKEN
    if not token:
        raise FinnhubError("Missing FINNHUB_TOKEN")
    url = "https://finnhub.io/api/v1/stock/recommendation"
    params = {"symbol": symbol.upper(), "token": token}
    r = http_get(url, params=params, timeout=15)
    if r.status_code == 403:
        raise FinnhubError("403 Forbidden: invalid token or plan lacks access to recommendation endpoint.")
    if r.status_code == 429:
        raise FinnhubError("429 Too Many Requests (rate limited).")
    if r.status_code >= 500:
        raise FinnhubError(f"Upstream error {r.status_code} from Finnhub.")
    if not r.ok:
        raise FinnhubError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if not isinstance(data, list) or not data:
        None
    return data[0]  # latest first

def fetch_price_target(symbol: str, token: Optional[str] = None) -> Optional[Dict]:
    token = token or FINNHUB_TOKEN
    if not token:
        return None
    url = "https://finnhub.io/api/v1/stock/price-target"
    params = {"symbol": symbol.upper(), "token": token}
    try:
        r = http_get(url, params=params, timeout=10)
        if r.status_code == 403:
            return None  # plan may not include PT
        if not r.ok:
            return None
        data = r.json() or {}
        if any(k in data and data[k] is not None for k in ("targetMean", "targetHigh", "targetLow")):
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

    embed = discord.Embed(
        title=title,
        description="\n".join(desc_lines),
        color=color,
    )
    embed.set_footer(text="Source: Finnhub • /rec <ticker>")
    embed.timestamp = discord.utils.utcnow()
    if label == "Strong Buy":
        embed.set_thumbnail(url="https://emoji.slack-edge.com/T02JZ2LQJ/rocket/2d9c7c2b7.png")
    elif label == "Strong Sell":
        embed.set_thumbnail(url="https://emoji.slack-edge.com/T02JZ2LQJ/skull/3d2e7c5f6.png")
    return embed

# ---------- Cog ----------
class AnalystRecoCog(commands.Cog):
    """Discord Cog: analyst recommendations via Finnhub + daily watchlist scan + on-demand ticker messages."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.tz = ZoneInfo("Asia/Jerusalem")
        self._start_scheduler_when_ready = asyncio.create_task(self._start_scheduler())

    async def cog_unload(self):
        if self.scheduler:
            self.scheduler.shutdown(wait=False)

    async def _start_scheduler(self):
        await self.bot.wait_until_ready()
        self.scheduler = AsyncIOScheduler(timezone=self.tz)
        self.scheduler.add_job(self._daily_watchlist_job, CronTrigger(hour=DAILY_HOUR, minute=DAILY_MIN))
        self.scheduler.start()
        print(f"[AnalystReco] Daily watchlist job scheduled at {DAILY_HOUR:02d}:{DAILY_MIN:02d} Asia/Jerusalem")
        print(f"[AnalystReco] Using channel ID: {CHANNEL_ID}")
        print(f"[AnalystReco] Watchlist file: {WATCHLIST_FILE}")

    async def _send_embed(self, embed: discord.Embed) -> None:
        if not CHANNEL_ID:
            print("[AnalystReco] DISCORD_CHANNEL_ID not set")
            return
        channel = self.bot.get_channel(CHANNEL_ID)
        if channel is None:
            print(f"[AnalystReco] get_channel None for {CHANNEL_ID}, trying fetch…")
            try:
                channel = await self.bot.fetch_channel(CHANNEL_ID)
            except Exception as e:
                print(f"[AnalystReco] Could not fetch channel {CHANNEL_ID}: {e!r}")
                return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            print(f"[AnalystReco] Missing permissions to send in channel {CHANNEL_ID}")
        except Exception as e:
            print(f"[AnalystReco] Failed to send to channel {CHANNEL_ID}: {e!r}")

    def _read_watchlist(self) -> List[str]:
        tickers: List[str] = []
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    t = line.strip().upper()
                    if not t or t.startswith("#"):
                        continue
                    m = TICKER_REGEX.fullmatch(t)
                    tickers.append(m.group(1) if m else t)
        except FileNotFoundError:
            print(f"[AnalystReco] Watchlist not found: {WATCHLIST_FILE}")
        except Exception as e:
            print(f"[AnalystReco] Error reading watchlist: {e!r}")
        return tickers

    async def _daily_watchlist_job(self):
        if not FINNHUB_TOKEN:
            print("[AnalystReco] FINNHUB_TOKEN missing; skipping daily job.")
            return
        tickers = self._read_watchlist()
        if not tickers:
            print("[AnalystReco] Watchlist is empty; nothing to do.")
            return

        print(f"[AnalystReco] Daily job: checking {len(tickers)} tickers...")
        for tkr in tickers:
            try:
                latest = fetch_latest_reco(tkr)
                pt = fetch_price_target(tkr)
                embed = build_embed(tkr, latest, pt)
                await self._send_embed(embed)
            except Exception as e:
                print(f"[AnalystReco] {tkr} failed: {e!r}")
            await asyncio.sleep(0.7)  # avoid rate limits

    # -------- Slash command --------
    @app_commands.command(name="rec", description="Get analyst recommendation for a ticker")
    @app_commands.describe(ticker="Stock ticker, e.g. AAPL")
    async def rec(self, interaction: discord.Interaction, ticker: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        symbol = ticker.strip().upper()
        try:
            latest = fetch_latest_reco(symbol)
            pt = fetch_price_target(symbol)
            embed = build_embed(symbol, latest, pt)
            await self._send_embed(embed)
            await interaction.followup.send(f"✅ Sent recommendation for **{symbol}** to <#{CHANNEL_ID}>", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    # -------- On-demand: user types a ticker in any channel --------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore ourselves and DMs
        if message.author.bot or not message.guild:
            return

        content = (message.content or "").strip().upper()
        if not content:
            return

        # Find first ticker token in the message
        m = TICKER_REGEX.fullmatch(content)
        if not m:
            # Also support messages that contain extra text, e.g. "check AAPL pls"
            in_text = list(TICKER_REGEX.finditer(content))
            if not in_text:
                return
            symbol = in_text[0].group(1)
        else:
            symbol = m.group(1)

        try:
            latest = fetch_latest_reco(symbol)
            pt = fetch_price_target(symbol)
            embed = build_embed(symbol, latest, pt)
            await self._send_embed(embed)  # always route to configured channel
        except Exception as e:
            # Try to inform in the same channel, but don't crash if perms fail
            try:
                await message.channel.send(f"Error fetching {symbol}: {e}")
            except Exception:
                pass

async def setup(bot: commands.Bot):
    await bot.add_cog(AnalystRecoCog(bot))
