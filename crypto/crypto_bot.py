import sys;sys.path.append(".")
import os, certifi
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())
import asyncio
from datetime import datetime, timezone
import yfinance as yf
import discord
from discord import app_commands
from discord.ext import tasks
from discord_stock.token import discord_tok
import aiohttp


# ==== CONFIG VIA ENV ====
TOKEN = f"{discord_tok.dis_1}{discord_tok.dis_2}{discord_tok.dis_3}"
CHANNEL_ID = int("1418555720160247958")  # make it int # target channel for auto-posts
POST_EVERY_MIN = int(os.getenv("POST_EVERY_MIN", "30")) # auto-post interval (minutes)
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "coinbase") 
USE_EMBEDS = True

# Public icons (replace if you have preferred assets)
COIN_ICONS = {
    "BTC-USD": "https://cryptologos.cc/logos/bitcoin-btc-logo.png?v=029",
    "ETH-USD": "https://cryptologos.cc/logos/ethereum-eth-logo.png?v=029",
}

INTENTS = discord.Intents.default()
INTENTS.message_content = True 
bot = discord.Client(intents=INTENTS)
tree = app_commands.CommandTree(bot)

def fmt_price(p: float) -> str:
    # Nice formatting for USD
    if p >= 1000:
        return f"${p:,.2f}"
    return f"${p:.2f}"

async def fetch_quotes(symbols=("BTC-USD", "ETH-USD")):
    data = {}
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            # Try fast_info first (fast)
            price = None
            try:
                price = float(t.fast_info.last_price)
            except Exception:
                pass
            if price is None:
                # Fallback: recent history (slightly slower)
                hist = t.history(period="1d", interval="1m")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
            if price is None:
                raise RuntimeError("No price found")
            data[sym] = price
        except Exception as e:
            data[sym] = f"ERR: {e}"
    return data

def build_embed(quotes: dict) -> discord.Embed:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = "🪙 Crypto Prices (Yahoo Finance)"
    desc_lines = []
    for sym, val in quotes.items():
        if isinstance(val, (int, float)):
            desc_lines.append(f"**{sym.replace('-USD','')}**: {fmt_price(val)}")
        else:
            desc_lines.append(f"**{sym.replace('-USD','')}**: {val}")
    e = discord.Embed(title=title, description="\n".join(desc_lines), timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Last update · {now}")
    # Add thumbnails—Discord supports one thumbnail per embed; we’ll add BTC as thumbnail and ETH as image
    if "BTC-USD" in quotes and COIN_ICONS.get("BTC-USD"):
        e.set_thumbnail(url=COIN_ICONS["BTC-USD"])
    if "ETH-USD" in quotes and COIN_ICONS.get("ETH-USD"):
        e.set_image(url=COIN_ICONS["ETH-USD"])
    return e

async def post_update(channel: discord.TextChannel):
    data = await get_prices(("BTC-USD", "ETH-USD"))
    if USE_EMBEDS:
        btc = build_coin_embed("BTC-USD", data["BTC-USD"]["price"], data["BTC-USD"]["change_pct"])
        eth = build_coin_embed("ETH-USD", data["ETH-USD"]["price"], data["ETH-USD"]["change_pct"])
        await channel.send(embeds=[btc, eth])
    else:
        lines = []
        for sym, d in data.items():
            price, change = d["price"], d["change_pct"]
            line = f"{sym}: {fmt_price(price) if isinstance(price,(int,float)) else price}"
            if isinstance(change,(int,float)):
                sign = "+" if change >= 0 else ""
                line += f"  ({sign}{change:.2f}%)"
            lines.append(line)
        await channel.send("\n".join(lines))

@tasks.loop(minutes=POST_EVERY_MIN)
async def auto_post_loop():
    if CHANNEL_ID == 0:
        return
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except Exception:
            return
    await post_update(channel)

@auto_post_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()
    await asyncio.sleep(2)

@tree.command(name="crypto", description="Get current BTC & ETH (spot) with logos & 24h change.")
async def crypto(interaction: discord.Interaction):
    data = await get_prices(("BTC-USD", "ETH-USD"))
    btc = build_coin_embed("BTC-USD", data["BTC-USD"]["price"], data["BTC-USD"]["change_pct"])
    eth = build_coin_embed("ETH-USD", data["ETH-USD"]["price"], data["ETH-USD"]["change_pct"])
    await interaction.response.send_message(embeds=[btc, eth])

@bot.event
async def on_ready():
    # Sync slash commands on start
    try:
        await tree.sync()
    except Exception as e:
        print("Slash sync error:", e)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Kick off auto poster
    if CHANNEL_ID != 0 and not auto_post_loop.is_running():
        auto_post_loop.start()



@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        return

    content = message.content.strip().lower()

    if content in {"?", "?crypto"}:
        data = await get_prices(("BTC-USD", "ETH-USD"))
        btc = build_coin_embed("BTC-USD", data["BTC-USD"]["price"], data["BTC-USD"]["change_pct"])
        eth = build_coin_embed("ETH-USD", data["ETH-USD"]["price"], data["ETH-USD"]["change_pct"])
        async with message.channel.typing():
            await message.channel.send(embeds=[btc, eth])
        return

    if content in {"?btc", "?bitcoin"}:
        data = await get_prices(("BTC-USD",))
        btc = build_coin_embed("BTC-USD", data["BTC-USD"]["price"], data["BTC-USD"]["change_pct"])
        await message.channel.send(embeds=[btc])
        return

    if content in {"?eth", "?ethereum"}:
        data = await get_prices(("ETH-USD",))
        eth = build_coin_embed("ETH-USD", data["ETH-USD"]["price"], data["ETH-USD"]["change_pct"])
        await message.channel.send(embeds=[eth])
        return

async def fetch_quotes_with_change(symbols=("BTC-USD", "ETH-USD")):
    """
    Returns dict like:
    {
      "BTC-USD": {"price": 61234.56, "change_pct": +1.23},
      "ETH-USD": {"price": 2945.10, "change_pct": -0.42},
    }
    """
    out = {}
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            price = None
            prev_close = None

            # Fast path
            try:
                price = float(t.fast_info.last_price)
            except Exception:
                price = None
            try:
                prev_close = float(t.fast_info.previous_close)
            except Exception:
                prev_close = None

            # Fallback to history if needed
            if price is None or prev_close is None:
                hist = t.history(period="2d", interval="1h")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1]) if price is None else price
                    # previous session close: last value from previous day if available
                    if prev_close is None:
                        if len(hist) >= 2:
                            prev_close = float(hist["Close"].iloc[-2])
                        else:
                            prev_close = float(hist["Close"].iloc[0])

            if price is None:
                raise RuntimeError("No price found")

            change_pct = None
            if prev_close and prev_close != 0:
                change_pct = (price - prev_close) / prev_close * 100.0

            out[sym] = {"price": price, "change_pct": change_pct}
        except Exception as e:
            out[sym] = {"price": f"ERR: {e}", "change_pct": None}
    return out

def _source_label() -> str:
    return "Coinbase · Live" if PRICE_SOURCE.lower() == "coinbase" else "Yahoo Finance · Live"

CUSTOM_EMOJIS = {
    "BTC-USD": "₿",  # example
    "ETH-USD": "𖢻",        # example
}

def coin_emoji(symbol: str) -> str:
    # Fallback to unicode if custom emoji missing
    return CUSTOM_EMOJIS.get(symbol, ("₿" if "BTC" in symbol else "Ξ"))

def build_coin_embed(symbol: str, price, change_pct) -> discord.Embed:
    coin_name = "Bitcoin" if "BTC" in symbol else "Ethereum"
    emoji = coin_emoji(symbol)

    brand_color = 0xF7931A if "BTC" in symbol else 0x3C3C3D
    color = (0x2ECC71 if isinstance(change_pct,(int,float)) and change_pct >= 0 else 0xE74C3C) if isinstance(change_pct,(int,float)) else brand_color

    desc = f"**Price:** {fmt_price(price)}"
    if isinstance(change_pct, (int, float)):
        arrow = "🟢" if change_pct >= 0 else "🔴"
        desc += f"\n**24h:** {arrow} {change_pct:.2f}%"

    embed = discord.Embed(
        title=f"{emoji} {coin_name} · Live",
        description=desc,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    logo_url = COIN_ICONS.get(symbol)
    if logo_url:
        embed.set_thumbnail(url=logo_url)
    embed.set_footer(text=_source_label())
    return embed


async def _cb_fetch_one(session: aiohttp.ClientSession, product: str):
    ticker_url = f"https://api.exchange.coinbase.com/products/{product}/ticker"
    stats_url  = f"https://api.exchange.coinbase.com/products/{product}/stats"

    async with session.get(ticker_url, timeout=10) as r:
        if r.status != 200:
            raise RuntimeError(f"Ticker HTTP {r.status}")
        t = await r.json()
    async with session.get(stats_url, timeout=10) as r:
        if r.status != 200:
            raise RuntimeError(f"Stats HTTP {r.status}")
        s = await r.json()

    def _to_float(x):
        try:
            return float(x)
        except Exception:
            return None

    price = _to_float(t.get("price"))
    open_price = _to_float(s.get("open"))
    change_pct = ((price - open_price) / open_price * 100.0) if (price is not None and open_price not in (None, 0)) else None
    if price is None:
        raise RuntimeError("No price")
    return {"price": price, "change_pct": change_pct}

async def fetch_quotes_coinbase_with_change(symbols=("BTC-USD", "ETH-USD")):
    out = {}
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [ _cb_fetch_one(session, s) for s in symbols ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for sym, res in zip(symbols, results):
        if isinstance(res, Exception):
            out[sym] = {"price": f"ERR: {res}", "change_pct": None}
        else:
            out[sym] = res
    return out


async def get_prices(symbols):
    if PRICE_SOURCE == "coinbase":
        return await fetch_quotes_coinbase_with_change(symbols)
    else:
        return await fetch_quotes_with_change(symbols) 

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Please set DISCORD_TOKEN env var.")
    bot.run(TOKEN)
