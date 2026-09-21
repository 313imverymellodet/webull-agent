"""Market-data feed. Default implementation: yfinance (free, delayed).

Provides two things the ORB engine needs:
  * intraday 1-minute bars (OHLCV) for the underlying, indexed in US/Eastern
  * an option contract pick (nearest weekly, ATM or first-OTM) with a price

Swap this class for a Polygon/Alpaca/Webull feed later without touching the
strategy or runner — they only depend on these method signatures.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
ET = "America/New_York"


@dataclass
class OptionPick:
    underlying: str
    right: str          # "CALL" / "PUT"
    strike: float
    expiry: str         # YYYY-MM-DD
    bid: float
    ask: float
    last: float
    contract_symbol: str

    @property
    def price(self) -> float:
        """Marketable-ish limit price: mid if both sides present, else last/ask."""
        if self.bid and self.ask:
            return round((self.bid + self.ask) / 2, 2)
        return round(self.ask or self.last, 2)


class YFinanceFeed:
    def intraday_bars(self, symbol: str, lookback_days: int = 1) -> pd.DataFrame:
        df = yf.Ticker(symbol).history(period=f"{lookback_days}d", interval="1m")
        if df.empty:
            return df
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ET)
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def session_bars(self, symbol: str, day: Optional[date] = None) -> pd.DataFrame:
        """Bars for ONE session (default: today), empty if that session has none yet.

        Fetches 2 days and filters, because a 1-day request pre-open returns the
        PREVIOUS session -- acting on that would trade yesterday's breakout.
        """
        day = day or date.today()
        df = self.intraday_bars(symbol, lookback_days=2)
        if df.empty:
            return df
        return df[[ts.date() == day for ts in df.index]]

    def spot(self, symbol: str) -> float:
        df = self.intraday_bars(symbol)
        if df.empty:
            raise RuntimeError(f"no bars for {symbol}")
        return float(df["Close"].iloc[-1])

    def bars(self, symbol: str, interval: str = "1d", period: str = "2y") -> pd.DataFrame:
        """OHLCV history for swing timeframes (1d / 1h). Index in US/Eastern."""
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        if df.empty:
            return df
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ET)
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

    def pick_option_dte(self, symbol: str, right: str, target_dte: int = 35,
                        moneyness: str = "ATM", spot: Optional[float] = None,
                        min_dte: int = 7) -> OptionPick:
        """ATM/OTM1 contract whose expiry is closest to target_dte.

        Swing holds run days, so short-dated contracts bleed theta; 30-45 DTE
        keeps decay small over a typical hold.
        """
        tk = yf.Ticker(symbol)
        today = date.today()
        exps = [(e, abs((datetime.strptime(e, "%Y-%m-%d").date() - today).days - target_dte))
                for e in tk.options
                if (datetime.strptime(e, "%Y-%m-%d").date() - today).days >= min_dte]
        if not exps:
            raise RuntimeError(f"no expiry >= {min_dte} DTE for {symbol}")
        expiry = min(exps, key=lambda x: x[1])[0]
        if spot is None:
            spot = self.spot(symbol)
        chain = tk.option_chain(expiry)
        table = (chain.calls if right == "CALL" else chain.puts).copy()
        table["dist"] = (table["strike"] - spot).abs()
        table = table.sort_values("dist").reset_index(drop=True)
        if moneyness == "ATM":
            row = table.iloc[0]
        else:
            otm = (table[table["strike"] > spot].sort_values("strike") if right == "CALL"
                   else table[table["strike"] < spot].sort_values("strike", ascending=False))
            row = otm.iloc[0] if not otm.empty else table.iloc[0]
        return OptionPick(
            underlying=symbol, right=right, strike=float(row["strike"]), expiry=expiry,
            bid=float(row.get("bid") or 0), ask=float(row.get("ask") or 0),
            last=float(row.get("lastPrice") or 0), contract_symbol=str(row["contractSymbol"]),
        )

    def _nearest_weekly_expiry(self, tk: yf.Ticker, skip_0dte: bool = True) -> str:
        today = date.today()
        for e in tk.options:
            d = datetime.strptime(e, "%Y-%m-%d").date()
            if skip_0dte and d <= today:
                continue
            return e
        return tk.options[0]  # fallback

    def pick_option(self, symbol: str, right: str, moneyness: str = "ATM",
                    spot: Optional[float] = None) -> OptionPick:
        """right: CALL/PUT. moneyness: ATM or OTM1 (first strike out of the money)."""
        tk = yf.Ticker(symbol)
        if spot is None:
            spot = self.spot(symbol)
        expiry = self._nearest_weekly_expiry(tk)
        chain = tk.option_chain(expiry)
        table = chain.calls if right == "CALL" else chain.puts
        table = table.copy()
        table["dist"] = (table["strike"] - spot).abs()
        table = table.sort_values("dist").reset_index(drop=True)

        if moneyness == "ATM":
            row = table.iloc[0]
        else:  # OTM1: first strike beyond spot in the right direction
            if right == "CALL":
                otm = table[table["strike"] > spot].sort_values("strike")
            else:
                otm = table[table["strike"] < spot].sort_values("strike", ascending=False)
            row = otm.iloc[0] if not otm.empty else table.iloc[0]

        return OptionPick(
            underlying=symbol, right=right, strike=float(row["strike"]), expiry=expiry,
            bid=float(row.get("bid") or 0), ask=float(row.get("ask") or 0),
            last=float(row.get("lastPrice") or 0), contract_symbol=str(row["contractSymbol"]),
        )
