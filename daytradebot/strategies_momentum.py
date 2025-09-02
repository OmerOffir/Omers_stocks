# strategies_momentum.py
from __future__ import annotations
import pandas as pd
import numpy as np

# ------------ helpers ------------
def _s(x):
    return x.iloc[:,0] if isinstance(x, pd.DataFrame) and x.shape[1] == 1 else x

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    macd_signal = ema(macd_line, signal)
    macd_hist = macd_line - macd_signal
    return macd_line, macd_signal, macd_hist

def cci(df: pd.DataFrame, n=14):
    tp = (_s(df["high"]) + _s(df["low"]) + _s(df["close"])) / 3.0
    ma = tp.rolling(n).mean()
    md = (tp - ma).abs().rolling(n).mean().replace(0, 1e-12)
    return (tp - ma) / (0.015 * md)

def compute_indicators(df: pd.DataFrame, macd_fast=12, macd_slow=26, macd_sig=9, cci_len=14) -> pd.DataFrame:
    out = df.copy()
    close = _s(out["close"])
    out["ema20"] = ema(close, 20)
    m_line, m_sig, m_hist = macd(close, macd_fast, macd_slow, macd_sig)
    out["macd_line"] = m_line
    out["macd_signal"] = m_sig
    out["macd_hist"] = m_hist
    out["cci"] = cci(out, cci_len)
    return out

# ------------ candlestick anatomy ------------
def _body(o, c): return (c - o).abs()
def _range(h, l): return (h - l).abs()
def _upper(o, h, c): return h - np.maximum(o, c)
def _lower(o, l, c): return np.minimum(o, c) - l

# ------------ pattern detectors ------------
def is_doji(o, h, l, c, body_pct=0.2):
    rng = _range(h, l)
    body = _body(o, c)
    return (rng > 0) & (body <= (rng * body_pct))

def is_hammer(o, h, l, c, body_max_pct=0.35, lower_to_body_min=2.0, upper_max_pct=0.25):
    rng = _range(h, l)
    body = _body(o, c)
    upper = _upper(o, h, c)
    lower = _lower(o, l, c)
    small_body = (body <= rng * body_max_pct)
    long_lower = (lower >= lower_to_body_min * (body + 1e-12))
    small_upper = (upper <= rng * upper_max_pct)
    # body near the top of range (close/open close to high)
    body_near_top = (np.maximum(o, c) >= (h - rng * 0.25))
    return small_body & long_lower & small_upper & body_near_top

def is_inverted_hammer(o, h, l, c, body_max_pct=0.35, upper_to_body_min=2.0, lower_max_pct=0.25):
    rng = _range(h, l)
    body = _body(o, c)
    upper = _upper(o, h, c)
    lower = _lower(o, l, c)
    small_body = (body <= rng * body_max_pct)
    long_upper = (upper >= upper_to_body_min * (body + 1e-12))
    small_lower = (lower <= rng * lower_max_pct)
    # body near the bottom of range (close/open close to low)
    body_near_bottom = (np.minimum(o, c) <= (l + rng * 0.25))
    return small_body & long_upper & small_lower & body_near_bottom

def is_shooting_star(o, h, l, c, body_max_pct=0.35, upper_to_body_min=2.0, lower_max_pct=0.25):
    # bearish analogue (like inverted hammer but in uptrend context)
    return is_inverted_hammer(o, h, l, c, body_max_pct, upper_to_body_min, lower_max_pct)

def bullish_engulfing(prev_o, prev_c, o, c):
    # current green candle engulfing previous real body
    return (c > o) & (prev_c < prev_o) & (o <= prev_c) & (c >= prev_o)

def bearish_engulfing(prev_o, prev_c, o, c):
    # current red candle engulfing previous real body
    return (c < o) & (prev_c > prev_o) & (o >= prev_c) & (c <= prev_o)

# ------------ signal constructors ------------
def momentum_entries(
    df: pd.DataFrame,
    *,
    doji_body_pct=0.20,
    macd_fast=12, macd_slow=26, macd_signal=9,
    cci_entry=0,
    confirm_break_high=True,
    use_patterns=("doji","hammer","inverted_hammer","bullish_engulfing"),
) -> pd.Series:
    """
    Long entry if the PREVIOUS candle is a bullish/neutral reversal pattern, AND:
      - MACD line > signal (bullish), and
      - CCI > cci_entry,
      - (optional) current close > previous high (breakout confirmation)
    Stop will be set to the previous candle's LOW in the bot loop.
    """
    o = _s(df["open"]); h = _s(df["high"]); l = _s(df["low"]); c = _s(df["close"])
    prev_o, prev_h, prev_l, prev_c = o.shift(1), h.shift(1), l.shift(1), c.shift(1)

    patt_prev = pd.Series(False, index=df.index)
    if "doji" in use_patterns:
        patt_prev |= is_doji(prev_o, prev_h, prev_l, prev_c, doji_body_pct)
    if "hammer" in use_patterns:
        patt_prev |= is_hammer(prev_o, prev_h, prev_l, prev_c)
    if "inverted_hammer" in use_patterns:
        patt_prev |= is_inverted_hammer(prev_o, prev_h, prev_l, prev_c)
    if "bullish_engulfing" in use_patterns:
        patt_prev |= bullish_engulfing(prev_o.shift(1), prev_c.shift(1), prev_o, prev_c)

    # momentum confirmations
    macd_line = _s(df["macd_line"]); macd_sig = _s(df["macd_signal"])
    cci_v = _s(df["cci"])

    cond = patt_prev & (macd_line > macd_sig) & (cci_v > cci_entry)
    if confirm_break_high:
        cond &= (c > prev_h)
    return cond.fillna(False)

def momentum_exit_flip(
    df: pd.DataFrame, *, cci_exit=0, include_patterns=("shooting_star","bearish_engulfing")
) -> pd.Series:
    """
    Exit when:
      - MACD crosses down AND CCI < threshold, OR
      - A bearish reversal candle appears (shooting star / bearish engulfing)
    """
    o = _s(df["open"]); h = _s(df["high"]); l = _s(df["low"]); c = _s(df["close"])
    prev_o, prev_c = o.shift(1), c.shift(1)

    macd_line = _s(df["macd_line"]); macd_sig = _s(df["macd_signal"]); cci_v = _s(df["cci"])
    cross_down = (macd_line <= macd_sig) & (macd_line.shift(1) > macd_sig.shift(1))
    momentum_flip = cross_down & (cci_v < cci_exit)

    bearish = pd.Series(False, index=df.index)
    if "shooting_star" in include_patterns:
        bearish |= is_shooting_star(o, h, l, c)
    if "bearish_engulfing" in include_patterns:
        bearish |= bearish_engulfing(prev_o, prev_c, o, c)

    return (momentum_flip | bearish).fillna(False)
