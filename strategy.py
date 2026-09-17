"""Opening Range Breakout (ORB) + volume strategy — pure signal logic.

No network, no broker: give it a DataFrame of the day's intraday bars and it
tells you whether a breakout with volume confirmation has occurred. This keeps
the trading rules unit-testable and independent of data source or broker.

Rules
-----
1. Opening range (OR) = high/low of the first `or_minutes` of the RTH session
   (session open 09:30 ET).
2. Volume baseline = average per-bar volume during the OR window.
3. After the OR window closes, the first bar that CLOSES above OR-high (with a
   small buffer) on volume >= `volume_mult` x baseline is a LONG/CALL signal;
   the first that closes below OR-low likewise is a SHORT/PUT signal.
4. At most one signal per side per day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Optional

import pandas as pd

SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)


@dataclass
class ORBConfig:
    or_minutes: int = 15          # opening-range window length
    volume_mult: float = 1.5      # breakout bar volume vs OR-window avg
    buffer_pct: float = 0.05      # % beyond OR level required to trigger (noise filter)
    allow_long: bool = True
    allow_short: bool = True
    entry_cutoff: time = time(12, 0)  # no NEW entries at/after this ET time


@dataclass
class Signal:
    symbol: str
    side: str          # "CALL" or "PUT"
    trigger_price: float
    or_high: float
    or_low: float
    bar_time: pd.Timestamp
    bar_volume: float
    vol_baseline: float
    reason: str


def _rth(bars: pd.DataFrame, session_date=None) -> pd.DataFrame:
    """Keep regular-trading-hours bars for ONE session (tz-aware index, ET).

    Defaults to the latest session present. This guard matters: a feed asked for
    "1 day" pre-market returns the PREVIOUS complete session, and without pinning
    the date the strategy would compute an opening range off stale bars and fire
    a signal for a day that already ended.
    """
    if bars.empty:
        return bars
    if session_date is None:
        session_date = max(ts.date() for ts in bars.index)
    mask = [(ts.date() == session_date and SESSION_OPEN <= ts.time() < SESSION_CLOSE)
            for ts in bars.index]
    return bars[mask]


def opening_range(bars: pd.DataFrame, cfg: ORBConfig, session_date=None):
    """Return (or_high, or_low, vol_baseline, or_end_ts) or None if OR not complete."""
    rth = _rth(bars, session_date)
    if rth.empty:
        return None
    day_open = rth.index[0].normalize() + pd.Timedelta(hours=9, minutes=30)
    or_end = day_open + pd.Timedelta(minutes=cfg.or_minutes)
    window = rth[(rth.index >= day_open) & (rth.index < or_end)]
    if window.empty or rth.index[-1] < or_end:
        return None  # not enough bars yet to define the range
    return (
        float(window["High"].max()),
        float(window["Low"].min()),
        float(window["Volume"].mean()),
        or_end,
    )


def evaluate(symbol: str, bars: pd.DataFrame, cfg: ORBConfig,
             taken_sides: Optional[set] = None, session_date=None,
             not_before=None) -> Optional[Signal]:
    """Return the first qualifying Signal not already in `taken_sides`, else None."""
    taken_sides = taken_sides or set()
    orr = opening_range(bars, cfg, session_date)
    if orr is None:
        return None
    or_high, or_low, vol_baseline, or_end = orr
    up = or_high * (1 + cfg.buffer_pct / 100)
    dn = or_low * (1 - cfg.buffer_pct / 100)

    post = _rth(bars, session_date)
    post = post[post.index >= or_end]
    for ts, row in post.iterrows():
        if ts.time() >= cfg.entry_cutoff:
            break  # too late in the session for a new breakout entry
        if not_before is not None and ts < not_before:
            continue  # breakout happened before we were watching; don't chase it
        vol_ok = row["Volume"] >= cfg.volume_mult * vol_baseline
        if not vol_ok:
            continue
        if cfg.allow_long and "CALL" not in taken_sides and row["Close"] > up:
            return Signal(symbol, "CALL", float(row["Close"]), or_high, or_low, ts,
                          float(row["Volume"]), vol_baseline,
                          f"close {row['Close']:.2f} > OR-high {or_high:.2f} +buf, "
                          f"vol {row['Volume']:.0f} >= {cfg.volume_mult}x{vol_baseline:.0f}")
        if cfg.allow_short and "PUT" not in taken_sides and row["Close"] < dn:
            return Signal(symbol, "PUT", float(row["Close"]), or_high, or_low, ts,
                          float(row["Volume"]), vol_baseline,
                          f"close {row['Close']:.2f} < OR-low {or_low:.2f} -buf, "
                          f"vol {row['Volume']:.0f} >= {cfg.volume_mult}x{vol_baseline:.0f}")
    return None
