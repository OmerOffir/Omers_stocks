# bots/gainers_cog.py
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List

import requests
import pandas as pd
import discord
from discord.ext import commands

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands


GAINERS_URL = "https://stockanalysis.com/markets/gainers/"
TZ = ZoneInfo(os.getenv("MARKET_TZ", "Asia/Jerusalem"))

# Where to post (falls back to LISTEN_CHANNEL_ID if not set)
CHANNEL_ID = int("1411064822635696188") 


# --------- scraping ---------
def get_top_gainers(limit: int = 20) -> pd.DataFrame:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    r = requests.get(GAINERS_URL, headers=headers, timeout=30)
    r.raise_for_status()

    df_raw = None
    for flavor in (None, "lxml", "html5lib"):
        try:
            tables = pd.read_html(r.text, flavor=None if flavor is None else flavor)
            if tables:
                df_raw = tables[0].copy()
                break
        except Exception:
            continue
    if df_raw is None:
        raise RuntimeError("Could not parse gainers table. Install lxml or html5lib.")

    col_map = {
        "Company Name": "Company",
        "Stock Price": "Price",
        "% Change": "% Change",
        "Symbol": "Symbol",
        "Volume": "Volume",
        "Market Cap": "Market Cap",
        "No.": "No.",
    }
    df = df_raw.rename(columns=col_map)
    keep = [c for c in ["No.", "Symbol", "Company", "% Change", "Price", "Volume", "Market Cap"] if c in df]
    df = df[keep].copy()

    to_num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce")
    if "% Change" in df:
        df["% Change"] = pd.to_numeric(df["% Change"].astype(str).str.replace("%", "", regex=False), errors="coerce")
    if "Price" in df:
        df["Price"] = pd.to_numeric(
            df["Price"].astype(str).str.replace("$", "", regex=False).str.replace(",", "", regex=False),
            errors="coerce",
        )
    if "Volume" in df:
        df["Volume"] = to_num(df["Volume"])

    return df.head(limit).reset_index(drop=True)


# --------- formatting ---------
def _human_price(x):
    return "-" if pd.isna(x) else f"${x:,.2f}"

def _human_num(n):
    if pd.isna(n):
        return "-"
    n = float(n)
    for unit, div in [("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)]:
        if abs(n) >= div:
            return f"{n/div:.2f}{unit}"
    return f"{n:,.0f}"

def build_embed_fields(df: pd.DataFrame, max_rows: int = 12):
    fields = []
    for _, row in df.head(max_rows).iterrows():
        sym = str(row.get("Symbol", "-"))
        comp = str(row.get("Company", "-"))
        chg = row.get("% Change", None)
        price = row.get("Price", None)
        vol = row.get("Volume", None)
        mcap = row.get("Market Cap", None)

        chg_str = "-" if pd.isna(chg) else f"+{chg:.2f}%"
        price_str = _human_price(price)
        vol_str = _human_num(vol)
        if isinstance(mcap, str) and any(s in mcap for s in ["B", "M", "K"]):
            mcap_str = mcap
        else:
            mcap_str = _human_num(mcap)

        name = f"**{sym}**  `⬆ {chg_str}`   •  `{price_str}`"
        value = f"{comp}\nVol {vol_str} • Mcap {mcap_str}"
        fields.append({"name": name, "value": value, "inline": True})
    return fields


class GainersCog(commands.Cog):
    """
    Posts Top Gainers on a schedule, responds to 'top' messages, and exposes /top.
    Env:
      - GAINERS_CHANNEL_ID (or LISTEN_CHANNEL_ID)
      - GAINERS_TIMES (comma-separated HH:MM list, default your list)
      - MARKET_TZ (default Asia/Jerusalem)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(timezone=TZ)
        default_times = ["16:31","16:35" ,"17:00", "17:30", "18:30", "19:00", "19:30", "20:00", "01:00"]
        raw = os.getenv("GAINERS_TIMES", ",".join(default_times))
        self.times: List[str] = [t.strip() for t in raw.split(",") if t.strip()]

    # --- lifecycle ---
    async def cog_load(self):
        # start fresh to avoid duplicate schedules after reloads
        try:
            self.scheduler.remove_all_jobs()
        except Exception:
            pass

        for t in self.times:
            hh, mm = t.split(":")
            self.scheduler.add_job(
                self._post_top_gainers,
                CronTrigger(hour=int(hh), minute=int(mm), day_of_week="mon-fri", timezone=TZ),
                id=f"gainers-{hh}{mm}",
                replace_existing=True,
                misfire_grace_time=300,
            )
        self.scheduler.start()
        print("[GainersCog] Scheduled jobs:", [j.id for j in self.scheduler.get_jobs()])

    async def cog_unload(self):
        self.scheduler.shutdown(wait=False)
    
    def _is_market_day(self, ts: datetime | None = None) -> bool:
        ts = ts or datetime.now(TZ)
        return ts.weekday() < 5  # 0=Mon ... 4=Fri

    # --- helpers ---
    async def _post_top_gainers(self, interaction: discord.Interaction | None = None, rows: int = 12):
        # pick channel every run
        ch = interaction.channel if interaction else self.bot.get_channel(CHANNEL_ID)
        if not ch:
            print("[GainersCog] Channel not found.")
            return

        now = datetime.now(TZ)
        if not self._is_market_day(now):
            await ch.send("⛔ Market is closed (weekend). Top Gainers run only Mon–Fri.")
            return
        try:
            df = get_top_gainers(limit=max(rows, 12))
            fields = build_embed_fields(df, max_rows=rows)

            now = datetime.now(TZ)
            embed = discord.Embed(
                title=f"Top Gainers — {now.strftime('%Y-%m-%d %H:%M %Z')}",
                url=GAINERS_URL,
                description="**Bold tickers** • `% change` • `price`",
                color=0x2ecc71,
            )
            for f in fields:
                embed.add_field(name=f["name"], value=f["value"], inline=True)
            embed.set_footer(text="Source: stockanalysis.com • Symbols are not recommendations")

            await ch.send(embed=embed)

        except Exception as e:
            if interaction:
                await interaction.followup.send(f"⚠️ Failed to fetch top gainers: `{e}`", ephemeral=True)
            else:
                await ch.send(f"⚠️ Failed to fetch top gainers: `{e}`")

    # --- message trigger: 'top' ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # only react to plain "top"
        if message.content and message.content.strip().lower() == "top":
            await self._post_top_gainers(interaction=None, rows=12)

    # --- slash command: /top ---
    @app_commands.command(name="top", description="Show the current Top Gainers")
    @app_commands.describe(rows="How many rows to show (1-20)")
    async def slash_top(self, interaction: discord.Interaction, rows: app_commands.Range[int, 1, 20] = 12):
        await interaction.response.defer(thinking=True)
        await self._post_top_gainers(interaction=interaction, rows=int(rows))


async def setup(bot: commands.Bot):
    await bot.add_cog(GainersCog(bot))
    # ensure slash commands appear
    try:
        await bot.tree.sync()
        print("[GainersCog] Slash commands synced.")
    except Exception as e:
        print(f"[GainersCog] Slash sync failed: {e!r}")
