"""EMA Crossover Swing — pure signal logic (Python port of the Pine v6 strategy).

Entry: 9/21 EMA cross, gated by EMA stack (9>21>55), higher-timeframe stack
agreement, and ADX >= threshold, with price on the correct side of the stop EMA.
Stop: the stop EMA's value at entry.  Target: entry +/- R * rr, where R is the
entry-to-stop distance.  Optional exit on an opposite signal.

Deliberate difference from the ORB strategy: stop and target are levels on the
UNDERLYING, never a percentage of option value. A -30% option stop equalled a
0.25% move in SPY, which sat inside normal noise.

No network and no broker here so the rules stay unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

HTF_RULE = {"1d": "W", "1h": "4h", "30m": "2h", "15m": "1h"}


@dataclass
class EMAConfig:
    len1: int = 9
    len2: int = 21
    len3: int = 55
    len4: int = 200
    rr: float = 2.0                 # target = rr * risk
    adx_len: int = 14
    adx_min: float = 20.0
    use_stack: bool = True
    use_htf: bool = True
    use_adx: bool = True
    stop_ema: str = "ema2"          # "ema2" (21) or "ema3" (55)
    opp_exit: bool = True
    allow_long: bool = True
    allow_short: bool = True
    min_risk_pct: float = 0.5       # skip crossovers whose stop sits inside the noise
    max_risk_pct: float = 0.0       # 0 = no cap


@dataclass
class EMASignal:
    symbol: str
    side: str            # CALL (long) / PUT (short)
    bar_time: pd.Timestamp
    entry: float
    stop: float
    target: float
    risk: float          # entry-to-stop distance, in price
    risk_pct: float
    adx: float
    reason: str


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def adx(high, low, close, n=14) -> pd.Series:
    """Wilder's ADX, matching Pine's ta.dmi()."""
    up, dn = high.diff(), -low.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus, index=high.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus, index=high.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def htf_stack(df: pd.DataFrame, rule: str):
    """Higher-timeframe EMA stack, using only COMPLETED HTF bars (no lookahead)."""
    r = df.resample(rule).agg({"Open": "first", "High": "max", "Low": "min",
                               "Close": "last"}).dropna()
    e1, e2, e3 = ema(r.Close, 9), ema(r.Close, 21), ema(r.Close, 55)
    bull = ((e1 > e2) & (e2 > e3)).shift(1)
    bear = ((e1 < e2) & (e2 < e3)).shift(1)
    return (bull.reindex(df.index, method="ffill").fillna(False),
            bear.reindex(df.index, method="ffill").fillna(False))


def indicators(df: pd.DataFrame, cfg: EMAConfig, htf_rule: str) -> pd.DataFrame:
    d = pd.DataFrame(index=df.index)
    d["close"] = df.Close
    d["e1"], d["e2"] = ema(df.Close, cfg.len1), ema(df.Close, cfg.len2)
    d["e3"], d["e4"] = ema(df.Close, cfg.len3), ema(df.Close, cfg.len4)
    d["adx"] = adx(df.High, df.Low, df.Close, cfg.adx_len)
    d["htf_bull"], d["htf_bear"] = htf_stack(df, htf_rule)
    d["stop_ema"] = d["e2"] if cfg.stop_ema == "ema2" else d["e3"]
    d["x_up"] = (d.e1 > d.e2) & (d.e1.shift(1) <= d.e2.shift(1))
    d["x_dn"] = (d.e1 < d.e2) & (d.e1.shift(1) >= d.e2.shift(1))
    stack_b = (d.e1 > d.e2) & (d.e2 > d.e3) if cfg.use_stack else True
    stack_r = (d.e1 < d.e2) & (d.e2 < d.e3) if cfg.use_stack else True
    okb = d.htf_bull if cfg.use_htf else True
    okr = d.htf_bear if cfg.use_htf else True
    adx_ok = (d.adx >= cfg.adx_min) if cfg.use_adx else True
    d["bull"] = d.x_up & stack_b & okb & adx_ok & (d.close > d.stop_ema)
    d["bear"] = d.x_dn & stack_r & okr & adx_ok & (d.close < d.stop_ema)
    return d


def signal_at(symbol: str, df: pd.DataFrame, cfg: EMAConfig, htf_rule: str,
              i: int = -1) -> Optional[EMASignal]:
    """Signal on bar i (default: the most recent COMPLETED bar)."""
    d = indicators(df, cfg, htf_rule)
    row = d.iloc[i]
    if row.bull and cfg.allow_long:
        side, sgn = "CALL", 1
    elif row.bear and cfg.allow_short:
        side, sgn = "PUT", -1
    else:
        return None
    entry, stop = float(row.close), float(row.stop_ema)
    risk = abs(entry - stop)
    risk_pct = risk / entry * 100
    if risk <= 0 or risk_pct < max(1e-3, cfg.min_risk_pct):
        return None                  # stop too close: noise would take it out
    if cfg.max_risk_pct and risk_pct > cfg.max_risk_pct:
        return None
    return EMASignal(symbol, side, d.index[i], entry, stop, entry + sgn * risk * cfg.rr,
                     risk, risk / entry * 100, float(row.adx),
                     f"9/21 cross {'up' if sgn > 0 else 'down'}, stack+HTF aligned, "
                     f"ADX {row.adx:.1f} >= {cfg.adx_min}, risk {risk/entry*100:.2f}%")


def exit_check(side: str, price: float, stop: float, target: float):
    """Return 'stop' / 'target' / None for a live underlying price."""
    if side == "CALL":
        if price <= stop:
            return "stop"
        if price >= target:
            return "target"
    else:
        if price >= stop:
            return "stop"
        if price <= target:
            return "target"
    return None
