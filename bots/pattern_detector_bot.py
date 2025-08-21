import sys; sys.path.append(".")
import pandas as pd
from pathlib import Path
from pattern_detector.pattern_detecto import PatternDirector
from discord_stock.discord_notifier import DiscordNotifier
from graph_maker.candlestick_plotter import CandlestickPlotter   # <- import your plotter

class BotPatternDetector:
    def __init__(self):
        self.pattern_driver = PatternDirector()
        self.discord_notifier = DiscordNotifier()
        self.plotter = CandlestickPlotter()  # <- init once

    def clean_images_folder(self, folder_path: str):
        """
        Remove all files in the given images folder.

        Args:
            folder_path (str): Path to the folder containing images.
        """
        folder = Path(folder_path)
        if not folder.exists():
            print(f"[clean_images_folder] Folder not found: {folder}")
            return

        removed = 0
        for file in folder.iterdir():
            if file.is_file():
                try:
                    file.unlink()
                    removed += 1
                except Exception as e:
                    print(f"[clean_images_folder] Failed to delete {file}: {e}")

        print(f"[clean_images_folder] Removed {removed} files from {folder}")



    def _pick_color(self, status: str, state: str) -> int:
        if status == "CANCELED NOW": return 0xE74C3C  # red
        if state == "BREAKOUT" or status == "VALID": return 0x2ECC71  # green
        if status == "PENDING": return 0xF39C12     # orange
        return 0x95A5A6                              # gray

    def _fmt(self, x):
        return "-" if x is None else (f"{x:.2f}" if isinstance(x, (int, float)) else str(x))

    def _next_step(self, best: dict) -> str:
        if best.get("status") == "PENDING":
            if best.get("state") == "PRE_BREAKOUT":
                return "Breakout above entry; CANCEL if close < stop."
            if best.get("state") == "CANDLE":
                side = (best.get("side") or "").lower()
                if side == "bull":
                    return "Confirm up next bar; CANCEL if close < signal low."
                if side == "bear":
                    return "Confirm down next bar; CANCEL if close > signal high."
                return "Doji: wait for direction."
        return "—"

    def _safe_codeblock(self, text: str) -> str:
        body = f"```{text}```"
        return body if len(body) <= 4096 else f"```{text[:4000]}\n…(truncated)```"

    def check_stocks_patterns(self):
        results = self.pattern_driver.run(include_report=True)

        for ticker, data in results.items():
            best   = data.get("best")
            report = data.get("report")
            price  = data.get("current_price")
            if not best or not report:
                continue

            color = self._pick_color(best.get("status",""), best.get("state",""))
            rr = best.get("rr")
            rr_txt = f"{rr:.2f}R" if isinstance(rr, (int, float)) else "-"

            header = (
                f"**{best['pattern']}** • **{best['side'].title()}** • **{best['state']}** • "
                f"{'⏳ PENDING' if best['status']=='PENDING' else best['status']}\n"
                f"*{best['date']} • {self.pattern_driver.period}, {self.pattern_driver.interval}*"
            )
            current_price = f"$ {price}"

            levels = (
                f"🔓 **Entry** {self._fmt(best.get('entry'))}  •  "
                f"🛑 **Stop** {self._fmt(best.get('stop'))}  •  "
                f"🎯 **Target** {self._fmt(best.get('target'))}"
            )

            embed = {
                "title": f"🚀 Stock Pattern Detect Alert — {ticker}",
                "description": (
                    f"{header}\n\n"
                    f"**Stock Price**\n{current_price}\n\n"
                    f"**Levels**\n{levels}\n\n"
                    f"**Risk/Reward**\n{rr_txt}\n\n"
                    f"**Next Step**\n{self._next_step(best)}\n\n"
                    f"**Details**\n{self._safe_codeblock(report)}"
                ),
                "color": color,
                "fields": [
                    {"name": "Pattern", "value": f"{best['pattern']} ({best['state']})", "inline": True},
                    {"name": "Status", "value": best['status'], "inline": True},
                ],
                "footer": {"text": "PatternDirector"},
                "timestamp": pd.Timestamp.utcnow().isoformat()
            }

            # ----- render and attach the chart image -----
            try:
                image_path = self.plotter.plot(
                    30, ticker, theme="dark", draw_sma150=True, mav=None, show_price_line=False
                )  # returns graph_maker/images/{TICKER}.png
                # Send embed + attached image
                self.discord_notifier.send_embed_with_image("detected_stocks", embed, image_path)
            except Exception as e:
                # Fall back to embed-only if plotting fails
                embed["description"] += f"\n\n*Chart attachment unavailable ({e}).*"
                self.discord_notifier.send_embed("detected_stocks", embed)
        self.clean_images_folder("graph_maker/images")

    def check_one_symbol(self, ticker: str) -> bool:
        """
        Run the detector only for `ticker`. 
        Returns True if something actionable was sent, else False.
        """
        # Build a one-off PatternDirector with the same settings as the main one
        pd_single = PatternDirector(config={
            "tickers": [ticker.upper()],
            "period":  self.pattern_driver.period,
            "interval": self.pattern_driver.interval,
            "risk": {
                "atr_mult": self.pattern_driver.atr_mult,
                "percent_buffer": self.pattern_driver.pct_buf
            },
            "long_only": self.pattern_driver.long_only,
            "show_bearish_info": self.pattern_driver.show_bearish_info,
            "min_rr_ok": self.pattern_driver.min_rr_ok,
            "momentum_filter": {
                "rsi_min": self.pattern_driver.rsi_min,
                "rsi_hot": self.pattern_driver.rsi_hot,
                "macd_hist_rising_window": self.pattern_driver.macd_hist_rising_window
            },
            "require_above_sma150_for_longs": self.pattern_driver.require_above_sma150_for_longs,
            "require_below_sma150_for_shorts": self.pattern_driver.require_below_sma150_for_shorts,
            "sma150_recent_cross_days": self.pattern_driver.sma150_recent_cross_days,
            "sma150_near_band_pct": self.pattern_driver.sma150_near_band_pct,
        })

        results = pd_single.run(include_report=True)
        data = results.get(ticker.upper())
        if not data:
            return False

        best   = data.get("best")
        report = data.get("report")
        price  = data.get("current_price")

        if not best or not report:
            return False

        # Build embed exactly like your daily method
        color = self._pick_color(best.get("status",""), best.get("state",""))
        rr = best.get("rr")
        rr_txt = f"{rr:.2f}R" if isinstance(rr, (int, float)) else "-"

        header = (
            f"**{best['pattern']}** • **{best['side'].title()}** • **{best['state']}** • "
            f"{'⏳ PENDING' if best['status']=='PENDING' else best['status']}\n"
            f"*{best['date']} • {pd_single.period}, {pd_single.interval}*"
        )
        current_price = f"$ {price}"

        levels = (
            f"🔓 **Entry** {self._fmt(best.get('entry'))}  •  "
            f"🛑 **Stop** {self._fmt(best.get('stop'))}  •  "
            f"🎯 **Target** {self._fmt(best.get('target'))}"
        )

        embed = {
            "title": f"🚀 Stock Pattern Detect Alert — {ticker.upper()}",
            "description": (
                f"{header}\n\n"
                f"**Stock Price**\n{current_price}\n\n"
                f"**Levels**\n{levels}\n\n"
                f"**Risk/Reward**\n{rr_txt}\n\n"
                f"**Next Step**\n{self._next_step(best)}\n\n"
                f"**Details**\n{self._safe_codeblock(report)}"
            ),
            "color": color,
            "fields": [
                {"name": "Pattern", "value": f"{best['pattern']} ({best['state']})", "inline": True},
                {"name": "Status", "value": best['status'], "inline": True},
            ],
            "footer": {"text": "PatternDirector"},
            "timestamp": pd.Timestamp.utcnow().isoformat()
        }

        try:
            image_path = self.plotter.plot(
                30, ticker.upper(), theme="dark", draw_sma150=True, mav=None, show_price_line=False
            )
            self.discord_notifier.send_embed_with_image("detected_stocks", embed, image_path)
        except Exception as e:
            embed["description"] += f"\n\n*Chart attachment unavailable ({e}).*"
            self.discord_notifier.send_embed("detected_stocks", embed)

        # tidy
        self.clean_images_folder("graph_maker/images")
        return True

if __name__ == "__main__":
    BotPatternDetector().check_stocks_patterns()
