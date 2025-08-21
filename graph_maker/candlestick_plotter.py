# candlestick_plotter.py
from __future__ import annotations
from typing import Optional, Tuple
import os
os.environ.setdefault("MPLBACKEND", "Agg")

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
import mplfinance as mpf


class CandlestickPlotter:
    def plot(
        self, days: int, ticker: str, *,
        interval: str = "1d",
        theme: str = "dark",
        mav: Optional[Tuple[int, ...]] = None,
        draw_sma150: bool = True,
        show_price_line: bool = False,
    ):
        if days <= 0:
            raise ValueError("days must be > 0")

        # pull enough history to compute SMA150 cleanly
        extra_days = max(days + 200, 200)
        df = yf.download(
            ticker, period=f"{int(extra_days)}d", interval=interval,
            auto_adjust=False, progress=False, group_by="column",
        )
        if df.empty:
            raise ValueError(f"No data for {ticker} ({days}d, {interval})")

        # flatten/select columns
        if isinstance(df.columns, pd.MultiIndex):
            if ticker in df.columns.get_level_values(0):
                df = df.xs(ticker, axis=1, level=0)
            else:
                df.columns = [" ".join(map(str, c)).strip() for c in df.columns.to_flat_index()]

        def norm(col: str) -> str:
            s = str(col).replace("_", " ").replace(".", " ").strip()
            toks = [t for t in s.split() if t.upper() != ticker.upper()]
            s = " ".join(toks) if toks else s
            l = s.lower()
            if "adj" in l and "close" in l: return "Adj Close"
            if "open" in l:  return "Open"
            if "high" in l:  return "High"
            if "low" in l:   return "Low"
            if "close" in l: return "Close"
            if "volume" in l or l == "vol": return "Volume"
            return s
        df.columns = [norm(c) for c in df.columns]

        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_localize(None)

        for c in ("Open", "High", "Low", "Close", "Adj Close", "Volume"):
            if c in df.columns:
                s = df[c]
                if isinstance(s, pd.DataFrame):
                    s = s.squeeze("columns")
                df[c] = pd.to_numeric(s, errors="coerce")

        need = ("Open", "High", "Low", "Close")
        if not all(c in df.columns for c in need):
            raise KeyError(f"Missing OHLC columns after cleaning. Got: {list(df.columns)}")

        keep = [*need]
        if "Volume" in df.columns: keep.append("Volume")
        if "Adj Close" in df.columns: keep.append("Adj Close")
        df = df[keep].dropna(subset=list(need))

        # --- indicators on full data ---
        if draw_sma150:
            src = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
            df["SMA150"] = src.rolling(150, min_periods=150).mean()

        # slice to the visible window
        df = df.tail(days)

        # ensure SMA150 spans entire window
        add_plots = []
        if draw_sma150 and "SMA150" in df.columns:
            df["SMA150"] = df["SMA150"].ffill()
            if df["SMA150"].notna().any():
                add_plots.append(mpf.make_addplot(df["SMA150"], color="#ffa500", width=2.0, panel=0))

        if show_price_line:
            last_close = float(df["Close"].iloc[-1])
            add_plots.append(mpf.make_addplot(np.full(len(df), last_close),
                                              color="#bfbfbf", linestyle="--", width=1.0, panel=0))

        # theme
        if theme.lower() == "dark":
            mc = mpf.make_marketcolors(up="#22c55e", down="#ef4444", wick="inherit",
                                       edge="inherit", volume="in")
            style = mpf.make_mpf_style(
                base_mpf_style="nightclouds", marketcolors=mc,
                gridstyle="--", gridcolor="#2b2f36",
                facecolor="#0f1419", edgecolor="#0f1419", figcolor="#0f1419",
                rc={"axes.labelcolor":"#e5e7eb","xtick.color":"#9ca3af","ytick.color":"#9ca3af"},
            )
        else:
            mc = mpf.make_marketcolors(up="#16a34a", down="#dc2626", wick="inherit",
                                       edge="inherit", volume="in")
            style = mpf.make_mpf_style(base_mpf_style="yahoo", marketcolors=mc,
                                       gridstyle="--", gridcolor="#e5e7eb")

        title = f"{ticker} • {days}d ({interval})"

        # build kwargs so we can omit mav when None
        plot_kwargs = dict(
            type="candle",
            style=style,
            addplot=(add_plots or None),
            volume=("Volume" in df.columns),
            panel_ratios=(3, 1),
            figratio=(16, 9),
            figscale=1.2,
            tight_layout=True,
            title=title,
            datetime_format="%b %d",
            returnfig=True,
        )
        if mav is not None:
            plot_kwargs["mav"] = mav

        fig, _ = mpf.plot(df, **plot_kwargs)

        # --- Save instead of show ---
        save_dir = os.path.join("graph_maker", "images")
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{ticker.upper()}.png")
        fig.savefig(save_path, dpi=150)
        plt.close(fig)
        return save_path


if __name__ == "__main__":
    path = CandlestickPlotter().plot(30, "AAPL", theme="dark", draw_sma150=True)
    print(f"Saved to {path}")
