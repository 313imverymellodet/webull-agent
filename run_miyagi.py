#!/usr/bin/env python3
"""12-Hour Miyagi (1-3-1) live runner — paper trading.

  9:30  arm setups: 1-3-1 + 4th candle a 2U/2D + price on the correct side of the trigger
  RTH   watch Webull's live price; at the trigger -> PUTS (2U) or CALLS (2D)
  exits target (MIYAGI_TARGET: T2 default, or T1), stop (MIYAGI_STOP: flip = the doc's
        60-minute flip, or candle3), flatten everything at MIYAGI_EOD (15:55)

Shares the account-wide $ limit with the EMA runner (EMA_ACCOUNT_SIZE).
Backtest (20 symbols, 2y): as written PF 0.54-0.91 — negative. Paper only.

Modes: --scan (report setups, no orders)  --live
"""
from __future__ import annotations

import argparse
import os
import time as _time
import traceback
from datetime import date, datetime, time

import pandas as pd
import yfinance as yf

import run_ema as R                       # shared order/fill/state helpers + env
from feed import YFinanceFeed
from strategy_miyagi import find_setup, premarket_kind

STATE = "state/miyagi_state.json"
ET = "America/New_York"


def env(k, d): return os.getenv(k, d)
def symbols(): return [s.strip().upper() for s in env("MIYAGI_SYMBOLS", env("EMA_SYMBOLS", "SPY")).split(",") if s.strip()]
def hm(s): h, m = s.split(":"); return time(int(h), int(m))


def emit(sym, kind, text):
    R.emit(sym, kind, f"[miyagi] {text}")


def hourly(sym):
    d = yf.Ticker(sym).history(period="10d", interval="1h", prepost=True)
    d.index = d.index.tz_convert(ET)
    return d[["Open", "High", "Low", "Close"]]


def minute_bars(sym):
    d = yf.Ticker(sym).history(period="1d", interval="1m", prepost=True)
    if d.empty:
        return d
    d.index = d.index.tz_convert(ET)
    return d[d.index.date == date.today()][["Open", "High", "Low", "Close"]]


def rth_hour_candles(m1: pd.DataFrame, now: datetime):
    """COMPLETED 60-minute RTH candles aligned to 9:30 (9:30-10:30, 10:30-11:30, ...)."""
    rth = m1[[(t.hour * 60 + t.minute) >= 570 for t in m1.index]]
    if rth.empty:
        return []
    mins_now = now.hour * 60 + now.minute
    out = {}
    for t, row in rth.iterrows():
        b = ((t.hour * 60 + t.minute) - 570) // 60
        if 570 + (b + 1) * 60 > mins_now:
            continue                     # still forming
        o = out.setdefault(b, {"bucket": b, "High": row.High, "Low": row.Low})
        o["High"], o["Low"] = max(o["High"], row.High), min(o["Low"], row.Low)
    return [out[k] for k in sorted(out)]


class Miyagi:
    def __init__(self, live=True):
        self.feed = YFinanceFeed()
        self.broker = None
        if live:
            from broker import WebullOptionsBroker
            self.broker = WebullOptionsBroker()
        st = R.load_json(STATE, {})
        fresh = st.get("date") == str(date.today())
        self.armed = st.get("armed", {}) if fresh else {}
        self.taken = set(st.get("taken", [])) if fresh else set()
        self.positions = st.get("positions", []) if st else []
        self.setups = st.get("setups", {}) if fresh else {}
        self.armed_done = bool(st.get("armed_done")) if fresh else False

    def save(self, phase):
        R.save_json(STATE, {"strategy": "miyagi", "date": str(date.today()), "phase": phase,
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                            "setups": self.setups, "armed": self.armed, "taken": sorted(self.taken),
                            "positions": self.positions, "armed_done": self.armed_done,
                            "config": {"target": env("MIYAGI_TARGET", "T2"), "stop": env("MIYAGI_STOP", "flip"),
                                       "target_dte": int(env("MIYAGI_TARGET_DTE", "5")),
                                       "max_position_usd": float(env("MIYAGI_MAX_POSITION_USD", "500")),
                                       "eod": env("MIYAGI_EOD", "15:55")},
                            "events": R.EVENTS})      # this process only holds Miyagi events

    # ---------- setups ----------
    def scan_setups(self):
        today = date.today()
        for sym in symbols():
            try:
                h = hourly(sym)
                s = find_setup(h, today)
                if not s:
                    self.setups[sym] = {"status": "no setup"}; continue
                pre = h[(h.index.date == today) & ((h.index.hour * 60 + h.index.minute) < 570)]
                kind = premarket_kind(s, pre.High.max(), pre.Low.min()) if len(pre) else "1"
                self.setups[sym] = {**s, "status": "setup", "premarket": kind}
            except Exception as e:
                self.setups[sym] = {"status": "error", "error": str(e)[:120]}

    def arm(self):
        """At the open: apply the 2U/2D and open-side rules."""
        for sym, s in self.setups.items():
            if s.get("status") != "setup":
                continue
            kind, T = s["premarket"], s["trigger"]
            if kind == "3":
                emit(sym, "skip", f"invalid: 4th candle broke both sides of candle 3 pre-market"); continue
            if kind == "1":
                emit(sym, "skip", "4th candle still inside candle 3 at the open (not a 2)"); continue
            m1 = minute_bars(sym)
            rth = m1[[(t.hour * 60 + t.minute) >= 570 for t in m1.index]]
            if rth.empty:
                emit(sym, "warn", "no 9:30 bar yet; will retry"); return False
            op = float(rth.Open.iloc[0])
            if (kind == "2U" and not op > T) or (kind == "2D" and not op < T):
                emit(sym, "skip", f"{kind} but opened {op:.2f} on the wrong side of trigger {T:.2f}"); continue
            side = "PUT" if kind == "2U" else "CALL"
            self.armed[sym] = {**s, "kind": kind, "side": side, "open": op}
            emit(sym, "armed", f"{kind} opened {op:.2f}; {side}S if price reaches {T:.2f} "
                               f"(T1 {s['c3_low' if side=='PUT' else 'c3_high']:.2f}, "
                               f"T2 {s['c2_low' if side=='PUT' else 'c2_high']:.2f})")
        self.armed_done = True
        return True

    # ---------- entries ----------
    def watch_triggers(self, now):
        if now.time() >= hm(env("MIYAGI_ENTRY_CUTOFF", "15:00")):
            return
        for sym, a in list(self.armed.items()):
            if sym in self.taken:
                continue
            q = self.broker.stock_quote(sym) or {}
            px = q.get("price")
            if not px:
                continue
            hit = px <= a["trigger"] if a["side"] == "PUT" else px >= a["trigger"]
            if not hit:
                continue
            self.taken.add(sym)                      # one attempt per setup, fill or not
            emit(sym, "signal", f"trigger {a['trigger']:.2f} hit at {px:.2f} -> {a['side']}S")
            self.enter(sym, a, px)

    def enter(self, sym, a, px):
        right = a["side"]
        pick = self.feed.pick_option_dte(sym, right, int(env("MIYAGI_TARGET_DTE", "5")), "ATM",
                                         spot=px, min_dte=int(env("MIYAGI_MIN_DTE", "1")))
        q = self.broker.option_quote(pick.contract_symbol) or {}
        ask = q.get("ask") or pick.ask
        if not ask:
            emit(sym, "blocked", f"no quote for {pick.contract_symbol}"); return
        size = R.envf("EMA_ACCOUNT_SIZE", "0")
        deployed = R.account_deployed(self.broker)
        available = (size - deployed) if size else float("inf")
        budget = min(available, float(env("MIYAGI_MAX_POSITION_USD", "500")))
        n = min(int(budget // (ask * 100)), int(R.envf("WEBULL_MAX_ORDER_QTY", "5")))
        if n < 1:
            emit(sym, "skipped", f"{pick.contract_symbol} ${ask*100:.0f}/contract; only ${budget:.0f} "
                                 f"available (account ${size:.0f}, deployed ${deployed:.0f})")
            return
        emit(sym, "sizing", f"{pick.contract_symbol} ({R.dte(pick.expiry)}DTE) ask {ask} x{n}")
        r = R.place_with_reprice(self.broker, pick, n, side="BUY")
        if not r:
            return
        self.positions.append({"symbol": sym, "side": right, "occ": pick.contract_symbol,
                               "strike": pick.strike, "expiry": pick.expiry, "qty": r["qty"],
                               "entry_premium": r["fill"], "trigger": a["trigger"],
                               "t1": a["c3_low"] if right == "PUT" else a["c3_high"],
                               "t2": a["c2_low"] if right == "PUT" else a["c2_high"],
                               "c3_high": a["c3_high"], "c3_low": a["c3_low"],
                               "entry_time": datetime.now().isoformat(timespec="seconds"),
                               "entry_bucket": (datetime.now().hour * 60 + datetime.now().minute - 570) // 60})

    # ---------- exits ----------
    def manage(self, now):
        keep = []
        eod = now.time() >= hm(env("MIYAGI_EOD", "15:55"))
        for p in self.positions:
            q = self.broker.stock_quote(p["symbol"]) or {}
            px = q.get("price")
            if not px:
                keep.append(p); continue
            p["last_px"] = px
            put = p["side"] == "PUT"
            tgt = p["t2"] if env("MIYAGI_TARGET", "T2") == "T2" else p["t1"]
            reason = None
            if (put and px <= tgt) or ((not put) and px >= tgt):
                reason = f"target {tgt:.2f}"
            elif env("MIYAGI_STOP", "flip") == "candle3":
                if (put and px > p["c3_high"]) or ((not put) and px < p["c3_low"]):
                    reason = "candle-3 stop"
            else:
                done = [c for c in rth_hour_candles(minute_bars(p["symbol"]), now)
                        if c["bucket"] >= p["entry_bucket"]]
                if done:
                    ref = done[-1]
                    if (put and px > ref["High"]) or ((not put) and px < ref["Low"]):
                        reason = f"60-min flip ({'above' if put else 'below'} {ref['High' if put else 'Low']:.2f})"
            if not reason and eod:
                reason = "end of day"
            if not reason:
                keep.append(p); continue
            emit(p["symbol"], "exit", f"{reason} at {px:.2f}")
            from feed import OptionPick
            pick = OptionPick(p["symbol"], p["side"], p["strike"], p["expiry"], 0, 0, 0, p["occ"])
            r = R.place_with_reprice(self.broker, pick, p["qty"], side="SELL")
            if r:
                emit(p["symbol"], "closed", f"{p['entry_premium']} -> {r['fill']} = "
                                            f"${(r['fill']-p['entry_premium'])*p['qty']*100:+.0f}")
            else:
                emit(p["symbol"], "warn", "exit did not fill; retrying"); keep.append(p)
        self.positions = keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["scan", "live"], default="scan")
    a = ap.parse_args()
    if a.mode == "live":
        from singleton import single_instance
        single_instance("miyagi")
    m = Miyagi(live=a.mode == "live")
    print(f"Miyagi | symbols={symbols()} | target={env('MIYAGI_TARGET','T2')} stop={env('MIYAGI_STOP','flip')}", flush=True)
    if a.mode == "scan":
        m.scan_setups()
        for k, v in m.setups.items():
            print(f"  {k:5} {v.get('status')}" + (f" | pre-market {v['premarket']} | trigger {v['trigger']:.2f}" if v.get("status") == "setup" else ""))
        return
    open_t, close_t = time(9, 30), time(16, 0)
    if date.today().weekday() >= 5:
        print(f"{date.today()} is a weekend, not a trading day. Exiting."); return
    while True:
        now = datetime.now()
        try:
            if now.time() < open_t:
                if not m.setups:
                    m.scan_setups()
                m.save("pre-market"); _time.sleep(20); continue
            if now.time() >= close_t:
                if m.positions:
                    m.manage(now)
                m.save("closed"); print("session closed", flush=True); return
            if not m.armed_done and now.time() >= time(9, 31):
                m.scan_setups()               # pre-market is complete now
                m.arm()
            m.watch_triggers(now)
            m.manage(now)
            print(f"[{now:%H:%M:%S}] armed={list(m.armed)} taken={sorted(m.taken)} "
                  f"positions={[p['symbol']+' '+p['side'] for p in m.positions]}", flush=True)
            m.save("live")
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)[-1]
            print(f"[{now:%H:%M:%S}] ERROR {type(e).__name__}: {e} @ {os.path.basename(tb.filename)}:{tb.lineno}", flush=True)
        _time.sleep(15)


if __name__ == "__main__":
    main()
