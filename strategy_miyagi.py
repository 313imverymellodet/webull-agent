"""12-Hour Miyagi (1-3-1) — pure setup logic, shared by backtest and live runner.

12-hour candles (ET, extended hours): 4:00 AM-4:00 PM and 4:00 PM-4:00 AM.
Setup: candle1 (4PM) inside, candle2 (4AM) outside, candle3 (4PM) inside.
Candle4 (4AM, live) must be a 2U or 2D by 9:30; breaking both sides first
invalidates it. Trigger = 50% of candle3. At/after 9:30: 2U + price at the
trigger -> PUTS; 2D + price at the trigger -> CALLS.
Targets: T1 = candle3 low/high; T2 = candle2 low/high.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def candles12(d: pd.DataFrame) -> pd.DataFrame:
    hm = d.index.hour * 60 + d.index.minute
    sess = np.where((hm >= 240) & (hm < 960), "AM", np.where(hm >= 960, "PM", None))
    x = d[sess != None].copy()  # noqa: E711
    x["sess"] = sess[sess != None]  # noqa: E711
    x["date"] = [t.date() for t in x.index]
    x["ts"] = x.index
    g = x.groupby(["date", "sess"], sort=False).agg(
        Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"),
        Close=("Close", "last"), start=("ts", "first"))
    return g.reset_index().sort_values("start").reset_index(drop=True)


def ctype(c, p) -> str:
    up, dn = c.High > p.High, c.Low < p.Low
    return "3" if up and dn else "2U" if up else "2D" if dn else "1"


def find_setup(hourly: pd.DataFrame, today):
    """1-3-1 setup whose 4th candle is today's 4AM candle, or None.

    Uses only candles completed before today, so it can run pre-market."""
    C = candles12(hourly)
    C = C[C.date < today].reset_index(drop=True)
    if len(C) < 4:
        return None
    c0, c1, c2, c3 = C.iloc[-4], C.iloc[-3], C.iloc[-2], C.iloc[-1]
    if (c1.sess, c2.sess, c3.sess) != ("PM", "AM", "PM"):
        return None
    if (ctype(c1, c0), ctype(c2, c1), ctype(c3, c2)) != ("1", "3", "1"):
        return None
    return {"trigger": (c3.High + c3.Low) / 2, "c3_high": float(c3.High), "c3_low": float(c3.Low),
            "c2_high": float(c2.High), "c2_low": float(c2.Low)}


def premarket_kind(setup, pre_high, pre_low) -> str:
    up, dn = pre_high > setup["c3_high"], pre_low < setup["c3_low"]
    return "3" if up and dn else "2U" if up else "2D" if dn else "1"
