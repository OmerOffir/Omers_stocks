import sys;sys.path.append(".")
import os, asyncio, yaml, traceback
from dataclasses import dataclass
from dotenv import load_dotenv
from discord.ext import commands
from discord import Intents
import numpy as np
import pandas as pd
from discord_stock.token import discord_tok

from adapters.alpaca_ws_provider import AlpacaWSProvider
from strategies_momentum import compute_indicators, momentum_entries, momentum_exit_flip
from daytradebot.rick import position_size, immediate_stop_hit

load_dotenv()

with open("daytradebot/config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

PROVIDER_NAME = CONFIG.get("provider", "alpaca_ws").lower()
DISCORD_CHANNEL_ID = int(CONFIG.get("discord_channel_id", 0))
TIMEFRAME = CONFIG.get("timeframe", "5m")

RISK = CONFIG["risk"]
EXITS = CONFIG["exits"]
MOMO = CONFIG["momentum"]

intents = Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@dataclass
class BotState:
    running: bool = False
    task: asyncio.Task | None = None
    symbol: str | None = None
    equity: float = RISK.get("equity", 10000)
    r_pct: float = RISK.get("per_trade_r_pct", 0.01)   # default risk if none given
    tp_R: float = EXITS.get("take_profit_R", 0.0)
    max_loss_pct: float = RISK.get("max_intrabar_loss_pct", 0.02)
    cmd_risk_pct: float | None = None   # <-- per-trade override from !start

STATE = BotState()
PROVIDER = AlpacaWSProvider()
TARGET_CHANNEL = None

@bot.event
async def on_ready():
    global TARGET_CHANNEL
    TARGET_CHANNEL = bot.get_channel(DISCORD_CHANNEL_ID)
    if TARGET_CHANNEL is None:
        print(f"[WARN] Could not resolve channel id={DISCORD_CHANNEL_ID}; using invoking channel fallback.")
    else:
        await TARGET_CHANNEL.send(f"✅ Momentum bot online (TF={TIMEFRAME}).")

async def analyze_and_trade(channel, symbol: str):
    await channel.send(f"🔎 Momentum mode for **{symbol}** on {TIMEFRAME}.")
    in_pos = False
    entry = stop = 0.0
    qty = 0
    last_trailing = None

    async for df in PROVIDER.stream_bars(symbol):
        try:
            df = compute_indicators(
                df,
                macd_fast=MOMO["macd_fast"],
                macd_slow=MOMO["macd_slow"],
                macd_sig=MOMO["macd_signal"],
                cci_len=MOMO["cci_len"],
            )
            if df.empty or len(df) < 30:
                continue

            last = df.iloc[-1]
            price = float(last["close"])

            # --- entries (Doji+MACD+CCI) on last bar ---
            entries_series = momentum_entries(
                df,
                doji_body_pct=MOMO["doji_body_pct"],
                cci_entry=MOMO["cci_entry"],
                macd_fast=MOMO["macd_fast"],
                macd_slow=MOMO["macd_slow"],
                macd_signal=MOMO["macd_signal"],
                confirm_break_high=bool(MOMO.get("confirm_break_high", True)),
            )
            sig_enter = bool(entries_series.iloc[-1])

            # --- exit momentum flip on last bar ---
            exit_flip = bool(momentum_exit_flip(df, cci_exit=MOMO["cci_exit"]).iloc[-1])

            if not in_pos:
                if sig_enter:
                    # Pattern stop = prior candle's low
                    pattern_stop = float(df["low"].shift(1).iloc[-1])

                    # Risk from command (fallback to default)
                    used_risk_pct = STATE.cmd_risk_pct if STATE.cmd_risk_pct is not None else STATE.r_pct

                    entry = price

                    # Risk-based stop cap (tightest between pattern stop and risk%)
                    risk_stop = entry * (1.0 - used_risk_pct)
                    stop = max(pattern_stop, risk_stop)   # ensure we don't risk more than requested

                    # Size the position using the same per-trade risk percent
                    qty = position_size(entry, stop, STATE.equity, used_risk_pct)

                    # Optional: also set the immediate fail-safe to the chosen risk (so a sudden dump exits fast)
                    STATE.max_loss_pct = used_risk_pct

                    if qty <= 0 or entry <= stop or not np.isfinite(stop):
                        continue

                    in_pos = True
                    last_trailing = stop
                    await channel.send(
                        f"✅ **ENTER** {symbol} @ {entry:.2f} | stop {stop:.2f} "
                        f"(pattern low {pattern_stop:.2f}, risk cap {risk_stop:.2f}) | qty {qty} | risk {used_risk_pct*100:.2f}%"
                    )
            else:
                # Emergency exit if huge drop
                if immediate_stop_hit(price, entry, STATE.max_loss_pct):
                    await channel.send(f"🛑 **EXIT** immediate loss {STATE.max_loss_pct*100:.1f}% (fail-safe).")
                    in_pos = False
                    continue

                # Structural stop
                if float(last["low"]) <= float(stop):
                    await channel.send(f"🛑 **EXIT** at stop {stop:.2f}.")
                    in_pos = False
                    continue

                # Momentum flip exit (MACD cross down + CCI below threshold)
                if exit_flip:
                    await channel.send("🟠 Momentum weakening (MACD↓ & CCI below threshold) — **consider exit**.")
                    # Optional: auto-exit. Uncomment to make it hard exit:
                    # in_pos = False
                    # await channel.send("🛑 **EXIT** on momentum flip.")
                    # continue

                # Optional fixed TP (disabled by default)
                if STATE.tp_R and STATE.tp_R > 0:
                    r = (entry - stop)
                    target = entry + STATE.tp_R * r
                    if price >= target:
                        await channel.send(f"🏁 **EXIT** take-profit {target:.2f} ({STATE.tp_R}R).")
                        in_pos = False
                        continue

                # Trailing stop logic (ratchet up under EMA20 and/or last swing lows)
                ema20 = float(df["ema20"].iloc[-1])
                new_trail = max(stop, ema20)  # simple trailing: follow EMA20
                if (last_trailing is None) or (new_trail > last_trailing):
                    stop = new_trail
                    last_trailing = new_trail
                    await channel.send(f"🔧 **Raise stop** to {stop:.2f} (EMA20 trail).")

        except asyncio.CancelledError:
            break
        except Exception as e:
            traceback.print_exc()
            await channel.send(f"Error: {e}")

def parse_risk_input(risk_text: str | None) -> float | None:
    """
    Accepts: '3.2%', '3.2', '0.032'
    Returns a fraction (e.g., 0.032) or None if invalid.
    Caps to [0.0001, 0.20] to avoid crazy values.
    """
    if not risk_text:
        return None
    s = risk_text.strip().replace('%', '')
    try:
        val = float(s)
        # If user typed 3.2 -> treat as 3.2%
        if val > 1.0:
            val = val / 100.0
        return max(0.0001, min(val, 0.20))
    except:
        return None

@bot.command()
async def start(ctx, symbol: str, risk: str = None):
    """
    Usage:
      !start WMT
      !start WMT 3.2%
      !start WMT 3.2
      !start WMT 0.032
    """
    if STATE.running:
        await ctx.send("Already running. Use !stop first.")
        return

    # parse optional risk
    parsed = parse_risk_input(risk)
    STATE.cmd_risk_pct = parsed  # can be None (means use default)

    STATE.running = True
    STATE.symbol = symbol.upper()
    channel = TARGET_CHANNEL or ctx.channel

    r_show = f"{(parsed if parsed is not None else STATE.r_pct)*100:.2f}%"
    await ctx.send(f"Starting {STATE.symbol} with per-trade risk {r_show}.")
    STATE.task = asyncio.create_task(analyze_and_trade(channel, STATE.symbol))

@bot.command()
async def stop(ctx):
    STATE.running = False
    if STATE.task:
        STATE.task.cancel()
        STATE.task = None
    await ctx.send("🛑 Stopped.")

@bot.command()
async def status(ctx):
    effective_risk = STATE.cmd_risk_pct if STATE.cmd_risk_pct is not None else STATE.r_pct
    await ctx.send(
        f"running={STATE.running} symbol={STATE.symbol} "
        f"risk={effective_risk*100:.2f}% max_loss_cutoff={STATE.max_loss_pct*100:.2f}%"
    )
    
@bot.command()
async def risk(ctx, pct: float):
    STATE.r_pct = pct
    await ctx.send(f"Risk per trade set to {pct*100:.2f}%")

@bot.command()
async def sl(ctx, pct: float):
    STATE.max_loss_pct = pct
    await ctx.send(f"Immediate loss cutoff set to {pct*100:.1f}%")

# -------- run --------
if __name__ == "__main__":
    token = f"{discord_tok.dis_1}{discord_tok.dis_2}{discord_tok.dis_3}"
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN in environment (.env)")
    bot.run(token)
