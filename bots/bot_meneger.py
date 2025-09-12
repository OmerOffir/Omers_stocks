import platform
import sys
import os, sys; sys.path.append(".")
import os, certifi
if platform.system() == "Darwin":
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["SSL_CERT_DIR"] = os.path.dirname(certifi.where())
import re
import json
import asyncio
import logging
from zoneinfo import ZoneInfo  # Python 3.9+
from datetime import datetime, timedelta
from urllib import request as _urlreq
from urllib.error import URLError
from discord_stock.token import discord_tok

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import discord
from discord.ext import commands
from discord import app_commands

# NEW: live prices
import yfinance as yf

from bots.pattern_detector_bot import BotPatternDetector


logging.basicConfig(level=logging.DEBUG) 
# -----------------------
# Config (env-driven)
# -----------------------
DISCORD_TOKEN = f"{discord_tok.dis_1}{discord_tok.dis_2}{discord_tok.dis_3}"

LISTEN_CHANNEL_ID = int(os.getenv("LISTEN_CHANNEL_ID", "0")) or 1406720397893828760  # replace fallback
PATTERN_CHANNEL_ID = int(os.getenv("PATTERN_CHANNEL_ID", "0")) or 1404051018320449627
ALERT_CHANNEL_ID  = int(os.getenv("ALERT_CHANNEL_ID", "0")) or None  # where crossing alerts are posted; defaults to listen channel

# Pattern detections destination
DETECTED_STOCKS_CHANNEL_ID = int(os.getenv("DETECTED_STOCKS_CHANNEL_ID", "0")) or None
DETECTED_STOCKS_WEBHOOK    = os.getenv("DETECTED_STOCKS_WEBHOOK")

# Files
CACHE_FILE = os.getenv("SEEN_ALERTS_FILE", r"crossing_bot\seen_alerts.json")              # fired crossing alerts for dedupe
WATCH_FILE = os.getenv("WATCHLIST_FILE", r"crossing_bot\watchlist.json")                  # watchlist: {"MAGS": [{"level": 60.0, "direction": "up"}], ...}

# Polling cadence (seconds)
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))

# Message format to register watches
ALERT_REGEX = re.compile(
    r"^\s*([A-Za-z]{1,10})\s+cross\s+([0-9]*\.?[0-9]+)\s*(up|down)?\s*$",
    re.IGNORECASE,
)

# -----------------------
# Utilities / persistence
# -----------------------
def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _load_json(path: str, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.getLogger("persist").warning(f"Failed to load {path}: {e!r}")
        return fallback

def _save_json(path: str, obj) -> None:
    _ensure_dir(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)

def _make_key(ticker: str, level: float | str, direction: str | None) -> str:
    d = (direction or "").lower()
    t = ticker.upper()
    try:
        lv = f"{float(level):.4f}".rstrip("0").rstrip(".")
    except Exception:
        lv = str(level)
    return f"{t}|{lv}|{d}"

# -----------------------
# Discord Cross-Alert Bot (with auto monitor)
# -----------------------
class CrossAlertBot:
    def __init__(self, pattern_bot: BotPatternDetector | None = None):
        intents = discord.Intents.default()
        intents.message_content = True  # ensure Message Content Intent enabled in portal
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self.tree = self.bot.tree
        self.pattern_bot = pattern_bot

        self.log = logging.getLogger("CrossAlertBot")
        self._task: asyncio.Task | None = None
        self._started = asyncio.Event()

        # persistence
        self.seen = _load_json(CACHE_FILE, {})              # fired crossings
        self.watchlist: dict[str, list[dict]] = _load_json(WATCH_FILE, {})
        self.last_prices: dict[str, float] = {}             # last seen price per ticker (for *crossing* detection)

        self._setup_handlers()

    def _normalize_ticker_for_yf(self, t: str) -> str:
        t = t.upper().strip()
        if "-" in t:
            return t
        m = re.fullmatch(r"([A-Z]{2,5})USD", t)
        return f"{m.group(1)}-USD" if m else t


    # ---------- Handlers / commands ----------
    def _setup_handlers(self):
        @self.bot.event
        async def on_ready():
            self.log.info(f"[discord] Logged in as {self.bot.user} (id: {self.bot.user.id})")
            try:
                await self.tree.sync()
                self.log.info("[discord] Slash commands synced.")
            except Exception as e:
                self.log.warning(f"[discord] Slash command sync failed: {e!r}")
            # start the monitor loop
            self.bot.loop.create_task(self._monitor_prices_task())
            self._started.set()

        @self.bot.event
        async def on_message(message: discord.Message):
            # --- deep debug so we know what's happening ---
            self.log.debug(
                "on_message: guild=%s channel_id=%s channel_name=%s author_bot=%s content=%r",
                getattr(message.guild, "id", None),
                getattr(message.channel, "id", None),
                getattr(message.channel, "name", None),
                message.author.bot,
                message.content,
            )

            if message.author.bot:
                return

            # If Message Content Intent is off, content can be None/empty
            if message.content is None:
                self.log.warning("Message content is None (Message Content Intent disabled?).")
                return

            content = message.content.strip()
            if not content:
                self.log.debug("Empty message content; ignoring.")
                return

            # ---------------- Pattern requests channel ----------------
            if message.channel.id == PATTERN_CHANNEL_ID:
                # Expect a bare ticker like AAPL / BTC-USD
                if re.fullmatch(r"[A-Za-z0-9.\-]{1,12}", content):
                    tkr = self._normalize_ticker_for_yf(content)
                    async with message.channel.typing():
                        ok = await asyncio.to_thread(self.pattern_bot.check_one_symbol, tkr)
                    if not ok:
                        await message.channel.send(f"🧐 `{tkr}` — nothing interesting found.")
                else:
                    await message.channel.send("Send just a ticker, e.g. `AAPL` or `BTC-USD`.")
                return

            # ---------------- housekeeping: clean ----------------
            if content.lower().startswith("clean "):
                parts = content.split()
                if len(parts) >= 2:
                    t = parts[1].upper()
                    lvl = None
                    dirn = None
                    if len(parts) >= 3:
                        try:
                            lvl = float(parts[2])
                        except ValueError:
                            lvl = None
                    if len(parts) >= 4 and parts[3].lower() in ("up", "down"):
                        dirn = parts[3].lower()

                    # clear fired alerts
                    removed_alerts = 0
                    for k in list(self.seen.keys()):
                        kt, kl, kd = (k.split("|") + ["", ""])[:3]
                        if kt != t:
                            continue
                        if lvl is not None:
                            nl = f"{float(lvl):.4f}".rstrip("0").rstrip(".")
                            if kl != nl:
                                continue
                        if dirn is not None and (kd or "") != dirn:
                            continue
                        del self.seen[k]
                        removed_alerts += 1
                    _save_json(CACHE_FILE, self.seen)

                    # clear from watchlist
                    removed_watches = 0
                    if t in self.watchlist:
                        if lvl is None:
                            removed_watches = len(self.watchlist.get(t, []))
                            if removed_watches:
                                del self.watchlist[t]
                                _save_json(WATCH_FILE, self.watchlist)
                        else:
                            cur_list = self.watchlist.get(t, [])
                            nl = f"{float(lvl):.4f}".rstrip("0").rstrip(".")
                            new_list = []
                            for it in cur_list:
                                it_lvl = f"{float(it['level']):.4f}".rstrip("0").rstrip(".")
                                it_dir = (it.get("direction") or None)
                                match = (it_lvl == nl) and ((dirn is None) or (it_dir == dirn))
                                if match:
                                    removed_watches += 1
                                else:
                                    new_list.append(it)
                            if removed_watches:
                                if new_list:
                                    self.watchlist[t] = new_list
                                else:
                                    del self.watchlist[t]
                                _save_json(WATCH_FILE, self.watchlist)

                    pieces = [f"🧹 Cleared {removed_alerts} fired alert(s)"]
                    if removed_watches or lvl is None:
                        pieces.append(f"and {removed_watches} watch(es)")
                    spec = []
                    if lvl is not None: spec.append(f"@ {lvl}")
                    if dirn is not None: spec.append(f" {dirn.upper()}")
                    await message.channel.send(f"{' '.join(pieces)} for **{t}**{''.join(spec)}")
                else:
                    await message.channel.send("Usage: `clean TICKER [level] [up|down]`")
                return

            # ---------------- crossing registrations channel ----------------
            if message.channel.id != LISTEN_CHANNEL_ID:
                self.log.debug("Ignoring message from non-listen channel %s", message.channel.id)
                return

            parsed = self._parse_alert(content)
            if not parsed:
                # Helpful hint so you can see it's at least reading messages
                await message.channel.send("Format: `TICKER cross LEVEL [up|down]` e.g. `AAPL cross 190 up`")
                return

            ticker, level, direction = parsed
            self._add_watch(ticker, level, direction)
            dest = self._resolve_alert_channel(message.channel)
            await self._send_confirmation(dest, ticker, level, direction)
            self.log.info(f"[watchlist] added: {ticker} level={level} dir={direction or 'any'}")

        # ----- slash commands (unchanged) -----
        @self.tree.command(name="watch_list", description="Show current watchlist")
        async def watch_list(interaction: discord.Interaction):
            lines = []
            for t, items in self.watchlist.items():
                for it in items:
                    lvl = f"{float(it['level']):.4f}".rstrip("0").rstrip(".")
                    d = it.get("direction") or "any"
                    lines.append(f"- {t} cross {lvl} {d}")
            msg = "\n".join(lines) if lines else "Watchlist is empty."
            await interaction.response.send_message(msg, ephemeral=True)

        @self.tree.command(name="watch_remove", description="Remove a watch (ticker, level, optional direction)")
        @app_commands.describe(ticker="e.g., MAGS", level="e.g., 60.0", direction="up/down (optional)")
        async def watch_remove(interaction: discord.Interaction, ticker: str, level: float, direction: str | None = None):
            ok = self._remove_watch(ticker, level, direction)
            await interaction.response.send_message("✅ Removed." if ok else "❌ Not found.", ephemeral=True)

        @self.tree.command(name="reset_alert", description="Reset a fired crossing alert")
        @app_commands.describe(ticker="e.g., MAGS", level="e.g., 60.0", direction="up/down (optional)")
        async def reset_alert(interaction: discord.Interaction, ticker: str, level: float, direction: str | None = None):
            key = _make_key(ticker, level, direction)
            if key in self.seen:
                del self.seen[key]
                _save_json(CACHE_FILE, self.seen)
                await interaction.response.send_message(f"♻️ Reset `{key}`", ephemeral=True)
            else:
                await interaction.response.send_message(f"ℹ️ Not found `{key}`", ephemeral=True)

        @self.tree.command(name="reset_all_alerts", description="Reset ALL fired crossing alerts")
        async def reset_all_alerts(interaction: discord.Interaction):
            self.seen.clear()
            _save_json(CACHE_FILE, self.seen)
            await interaction.response.send_message("🧹 Cleared all fired alerts.", ephemeral=True)

        @self.tree.command(name="detect", description="Run pattern detector for a single ticker now")
        @app_commands.describe(ticker="e.g., GOOGL")
        async def detect(interaction: discord.Interaction, ticker: str):
            await interaction.response.defer(ephemeral=True, thinking=True)
            tkr = self._normalize_ticker_for_yf(ticker)
            if not re.fullmatch(r"[A-Za-z0-9.\-]{1,12}", tkr):
                await interaction.followup.send("Ticker format not recognized.", ephemeral=True)
                return
            ok = False
            if self.pattern_bot:
                ok = await asyncio.to_thread(self.pattern_bot.check_one_symbol, tkr)
            await interaction.followup.send(
                f"Done. {'Posted an alert.' if ok else 'Nothing interesting here.'}",
                ephemeral=True
            )


    # ---------- Helpers ----------
    @staticmethod
    def _parse_alert(msg: str):
        m = ALERT_REGEX.match(msg)
        if not m:
            return None
        ticker = m.group(1).upper()
        level = float(m.group(2))
        direction = m.group(3).lower() if m.group(3) else None
        return ticker, level, direction

    def _resolve_alert_channel(self, fallback_channel: discord.abc.Messageable):
        if ALERT_CHANNEL_ID and hasattr(self.bot, "get_channel"):
            ch = self.bot.get_channel(ALERT_CHANNEL_ID)
            if ch:
                return ch
        return fallback_channel

    async def _send_confirmation(self, channel: discord.abc.Messageable, ticker: str, level: float, direction: str | None):
        dir_txt = f" {direction.upper()}" if direction else ""
        lvl = f"{float(level):.4f}".rstrip("0").rstrip(".")
        await channel.send(f"✅ Alert recorded: **{ticker}** cross **{lvl}**{dir_txt}. (monitoring)")

    def _add_watch(self, ticker: str, level: float, direction: str | None):
        t = ticker.upper()
        w = self.watchlist.get(t, [])
        # avoid duplicates
        for it in w:
            if abs(float(it["level"]) - level) < 1e-9 and (it.get("direction") or None) == (direction or None):
                break
        else:
            w.append({"level": float(level), "direction": (direction or None)})
            self.watchlist[t] = w
            _save_json(WATCH_FILE, self.watchlist)

    def _remove_watch(self, ticker: str, level: float, direction: str | None) -> bool:
        t = ticker.upper()
        w = self.watchlist.get(t, [])
        new_w = [it for it in w if not (abs(float(it["level"]) - level) < 1e-9 and (it.get("direction") or None) == (direction or None))]
        if len(new_w) != len(w):
            self.watchlist[t] = new_w
            _save_json(WATCH_FILE, self.watchlist)
            return True
        return False

    # ---------- Price monitor ----------
    async def _monitor_prices_task(self):
        await self.bot.wait_until_ready()
        self.log.info(f"[monitor] started; polling every {POLL_SECONDS}s")

        dest_channel = None  # resolve once available
        while not self.bot.is_closed():
            try:
                if dest_channel is None:
                    dest_channel = self.bot.get_channel(ALERT_CHANNEL_ID or LISTEN_CHANNEL_ID)

                for ticker, items in list(self.watchlist.items()):
                    cur = await self._fetch_last_price(ticker)
                    if cur is None:
                        continue

                    last = self.last_prices.get(ticker, cur)  # initialize to cur to avoid false first tick
                    self.last_prices[ticker] = cur

                    for it in items:
                        level = float(it["level"])
                        direction = it.get("direction")
                        if self._crossed(last, cur, level, direction):
                            key = _make_key(ticker, level, direction)
                            if not self.seen.get(key):
                                if dest_channel:
                                    dir_txt = f" {direction.upper()}" if direction else ""
                                    lvl = f"{level:.4f}".rstrip("0").rstrip(".")
                                    await dest_channel.send(f"📈 **{ticker}** crossed **{lvl}**{dir_txt} — last: `{cur:.2f}`")
                                self.seen[key] = True
                                _save_json(CACHE_FILE, self.seen)
                                self.log.info(f"[alert] fired {key} at price={cur:.4f}")

            except Exception as e:
                self.log.error(f"[monitor] loop error: {e!r}")

            await asyncio.sleep(POLL_SECONDS)

    def _to_yf_symbol(self, ticker: str) -> str:
        t = ticker.upper().strip()

        # If already contains a hyphen pair like BTC-USD, leave it:
        if "-" in t:
            return t

        # Common cryptos typed without hyphen -> add it
        # Pattern: 3-5 letters followed by USD
        # e.g., ETHUSD, BTCUSD, SOLUSD, ADAUSD, XRPUSD
        m = re.match(r"^([A-Z]{2,5})USD$", t)
        if m:
            base = m.group(1)
            return f"{base}-USD"

        # Everything else (equities/ETFs like AAPL, MAGS, QQQ)
        return t


    @staticmethod
    def _crossed(last_p: float, cur_p: float, level: float, direction: str | None) -> bool:
        if direction == "up":
            return last_p < level <= cur_p
        if direction == "down":
            return last_p > level >= cur_p
        return (last_p < level <= cur_p) or (last_p > level >= cur_p)

    async def _fetch_last_price(self, ticker: str) -> float | None:
        yf_symbol = self._to_yf_symbol(ticker)

        # Try fast_info robustly
        try:
            fi = yf.Ticker(yf_symbol).fast_info
            def _getfi(k):
                try:
                    return fi.get(k) if hasattr(fi, "get") else getattr(fi, k, None)
                except Exception:
                    return None
            for k in ("last_price", "regular_market_price", "last_close"):
                v = _getfi(k)
                if v is not None:
                    return float(v)
        except Exception:
            pass

        # Fallback 1m history (include pre/post for crypto & extended hours)
        try:
            hist = yf.download(
                tickers=yf_symbol, period="1d", interval="1m",
                progress=False, threads=False, prepost=True
            )
            if hist is not None and not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as e:
            self.log.warning(f"[price] fetch failed for {ticker} -> {yf_symbol}: {e!r}")

        self.log.warning(f"[price] no data for {ticker} -> {yf_symbol}")
        return None

    # ---------- Lifecycle ----------
    async def start(self):
        if self._task and not self._task.done():
            self.log.info("[discord] already running.")
            return
        if not DISCORD_TOKEN:
            raise RuntimeError("DISCORD_TOKEN env var is missing.")
        self.log.info("[discord] starting…")
        self._task = asyncio.create_task(self.bot.start(DISCORD_TOKEN))
        await self._started.wait()
        self.log.info("[discord] started.")

    async def stop(self):
        if self.bot.is_closed():
            return
        self.log.info("[discord] stopping…")
        await self.bot.close()
        if self._task:
            try:
                await self._task
            except Exception:
                pass
        self.log.info("[discord] stopped.")

# -----------------------
# Notifier for pattern bot
# -----------------------
class DiscordNotifier:
    def __init__(self, cross_bot: CrossAlertBot):
        self.cross_bot = cross_bot
        self.log = logging.getLogger("DiscordNotifier")

    async def send_to_detected_stocks(self, text: str):
        # Prefer bot + channel ID
        if DETECTED_STOCKS_CHANNEL_ID and self.cross_bot and self.cross_bot.bot and not self.cross_bot.bot.is_closed():
            ch = self.cross_bot.bot.get_channel(DETECTED_STOCKS_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(text)
                    self.log.info("[detected] sent via bot")
                    return
                except Exception as e:
                    self.log.warning(f"[detected] bot send failed: {e!r}")

        # Fallback webhook
        if DETECTED_STOCKS_WEBHOOK:
            payload = json.dumps({"content": text}).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            def _post():
                req = _urlreq.Request(DETECTED_STOCKS_WEBHOOK, data=payload, headers=headers, method="POST")
                with _urlreq.urlopen(req, timeout=10) as _:
                    pass
            try:
                await asyncio.to_thread(_post)
                self.log.info("[detected] sent via webhook")
                return
            except URLError as e:
                self.log.error(f"[detected] webhook failed: {e!r}")

        self.log.error("[detected] no route to send (set DETECTED_STOCKS_CHANNEL_ID or DETECTED_STOCKS_WEBHOOK)")

# -----------------------
# Manager (pattern @ 08:00 + make sure Discord is running)
# -----------------------
class BotManager:
    def __init__(self, tz: str = "Asia/Tel_Aviv"):
        self.tz = ZoneInfo(tz)
        self.scheduler = AsyncIOScheduler(timezone=self.tz)
        self.pattern_bot = BotPatternDetector()
        self.cross_bot = CrossAlertBot(pattern_bot=self.pattern_bot)
        self.notifier = DiscordNotifier(self.cross_bot)
        self._discord_started = False

    async def _run_pattern_bot(self):
        try:
            if self.pattern_bot is None:
                from bots.pattern_detector_bot import BotPatternDetector
                self.pattern_bot = BotPatternDetector()
            await asyncio.to_thread(self.pattern_bot.check_stocks_patterns)
        except Exception as e:
            print(f"[pattern_bot] error: {e!r}")


    async def _start_discord_once(self):
        if self._discord_started:
            print("[discord] already started; skipping.")
            return
        try:
            await self.cross_bot.start()
            self._discord_started = True
        except Exception as e:
            print(f"[discord] start error: {e!r}")

    def schedule_jobs(self):
        # Daily at 08:00 — run your report/pattern bot
        self.scheduler.add_job(
            self._run_pattern_bot,
            CronTrigger(hour=8, minute=0),
            id="pattern_daily",
            max_instances=1,
            misfire_grace_time=900,
            coalesce=True,
        )
        # Daily at 08:00 — ensure Discord is running
        self.scheduler.add_job(
            self._start_discord_once,
            CronTrigger(hour=8, minute=0),
            id="discord_start_daily",
            max_instances=1,
            misfire_grace_time=900,
            coalesce=True,
        )

    def _fmt_tdelta(self, td: timedelta) -> str:
        secs = int(max(td.total_seconds(), 0))
        h, r = divmod(secs, 3600)
        m, s = divmod(r, 60)
        if h: return f"{h}h {m}m {s}s"
        if m: return f"{m}m {s}s"
        return f"{s}s"

    def start(self):
        self.schedule_jobs()
        self.scheduler.start()

        # START DISCORD IMMEDIATELY ON BOOT
        asyncio.create_task(self._start_discord_once())   # <-- change to this

        for jid in ("pattern_daily", "discord_start_daily"):
            job = self.scheduler.get_job(jid)
            if job and job.next_run_time:
                nxt = job.next_run_time.astimezone(self.tz)
                print(f"[{jid}] next run at {nxt:%Y-%m-%d %H:%M %Z}")

    async def run_forever(self):
        self.start()
        while True:
            jobs = [j for j in (self.scheduler.get_job("pattern_daily"),
                                self.scheduler.get_job("discord_start_daily")) if j]
            next_times = [j.next_run_time.astimezone(self.tz) for j in jobs if j and j.next_run_time]
            if next_times:
                nxt = min(next_times)
                now = datetime.now(self.tz)
                remaining = self._fmt_tdelta(nxt - now)
                print(f"\r[next job] at {nxt:%Y-%m-%d %H:%M %Z} (in {remaining})", end="", flush=True)
            await asyncio.sleep(1)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(BotManager().run_forever())
