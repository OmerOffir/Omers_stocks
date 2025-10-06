# bot.py
# Discord bot that runs your TenKMonitor daily (and on-demand) and posts results.

import io
import glob
import asyncio
import platform
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Optional, Tuple
import os, sys; sys.path.append(".")
import os, certifi
if platform.system() == "Darwin":
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())

import discord
from discord.ext import commands, tasks

# ---- Your modules ----
from sec_client import SECClient
from earnings_client import EarningsClient
from analyzer import FinancialAnalyzer
from monitor import TenKMonitor
from discord_stock.token import discord_tok

intents = discord.Intents.default()
intents.message_content = True  # <-- REQUIRED for on_message to see "!10k AAPL"
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Config (env-driven) ----------
DISCORD_TOKEN = f"{discord_tok.dis_1}{discord_tok.dis_2}{discord_tok.dis_3}"
CHANNEL_ID = int(1424849157348130826) 
WATCHLIST_FILE = os.getenv("WATCHLIST_FILE", "stocks_watchlist/10k_stocks.txt")

# Daily schedule (Asia/Jerusalem by default)
TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Asia/Jerusalem"))
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "23"))     # 09:00 local by default
DAILY_MIN  = int(os.getenv("DAILY_MIN", "59"))

# Optional: SEC UA (recommended)
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "TenKMonitorBot/1.0 (email@example.com)")

# ---------- Discord Intents ----------
intents = discord.Intents.default()  # no privileged intents needed
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Helpers ----------
def load_watchlist(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        tickers = [ln.strip().upper() for ln in f if ln.strip()]
    # de-dup, keep order
    seen = set()
    uniq = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq

def find_latest_analysis_file(ticker: str) -> Optional[str]:
    # Match files like: AAPL_20250131_analysis.txt
    pattern = f"{ticker}_*_analysis.txt"
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    # Choose the newest by mtime
    latest = max(candidates, key=os.path.getmtime)
    return latest

async def run_monitor_once(ticker: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Run your monitor once (blocking) in a thread, then return:
      (analysis_file_path, filing_date_str, filing_link)
    """
    def _blocking():
        # Build fresh clients each run (simple & stateless)
        # If your SECClient needs User-Agent, pass it here:
        try:
            sec_client = SECClient(SEC_USER_AGENT)  # or SECClient() if your version has no args
        except TypeError:
            # Fallback in case your SECClient has no __init__ args
            sec_client = SECClient()

        earnings_client = EarningsClient()
        analyzer = FinancialAnalyzer()

        mon = TenKMonitor(sec_client, earnings_client, analyzer, None)
        # IMPORTANT: this should be the version that returns immediately (no loop/sleep)
        mon.start_monitoring(ticker, True)  # (ticker, auto_analyze)

        # After monitor runs, locate the newest analysis file for this ticker
        latest_file = find_latest_analysis_file(ticker)

        # Also get latest 10-K meta for message context
        try:
            meta = sec_client.get_latest_10k(ticker)
            filing_date_str = meta["filing_date"].strftime("%Y-%m-%d") if meta and meta.get("filing_date") else None
            filing_link = meta.get("link") if meta else None
        except Exception:
            filing_date_str = None
            filing_link = None

        return latest_file, filing_date_str, filing_link

    return await asyncio.to_thread(_blocking)

# --- no-threads, auto-delete version ---
async def post_result(channel: discord.abc.Messageable, ticker: str):
    analysis_file, filing_date, filing_link = await run_monitor_once(ticker)

    if not analysis_file:
        await channel.send(f"🔎 `{ticker}` — no analysis found (maybe no fresh 10-K).")
        return

    # Read the analysis text from the file
    analysis_text = _read_text(analysis_file, limit=3800)  # keep under Discord 4000 char limit

    # Build embed
    emb = discord.Embed(
        title=f"{ticker} — Latest 10-K",
        description=analysis_text or "No analysis text available.",
        color=0x2ECC71,  # green accent
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

    # Send embed only (no file, no extra summary)
    await channel.send(embed=emb, view=view, silent=True)

    # Delete the temporary file after reading
    try:
        if os.path.exists(analysis_file):
            os.remove(analysis_file)
    except Exception as e:
        print(f"[cleanup] could not delete {analysis_file}: {e}")

def _pick_embed_color(ticker: str) -> int:
    # stable-ish color per ticker
    return int.from_bytes(ticker.encode("utf-8")[:3].ljust(3,b"\x00"), "big") % 0xFFFFFF

def _tldr_from_analysis(text: str, max_len: int = 900) -> str:
    """
    Try to create a compact TL;DR:
    - prefer first 6 bullet/numbered lines
    - else first ~4 sentences
    """
    if not text:
        return "No analysis text available."
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullets = [ln for ln in lines if ln.startswith(("-", "•", "*", "—", "–")) or ln[:2].isdigit()]
    chunk = "\n".join(bullets[:6]) if bullets else " ".join(lines[:4])

    # clamp
    if len(chunk) > max_len:
        chunk = chunk[: max_len - 1] + "…"
    return chunk

def _make_10k_embed(ticker: str, filing_date: Optional[str], filing_link: Optional[str], analysis_text: Optional[str]) -> discord.Embed:
    title = f"{ticker} — Latest 10-K"
    desc  = _tldr_from_analysis(analysis_text or "No analysis.")
    color = _pick_embed_color(ticker)
    emb = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.utcnow())
    if filing_date:
        emb.add_field(name="Filed", value=f"`{filing_date}`", inline=True)
    if filing_link:
        emb.add_field(name="Document", value=f"[Open 10-K]({filing_link})", inline=True)
    emb.set_footer(text="10-K Monitor • auto-generated")
    return emb

class TenKLinkView(discord.ui.View):
    def __init__(self, link_url: Optional[str]):
        super().__init__(timeout=None)
        if link_url:
            self.add_item(discord.ui.Button(label="Open 10-K", url=link_url))

# --- helper to read the analysis text (for TL;DR in the embed) ---
def _read_text(path: str, limit: int = 6000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        if len(txt) > limit:
            txt = txt[: limit - 1] + "…"
        return txt
    except Exception:
        return ""

# ---------- Daily Task ----------
@tasks.loop(minutes=1)
async def daily_runner():
    """Check once per minute; when local time hits DAILY_HOUR:DAILY_MIN, run watchlist."""
    now = datetime.now(TZ)
    if now.hour == DAILY_HOUR and now.minute == DAILY_MIN:
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            # If bot just started and cache is cold, fetch
            channel = await bot.fetch_channel(CHANNEL_ID)

        watchlist = load_watchlist(WATCHLIST_FILE)
        if not watchlist:
            await channel.send("ℹ️ Watchlist is empty. Add tickers to `watchlist.txt` (one per line).")
            return

        await channel.send(
            f"⏱️ Running daily 10-K check for {len(watchlist)} tickers "
            f"(local {now.strftime('%Y-%m-%d %H:%M')} {now.tzname()})…"
        )

        for t in watchlist:
            try:
                await post_result(channel, t)
            except Exception as e:
                await channel.send(f"⚠️ `{t}` failed: `{e}`")

# ---------- Commands ----------
@bot.tree.command(name="run10k", description="Run a 10-K analysis now for a specific ticker.")
async def run10k(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    ticker = ticker.strip().upper()
    await post_result(interaction.channel, ticker)
    await interaction.followup.send(f"✅ Done for `{ticker}`")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Slash sync error:", e)

    # sanity ping
    try:
        channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
        await channel.send("✅ TenKMonitor bot is online.")
    except Exception as e:
        print("Startup post failed:", e)

    daily_runner.start()
    print("Daily runner started.")



if __name__ == "__main__":
    if not DISCORD_TOKEN or CHANNEL_ID == 0:
        raise SystemExit("Please set DISCORD_TOKEN and CHANNEL_ID environment variables.")
    bot.run(DISCORD_TOKEN)
