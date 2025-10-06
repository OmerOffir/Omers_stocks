# tenk_monitor_cog.py
# Cog: Ten-K monitor (embed-only report, no threads). Auto-deletes temp *_analysis.txt.
from __future__ import annotations

import os
import io
import glob
import asyncio
from datetime import datetime
from typing import List, Optional, Tuple
import os, sys; sys.path.append(".")
import os, certifi
import platform
if platform.system() == "Darwin":
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())

import discord
from discord.ext import commands, tasks
from discord import app_commands
from zoneinfo import ZoneInfo

# ---- your modules ----
from tenk.sec_client import SECClient
from tenk.earnings_client import EarningsClient
from tenk.analyzer import FinancialAnalyzer
from tenk.monitor import TenKMonitor


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _load_watchlist(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        tickers = [ln.strip().upper() for ln in f if ln.strip()]
    seen, uniq = set(), []
    for t in tickers:
        if t not in seen:
            seen.add(t); uniq.append(t)
    return uniq


def _read_text(path: str, limit: int = 3800) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        if len(txt) > limit:
            txt = txt[:limit-1] + "…"
        return txt
    except Exception:
        return ""


def _find_latest_analysis_file(ticker: str) -> Optional[str]:
    pattern = f"{ticker}_*_analysis.txt"
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


class TenKMonitorCog(commands.Cog):
    """Daily + on-demand 10-K monitor (embed-only)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # config
        self.TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Asia/Jerusalem"))
        self.DAILY_HOUR = _env_int("DAILY_HOUR", 23)
        self.DAILY_MIN  = _env_int("DAILY_MIN", 59)

        # where to post; prefer TENK_CHANNEL_ID, fallback CHANNEL_ID
        self.CHANNEL_ID = _env_int("TENK_CHANNEL_ID", _env_int("CHANNEL_ID", 0))

        self.SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "TenKMonitorBot/1.0 (email@example.com)")
        self.WATCHLIST_FILE = os.getenv("TENK_WATCHLIST_FILE", os.getenv("WATCHLIST_FILE", "stocks_watchlist/10k_stocks.txt"))

        self._last_daily_key: Optional[str] = None

        # note: DO NOT start loops in __init__; use cog_load()

    async def cog_load(self):
        """Called after the cog is added and the bot is ready to set up tasks."""
        # start the minute ticker
        self.daily_runner.start()
        print("[tenk] Cog loaded, daily_runner started.")

    async def cog_unload(self):
        """Called when the cog is being removed; stop loops."""
        self.daily_runner.cancel()
        print("[tenk] Cog unloaded, daily_runner stopped.")

    # ----------- core run/post -----------
    async def _run_monitor_once(self, ticker: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """(analysis_file_path, filing_date_str, filing_link)"""
        def _blocking():
            try:
                sec_client = SECClient(self.SEC_USER_AGENT)
            except TypeError:
                sec_client = SECClient()

            earnings_client = EarningsClient()
            analyzer = FinancialAnalyzer()
            mon = TenKMonitor(sec_client, earnings_client, analyzer, None)

            # single-check version:
            mon.start_monitoring(ticker, True)

            latest_file = _find_latest_analysis_file(ticker)

            filing_date_str, filing_link = None, None
            try:
                meta = sec_client.get_latest_10k(ticker)
                if meta and meta.get("filing_date"):
                    filing_date_str = meta["filing_date"].strftime("%Y-%m-%d")
                if meta:
                    filing_link = meta.get("link")
            except Exception:
                pass

            return latest_file, filing_date_str, filing_link

        return await asyncio.to_thread(_blocking)

    async def _post_embed_only(self, channel: discord.abc.Messageable, ticker: str):
        analysis_file, filing_date, filing_link = await self._run_monitor_once(ticker)

        if not analysis_file:
            await channel.send(f"🔎 `{ticker}` — no analysis found (maybe no fresh 10-K).")
            return

        analysis_text = _read_text(analysis_file, limit=3800)

        emb = discord.Embed(
            title=f"{ticker} — Latest 10-K",
            description=analysis_text or "No analysis text available.",
            color=0x2ECC71,
            timestamp=datetime.utcnow(),
        )
        if filing_date:
            emb.add_field(name="Filed", value=f"`{filing_date}`", inline=True)
        if filing_link:
            emb.add_field(name="Document", value=f"[Open 10-K]({filing_link})", inline=True)
        emb.set_footer(text="10-K Monitor • auto-generated")

        class TenKLinkView(discord.ui.View):
            def __init__(self, link_url: Optional[str]):
                super().__init__(timeout=None)
                if link_url:
                    self.add_item(discord.ui.Button(label="Open 10-K", url=link_url))

        view = TenKLinkView(filing_link)

        await channel.send(embed=emb, view=view, silent=True)

        # cleanup local file
        try:
            if os.path.exists(analysis_file):
                os.remove(analysis_file)
        except Exception as e:
            print(f"[tenk] cleanup failed: {e!r}")

    # ----------- scheduler -----------
    @tasks.loop(minutes=1)
    async def daily_runner(self):
        if not self.CHANNEL_ID:
            return
        now = datetime.now(self.TZ)
        if now.hour != self.DAILY_HOUR or now.minute != self.DAILY_MIN:
            return

        # once per minute key
        key = now.strftime("%Y-%m-%d %H:%M")
        if self._last_daily_key == key:
            return
        self._last_daily_key = key

        channel = self.bot.get_channel(self.CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.CHANNEL_ID)
            except Exception as e:
                print(f"[tenk] cannot fetch channel {self.CHANNEL_ID}: {e!r}")
                return

        watchlist = _load_watchlist(self.WATCHLIST_FILE)
        if not watchlist:
            await channel.send("ℹ️ 10-K watchlist is empty. Add tickers to the configured file.")
            return

        await channel.send(
            f"⏱️ Running daily 10-K check for {len(watchlist)} tickers "
            f"(local {now.strftime('%Y-%m-%d %H:%M')} {now.tzname()})…"
        )

        for t in watchlist:
            try:
                await self._post_embed_only(channel, t)
            except Exception as e:
                await channel.send(f"⚠️ `{t}` failed: `{e}`")

    @daily_runner.before_loop
    async def _before_daily(self):
        await self.bot.wait_until_ready()

    # ----------- commands -----------
    @app_commands.command(name="run10k", description="Run a 10-K analysis now for a specific ticker.")
    async def run10k(self, interaction: discord.Interaction, ticker: str):
        await interaction.response.defer(thinking=True)
        ticker = ticker.strip().upper()
        ch = interaction.channel
        if ch is None:
            await interaction.followup.send("No channel context.", ephemeral=True)
            return
        await self._post_embed_only(ch, ticker)
        await interaction.followup.send(f"✅ Done for `{ticker}`", ephemeral=True)



async def setup(bot: commands.Bot):
    await bot.add_cog(TenKMonitorCog(bot))
