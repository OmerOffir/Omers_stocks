from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from pprint import pprint
import numpy as np
import pandas as pd
import yfinance as yf
import warnings
import re

# Quiet the noisy library warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)


class PatternDirector:
    """
    One-stop 'present-focused' pattern detector for stocks.
    - Candlesticks: Hammer, Shooting Star, Doji, Bullish/Bearish Engulfing
    - Chart patterns: Cup & Handle, Double Bottom, Head & Shoulders
    - Prints actionable plan (entry/stop/target) + 'cancel now' logic
    """

    @dataclass
    class Plan:
        pattern: str
        side: str           # 'bull' | 'bear' | 'neutral'
        state: str          # 'CANDLE' | 'PRE_BREAKOUT' | 'BREAKOUT'
        date: pd.Timestamp
        entry: Optional[float]
        stop: Optional[float]
        target: Optional[float]
        cancel_now: bool
        status: str         # 'VALID' | 'PENDING' | 'CANCELED NOW'
        notes: str = ""

    def __init__(self,
                 config: Optional[Dict] = None,
                 config_path: Optional[str | Path] = "pattern_detectort\stocks.json"):
        """
        config can be a dict like:
          {
            "tickers": ["AAPL", ...],
            "period": "1y",
            "interval": "1d",
            "risk": {"atr_mult": 1.0, "percent_buffer": 0.0025}
          }
        If config is None, reads config_path JSON.
        """
        if config is None:
            cfg = self._load_config(config_path)
        else:
            cfg = self._normalize_config(config)

        self.show_bearish_info = bool(cfg.get("show_bearish_info", True))
        self.tickers: List[str] = cfg["tickers"]
        self.period: str = cfg["period"]
        self.interval: str = cfg["interval"]
        self.atr_mult: float = float(cfg["risk"].get("atr_mult", 1.0))
        self.pct_buf: float = float(cfg["risk"].get("percent_buffer", 0.0025))
        self.long_only: bool = bool(cfg.get("long_only", False))
        self.relevance = {
            "fresh_breakout_max_atr": 0.75,   # how far above entry a breakout can be and still be 'fresh'
            "fresh_breakout_max_pct": 0.01,   # or within 1% of entry
            "max_beyond_target_pct": 0.0025  # if price basically tagged target, hide
        }
        self.fresh_breakout_max_atr = self.relevance["fresh_breakout_max_atr"]
        self.fresh_breakout_max_pct = self.relevance["fresh_breakout_max_pct"]
        self.max_beyond_target_pct  = self.relevance["max_beyond_target_pct"]
        mom = cfg.get("momentum_filter", {})
        self.min_rr_ok =  float(cfg["min_rr_ok"])
        self.rsi_min  = float(mom.get("rsi_min", 50))
        self.rsi_hot  = float(mom.get("rsi_hot", 80))
        self.macd_hist_rising_window = int(mom.get("macd_hist_rising_window", 3))
        self.require_above_sma150_for_longs = bool(cfg.get("require_above_sma150_for_longs", False))
        self.require_below_sma150_for_shorts = bool(cfg.get("require_below_sma150_for_shorts", False))
        self.sma150_recent_cross_days = int(cfg.get("sma150_recent_cross_days", 0))
        self.sma150_near_band_pct = float(cfg.get("sma150_near_band_pct", 5.0))

        thrust = cfg.get("thrust", {})
        self.thrust_min_atr_mult = float(thrust.get("min_atr_mult", 1.0))
        self.thrust_min_body_ratio = float(thrust.get("min_body_ratio", 0.6))
        self.thrust_min_close_pos  = float(thrust.get("min_close_pos", 0.8))

    # --------------- Public API ---------------

    def run(self, include_report: bool = True) -> Dict[str, Dict]:
        results: Dict[str, Dict] = {}
        for t in self.tickers:
            df = self._get_history(t, self.period, self.interval)

            chart_plans: List[PatternDirector.Plan] = []
            bear_chart_info: List[PatternDirector.Plan] = []

            for detector in (
                self._detect_cup_and_handle,
                self._detect_double_bottom,
                self._detect_head_and_shoulders,
                self._detect_double_top,
                self._detect_triple_bottom,
                self._detect_ascending_triangle,
                self._detect_descending_triangle,
                self._detect_symmetrical_triangle,
                # flags (bull/bear)
                lambda df: self._detect_flag(df, side="bull"),
                (lambda df: None) if self.long_only else (lambda df: self._detect_flag(df, side="bear")),
                self._detect_near_sma150,
                ):
                plan = detector(df)
                if not plan:
                    continue
                if plan.side == "bear" and self.long_only:
                    if self.show_bearish_info:
                        bear_chart_info.append(plan)
                    continue
                chart_plans.append(plan)

            candle_plans, bear_candle_info = self._latest_candle_plan(df)

            # actionable filter (long-only remains actionable)
            actionable_charts, actionable_candles = [], []
            for p in chart_plans:
                ok, why = self._is_actionable_now(p, df)
                (actionable_charts if ok else []).append(p) if ok else setattr(p, "notes", (p.notes + f" | filtered:{why}").strip())
            for c in candle_plans:
                ok, why = self._is_actionable_now(c, df)
                (actionable_candles if ok else []).append(c) if ok else setattr(c, "notes", (c.notes + f" | filtered:{why}").strip())

            best = self._choose_best_plan(actionable_charts, actionable_candles)
            current_price = self.get_current_price(t)

            payload = {
                "ticker": t,
                "period": self.period,
                "interval": self.interval,
                "best": self._plan_to_dict(best) if best else None,
                "current_price": current_price,
                "actionable": {
                    "charts":  [self._plan_to_dict(p) for p in actionable_charts],
                    "candles": [self._plan_to_dict(p) for p in actionable_candles],
                },
                "bearish_info": {
                    "charts":  [self._plan_to_dict(p) for p in bear_chart_info],
                    "candles": [self._plan_to_dict(p) for p in bear_candle_info],
                },
            }
            if best and include_report:
                payload["report"] = self._format_report(t, df, best, actionable_charts, actionable_candles)

            results[t] = payload
        return results

    # --------------- Config / Data ---------------

    def _load_config(self, json_path: str | Path) -> Dict:
        json_path = Path(json_path)
        with open(json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return self._normalize_config(raw)

    def get_current_price(self, ticker: str, prefer_intraday: bool = True) -> Optional[float]:
        """
        Return the most recent traded price for `ticker` using yfinance.
        - Tries intraday (1m) first if available.
        - Falls back to last daily close.
        - As a last resort, uses fast_info/info.
        Returns None if nothing could be fetched.
        """
        try:
            # 1) Intraday (best when market is open)
            if prefer_intraday:
                df = yf.download(ticker, period="5d", interval="1m",
                                progress=False, auto_adjust=False)
                if not df.empty and "Close" in df.columns:
                    last = df["Close"].dropna().iloc[-1]
                    return float(last)

            # 2) Last daily close
            df = yf.download(ticker, period="1mo", interval="1d",
                            progress=False, auto_adjust=False)
            if not df.empty and "Close" in df.columns:
                last = df["Close"].dropna().iloc[-1]
                return float(last)

            # 3) Fallbacks: fast_info / info
            tkr = yf.Ticker(ticker)
            fi = getattr(tkr, "fast_info", None)
            if fi and hasattr(fi, "get"):
                val = fi.get("last_price") or fi.get("regular_market_price") or fi.get("previous_close")
                if val is not None:
                    return float(val)

            info = getattr(tkr, "info", {}) or {}
            val = info.get("regularMarketPrice") or info.get("previousClose")
            return float(val) if val is not None else None

        except Exception:
            return None

    def _read_tickers_file(self, path: str):
        p = Path(path)
        if not p.exists(): return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            # allow comma/space separated on one line
            out.extend([s for s in re.split(r"[,\s]+", line.upper()) if s])
        return out


    def _normalize_config(self, raw: Dict) -> Dict:
        raw.setdefault("tickers", [])
        raw.setdefault("tickers_files", [])
        raw.setdefault("period", "1y")
        raw.setdefault("interval", "1d")
        raw.setdefault("risk", {"atr_mult": 1.0, "percent_buffer": 0.0025})
        raw.setdefault("long_only", True)
        raw.setdefault("show_bearish_info", True)
        raw.setdefault("min_rr_ok", 1.2)
        raw.setdefault("require_above_sma150_for_longs", False)
        raw.setdefault("require_below_sma150_for_shorts", False)
        raw.setdefault("sma150_recent_cross_days", 0)

        tickers = [t.upper() for t in raw.get("tickers", [])]
        for f in raw.get("tickers_files", []):
            tickers += self._read_tickers_file(f)

        # dedupe + stable sort
        raw["tickers"] = sorted(set(tickers))
        if not raw["tickers"]:
            raise ValueError("Config must contain non-empty 'tickers' (direct or from tickers_files).")
        return raw

    def _get_history(self, ticker: str, period: str, interval: str) -> pd.DataFrame:
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=False)
        if df.empty:
            raise ValueError(f"No data for {ticker}.")
        df = df.rename(columns=str.lower).dropna()
        df["atr14"] = self._atr(df, 14)
        df["rsi14"] = self._rsi(df["close"], 14)
        df["macd"], df["macd_signal"], df["macd_hist"] = self._macd(df["close"])
        df["sma150"] = df["close"].rolling(150, min_periods=150).mean()  
        return df

    def _atr(self, df: pd.DataFrame, n: int = 14) -> pd.Series:
        h, l, c = df["high"], df["low"], df["close"]
        prev_c = c.shift(1)
        tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        return tr.rolling(n, min_periods=n).mean()

    def _rsi(self, close: pd.Series, n: int = 14) -> pd.Series:
        delta = close.diff()
        up = delta.clip(lower=0.0)
        down = -delta.clip(upper=0.0)
        roll_up = up.ewm(alpha=1/n, adjust=False).mean()
        roll_down = down.ewm(alpha=1/n, adjust=False).mean()
        rs = roll_up / (roll_down + 1e-9)
        return 100 - (100 / (1 + rs))

    def _macd(self, close: pd.Series, fast=12, slow=26, signal=9):
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        sig = macd.ewm(span=signal, adjust=False).mean()
        hist = macd - sig
        return macd, sig, hist


    def _levels_for_inside_bullish(self, hi, lo, atr_now, ref_price, r_mult=2.0, atr_buf_mult=0.25):
        """
        Buy-stop above inside bar high; stop just below inside bar low; target = r_mult * risk.
        atr_buf_mult defaults to 0.25 ATR for tighter triggers on inside bars.
        """
        buf = max(atr_now * atr_buf_mult, ref_price * self.pct_buf)
        entry  = float(hi) + buf
        stop   = float(lo) - buf
        risk   = max(entry - stop, 1e-9)
        target = entry + r_mult * risk
        return entry, stop, float(target)

    def _status_for_signal(self, cancel_now: bool, signal_is_today: bool) -> str:
        if cancel_now:
            return "CANCELED NOW"
        return "PENDING" if signal_is_today else "VALID"


    def _plan_to_dict(self, p: "PatternDirector.Plan") -> Dict:
        d = {
            "pattern": p.pattern,
            "side": p.side,
            "state": p.state,
            "date": str(pd.Timestamp(p.date).date()) if p.date is not None else None,
            "entry": None if p.entry is None else float(p.entry),
            "stop":  None if p.stop  is None else float(p.stop),
            "target":None if p.target is None else float(p.target),
            "cancel_now": bool(p.cancel_now),
            "status": p.status,
            "notes": p.notes or "",
        }
        # attach RR if we can compute it
        if d["entry"] is not None and d["stop"] is not None and d["target"] is not None:
            risk   = max(d["entry"] - d["stop"], 1e-9) if p.side == "bull" else max(d["stop"] - d["entry"], 1e-9)
            reward = (d["target"] - d["entry"]) if p.side == "bull" else (d["entry"] - d["target"])
            d["rr"] = float(reward / risk)
        return d

    def _watch_text(self, best: "PatternDirector.Plan") -> str:
        if best.status == "PENDING":
            if best.state == "PRE_BREAKOUT":
                return "Watch: Breakout above entry; CANCEL NOW if price closes below stop."
            if best.state == "CANDLE":
                if best.side == "bull":
                    return "Watch: Confirm up next bar; CANCEL NOW if present close < signal low."
                if best.side == "bear":
                    return "Watch: Confirm down next bar; CANCEL NOW if present close > signal high."
                return "Note : Doji is neutral; wait for direction."
        return ""

    def _format_report(self, ticker: str, df: pd.DataFrame, best: "PatternDirector.Plan",
                    actionable_charts: List["PatternDirector.Plan"],
                    actionable_candles: List["PatternDirector.Plan"]) -> str:
        last_atr = self._val(df["atr14"].iloc[-1])
        price = self._val(df["close"].iloc[-1])
        buff = max(last_atr * self.atr_mult, price * self.pct_buf)

        def f(x): return "-" if x is None else f"{x:.2f}"
        lines = []
        lines.append(f"=== {ticker} — Present Pattern Director ({self.period}, {self.interval}) ===")
        lines.append(f"[{best.date.date()}] {best.pattern} ({best.state}) -> {best.status} | {best.notes}")
        lines.append(f"  Entry : {f(best.entry)}")
        lines.append(f"  Stop  : {f(best.stop)}")
        lines.append(f"  Target: {f(best.target)}")

        # Risk/Reward
        rr_val = self._rr(best)
        if rr_val is not None:
            lines.append(f"  R/R   : {rr_val:.2f}R")

        # Buffers
        if best.entry and best.stop:
            lines.append(f"  Buffers -> ATR14={last_atr:.2f}, min_pct_buffer={self.pct_buf*100:.2f}% (use >= {buff:.2f})")

        # Next Step (works for both PENDING and VALID if not yet triggered)
        price_now = float(df['close'].iloc[-1])
        ns = self._next_step_text(best, price_now)
        if ns:
            lines.append(f"  Next  : {ns}")

        # bullets (actionable only)
        for p in actionable_charts:
            lines.append(f"  • {p.pattern} {p.state} {p.status} entry={f(p.entry)} stop={f(p.stop)}")
            nxt = self._next_step_text(p, price_now)
            if nxt:
                lines.append(f"     ↳ Next: {nxt}")

        for c in actionable_candles:
            lines.append(f"  • Candle {c.pattern} {c.state} {c.status} ({c.notes})")
            nxt = self._next_step_text(c, price_now)
            if nxt:
                lines.append(f"     ↳ Next: {nxt}")

        side150, last_cross_date, last_cross_dir, dist_pct = self._sma150_info(df)
        if side150 != "unknown":
            cross_txt = ""
            if last_cross_date is not None:
                cross_txt = f", last {last_cross_dir} {(df.index[-1]-last_cross_date).days}d ago"
            lines.append(f"  SMA150: {side150} ({dist_pct:+.1f}%)" + cross_txt)
        return "\n".join(lines)


    def _sma150_info(self, df: pd.DataFrame):
        if "sma150" not in df.columns:
            return "unknown", None, None, float("nan")

        # Force 1-D Series (squeeze handles the 250x1 case)
        close = df["close"]
        if isinstance(close, pd.DataFrame):
            close = close.squeeze("columns")

        sma = df["sma150"]
        if isinstance(sma, pd.DataFrame):
            # if there is exactly one column under that name, pick it
            sma = sma.squeeze("columns")

        # Align indices before any arithmetic/comparisons
        close, sma = close.align(sma, join="inner")

        if close.empty or sma.empty or pd.isna(sma.iloc[-1]):
            return "unknown", None, None, float("nan")

        side = "above" if close.iloc[-1] > sma.iloc[-1] else ("below" if close.iloc[-1] < sma.iloc[-1] else "at")
        dist_pct = 100.0 * (close.iloc[-1] - sma.iloc[-1]) / max(sma.iloc[-1], 1e-9)

        above = close > sma
        cross_up   = above & (~above.shift(1).fillna(False))
        cross_down = (~above) & (above.shift(1).fillna(False))
        last_up   = cross_up[cross_up].index.max() if cross_up.any() else None
        last_down = cross_down[cross_down].index.max() if cross_down.any() else None

        last_dir, last_date = None, None
        if last_up is not None or last_down is not None:
            if last_down is None or (last_up is not None and last_up > last_down):
                last_dir, last_date = "cross_up", last_up
            else:
                last_dir, last_date = "cross_down", last_down

        return side, last_date, last_dir, float(dist_pct)


    # --------------- Utilities ---------------

    @staticmethod
    def _real_body(o: float, c: float) -> float:
        return abs(c - o)

    @staticmethod
    def _upper_shadow(o: float, h: float, c: float) -> float:
        return h - max(o, c)

    @staticmethod
    def _lower_shadow(o: float, l: float, c: float) -> float:
        return min(o, c) - l

    @staticmethod
    def _find_peaks(arr: np.ndarray, window: int = 3) -> List[int]:
        idxs = []
        for i in range(window, len(arr) - window):
            if arr[i] == max(arr[i - window:i + window + 1]) and arr[i] > arr[i-1] and arr[i] > arr[i+1]:
                idxs.append(i)
        return idxs

    @staticmethod
    def _find_troughs(arr: np.ndarray, window: int = 3) -> List[int]:
        idxs = []
        for i in range(window, len(arr) - window):
            if arr[i] == min(arr[i - window:i + window + 1]) and arr[i] < arr[i-1] and arr[i] < arr[i+1]:
                idxs.append(i)
        return idxs

    @staticmethod
    def _val(x):
        """
        Return a clean Python float from pandas/NumPy singletons.
        Works for: numpy scalars, 0-D arrays, 1-D length-1 arrays, pandas Series length-1, or plain floats.
        """
        if isinstance(x, pd.Series):
            return float(x.iloc[0])
        arr = np.asarray(x)
        if arr.ndim == 0:
            return float(arr.item())
        if arr.size == 1:
            return float(arr.reshape(()).item())
        return float(x)

    # --------------- Candlestick detectors ---------------

    def _is_doji(self, o, h, l, c, rb_ratio: float = 0.1) -> bool:
        rng = max(h - l, 1e-9)
        return self._real_body(o, c) <= rb_ratio * rng

    def _is_shooting_star(self, o, h, l, c) -> bool:
        rng = max(h - l, 1e-9)
        rb = self._real_body(o, c)
        return (self._upper_shadow(o, h, c) >= 0.6 * rng and
                self._lower_shadow(o, l, c) <= 0.1 * rng and
                rb <= 0.3 * rng)

    def _is_hammer(self, o, h, l, c) -> bool:
        rng = max(h - l, 1e-9)
        rb = self._real_body(o, c)
        return (self._lower_shadow(o, l, c) >= 0.6 * rng and
                self._upper_shadow(o, h, c) <= 0.1 * rng and
                rb <= 0.3 * rng)

    # NOTE: make these instance methods so we can use self._val(...)
    def _is_bullish_engulfing(self, prev: pd.Series, cur: pd.Series) -> bool:
        p_o = self._val(prev["open"]);  p_c = self._val(prev["close"])
        c_o = self._val(cur["open"]);   c_c = self._val(cur["close"])
        return (p_c < p_o) and (c_c > c_o) and (c_o <= p_c) and (c_c >= p_o)

    def _is_bearish_engulfing(self, prev: pd.Series, cur: pd.Series) -> bool:
        p_o = self._val(prev["open"]);  p_c = self._val(prev["close"])
        c_o = self._val(cur["open"]);   c_c = self._val(cur["close"])
        return (p_c > p_o) and (c_c < c_o) and (c_o >= p_c) and (c_c <= p_o)

    def _bullish_engulfing_breakout_plan(self, prev: pd.Series, row: pd.Series, date, df: pd.DataFrame, last_close: float) -> "PatternDirector.Plan":
            """
            Entry: buy-stop above engulfing high + buffer
            Stop:  below engulfing low  - buffer
            Target: 2R by default (reuse inside-bar logic)
            """
            hi  = self._val(row["high"])
            lo  = self._val(row["low"])
            atr = self._val(df["atr14"].loc[date])
            ref = self._val(row["close"])

            # reuse your existing helper
            e, s, t = self._levels_for_inside_bullish(hi, lo, atr, ref, r_mult=2.0, atr_buf_mult=0.25)

            cancel_now = last_close < lo
            status = self._status_for_signal(cancel_now, signal_is_today=(date == df.index[-1]))
            ctx = self._trend_context(df)
            return self.Plan("Bullish Engulfing", "bull", "CANDLE", date, e, s, t, cancel_now, status, f"2-bar | {ctx}")



    def _doji_breakout_plan(self, row, date, df, last_close):
        hi  = self._val(row["high"])
        lo  = self._val(row["low"])
        atr = float(df["atr14"].loc[date])
        ref = self._val(row["close"])

        # Reuse your inside-bar logic: entry=hi+buf, stop=lo-buf, target = entry + 2R
        entry, stop, target = self._levels_for_inside_bullish(
            hi, lo, atr, ref, r_mult=2.0, atr_buf_mult=0.25
        )

        cancel_now = last_close < lo
        status = self._status_for_signal(cancel_now, signal_is_today=(date == df.index[-1]))
        ctx = self._trend_context(df)
        return self.Plan("Doji Breakout", "bull", "CANDLE", date, entry, stop, target, cancel_now, status, f"single | {ctx}")


    def _latest_candle_plan(self, df: pd.DataFrame) -> Tuple[List["PatternDirector.Plan"], List["PatternDirector.Plan"]]:
        plans: List["PatternDirector.Plan"] = []
        bears_info: List["PatternDirector.Plan"] = []
        if len(df) < 6:
            return plans, bears_info

        last_close = self._val(df["close"].iloc[-1])

        def add(name: str, side: str, hi: float, lo: float, date, note=""):
            is_today = (date == df.index[-1])
            cancel_now = (last_close > hi) if side == "bear" else ((last_close < lo) if side == "bull" else False)
            status = self._status_for_signal(cancel_now, signal_is_today=is_today)
            stop = lo if side == "bull" else (hi if side == "bear" else None)
            plan = self.Plan(name, side, "CANDLE", date, None,
                            float(stop) if stop is not None else None,
                            None, cancel_now, status, note)
            # route bears into awareness bucket when long_only=True
            if side == "bear" and self.long_only:
                if self.show_bearish_info:
                    bears_info.append(plan)
            else:
                plans.append(plan)

        # use the last 5 bars: p4,p3,p2,p1,cur
        p4, p3, p2, p1, cur = [df.iloc[-5+i] for i in range(5)]
        prev = p1

        # keep your original single-candle checks on yesterday & today
        for i in [-2, -1]:
            row, prv = df.iloc[i], df.iloc[i-1]
            o,h,l,c = map(self._val, (row["open"], row["high"], row["low"], row["close"]))
            ph,pl = self._val(prv["high"]), self._val(prv["low"])
            date = df.index[i]
            if self._is_shooting_star(o,h,l,c): add("Shooting Star","bear", h,l,date,"single")
            if self._is_hammer(o,h,l,c):        add("Hammer","bull", h,l,date,"single")
            if self._is_doji(o,h,l,c):          
                plans.append(self._doji_breakout_plan(row, date, df, last_close))
            # if self._is_bullish_engulfing(prv, row): add("Bullish Engulfing","bull", h,l,date,"2-bar")
            if self._is_bullish_engulfing(prv, row):
                plans.append(self._bullish_engulfing_breakout_plan(prv, row, date, df, last_close))

            if self._is_bearish_engulfing(prv, row):
                add("Bearish Engulfing","bear", h,l,date,"2-bar")

            if self._is_bullish_thrust(row, df):
                plans.append(self._bullish_thrust_plan(row, date, df, last_close))

        if self._is_inside_bar(prev, cur):
            if self._is_bull(cur["open"], cur["close"]):
                in_hi = self._val(cur["high"])
                in_lo = self._val(cur["low"])
                atr_now = self._val(df["atr14"].iloc[-1])
                ref_price = self._val(cur["close"])
                e, s, t = self._levels_for_inside_bullish(in_hi, in_lo, atr_now, ref_price)

                cancel_now = last_close < in_lo
                status = self._status_for_signal(cancel_now, signal_is_today=True)

                plans.append(self.Plan(
                    pattern="Bullish Inside (Harami)",
                    side="bull",
                    state="CANDLE",
                    date=df.index[-1],
                    entry=e, stop=s, target=t,
                    cancel_now=cancel_now, status=status, notes="2-bar"
                ))
            elif self._is_bear(cur["open"], cur["close"]):
                add("Bearish Inside (Harami)","bear",
                    self._val(prev["high"]), self._val(prev["low"]),
                    df.index[-1], "2-bar")


        if self._is_bullish_harami(prev, cur):
            hi  = self._val(cur["high"])
            lo  = self._val(cur["low"])
            atr = self._val(df["atr14"].iloc[-1])
            ref = self._val(cur["close"])
            e, s, t = self._levels_for_inside_bullish(hi, lo, atr, ref)  # reuse
            cancel_now = last_close < lo
            status = self._status_for_signal(cancel_now, signal_is_today=True)
            plans.append(self.Plan("Bullish Harami", "bull", "CANDLE",
                                df.index[-1], e, s, t, cancel_now, status, "2-bar"))
        if self._is_bearish_harami(prev, cur):
            add("Bearish Harami","bear", self._val(prev["high"]), self._val(prev["low"]), df.index[-1], "2-bar")

        if self._is_bullish_outside(prev, cur):
            add("Bullish Outside (Engulfing Range)","bull", self._val(cur["high"]), self._val(cur["low"]), df.index[-1], "2-bar")
        if self._is_bearish_outside(prev, cur):
            add("Bearish Outside (Engulfing Range)","bear", self._val(cur["high"]), self._val(cur["low"]), df.index[-1], "2-bar")

        if self._is_bullish_kicker(prev, cur):
            add("Bullish Kicker","bull", self._val(cur["high"]), self._val(prev["low"]), df.index[-1], "gap-reversal")
        if self._is_bearish_kicker(prev, cur):
            add("Bearish Kicker","bear", self._val(prev["high"]), self._val(cur["low"]), df.index[-1], "gap-reversal")

        # --- NEW 3-bar patterns (p2, p1, cur) ---
        if self._is_morning_star(p2, p1, cur):
            hi = max(self._val(p2["high"]), self._val(cur["high"]))
            lo = min(self._val(p2["low"]),  self._val(p1["low"]))
            add("Morning Star","bull", hi, lo, df.index[-1], "3-bar")

        if self._is_evening_star(p2, p1, cur):
            hi = max(self._val(p2["high"]), self._val(p1["high"]))
            lo = min(self._val(p2["low"]),  self._val(cur["low"]))
            add("Evening Star","bear", hi, lo, df.index[-1], "3-bar")

        if self._is_three_inside_up(p2, p1, cur):
            add("Three Inside Up","bull", self._val(p1["high"]), self._val(p1["low"]), df.index[-1], "3-bar")

        if self._is_three_inside_down(p2, p1, cur):
            add("Three Inside Down","bear", self._val(p1["high"]), self._val(p1["low"]), df.index[-1], "3-bar")

        # --- NEW 5-bar continuation (p4,p3,p2,p1,cur) ---
        if self._is_rising_three_methods(p4, p3, p2, p1, cur):
            hi = max(self._val(p4["high"]), self._val(cur["high"]))
            lo = self._val(min(p3["low"], p2["low"], p1["low"], p4["low"]))
            add("Rising Three Methods","bull", hi, lo, df.index[-1], "5-bar")

        if self._is_falling_three_methods(p4, p3, p2, p1, cur):
            hi = self._val(max(p3["high"], p2["high"], p1["high"], p4["high"]))
            lo = min(self._val(p4["low"]), self._val(cur["low"]))
            add("Falling Three Methods","bear", hi, lo, df.index[-1], "5-bar")

        return plans, bears_info

    # --------------- Chart pattern detectors ---------------

    def _is_bullish_thrust(self, row: pd.Series, df: pd.DataFrame,
                        min_atr_mult: float = 1.0,
                        min_body_ratio: float = 0.6,
                        min_close_pos: float = 0.8) -> bool:
        o, h, l, c = map(self._val, (row["open"], row["high"], row["low"], row["close"]))
        rng  = max(h - l, 1e-9)
        body = abs(c - o)
        # position of close within the bar: 0=low, 1=high
        close_pos = (c - l) / rng
        atr_now = float(df["atr14"].loc[row.name])
        if not np.isfinite(atr_now) or atr_now <= 0:
            return False

        return (c > o) and (rng >= self.thrust_min_atr_mult * atr_now) and \
            (body / rng >= self.thrust_min_body_ratio) and (close_pos >= self.thrust_min_close_pos)

    def _bullish_thrust_plan(self, row: pd.Series, date, df: pd.DataFrame, last_close: float) -> "PatternDirector.Plan":
        hi  = self._val(row["high"])
        lo  = self._val(row["low"])
        atr = float(df["atr14"].loc[date])
        ref = self._val(row["close"])

        entry, stop, target = self._levels_for_inside_bullish(hi, lo, atr, ref, r_mult=2.0, atr_buf_mult=0.25)
        cancel_now = last_close < lo
        status = self._status_for_signal(cancel_now, signal_is_today=(date == df.index[-1]))

        ctx = self._trend_context(df)
        return self.Plan("Bullish Thrust (Marubozu)", "bull", "CANDLE",
                    date, entry, stop, target, cancel_now, status, f"single-strong | {ctx}")



    def _detect_cup_and_handle(self,
                               df: pd.DataFrame,
                               lookback: int = 150,
                               min_depth: float = 0.12,
                               max_handle_depth: float = 0.10) -> Optional[Plan]:
        section = df.iloc[-lookback:].copy()
        highs = section["high"].to_numpy()
        lows = section["low"].to_numpy()
        closes = section["close"].to_numpy()
        last_close = self._val(closes[-1])
        atr14 = self._val(section["atr14"].iloc[-1])

        rim_price = self._val(highs.max())
        bottom_price = self._val(lows.min())
        depth = (rim_price - bottom_price) / max(rim_price, 1e-9)
        if depth < min_depth:
            return None

        rim_idx = int(highs.argmax())
        bottom_idx = int(lows.argmin())
        if not (rim_idx < bottom_idx < len(section) - 6):
            return None

        # Handle = last 5–20 bars staying near rim
        handle_window = min(20, len(section) - (bottom_idx + 1))
        if handle_window < 5:
            return None
        handle_lows = lows[-handle_window:]
        handle_highs = highs[-handle_window:]

        handle_low = self._val(handle_lows.min())
        handle_high = self._val(handle_highs.max())
        handle_depth = (rim_price - handle_low) / max(rim_price, 1e-9)
        if handle_depth > max_handle_depth or handle_low <= bottom_price:
            return None

        entry = max(handle_high, rim_price)  # breakout level
        stop = handle_low
        target = entry + (rim_price - bottom_price)

        if last_close > entry:
            buff = max(atr14, entry * 0.0025)
            cancel_now = last_close < (entry - 0.25 * buff)
            status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
        else:
            cancel_now = last_close < stop
            status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")

        notes = f"depth={depth:.1%}, handle_depth={handle_depth:.1%}"
        return self.Plan("Cup & Handle", "bull", state, section.index[-1], float(entry), float(stop),
                         float(target), cancel_now, status, notes)

    def _trend_context(self, df: pd.DataFrame) -> str:
        side, _, _, _ = self._sma150_info(df)
        # slope of SMA150 last 5 bars
        s = df["sma150"].diff().rolling(5).mean().iloc[-1]
        if side == "above" and s > 0: return "continuation"
        if side == "below" and s < 0: return "continuation"
        return "reversal"

    def _detect_double_bottom(self,
                              df: pd.DataFrame,
                              lookback: int = 120,
                              max_gap_pct: float = 0.03) -> Optional[Plan]:
        section = df.iloc[-lookback:].copy()
        lows = section["low"].to_numpy()
        highs = section["high"].to_numpy()
        closes = section["close"].to_numpy()
        atr14 = self._val(section["atr14"].iloc[-1])
        last_close = self._val(closes[-1])

        troughs = self._find_troughs(lows, window=3)
        if len(troughs) < 2:
            return None
        i1, i2 = troughs[-2], troughs[-1]
        low1, low2 = self._val(lows[i1]), self._val(lows[i2])

        if abs(low2 - low1) / max((low1 + low2) / 2, 1e-9) > max_gap_pct:
            return None

        neckline = self._val(highs[i1:i2+1].max())
        entry = neckline
        stop = min(low1, low2)
        target = entry + (entry - stop)

        if last_close > entry:
            buff = max(atr14, entry * 0.0025)
            cancel_now = last_close < (entry - 0.25 * buff)
            status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
        else:
            cancel_now = last_close < stop
            status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")

        notes = f"lows≈equal ({low1:.2f}/{low2:.2f})"
        return self.Plan("Double Bottom", "bull", state, section.index[-1], float(entry), float(stop),
                         float(target), cancel_now, status, notes)

    def _detect_head_and_shoulders(self,
                                   df: pd.DataFrame,
                                   lookback: int = 150,
                                   shoulder_tol: float = 0.10) -> Optional[Plan]:
        section = df.iloc[-lookback:].copy()
        highs = section["high"].to_numpy()
        lows = section["low"].to_numpy()
        closes = section["close"].to_numpy()
        atr14 = self._val(section["atr14"].iloc[-1])
        last_close = self._val(closes[-1])

        peaks = self._find_peaks(highs, window=3)
        troughs = self._find_troughs(lows, window=3)
        if len(peaks) < 3 or len(troughs) < 2:
            return None

        p1, p2, p3 = peaks[-3], peaks[-2], peaks[-1]
        LS, HEAD, RS = self._val(highs[p1]), self._val(highs[p2]), self._val(highs[p3])
        if not (HEAD > LS and HEAD > RS):
            return None
        if abs(LS - RS) / max((LS + RS) / 2, 1e-9) > shoulder_tol:
            return None

        lows_between = [t for t in troughs if p1 < t < p3]
        if len(lows_between) < 2:
            return None
        t1, t2 = lows_between[0], lows_between[-1]
        neck1, neck2 = self._val(lows[t1]), self._val(lows[t2])
        neckline = (neck1 + neck2) / 2.0  # simple avg neckline

        entry = neckline
        stop = max(LS, RS)
        target = entry - (HEAD - neckline)  # measured move downward

        if last_close < entry:
            buff = max(atr14, entry * 0.0025)
            cancel_now = last_close > (entry + 0.25 * buff)
            status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
        else:
            cancel_now = last_close > stop
            status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")

        notes = f"LS={LS:.2f} HEAD={HEAD:.2f} RS={RS:.2f}"
        return self.Plan("Head & Shoulders", "bear", state, section.index[-1], float(entry), float(stop),
                         float(target), cancel_now, status, notes)
    
    def _detect_double_top(self, df: pd.DataFrame,
        lookback: int = 120, tol: float = 0.01) -> Optional[Plan]:
        section = df.iloc[-lookback:].copy()
        highs = section["high"].to_numpy()
        closes = section["close"].to_numpy()
        atr14 = float(section["atr14"].iloc[-1])
        last_close = float(closes[-1])

        piv = self._swing_highs(highs, w=3)
        if len(piv) < 2: return None
        i1, i2 = piv[-2], piv[-1]
        t1, t2 = float(highs[i1]), float(highs[i2])
        if not self._percent_eq(t1, t2, tol): return None

        neckline = float(section["low"].iloc[i1:i2+1].min())
        entry = neckline      # breakdown
        stop  = max(t1, t2)
        target = entry - (stop - entry)  # measured move ~ height

        if last_close < entry:
            buff = max(atr14, entry*0.0025)
            cancel_now = last_close > (entry + 0.25*buff)
            status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
        else:
            cancel_now = last_close > stop
            status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")

        notes = f"tops≈equal ({t1:.2f}/{t2:.2f}) | {self._trend_context(df)}"
        return self.Plan("Bearish Double Top", "bear", state, section.index[-1],
                        float(entry), float(stop), float(target), cancel_now, status, notes)


    def _detect_triple_bottom(self, df: pd.DataFrame,
                            lookback: int = 150, tol: float = 0.012) -> Optional[Plan]:
        section = df.iloc[-lookback:].copy()
        lows = section["low"].to_numpy()
        highs = section["high"].to_numpy()
        closes = section["close"].to_numpy()
        atr14 = float(section["atr14"].iloc[-1])
        last_close = float(closes[-1])

        piv = self._swing_lows(lows, w=3)
        if len(piv) < 3: return None
        i1, i2, i3 = piv[-3], piv[-2], piv[-1]
        b1, b2, b3 = float(lows[i1]), float(lows[i2]), float(lows[i3])
        if not (self._percent_eq(b1,b2,tol) and self._percent_eq(b2,b3,tol)): return None

        rim = float(highs[i1:i3+1].max())
        entry = rim
        stop  = min(b1,b2,b3)
        target = entry + (entry - stop)

        if last_close > entry:
            buff = max(atr14, entry*0.0025)
            cancel_now = last_close < (entry - 0.25*buff)
            status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
        else:
            cancel_now = last_close < stop
            status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")

        notes = f"bottoms≈equal ({b1:.2f}/{b2:.2f}/{b3:.2f}) | {self._trend_context(df)}"
        return self.Plan("Bullish Triple Bottom", "bull", state, section.index[-1],
                        float(entry), float(stop), float(target), cancel_now, status, notes)

    def _detect_ascending_triangle(self, df: pd.DataFrame,
                                lookback: int = 120, tol: float = 0.01,
                                min_rr: float = None) -> Optional["PatternDirector.Plan"]:
        section = df.iloc[-lookback:].copy()
        highs = section["high"].to_numpy()
        lows  = section["low"].to_numpy()
        closes = section["close"].to_numpy()
        i_last = section.index[-1]
        last_close = float(closes[-1])
        atr14 = float(section["atr14"].iloc[-1])

        ups = self._swing_highs(highs, 3)
        dns = self._swing_lows(lows, 3)
        if len(ups) < 2 or len(dns) < 2:
            return None

        # flat top (equal highs) + rising lows
        ht = float(np.median([highs[i] for i in ups[-min(3,len(ups)):]]))
        near_flat = all(self._percent_eq(ht, float(highs[i]), tol) for i in ups[-2:])
        rising = lows[dns[-1]] > lows[dns[-2]]
        if len(dns) >= 3:
            rising = rising and (lows[dns[-2]] > lows[dns[-3]])
        if not (near_flat and rising):
            return None

        # levels
        last_swing_low = float(lows[dns[-1]])                  # tighter stop anchor
        pattern_low    = float(min(lows[d] for d in dns[-3:])) # for measured move
        height         = max(ht - pattern_low, 1e-9)

        # buffers
        buf = max(atr14 * self.atr_mult, ht * self.pct_buf)
        entry  = ht + 0.25 * buf
        stop   = last_swing_low - 0.25 * buf                    # tighter than using min of 3
        target = entry + height                                  # measured‑move target

        # state / status
        if last_close > entry:
            cancel_now = last_close < (entry - 0.25 * buf)
            status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
        else:
            cancel_now = last_close < stop
            status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")

        # enforce a minimum RR (use config’s min_rr_ok unless overridden)
        rr_needed = self.min_rr_ok if min_rr is None else float(min_rr)
        risk   = max(entry - stop, 1e-9)
        reward = target - entry
        rr_val = reward / risk
        if rr_val < rr_needed:
            return None  # or: target = entry + rr_needed * risk

        notes = f"Ascending Triangle | RR={rr_val:.2f}R | {self._trend_context(df)}"
        return self.Plan("Ascending Triangle", "bull", state, i_last,
                        float(entry), float(stop), float(target), cancel_now, status, notes)


    def _detect_descending_triangle(self, df: pd.DataFrame,
                                    lookback: int = 120, tol: float = 0.01) -> Optional[Plan]:
        section = df.iloc[-lookback:].copy()
        highs = section["high"].to_numpy()
        lows  = section["low"].to_numpy()
        closes = section["close"].to_numpy()
        last_close = float(closes[-1])
        atr14 = float(section["atr14"].iloc[-1])

        ups = self._swing_highs(highs, 3)
        dns = self._swing_lows(lows, 3)
        if len(ups) < 2 or len(dns) < 2: return None
        lb = float(np.median(lows[dns[-3:]])) if len(dns)>=3 else float(np.mean(lows[dns[-2:]]))
        falling = highs[ups[-1]] < highs[ups[-2]] and (len(ups)<3 or highs[ups[-2]] < highs[ups[-3]])
        near_flat = all(self._percent_eq(lb, float(lows[i]), tol) for i in dns[-2:])

        if not (near_flat and falling): return None

        entry = lb
        stop  = float(max(highs[u] for u in ups[-3:]))
        target = entry - (stop - entry)

        if last_close < entry:
            buff = max(atr14, entry*0.0025)
            cancel_now = last_close > (entry + 0.25*buff)
            status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
        else:
            cancel_now = last_close > stop
            status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")

        notes = f"Descending Triangle | {self._trend_context(df)}"
        return self.Plan("Descending Triangle", "bear", state, section.index[-1],
                        float(entry), float(stop), float(target), cancel_now, status, notes)


    def _detect_symmetrical_triangle(self, df: pd.DataFrame,
                                    lookback: int = 120) -> Optional[Plan]:
        section = df.iloc[-lookback:].copy()
        highs = section["high"].to_numpy()
        lows  = section["low"].to_numpy()
        closes = section["close"].to_numpy()
        atr14 = float(section["atr14"].iloc[-1])
        last_close = float(closes[-1])

        ups = self._swing_highs(highs, 3)
        dns = self._swing_lows(lows, 3)
        if len(ups) < 2 or len(dns) < 2: return None
        contracting = highs[ups[-1]] < highs[ups[-2]] and lows[dns[-1]] > lows[dns[-2]]
        if not contracting: return None

        top = float(max(highs[ups[-1]], highs[ups[-2]]))
        bot = float(min(lows[dns[-1]], lows[dns[-2]]))
        mid = (top + bot)/2

        # Direction preference by prior trend; otherwise choose by where price breaks
        side_pref = self._prior_trend(df)
        if last_close > mid:
            entry = top; stop = bot; target = entry + (entry - stop); side = "bull"
            if last_close > entry:
                buff = max(atr14, entry*0.0025)
                cancel_now = last_close < (entry - 0.25*buff)
                status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
            else:
                cancel_now = last_close < stop
                status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")
        else:
            entry = bot; stop = top; target = entry - (stop - entry); side = "bear"
            if last_close < entry:
                buff = max(atr14, entry*0.0025)
                cancel_now = last_close > (entry + 0.25*buff)
                status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
            else:
                cancel_now = last_close > stop
                status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")

        notes = f"Symmetrical Triangle ({side_pref}) | {self._trend_context(df)}"
        return self.Plan("Symmetrical Triangle", side, state, section.index[-1],
                        float(entry), float(stop), float(target), cancel_now, status, notes)


    def _detect_flag(self, df: pd.DataFrame,
                    lookback: int = 60, min_pole_pct: float = 0.05,
                    max_pullback_pct: float = 0.5, side: str = "bull") -> Optional[Plan]:
        """Very simple flag: sharp pole, then 5–20 bars drifting counter‑trend in a tight channel."""
        section = df.iloc[-lookback:].copy()
        close = section["close"].to_numpy()
        high  = section["high"].to_numpy()
        low   = section["low"].to_numpy()
        atr14 = float(section["atr14"].iloc[-1])

        n = len(section)
        if n < 25: return None
        # pole = last 10–15 bars momentum
        win = 12
        pole_ret = (close[-1-win] - close[-1]) / max(close[-1],1e-9) if side=="bear" else (close[-1] - close[-1-win]) / max(close[-1-win],1e-9)
        if pole_ret < min_pole_pct: return None

        # consolidation window
        cons = section.iloc[-8:]  # last ~8 bars channel
        cons_hi = float(cons["high"].max()); cons_lo = float(cons["low"].min())
        tight = (cons_hi - cons_lo)/max(close[-1],1e-9) < 0.03
        if not tight: return None

        if side == "bull":
            entry = cons_hi
            stop  = cons_lo
            target = entry + 2*(entry - stop)
            last_close = float(close[-1])
            if last_close > entry:
                buff = max(atr14, entry*0.0025)
                cancel_now = last_close < (entry - 0.25*buff)
                status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
            else:
                cancel_now = last_close < stop
                status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")
            name = "Bullish Flag Pattern"
            side_out = "bull"
        else:
            entry = cons_lo
            stop  = cons_hi
            target = entry - 2*(stop - entry)
            last_close = float(close[-1])
            if last_close < entry:
                buff = max(atr14, entry*0.0025)
                cancel_now = last_close > (entry + 0.25*buff)
                status, state = ("CANCELED NOW" if cancel_now else "VALID", "BREAKOUT")
            else:
                cancel_now = last_close > stop
                status, state = ("CANCELED NOW" if cancel_now else "PENDING", "PRE_BREAKOUT")
            name = "Bearish Flag Pattern"
            side_out = "bear"

        notes = f"Flag ({side}) | {self._trend_context(df)}"
        return self.Plan(name, side_out, state, section.index[-1],
                        float(entry), float(stop), float(target), cancel_now, status, notes)




    def _prior_trend(self, df: pd.DataFrame, lookback: int = 30) -> str:
        """Crude trend: compare last close vs close N bars ago."""
        if len(df) < lookback+1: return "unknown"
        a = float(df["close"].iloc[-lookback-1])
        b = float(df["close"].iloc[-1])
        return "up" if b > a*1.03 else ("down" if b < a*0.97 else "side")

    def _swing_highs(self, arr: np.ndarray, w: int = 3) -> List[int]:
        return [i for i in range(w, len(arr)-w)
                if arr[i] == max(arr[i-w:i+w+1]) and arr[i] > arr[i-1] and arr[i] > arr[i+1]]

    def _swing_lows(self, arr: np.ndarray, w: int = 3) -> List[int]:
        return [i for i in range(w, len(arr)-w)
                if arr[i] == min(arr[i-w:i+w+1]) and arr[i] < arr[i-1] and arr[i] < arr[i+1]]

    def _percent_eq(self, a: float, b: float, tol: float = 0.01) -> bool:
        """Are a and b within tol (1% default)?"""
        m = (abs(a)+abs(b))/2 or 1.0
        return abs(a-b)/m <= tol

    def _rr_levels_from_breakout(self, entry, stop, target_mult=2.0) -> Tuple[float,float,float]:
        """Risk = |entry-stop|; target = entry +/− target_mult*Risk (bull/bear set by caller)."""
        risk = max(abs(entry - stop), 1e-9)
        # caller chooses direction when computing target
        return float(entry), float(stop), float(risk), float(target_mult*risk)






    # --------------- Planner ---------------

    def _choose_best_plan(self, chart_plans: List["PatternDirector.Plan"],
                        candle_plans: List["PatternDirector.Plan"]) -> Optional["PatternDirector.Plan"]:
        """
        Choose among *pre-filtered actionable* plans.
        Priority: Chart BREAKOUT > Chart PRE_BREAKOUT > Candle VALID > Candle PENDING.
        """
        def pri(p: "PatternDirector.Plan") -> Tuple[int, int]:
            state_rank = {"BREAKOUT": 3, "PRE_BREAKOUT": 2, "CANDLE": 1}[p.state]
            ok_rank = {"VALID": 2, "PENDING": 1, "CANCELED NOW": 0}[p.status]
            return (state_rank, ok_rank)

        pool = [p for p in chart_plans + candle_plans if p.status != "CANCELED NOW"]
        if not pool:
            return None
        pool.sort(key=pri, reverse=True)
        return pool[0]

    def _print_best(self, df: pd.DataFrame, best: Plan):
        last_atr = self._val(df["atr14"].iloc[-1])
        price = self._val(df["close"].iloc[-1])
        buff = max(last_atr * self.atr_mult, price * self.pct_buf)

        e = f"{best.entry:.2f}" if best.entry else "-"
        s = f"{best.stop:.2f}" if best.stop else "-"
        t = f"{best.target:.2f}" if best.target else "-"

        print(f"[{best.date.date()}] {best.pattern} ({best.state}) -> {best.status} | {best.notes}")
        print(f"  Entry : {e}")
        print(f"  Stop  : {s}")
        print(f"  Target: {t}")
        if best.entry and best.stop:
            print(f"  Buffers -> ATR14={last_atr:.2f}, min_pct_buffer={self.pct_buf*100:.2f}% (use >= {buff:.2f})")

        if best.status == "PENDING":
            if best.state == "PRE_BREAKOUT":
                print("  Watch: Breakout above entry; CANCEL NOW if price closes below stop.")
            elif best.state == "CANDLE":
                if best.side == "bull":
                    print("  Watch: Confirm up next bar; CANCEL NOW if present close < signal low.")
                elif best.side == "bear":
                    print("  Watch: Confirm down next bar; CANCEL NOW if present close > signal high.")
                else:
                    print("  Note : Doji is neutral; wait for direction.")

    def _detect_near_sma150(self, df: pd.DataFrame, band_pct: float = None) -> Optional["PatternDirector.Plan"]:
        """
        Awareness-only signal (treated like a 'pattern'):
        Fire when last close is within ±band_pct of SMA150.
        Emits a neutral CANDLE plan (no entry/stop/target).
        """
        if "sma150" not in df.columns or df["sma150"].isna().iloc[-1]:
            return None

        band = self.sma150_near_band_pct if band_pct is None else float(band_pct)
        side, last_cross_date, last_cross_dir, dist_pct = self._sma150_info(df)
        if not np.isfinite(dist_pct):
            return None

        if abs(dist_pct) <= band:
            # neutral so it shows even in long_only mode and skips momentum gating
            note = f"{side}; {dist_pct:+.1f}% from SMA150"
            if last_cross_date is not None:
                note += f", last {last_cross_dir} {(df.index[-1]-last_cross_date).days}d ago"
            return self.Plan(
                pattern=f"Near SMA150 (±{band:.0f}%)",
                side="neutral",
                state="CANDLE",
                date=df.index[-1],
                entry=None, stop=None, target=None,
                cancel_now=False,
                status="VALID",
                notes=note
            )
        return None


    def _is_actionable_now(self, plan: "PatternDirector.Plan", df: pd.DataFrame) -> Tuple[bool, str]:
        """
        Keep:
        - PRE_BREAKOUT not canceled (watch for trigger)
        - BREAKOUT that is 'fresh' (close near entry) and still decent R/R from *now*
        - Candles not canceled
        - NEW: Bullish signals must pass RSI/MACD momentum filter
        """
        if self.long_only and plan.side == "bear":
            return (False, "short_hidden")

        # thresholds (configurable)
        fresh_breakout_max_atr = getattr(self, "fresh_breakout_max_atr", 0.75)
        fresh_breakout_max_pct = getattr(self, "fresh_breakout_max_pct", 0.01)
        max_beyond_target_pct  = getattr(self, "max_beyond_target_pct", 0.0025)

        # momentum thresholds (override via cfg["momentum_filter"] if you want)
        rsi_min  = getattr(self, "rsi_min", 50)   # require RSI >= 50 for longs
        rsi_hot  = getattr(self, "rsi_hot", 80)   # avoid fresh entries if RSI > 80
        hist_win = getattr(self, "macd_hist_rising_window", 3)  # rising window

        price = self._val(df["close"].iloc[-1])
        atr   = self._val(df["atr14"].iloc[-1])

        # --- SMA150 trend filter (optional) ---
        side150, last_cross_date, last_cross_dir, _ = self._sma150_info(df)

        if plan.side == "bull" and self.require_above_sma150_for_longs and side150 != "above":
            return (False, f"sma150_{side150}")

        if plan.side == "bear" and self.require_below_sma150_for_shorts and side150 != "below":
            return (False, f"sma150_{side150}")

        if self.sma150_recent_cross_days > 0:
            if last_cross_date is None:
                return (False, "sma150_no_cross")
            days_ago = (df.index[-1] - last_cross_date).days
            if days_ago > self.sma150_recent_cross_days:
                return (False, f"sma150_cross_older({days_ago}d)")

        # --- helper: momentum pass for bullish-only plans ---
        def bullish_momentum_ok() -> Tuple[bool, str]:
            if "rsi14" not in df.columns or "macd_hist" not in df.columns:
                return (True, "mom_skip_no_cols")  # fail-open if not computed

            rsi  = float(df["rsi14"].iloc[-1])
            hist = float(df["macd_hist"].iloc[-1])
            if len(df) > hist_win:
                hist_prev = float(df["macd_hist"].iloc[-1 - hist_win])
                hist_rising = (hist - hist_prev) > 0
            else:
                hist_rising = hist > 0

            if rsi < rsi_min:
                return (False, f"rsi<min({rsi:.0f}<{rsi_min})")
            if rsi > rsi_hot:
                return (False, f"rsi_hot({rsi:.0f}>{rsi_hot})")
            if not (hist > 0 or hist_rising):
                return (False, "macd_hist_not_pos_or_rising")
            return (True, "mom_ok")

        # Cancelled is out
        if plan.status == "CANCELED NOW":
            return (False, "canceled")

        # Apply momentum gating to bullish setups (skip neutral/doji)
        if plan.side == "bull":
            ok_mom, why_mom = bullish_momentum_ok()
            if not ok_mom:
                return (False, why_mom)

        # Candle logic (after momentum)
        if plan.state == "CANDLE":
            if plan.entry and plan.stop and plan.target:
                risk   = max(plan.entry - plan.stop, 1e-9) if plan.side == "bull" else max(plan.stop - plan.entry, 1e-9)
                reward = (plan.target - plan.entry) if plan.side == "bull" else (plan.entry - plan.target)
                rr = reward / risk
                if rr < self.min_rr_ok:
                    return (False, f"rr_plan_too_low({rr:.2f}R)")
            return (plan.status in {"VALID", "PENDING"}, "candle")

        # Pre-breakout chart pattern
        if plan.state == "PRE_BREAKOUT":
            return (True, "pre_breakout")

        # Fresh breakout checks
        if plan.state == "BREAKOUT":
            if not plan.entry:
                return (False, "no_entry")

            if plan.target:
                near_target = price >= plan.target * (1 - max_beyond_target_pct)
                if near_target:
                    return (False, "target_already_hit")

            dist = max(price - plan.entry, 0.0)
            fresh_by_atr = dist <= fresh_breakout_max_atr * atr
            fresh_by_pct = dist <= fresh_breakout_max_pct * (plan.entry or price)
            if not (fresh_by_atr or fresh_by_pct):
                return (False, "late_breakout")

            if plan.target and plan.stop:
                risk_now = max(price - plan.stop, 1e-9)
                reward_now = plan.target - price
                rr_now = reward_now / risk_now
                if rr_now < self.min_rr_ok:
                    return (False, f"rr_now_too_low({rr_now:.2f}R)")

            return (True, "fresh_breakout")

        return (False, "unknown_state")

# --------------- Cendels helper ---------------

    def _f(self, x) -> float:
        return self._val(x)
    def _is_bull(self, o, c): return self._f(c) > self._f(o)
    def _is_bear(self, o, c): return self._f(c) < self._f(o)
    def _body_hi(self, o, c): return max(self._f(o), self._f(c))
    def _body_lo(self, o, c): return min(self._f(o), self._f(c))
    def _gap_above(self, prev_h, cur_l): return self._f(cur_l) > self._f(prev_h)
    def _gap_below(self, prev_l, cur_h): return self._f(cur_h) < self._f(prev_l)
    def _range(self, h, l): return max(self._f(h) - self._f(l), 1e-9)
    def _body(self, o, c): return abs(self._f(c) - self._f(o))
    def _allow(self, side: str) -> bool:
        return not (self.long_only and side == "bear")

    def _is_inside_bar(self, prev, cur) -> bool:
        ph, pl = self._f(prev["high"]), self._f(prev["low"])
        ch, cl = self._f(cur["high"]),  self._f(cur["low"])
        return (ch <= ph) and (cl >= pl)

    def _is_bullish_harami(self, prev, cur) -> bool:
        return (self._is_bear(prev["open"], prev["close"]) and
                self._is_bull(cur["open"],  cur["close"])  and
                self._body_hi(cur["open"], cur["close"]) <= self._body_hi(prev["open"], prev["close"]) and
                self._body_lo(cur["open"], cur["close"]) >= self._body_lo(prev["open"], prev["close"]))

    def _is_bearish_harami(self, prev, cur) -> bool:
        return (self._is_bull(prev["open"], prev["close"]) and
                self._is_bear(cur["open"],  cur["close"])  and
                self._body_hi(cur["open"], cur["close"]) <= self._body_hi(prev["open"], prev["close"]) and
                self._body_lo(cur["open"], cur["close"]) >= self._body_lo(prev["open"], prev["close"]))

    def _is_bullish_outside(self, prev, cur) -> bool:
        ph, pl = self._f(prev["high"]), self._f(prev["low"])
        ch, cl = self._f(cur["high"]),  self._f(cur["low"])
        return (ch >= ph) and (cl <= pl) and self._is_bull(cur["open"], cur["close"])

    def _is_bearish_outside(self, prev, cur) -> bool:
        ph, pl = self._f(prev["high"]), self._f(prev["low"])
        ch, cl = self._f(cur["high"]),  self._f(cur["low"])
        return (ch >= ph) and (cl <= pl) and self._is_bear(cur["open"], cur["close"])

    def _is_bullish_kicker(self, prev, cur) -> bool:
        return (self._is_bear(prev["open"], prev["close"]) and
                self._gap_above(prev["high"], cur["low"]) and
                self._is_bull(cur["open"],  cur["close"]))

    def _is_bearish_kicker(self, prev, cur) -> bool:
        return (self._is_bull(prev["open"], prev["close"]) and
                self._gap_below(prev["low"], cur["high"]) and
                self._is_bear(cur["open"],  cur["close"]))

    def _is_morning_star(self, p2, p1, cur) -> bool:
        # bear -> small body -> bull closing into p2 body midpoint
        mid_p2 = (self._f(p2["open"]) + self._f(p2["close"])) / 2.0
        return (self._is_bear(p2["open"], p2["close"]) and
                self._body(p1["open"], p1["close"]) <= 0.5 * self._body(p2["open"], p2["close"]) and
                self._is_bull(cur["open"], cur["close"]) and
                self._f(cur["close"]) >= mid_p2)

    def _is_evening_star(self, p2, p1, cur) -> bool:
        mid_p2 = (self._f(p2["open"]) + self._f(p2["close"])) / 2.0
        return (self._is_bull(p2["open"], p2["close"]) and
                self._body(p1["open"], p1["close"]) <= 0.5 * self._body(p2["open"], p2["close"]) and
                self._is_bear(cur["open"], cur["close"]) and
                self._f(cur["close"]) <= mid_p2)

    def _is_three_inside_up(self, prev, cur, nxt) -> bool:
        return self._is_bullish_harami(prev, cur) and (self._f(nxt["close"]) > self._f(prev["open"]))

    def _is_three_inside_down(self, prev, cur, nxt) -> bool:
        return self._is_bearish_harami(prev, cur) and (self._f(nxt["close"]) < self._f(prev["open"]))

    def _is_rising_three_methods(self, p4, p3, p2, p1, cur) -> bool:
        big_bull = self._is_bull(p4["open"], p4["close"]) and \
                self._body(p4["open"], p4["close"]) >= 0.5 * self._range(p4["high"], p4["low"])
        inside_all = all([(self._f(b["high"]) <= self._f(p4["high"]) and self._f(b["low"]) >= self._f(p4["low"]))
                        for b in (p3, p2, p1)])
        small_counter = all([self._body(b["open"], b["close"]) <= 0.6 * self._body(p4["open"], p4["close"])
                            for b in (p3, p2, p1)])
        return big_bull and inside_all and small_counter and \
            self._is_bull(cur["open"], cur["close"]) and (self._f(cur["close"]) > self._f(p4["high"]))

    def _is_falling_three_methods(self, p4, p3, p2, p1, cur) -> bool:
        big_bear = self._is_bear(p4["open"], p4["close"]) and \
                self._body(p4["open"], p4["close"]) >= 0.5 * self._range(p4["high"], p4["low"])
        inside_all = all([(self._f(b["high"]) <= self._f(p4["high"]) and self._f(b["low"]) >= self._f(p4["low"]))
                        for b in (p3, p2, p1)])
        small_counter = all([self._body(b["open"], b["close"]) <= 0.6 * self._body(p4["open"], p4["close"])
                            for b in (p3, p2, p1)])
        return big_bear and inside_all and small_counter and \
            self._is_bear(cur["open"], cur["close"]) and (self._f(cur["close"]) < self._f(p4["low"]))

    def _rr(self, p: "PatternDirector.Plan") -> Optional[float]:
        if p.entry is None or p.stop is None or p.target is None:
            return None
        if p.side == "bull":
            risk   = max(p.entry - p.stop, 1e-9)
            reward = p.target - p.entry
        else:
            risk   = max(p.stop - p.entry, 1e-9)
            reward = p.entry - p.target
        return float(reward / risk)

    def _next_step_text(self, p: "PatternDirector.Plan", price: Optional[float]) -> str:
        # If we don't have levels, nothing actionable to say.
        if p.entry is None or p.stop is None or p.target is None:
            # For pure-neutral candles you could return a generic hint, but we prefer silence.
            return ""

        # Prefer a real-time price if caller didn't provide one
        if price is None:
            return f"Arm a buy-stop at {p.entry:.2f}; initial stop {p.stop:.2f}; target {p.target:.2f}. Cancel if close < {p.stop:.2f}."

        # Bull logic (default in your long-only flow)
        if p.side == "bull":
            if price < p.entry:
                return f"Set buy-stop {p.entry:.2f}; stop {p.stop:.2f}; target {p.target:.2f}. Cancel if close < {p.stop:.2f}."
            else:
                return f"Triggered above {p.entry:.2f}. Manage toward {p.target:.2f}; exit if close < {p.stop:.2f}."
        else:
            # For completeness if you enable shorts
            if price > p.entry:
                return f"Set sell-stop {p.entry:.2f}; stop {p.stop:.2f}; target {p.target:.2f}. Cancel if close > {p.stop:.2f}."
            else:
                return f"Triggered below {p.entry:.2f}. Manage toward {p.target:.2f}; exit if close > {p.stop:.2f}."



if __name__ == "__main__":
    PatternDirector().run()
