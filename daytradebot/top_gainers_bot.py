import sys;sys.path.append(".")
import os, certifi
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())


import requests
import pandas as pd
import discord
from discord.ext import commands
from discord import app_commands
from discord_stock.token import discord_tok

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


GAINERS_URL = "https://stockanalysis.com/markets/gainers/"
TZ = ZoneInfo("Asia/Jerusalem")  # Your timezone

# ====== CONFIG VIA ENV VARS ======
# export DISCORD_TOKEN="xxxxxxxx"
# export DISCORD_CHANNEL_ID="123456789012345678"

DISCORD_TOKEN = f"{discord_tok.dis_1}{discord_tok.dis_2}{discord_tok.dis_3}"
CHANNEL_ID = int("1411064822635696188") 

# ---------- Data Fetch ----------

def get_top_gainers(limit: int = 20) -> pd.DataFrame:
    """
    Fetch today's top stock gainers from StockAnalysis and return a tidy DataFrame.
    Columns: Symbol, Company, % Change, Price, Volume, Market Cap (if present).
    """
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    }
    r = requests.get(GAINERS_URL, headers=headers, timeout=30)
    r.raise_for_status()

    # Try lxml first; if missing, fall back to html5lib (so it works on Raspberry Pi)
    for flavor in (None, "lxml", "html5lib"):
        try:
            tables = pd.read_html(r.text, flavor=None if flavor is None else flavor)
            if tables:
                df_raw = tables[0].copy()
                break
        except Exception:
            df_raw = None
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
    keep_cols = [c for c in ["No.","Symbol","Company","% Change","Price","Volume","Market Cap"] if c in df]
    df = df[keep_cols].copy()

    # Clean numerics (keep strings pretty for the table but ensure sortability if needed)
    def pct_to_float(s):
        return pd.to_numeric(s.astype(str).str.replace("%","", regex=False), errors="coerce")

    def money_to_float(s):
        return pd.to_numeric(
            s.astype(str).str.replace("$","", regex=False).str.replace(",","", regex=False),
            errors="coerce"
        )

    if "% Change" in df: df["% Change"] = pct_to_float(df["% Change"])
    if "Price" in df:    df["Price"]    = money_to_float(df["Price"])
    if "Volume" in df:   df["Volume"]   = pd.to_numeric(df["Volume"].astype(str).str.replace(",","", regex=False), errors="coerce")

    if limit is not None:
        df = df.head(limit).reset_index(drop=True)

    return df


# ---------- Pretty Table Formatting ----------

def fmt_num(n, sig=3):
    if pd.isna(n):
        return "-"
    # Compact human format (e.g., 1.2K, 3.4M, 2.1B)
    absn = abs(n)
    if absn >= 1_000_000_000:
        return f"{n/1_000_000_000:.{sig}g}B"
    if absn >= 1_000_000:
        return f"{n/1_000_000:.{sig}g}M"
    if absn >= 1_000:
        return f"{n/1_000:.{sig}g}K"
    return f"{n:.{sig}g}"


def human_price(x):
    return "-" if pd.isna(x) else f"${x:,.2f}"

def human_num(n):
    if pd.isna(n): return "-"
    n = float(n)
    for unit, div in [("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)]:
        if abs(n) >= div:
            return f"{n/div:.2f}{unit}"
    return f"{n:,.0f}"

def build_embed_fields(df: pd.DataFrame, max_rows: int = 12):
    """
    Returns a list of dicts: [{'name': ..., 'value': ..., 'inline': True}, ...]
    Layout: each row is an inline field:
      **TICKER**  +12.34%   |  $Price
      Company name
      Vol 123.4M • Mcap 5.6B
    """
    fields = []
    df = df.head(max_rows).copy()

    for _, row in df.iterrows():
        sym = str(row.get("Symbol", "-"))
        comp = str(row.get("Company", "-"))
        chg = row.get("% Change", None)
        price = row.get("Price", None)
        vol = row.get("Volume", None)
        mcap = row.get("Market Cap", None)

        chg_str = "-" if pd.isna(chg) else f"+{chg:.2f}%"
        price_str = human_price(price)
        vol_str = human_num(vol)
        mcap_str = str(mcap) if isinstance(mcap, str) and any(s in mcap for s in ["B","M","K"]) else human_num(mcap)

        # Header line (bold ticker + highlighted change)
        name = f"**{sym}**  `⬆ {chg_str}`   •  `{price_str}`"
        # Body line (company + small stats)
        value = f"{comp}\nVol {vol_str} • Mcap {mcap_str}"
        fields.append({"name": name, "value": value, "inline": True})

    return fields



# ---------- Discord Bot ----------

intents = discord.Intents.default()
intents.message_content = True  # needed to respond to "top"

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=TZ)


async def post_top_gainers():
    """Fetch and post the current top gainers to the configured channel (rich embed)."""
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found or not cached yet.")
        return

    try:
        df = get_top_gainers(limit=20)
        fields = build_embed_fields(df, max_rows=12)

        now = datetime.now(TZ)
        embed = discord.Embed(
            title=f"Top Gainers — {now.strftime('%Y-%m-%d %H:%M %Z')}",
            url=GAINERS_URL,
            description="**Bold tickers** • `% change` • `price`\n(hover/click title for source)",
            color=0x2ecc71
        )

        # Add fields, three per row (Discord auto-wraps inline fields in rows)
        for f in fields:
            embed.add_field(name=f["name"], value=f["value"], inline=True)

        embed.set_footer(text="Source: stockanalysis.com • Symbols are not recommendations")
        await channel.send(embed=embed)

    except Exception as e:
        await channel.send(f"⚠️ Failed to fetch top gainers: `{e}`")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    print(f"Posting to channel ID: {CHANNEL_ID}")

    # Schedule jobs: 15:00, 15:30, 16:30, 17:00, 17:30, 18:30, 19:00, 19:30, 20:00, and 01:00
    times = [
        "15:00", "15:30", "16:30", "17:00", "17:30",
        "18:30", "19:00", "19:30", "20:00", "01:00"
    ]
    for t in times:
        hh, mm = t.split(":")
        scheduler.add_job(post_top_gainers, CronTrigger(hour=int(hh), minute=int(mm)))
    scheduler.start()


@bot.event
async def on_message(message: discord.Message):
    # Prevent bot from responding to itself
    if message.author == bot.user:
        return

    # On-demand: user types "top" (case-insensitive) anywhere
    if message.content.strip().lower() == "top":
        await post_top_gainers()
        return

    await bot.process_commands(message)


# Optional: slash command /top (rich embed style)
@bot.tree.command(name="top", description="Show the current Top Gainers")
@app_commands.describe(rows="How many rows to show (1-20)")
async def slash_top(
    interaction: discord.Interaction,
    rows: app_commands.Range[int, 1, 20] = 12
):
    await interaction.response.defer(thinking=True)
    try:
        df = get_top_gainers(limit=20)
        fields = build_embed_fields(df, max_rows=rows)

        now = datetime.now(TZ)
        embed = discord.Embed(
            title=f"Top Gainers — {now.strftime('%Y-%m-%d %H:%M %Z')}",
            url=GAINERS_URL,
            description="**Bold tickers** • `% change` • `price`",
            color=0x2ecc71
        )
        for f in fields:
            embed.add_field(name=f["name"], value=f["value"], inline=True)

        embed.set_footer(text="Source: stockanalysis.com • Symbols are not recommendations")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Failed to fetch top gainers: `{e}`",
            ephemeral=True
        )


def main():
    if not DISCORD_TOKEN or not CHANNEL_ID:
        raise SystemExit("Please set DISCORD_TOKEN and DISCORD_CHANNEL_ID environment variables.")
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
