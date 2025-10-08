# portfolio/portfolio_cog.py
from __future__ import annotations

import os, json, time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, List

import discord
from discord.ext import commands, tasks
from discord import app_commands

import yfinance as yf
from dateutil import tz

# ---------- config (env-driven) ----------
OWNER_ID = int(os.getenv("PORTFOLIO_OWNER_ID", "0"))                   # your user id; 0 = no owner check
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Asia/Jerusalem")
DATA_FILE = os.getenv("PORTFOLIO_DB", "portfolio/portfolio.json")
REFRESH_COOLDOWN_SECONDS = int(os.getenv("PORTFOLIO_REFRESH_COOLDOWN", "30"))
CLOSED_MAX = int(os.getenv("PORTFOLIO_CLOSED_MAX", "500"))
PORTFOLIO_CHANNEL_ID = int("1425525386665529374")


# ---------- persistence ----------
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

def save_data(d: Dict) -> None:
    _ensure_db_dir()
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

# ---------- pricing / math ----------
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

# ---------- the cog ----------
class PortfolioCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = load_data()
        self._view = self.RefreshView(self)

    # persistent refresh button
    class RefreshView(discord.ui.View):
        def __init__(self, cog: "PortfolioCog"):
            super().__init__(timeout=None)
            self.cog = cog
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
                await self.cog.render_and_publish()
                await interaction.followup.send("Refreshed.", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Refresh failed: {e}", ephemeral=True)

    # lifecycle
    async def cog_load(self):
        # button survives restarts
        self.bot.add_view(self._view)
        # register slash group & commands
        self._register_commands()
        if not self.updater.is_running():
            self.updater.start()
        if PORTFOLIO_CHANNEL_ID and not self.state.get("channel_id"):
            self.state["channel_id"] = PORTFOLIO_CHANNEL_ID
            save_data(self.state)
            await self.render_and_publish()

    def cog_unload(self):
        if self.updater.is_running():
            self.updater.cancel()

    # owner check
    @staticmethod
    def _owner_only(inter: discord.Interaction) -> bool:
        return (OWNER_ID == 0) or (inter.user and inter.user.id == OWNER_ID)

    # ---------- renderer ----------
    async def render_content_and_maybe_autoclose(self) -> Tuple[str, float, float]:
        positions = self.state.get("positions", {}) or {}
        closed = self.state.get("closed", []) or []

        perfs_open: List[float] = []
        rows_open = [["Ticker", "Entry", "Stop", "Target", "Price", "%PnL", "% to Stop", "% to Target", "Status"]]
        to_close: List[Tuple[str, float, str]] = []

        for tkr, p in sorted(positions.items()):
            entry = float(p["entry"]); stop = float(p["stop"]); target = float(p["target"])
            last = await fetch_last_price(tkr)
            if last is None:
                rows_open.append([tkr, f"{entry:.2f}", f"{stop:.2f}", f"{target:.2f}", "—","—","—","—","—"])
                continue

            pnl = pct(entry, last)
            perfs_open.append(pnl)
            to_stop = distance_from_stop(last, stop)
            to_tgt  = remaining_to_target(last, target)
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
            if reason:
                to_close.append((tkr, last, reason))

        # auto-close
        if to_close:
            for tkr, exit_price, reason in to_close:
                pos = self.state["positions"].pop(tkr, None)
                if not pos:
                    continue
                rec = {
                    "ticker": tkr, "entry": float(pos["entry"]), "stop": float(pos["stop"]),
                    "target": float(pos["target"]), "exit": float(exit_price), "reason": reason,
                    "created_at": pos.get("created_at"), "closed_at": _now_iso(),
                    "pnl_pct": pct(float(pos["entry"]), float(exit_price)),
                }
                self.state.setdefault("closed", []).append(rec)
                if len(self.state["closed"]) > CLOSED_MAX:
                    self.state["closed"] = self.state["closed"][-CLOSED_MAX:]
            save_data(self.state)

        # closed table
        rows_closed = [["Ticker", "Entry", "Stop", "Target", "Exit", "%PnL", "Reason", "Closed At"]]
        perfs_closed: List[float] = []
        for rec in reversed(self.state.get("closed", [])):
            pnlv = float(rec.get("pnl_pct", pct(float(rec["entry"]), float(rec["exit"]))))
            perfs_closed.append(pnlv)
            rows_closed.append([
                rec["ticker"],
                f"{float(rec['entry']):.2f}",
                f"{float(rec['stop']):.2f}",
                f"{float(rec['target']):.2f}",
                f"{float(rec['exit']):.2f}",
                fmt_pct(pnlv),
                rec.get("reason", "manual"),
                (rec.get("closed_at") or "")[:10],
            ])

        # recompute open table & metrics (post auto-close)
        positions = self.state.get("positions", {}) or {}
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

        table_open   = monospaced_table(final_rows_open) if len(final_rows_open) > 1 else "No open positions."
        table_closed = monospaced_table(rows_closed)     if len(rows_closed)     > 1 else "No closed trades yet."

        open_pct     = (sum(perfs_open_final) / max(1, len(perfs_open_final))) if perfs_open_final else 0.0
        realized_pct = (sum(perfs_closed)     / max(1, len(perfs_closed)))     if perfs_closed     else 0.0

        content = (
            "**Open Positions**\n" + table_open + "\n"
            "**Closed Trades**\n" + table_closed + "\n"
            "_(Equal-weight averages; amounts intentionally omitted)_"
        )
        return content, open_pct, realized_pct

    def _build_embed(self, open_pct: float, realized_pct: float) -> discord.Embed:
        tzinfo = tz.gettz(DEFAULT_TZ)
        now_str = datetime.now(tzinfo).strftime("%Y-%m-%d %H:%M")
        eo = "🟢" if open_pct >= 0 else "🔴"
        er = "🟢" if realized_pct >= 0 else "🔴"
        title = f"{now_str} • Open PnL: {eo}{open_pct:+.2f}% • Realized: {er}{realized_pct:+.2f}%"
        return green_embed(title) if open_pct >= 0 else red_embed(title)

    async def render_and_publish(self):
        ch_id = self.state.get("channel_id")
        if not ch_id:
            return
        ch = self.bot.get_channel(int(ch_id))
        if not isinstance(ch, discord.TextChannel):
            return

        content, open_pct, realized_pct = await self.render_content_and_maybe_autoclose()
        emb = self._build_embed(open_pct, realized_pct)
        msg_id = self.state.get("message_id")

        if msg_id:
            try:
                msg = await ch.fetch_message(int(msg_id))
                await msg.edit(content=content, embed=emb, view=self._view)
                return
            except Exception:
                pass

        new_msg = await ch.send(content=content, embed=emb, view=self._view)
        try:
            await new_msg.pin()
        except Exception:
            pass
        self.state["message_id"] = new_msg.id
        save_data(self.state)

    # background refresh
    @tasks.loop(minutes=5)
    async def updater(self):
        try:
            await self.render_and_publish()
        except Exception:
            pass

    @updater.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        import traceback, logging
        logging.exception("Slash command crashed", exc_info=error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ {error.__class__.__name__}: {error}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ {error.__class__.__name__}: {error}", ephemeral=True)
        except Exception:
            pass

    # ---------- slash commands (grouped as /pnl …) ----------
    def _register_commands(self):
        group = app_commands.Group(name="pnl", description="Portfolio PnL channel management")

        async def setchannel(inter: discord.Interaction, channel: discord.TextChannel):
            if not self._owner_only(inter):
                return await inter.response.send_message("Only the owner can do this.", ephemeral=True)
            self.state["channel_id"] = channel.id
            save_data(self.state)
            try:
                ow_all = channel.overwrites_for(channel.guild.default_role)
                ow_all.send_messages = False
                await channel.set_permissions(channel.guild.default_role, overwrite=ow_all)
                me = channel.guild.me
                if me:
                    ow_me = channel.overwrites_for(me)
                    ow_me.send_messages = True
                    await channel.set_permissions(me, overwrite=ow_me)
            except Exception:
                pass
            await inter.response.send_message(f"Channel set to {channel.mention}. Locked for public posting.", ephemeral=True)
            await self.render_and_publish()

        async def publish(inter: discord.Interaction):
            if not self._owner_only(inter):
                return await inter.response.send_message("Only the owner can do this.", ephemeral=True)
            if not self.state.get("channel_id"):
                return await inter.response.send_message("No channel configured. Use `/pnl setchannel` first.", ephemeral=True)
            await inter.response.defer(ephemeral=True)
            await self.render_and_publish()
            await inter.followup.send("Published/refreshed.", ephemeral=True)

        async def debug(inter: discord.Interaction):
            await inter.response.send_message(
                f"channel_id={self.state.get('channel_id')!r}, message_id={self.state.get('message_id')!r}, "
                f"positions={list((self.state.get('positions') or {}).keys())}, closed={len(self.state.get('closed') or [])}",
                ephemeral=True
            )

        async def add(inter: discord.Interaction, ticker: str, entry: float, stop: float, target: float):
            if not self._owner_only(inter):
                await inter.response.send_message("Only the owner can do this.", ephemeral=True)
                return

            # Ack immediately so we never time out
            await inter.response.defer(ephemeral=True)

            t = ticker.upper().strip()
            if entry <= 0 or stop <= 0 or target <= 0:
                await inter.followup.send("Values must be positive.", ephemeral=True)
                return

            self.state.setdefault("positions", {})[t] = {
                "entry": entry, "stop": stop, "target": target, "created_at": _now_iso()
            }
            save_data(self.state)

            # Do the heavy work after defer
            await self.render_and_publish()
            await inter.followup.send(f"Added {t}: entry={entry}, stop={stop}, target={target}.", ephemeral=True)

        async def setpos(inter: discord.Interaction, ticker: str,
                         entry: Optional[float] = None, stop: Optional[float] = None, target: Optional[float] = None):
            if not self._owner_only(inter):
                return await inter.response.send_message("Only the owner can do this.", ephemeral=True)
            t = ticker.upper().strip()
            pos = self.state.get("positions", {}).get(t)
            if not pos:
                return await inter.response.send_message("Ticker not found.", ephemeral=True)
            if entry and entry > 0:  pos["entry"] = entry
            if stop and stop > 0:    pos["stop"] = stop
            if target and target > 0: pos["target"] = target
            save_data(self.state)
            await inter.response.send_message(f"Updated {t}.", ephemeral=True)
            await self.render_and_publish()

        async def remove(inter: discord.Interaction, ticker: str):
            if not self._owner_only(inter):
                return await inter.response.send_message("Only the owner can do this.", ephemeral=True)
            t = ticker.upper().strip()
            if t in self.state.get("positions", {}):
                self.state["positions"].pop(t, None)
                save_data(self.state)
                await inter.response.send_message(f"Removed {t}.", ephemeral=True)
                await self.render_and_publish()
            else:
                await inter.response.send_message("Ticker not found.", ephemeral=True)

        async def close(inter: discord.Interaction, ticker: str,
                        exit_price: Optional[float] = None, reason: Optional[str] = "manual"):
            if not self._owner_only(inter):
                return await inter.response.send_message("Only the owner can do this.", ephemeral=True)
            t = ticker.upper().strip()
            pos = self.state.get("positions", {}).pop(t, None)
            if not pos:
                return await inter.response.send_message("Ticker not found in open positions.", ephemeral=True)
            if exit_price is None:
                last = await fetch_last_price(t)
                if last is None:
                    return await inter.response.send_message("Could not fetch price. Provide exit_price.", ephemeral=True)
                exit_price = float(last)
            r = (reason or "manual").lower()
            if r not in ("stop", "target", "manual"):
                r = "manual"
            rec = {
                "ticker": t,
                "entry": float(pos["entry"]),
                "stop": float(pos["stop"]),
                "target": float(pos["target"]),
                "exit": float(exit_price),
                "reason": r,
                "created_at": pos.get("created_at"),
                "closed_at": _now_iso(),
                "pnl_pct": pct(float(pos["entry"]), float(exit_price)),
            }
            self.state.setdefault("closed", []).append(rec)
            if len(self.state["closed"]) > CLOSED_MAX:
                self.state["closed"] = self.state["closed"][-CLOSED_MAX:]
            save_data(self.state)
            await inter.response.send_message(
                f"Closed {t} at {exit_price} ({r}), PnL={rec['pnl_pct']:+.2f}%.", ephemeral=True
            )
            await self.render_and_publish()

        # wire commands into group and tree
        group.add_command(app_commands.Command(name="setchannel",  description="Select the read-only channel", callback=setchannel))
        group.add_command(app_commands.Command(name="publish",     description="Publish/refresh the pinned message", callback=publish))
        group.add_command(app_commands.Command(name="debug",       description="Show current config", callback=debug))
        group.add_command(app_commands.Command(
            name="add",
            description="Add a position",
            callback=add
        ))
        group.add_command(app_commands.Command(name="set",         description="Update a position", callback=setpos))
        group.add_command(app_commands.Command(name="remove",      description="Remove a position", callback=remove))
        group.add_command(app_commands.Command(name="close",       description="Manually close a position", callback=close))

        try:
            self.bot.tree.add_command(group)
        except Exception:
            # if hot-reloaded and group already exists, try replacing it
            try:
                self.bot.tree.remove_command("pnl", type=None)
            except Exception:
                pass
            self.bot.tree.add_command(group)

# extension entrypoint
async def setup(bot: commands.Bot):
    await bot.add_cog(PortfolioCog(bot))
