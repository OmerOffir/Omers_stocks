# --- macOS certificate fix: set before importing discord/aiohttp ---
import os, platform
if platform.system() == "Darwin":
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ.pop("SSL_CERT_DIR", None)
import os, sys; sys.path.append(".")
# -------------------------------------------------------------------

import json, time, logging
from datetime import datetime
from typing import Dict, Optional, Tuple, List

import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timezone

import yfinance as yf
from dateutil import tz

# ----------------- Config -----------------
try:
    from discord_stock.token import discord_tok
    TOKEN = f"{discord_tok.dis_1}{discord_tok.dis_2}{discord_tok.dis_3}"
except Exception:
    TOKEN = os.getenv("DISCORD_TOKEN") or None
if not TOKEN:
    raise SystemExit("Missing token: set DISCORD_TOKEN or provide discord_stock.token.discord_tok")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))                # your Discord user ID
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Asia/Jerusalem")
TEST_CHANNEL_ID = int("1425525386665529374")
DATA_FILE = os.getenv("PORTFOLIO_DB", "portfolio/portfolio.json")
REFRESH_COOLDOWN_SECONDS = 30
CLOSED_MAX = 500

# ------------- Intents / Bot -------------
intents = discord.Intents.none()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------- Persistence ---------------
def _ensure_db_dir() -> None:
    d = os.path.dirname(DATA_FILE) or "."
    os.makedirs(d, exist_ok=True)

def load_data() -> Dict:
    _ensure_db_dir()
    base = {"channel_id": None, "message_id": None, "positions": {}, "closed": []}
    if not os.path.exists(DATA_FILE):
        return base
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("closed", [])
        return data
    except Exception:
        return base

def save_data(data: Dict) -> None:
    _ensure_db_dir()
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

state = load_data()

# ------------- Prices / Math -------------
async def fetch_last_price(ticker: str) -> Optional[float]:
    try:
        t = yf.Ticker(ticker)
        try:
            fi = getattr(t, "fast_info", None)
            if fi:
                lp = getattr(fi, "last_price", None) or fi.get("last_price")
                if lp is not None:
                    return float(lp)
        except Exception:
            pass
        try:
            df = t.history(period="1d", interval="1m")
            if df is not None and len(df) > 0:
                return float(df["Close"].iloc[-1])
        except Exception:
            pass
        df = yf.download(ticker, period="5d", interval="1d", progress=False)
        if df is not None and len(df) > 0:
            return float(df["Close"].iloc[-1])
        return None
    except Exception:
        return None

def pct(entry: float, last: float) -> float:
    if entry is None or last is None or entry == 0:
        return 0.0
    return (last / entry - 1.0) * 100.0

def remaining_to_target(last: float, target: float) -> Optional[float]:
    if last and target:
        return (target / last - 1.0) * 100.0
    return None

def distance_from_stop(last: float, stop: float) -> Optional[float]:
    if last and stop:
        return (last / stop - 1.0) * 100.0
    return None

def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    emoji = "🟢" if x >= 0 else "🔴"
    return f"{emoji}{x:+.2f}%"

def monospaced_table(rows: List[List[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for ridx, r in enumerate(rows):
        parts = []
        for i, cell in enumerate(r):
            pad = " " * (widths[i] - len(cell))
            parts.append(cell + pad)
        line = "  ".join(parts)
        lines.append(line)
        if ridx == 0:
            lines.append("-" * len(line))
    return "```\n" + "\n".join(lines) + "\n```"

def green_embed(title: str) -> discord.Embed:
    return discord.Embed(title=title, colour=discord.Colour.green())

def red_embed(title: str) -> discord.Embed:
    return discord.Embed(title=title, colour=discord.Colour.red())


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def _append_closed(data: Dict, rec: Dict) -> None:
    data.setdefault("closed", []).append(rec)
    if len(data["closed"]) > CLOSED_MAX:
        data["closed"] = data["closed"][-CLOSED_MAX:]

# ------------- Refresh View --------------
class RefreshView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self._last_click = 0.0

    @discord.ui.button(label="Refresh now", style=discord.ButtonStyle.primary,
                       custom_id="pnl_refresh_now", emoji="🔄")
    async def refresh_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        now = time.monotonic()
        if now - self._last_click < REFRESH_COOLDOWN_SECONDS:
            wait_s = int(REFRESH_COOLDOWN_SECONDS - (now - self._last_click))
            return await interaction.response.send_message(
                f"Please wait {wait_s}s before refreshing again.", ephemeral=True
            )
        self._last_click = now
        await interaction.response.defer(ephemeral=True)
        try:
            await render_and_publish()
            await interaction.followup.send("Refreshed.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Refresh failed: {e}", ephemeral=True)

# NOTE: don't instantiate RefreshView here. Create it in on_ready().
_persistent_view: Optional[RefreshView] = None

# ------------- Rendering -----------------
async def render_content_and_maybe_autoclose() -> Tuple[str, float, float]:
    positions = state.get("positions", {}) or {}
    closed = state.get("closed", []) or []

    perfs_open: List[float] = []
    rows_open = [["Ticker", "Entry", "Stop", "Target", "Price", "%PnL", "% to Stop", "% to Target", "Status"]]
    to_close: List[Tuple[str, float, str]] = []

    for tkr, p in sorted(positions.items()):
        entry = float(p["entry"])
        stop = float(p["stop"])
        target = float(p["target"])

        last = await fetch_last_price(tkr)
        if last is None:
            rows_open.append([tkr, f"{entry:.2f}", f"{stop:.2f}", f"{target:.2f}", "—", "—", "—", "—", "—"])
            continue

        pnl = pct(entry, last)
        perfs_open.append(pnl)

        to_stop = distance_from_stop(last, stop)
        to_tgt = remaining_to_target(last, target)

        status = "🟢" if last >= entry else "🔴"
        reason = None
        if last <= stop:
            status = "⛔ below stop"; reason = "stop"
        elif last >= target:
            status = "🎯 target hit"; reason = "target"

        rows_open.append([
            tkr, f"{entry:.2f}", f"{stop:.2f}", f"{target:.2f}", f"{last:.2f}",
            fmt_pct(pnl), fmt_pct(to_stop), fmt_pct(to_tgt), status
        ])

        if reason is not None:
            to_close.append((tkr, last, reason))

    if to_close:
        for tkr, exit_price, reason in to_close:
            pos = state["positions"].pop(tkr, None)
            if not pos:
                continue
            rec = {
                "ticker": tkr, "entry": float(pos["entry"]), "stop": float(pos["stop"]),
                "target": float(pos["target"]), "exit": float(exit_price), "reason": reason,
                "created_at": pos.get("created_at"), "closed_at": _now_iso(),
                "pnl_pct": pct(float(pos["entry"]), float(exit_price)),
            }
            _append_closed(state, rec)
        save_data(state)

        # re-render skeleton of open table (prices will refresh next tick)
        positions = state.get("positions", {})
        perfs_open = []
        rows_open = [["Ticker", "Entry", "Stop", "Target", "Price", "%PnL", "% to Stop", "% to Target", "Status"]]
        if positions:
            for tkr, p in sorted(positions.items()):
                entry = float(p["entry"]); stop = float(p["stop"]); target = float(p["target"])
                rows_open.append([tkr, f"{entry:.2f}", f"{stop:.2f}", f"{target:.2f}", "—", "—", "—", "—", "—"])

    able_open  = monospaced_table(rows_open)  if len(rows_open)  > 1 else "No open positions."
    

    rows_closed = [["Ticker", "Entry", "Stop", "Target", "Exit", "%PnL", "Reason", "Closed At"]]
    perfs_closed: List[float] = []
    for rec in reversed(closed):
        pnlv = float(rec.get("pnl_pct", pct(float(rec["entry"]), float(rec["exit"]))))
        perfs_closed.append(pnlv)
        rows_closed.append([
            rec["ticker"], f"{float(rec['entry']):.2f}", f"{float(rec['stop']):.2f}",
            f"{float(rec['target']):.2f}", f"{float(rec['exit']):.2f}",
            fmt_pct(pnlv), rec.get("reason", "manual"), (rec.get("closed_at") or "")[:10],
        ])
    table_closed = monospaced_table(rows_closed) if len(rows_closed) > 1 else "No closed trades yet."

    open_pct = (sum(perfs_open) / max(1, len(perfs_open))) if perfs_open else 0.0
    realized_pct = (sum(perfs_closed) / max(1, len(perfs_closed))) if perfs_closed else 0.0
    positions = state.get("positions", {}) or {}
    final_rows_open = [["Ticker", "Entry", "Stop", "Target", "Price", "%PnL", "% to Stop", "% to Target", "Status"]]
    perfs_open_final: List[float] = []

    for tkr, p in sorted(positions.items()):
        entry = float(p["entry"]); stop = float(p["stop"]); target = float(p["target"])
        last = await fetch_last_price(tkr)
        if last is None:
            final_rows_open.append([tkr, f"{entry:.2f}", f"{stop:.2f}", f"{target:.2f}", "—","—","—","—","—"])
            continue
        pnl = pct(entry, last); perfs_open_final.append(pnl)
        to_stop = distance_from_stop(last, stop); to_tgt = remaining_to_target(last, target)
        status = "🟢" if last >= entry else "🔴"
        final_rows_open.append([
            tkr, f"{entry:.2f}", f"{stop:.2f}", f"{target:.2f}", f"{last:.2f}",
            fmt_pct(pnl), fmt_pct(to_stop), fmt_pct(to_tgt), status
        ])

    table_open = monospaced_table(final_rows_open) if len(final_rows_open) > 1 else "No open positions."
    # rows_closed/perfs_closed already built above
    open_pct = (sum(perfs_open_final) / max(1, len(perfs_open_final))) if perfs_open_final else 0.0
    realized_pct = (sum(perfs_closed) / max(1, len(perfs_closed))) if perfs_closed else 0.0
    content = (
        "**Open Positions**\n" + (table_open if isinstance(table_open, str) else "") + "\n"
        "**Closed Trades**\n" + (table_closed if isinstance(table_closed, str) else "") + "\n"
        "_(Equal-weight averages; amounts intentionally omitted)_"
    )
    return content, open_pct, realized_pct

def build_embed(open_pct: float, realized_pct: float) -> discord.Embed:
    tzinfo = tz.gettz(DEFAULT_TZ)
    now_str = datetime.now(tzinfo).strftime("%Y-%m-%d %H:%M")
    emoji_open = "🟢" if open_pct >= 0 else "🔴"
    emoji_real = "🟢" if realized_pct >= 0 else "🔴"
    title = f"{now_str} • Open PnL: {emoji_open}{open_pct:+.2f}% • Realized: {emoji_real}{realized_pct:+.2f}%"
    return green_embed(title) if open_pct >= 0 else red_embed(title)

async def render_and_publish():
    channel_id = state.get("channel_id")
    if not channel_id:
        return
    ch = bot.get_channel(int(channel_id))
    if not isinstance(ch, discord.TextChannel):
        return

    content, open_pct, realized_pct = await render_content_and_maybe_autoclose()
    emb = build_embed(open_pct, realized_pct)
    msg_id = state.get("message_id")

    view = _persistent_view  # will be set in on_ready
    if msg_id:
        try:
            msg = await ch.fetch_message(int(msg_id))
            await msg.edit(content=content, embed=emb, view=view)
            return
        except Exception:
            pass

    new_msg = await ch.send(content=content, embed=emb, view=view)
    try:
        await new_msg.pin()
    except Exception:
        pass
    state["message_id"] = new_msg.id
    save_data(state)

# ------------- Updater loop --------------
@tasks.loop(minutes=5)
async def updater():
    try:
        await render_and_publish()
    except Exception:
        pass

# ------------- Command checks ------------
def owner_only(interaction: discord.Interaction) -> bool:
    return (OWNER_ID == 0) or (interaction.user and interaction.user.id == OWNER_ID)

def require_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if owner_only(interaction):
            return True
        try:
            await interaction.response.send_message("Only the owner can do this.", ephemeral=True)
        except discord.InteractionResponded:
            pass
        return False
    return app_commands.check(predicate)

# ------------- Slash commands ------------
pnl = app_commands.Group(name="pnl", description="Portfolio PnL channel management")
bot.tree.add_command(pnl)


@pnl.command(name="setchannelid", description="(Fallback) Configure channel by numeric ID")
@app_commands.describe(raw_id="Numeric channel ID, e.g., 1425525386665529374")
@require_owner()
async def pnl_setchannelid(interaction: discord.Interaction, raw_id: str):
    try:
        ch_id = int(raw_id)
    except ValueError:
        return await interaction.response.send_message("ID must be a number.", ephemeral=True)

    ch = bot.get_channel(ch_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(ch_id)  # fetch if not cached
        except Exception:
            ch = None

    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("Channel not found or not a text channel.", ephemeral=True)

    state["channel_id"] = ch.id
    save_data(state)

    # (optional) lock channel read-only for @everyone, allow bot
    try:
        overwrites_everyone = ch.overwrites_for(ch.guild.default_role)
        overwrites_everyone.send_messages = False
        await ch.set_permissions(ch.guild.default_role, overwrite=overwrites_everyone)

        me = ch.guild.me
        if me:
            ow_me = ch.overwrites_for(me)
            ow_me.send_messages = True
            await ch.set_permissions(me, overwrite=ow_me)
    except Exception:
        pass

    await interaction.response.send_message(f"Channel set to {ch.mention}.", ephemeral=True)
    await render_and_publish()


@pnl.command(name="setchannel", description="Select the read-only channel to display the portfolio")
@app_commands.describe(channel="Text channel to use")
@require_owner()
async def pnl_setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    state["channel_id"] = channel.id
    save_data(state)

    # Lock channel read-only for everyone; allow bot to post
    try:
        overwrites_everyone = channel.overwrites_for(channel.guild.default_role)
        overwrites_everyone.send_messages = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrites_everyone)

        me = channel.guild.me
        if me:
            overwrites_me = channel.overwrites_for(me)
            overwrites_me.send_messages = True
            await channel.set_permissions(me, overwrite=overwrites_me)
    except Exception:
        pass

    await interaction.response.send_message(f"Channel set to {channel.mention}. Locked for public posting.", ephemeral=True)
    await render_and_publish()

@pnl.command(name="publish", description="Publish/refresh the pinned portfolio message")
@require_owner()
async def pnl_publish(interaction: discord.Interaction):
    if not state.get("channel_id"):
        return await interaction.response.send_message(
            "No channel is configured. Use `/pnl setchannel` first.", ephemeral=True
        )
    await interaction.response.defer(ephemeral=True)
    await render_and_publish()
    await interaction.followup.send("Published/refreshed.", ephemeral=True)

@pnl.command(name="debug", description="Show current config")
async def pnl_debug(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"channel_id={state.get('channel_id')!r}, message_id={state.get('message_id')!r}, "
        f"positions={list((state.get('positions') or {}).keys())}, closed={len(state.get('closed') or [])}",
        ephemeral=True
    )

@pnl.command(name="add", description="Add a position")
@app_commands.describe(ticker="e.g., AAPL", entry="Entry price", stop="Stop price", target="Target price")
@require_owner()
async def pnl_add(interaction: discord.Interaction, ticker: str, entry: float, stop: float, target: float):
    t = ticker.upper().strip()
    if entry <= 0 or stop <= 0 or target <= 0:
        return await interaction.response.send_message("Values must be positive.", ephemeral=True)
    state.setdefault("positions", {})[t] = {
        "entry": entry, "stop": stop, "target": target, "created_at": _now_iso()
    }
    save_data(state)
    await interaction.response.send_message(f"Added {t}: entry={entry}, stop={stop}, target={target}.", ephemeral=True)
    await render_and_publish()

@pnl.command(name="set", description="Update a position")
@app_commands.describe(
    ticker="e.g., AAPL",
    entry="Entry price (optional)",
    stop="Stop price (optional)",
    target="Target price (optional)",
)
@require_owner()
async def pnl_set(interaction: discord.Interaction, ticker: str,
                  entry: Optional[float] = None, stop: Optional[float] = None, target: Optional[float] = None):
    t = ticker.upper().strip()
    pos = state.get("positions", {}).get(t)
    if not pos:
        return await interaction.response.send_message("Ticker not found.", ephemeral=True)
    if entry and entry > 0:  pos["entry"] = entry
    if stop and stop > 0:    pos["stop"] = stop
    if target and target > 0: pos["target"] = target
    save_data(state)
    await interaction.response.send_message(f"Updated {t}.", ephemeral=True)
    await render_and_publish()

@pnl.command(name="remove", description="Remove a position")
@app_commands.describe(ticker="e.g., AAPL")
@require_owner()
async def pnl_remove(interaction: discord.Interaction, ticker: str):
    t = ticker.upper().strip()
    if t in state.get("positions", {}):
        state["positions"].pop(t, None)
        save_data(state)
        await interaction.response.send_message(f"Removed {t}.", ephemeral=True)
        await render_and_publish()
    else:
        await interaction.response.send_message("Ticker not found.", ephemeral=True)

@pnl.command(name="close", description="Manually close a position into Closed Trades")
@app_commands.describe(
    ticker="e.g., AAPL",
    exit_price="If omitted, the latest market price will be used",
    reason="stop | target | manual (default manual)"
)
@require_owner()
async def pnl_close(interaction: discord.Interaction, ticker: str,
                    exit_price: Optional[float] = None, reason: Optional[str] = "manual"):
    t = ticker.upper().strip()
    pos = state.get("positions", {}).pop(t, None)
    if not pos:
        return await interaction.response.send_message("Ticker not found in open positions.", ephemeral=True)

    if exit_price is None:
        last = await fetch_last_price(t)
        if last is None:
            return await interaction.response.send_message("Could not fetch price. Provide exit_price.", ephemeral=True)
        exit_price = float(last)

    reason = (reason or "manual").lower()
    if reason not in ("stop", "target", "manual"):
        reason = "manual"

    rec = {
        "ticker": t,
        "entry": float(pos["entry"]),
        "stop": float(pos["stop"]),
        "target": float(pos["target"]),
        "exit": float(exit_price),
        "reason": reason,
        "created_at": pos.get("created_at"),
        "closed_at": _now_iso(),
        "pnl_pct": pct(float(pos["entry"]), float(exit_price)),
    }
    _append_closed(state, rec)
    save_data(state)
    await interaction.response.send_message(
        f"Closed {t} at {exit_price} ({reason}), PnL={rec['pnl_pct']:+.2f}%.", ephemeral=True
    )
    await render_and_publish()

# ------------- Lifecycle -----------------
@bot.event
async def on_ready():
    logging.warning(f"Logged in as {bot.user} (id={bot.user.id})")

    # Create & register the persistent view **after** loop is running
    global _persistent_view
    _persistent_view = RefreshView()
    bot.add_view(_persistent_view)

    # Invite URL with proper scopes
    app = await bot.application_info()
    invite = discord.utils.oauth_url(
        app.id,
        permissions=discord.Permissions(permissions=274877975552),
        scopes=("bot", "applications.commands"),
    )
    logging.warning(f"Invite URL: {invite}")

    # Fast guild sync using TEST_CHANNEL_ID (optional)
    guild_for_sync = None
    if TEST_CHANNEL_ID:
        ch = bot.get_channel(TEST_CHANNEL_ID)
        if ch:
            guild_for_sync = ch.guild
        else:
            logging.warning("TEST_CHANNEL_ID not visible yet; will do global sync.")

    try:
        if guild_for_sync:
            gid = discord.Object(id=guild_for_sync.id)
            bot.tree.copy_global_to(guild=gid)
            synced = await bot.tree.sync(guild=gid)
            logging.warning(f"Synced {len(synced)} commands to guild {guild_for_sync.id}.")
        else:
            synced = await bot.tree.sync()
            logging.warning(f"Synced {len(synced)} global commands.")
    except discord.Forbidden:
        logging.exception("Sync forbidden: re-invite with scopes bot+applications.commands.")
    except Exception:
        logging.exception("Slash sync failed unexpectedly.")

    # Start updater only after ready (avoids 'no running event loop')
    if not updater.is_running():
        updater.start()

# ------------- Main ----------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bot.run(TOKEN)
