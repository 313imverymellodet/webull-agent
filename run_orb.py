#!/usr/bin/env python3
"""ORB options strategy runner: feed -> strategy -> Webull broker.

Modes
-----
  --scan     evaluate the latest bars once and print any signal (no orders)
  --once     evaluate once; on a signal, PREVIEW the option order (paper may place)
  --live-loop  poll every minute during RTH and act on signals
  --backtest run the rules over the most recent completed session and report

Safety
------
  * Paper endpoint (sandbox): signals auto-place.
  * Live endpoint: place is refused unless --arm-live is passed AND you confirm.
  * All orders pass the broker guardrails (allow-list, max contracts, max notional).
"""
from __future__ import annotations

import argparse
import json
import os
import time as _time
from datetime import date, datetime, time

def _parse_time(s: str, default: time) -> time:
    try:
        hh, mm = s.split(":"); return time(int(hh), int(mm))
    except Exception:
        return default

import pandas as pd
from dotenv import load_dotenv

from strategy import ORBConfig, Signal, evaluate, opening_range
from feed import YFinanceFeed

load_dotenv()
ET = "America/New_York"


def cfg_from_env() -> ORBConfig:
    return ORBConfig(
        or_minutes=int(os.getenv("ORB_OR_MINUTES", "15")),
        volume_mult=float(os.getenv("ORB_VOLUME_MULT", "1.5")),
        buffer_pct=float(os.getenv("ORB_BUFFER_PCT", "0.05")),
        allow_long=os.getenv("ORB_ALLOW_LONG", "1") == "1",
        allow_short=os.getenv("ORB_ALLOW_SHORT", "1") == "1",
        entry_cutoff=_parse_time(os.getenv("ORB_ENTRY_CUTOFF", "12:00"), time(12, 0)),
    )


def symbols():
    return [s.strip().upper() for s in os.getenv("ORB_SYMBOLS", "SPY,QQQ").split(",") if s.strip()]


def size_contracts(price: float) -> int:
    """Contracts to buy, sized to risk budget then CLAMPED to the broker caps.

    Clamping (rather than letting the guardrail reject) matters: a cheap option
    can size past the contract cap, and we want a smaller order, not no order.
    Returns 0 when even one contract breaches the notional cap.
    """
    risk = float(os.getenv("ORB_RISK_PER_TRADE", "500"))
    max_qty = float(os.getenv("WEBULL_MAX_ORDER_QTY", "0") or 0)
    max_val = float(os.getenv("WEBULL_MAX_ORDER_VALUE", "0") or 0)
    per_contract = price * 100
    if per_contract <= 0:
        return 0
    n = max(1, int(risk // per_contract))
    if max_qty:
        n = min(n, int(max_qty))
    if max_val:
        n = min(n, int(max_val // per_contract))
    return max(0, n)


def describe_signal(sig: Signal):
    print(f"  SIGNAL {sig.symbol} {sig.side} @ {sig.trigger_price:.2f}  ({sig.bar_time})")
    print(f"    OR high/low: {sig.or_high:.2f}/{sig.or_low:.2f}")
    print(f"    {sig.reason}")


STATE_DIR = "state"
_EVENTS = None   # list the live loop persists; None in scan/preview modes


def emit(sym, kind, text):
    if _EVENTS is not None:
        _EVENTS.append({"t": datetime.now().strftime("%H:%M:%S"), "sym": sym,
                        "kind": kind, "text": text})
        del _EVENTS[:-200]


class _Outcome:
    """ok=True stops further entries on this side (filled, or deliberately gave up);
    ok=False means the order couldn't be placed and may be retried."""
    def __init__(self, ok, filled=False):
        self.ok, self.filled = ok, filled


def _wait_for_fill(broker, coid, timeout):
    deadline = _time.time() + timeout
    st = broker.order_state(coid)
    while _time.time() < deadline:
        if st["total"] and st["filled"] >= st["total"]:
            break
        if st["status"] in ("CANCELLED", "REJECTED", "FAILED", "EXPIRED"):
            break
        _time.sleep(5)
        st = broker.order_state(coid)
    return st


def act_on_signal(sig: Signal, feed, broker, arm_live: bool, place: bool):
    right = sig.side
    moneyness = os.getenv("ORB_MONEYNESS", "ATM")
    pick = feed.pick_option(sig.symbol, right, moneyness, spot=sig.trigger_price)

    # Price off Webull's real-time quote; yfinance option quotes can lag, which is
    # why the 9/14 order (limit at a stale mid) never filled.
    q = broker.option_quote(pick.contract_symbol)
    if q and q["ask"] > 0:
        bid, ask, src = q["bid"], q["ask"], "Webull live"
    else:
        bid, ask, src = pick.bid, (pick.ask or pick.price), "yfinance (Webull quote unavailable)"
    ref_ask = ask
    # Marketable limit with a cushion above the ask. Live, a buy limit above the ask
    # fills at the best offer, so the cushion is only a ceiling. Webull's paper
    # simulator fills options at its own reference price (tested 9/15: live ask 3.41,
    # filled 3.19), so a limit AT the live ask can miss while a cushioned one fills.
    cushion = float(os.getenv("ORB_LIMIT_CUSHION_PCT", "10")) / 100
    limit = round(ask * (1 + cushion), 2)
    contracts = size_contracts(limit)
    if contracts < 1:
        print(f"    SKIPPED: 1 contract of {pick.contract_symbol} (${limit*100:.0f}) exceeds "
              f"the ${os.getenv('WEBULL_MAX_ORDER_VALUE')} notional cap")
        emit(sig.symbol, "skipped", f"{right} skipped: 1 contract ${limit*100:.0f} over cap")
        return None
    print(f"    -> {pick.contract_symbol} ({src}) bid {bid}/ask {ask} "
          f"| limit {limit} (ask +{cushion*100:.0f}%) x{contracts} = max ${limit*contracts*100:.0f}")
    try:
        coid, order = broker.build_option_order(
            pick.underlying, right, pick.strike, pick.expiry, contracts, limit)
    except Exception as e:
        print(f"    GUARDRAIL blocked: {e}")
        emit(sig.symbol, "blocked", f"{right} blocked: {e}")
        return None
    print(f"    preview: {broker.preview(order)}")
    if not place:
        print("    (scan/preview only — not placing)")
        return None
    if not broker.is_paper and not arm_live:
        print("    LIVE endpoint: not placing (pass --arm-live and confirm to enable).")
        return None

    timeout = float(os.getenv("ORB_FILL_TIMEOUT_SEC", "45"))
    max_reprices = int(os.getenv("ORB_MAX_REPRICES", "2"))
    max_chase = float(os.getenv("ORB_MAX_CHASE_PCT", "10"))

    for attempt in range(max_reprices + 1):
        if attempt:
            coid, order = broker.build_option_order(
                pick.underlying, right, pick.strike, pick.expiry, contracts, limit)
        res = broker.place(order, arm_live=arm_live)
        print(f"    PLACED #{attempt+1} limit {limit} x{contracts} ok={res.ok} coid={coid}"
              + ("" if res.ok else f" detail={res.detail}"))
        emit(sig.symbol, "placed", f"{pick.contract_symbol} BUY {contracts} @ limit {limit} (attempt {attempt+1})"
             if res.ok else f"{right} order rejected: {str(res.detail)[:120]}")
        if not res.ok:
            return _Outcome(False)
        st = _wait_for_fill(broker, coid, timeout)
        if st["total"] and st["filled"] >= st["total"]:
            fp = (broker._run(lambda: broker.tc.order_v2.get_order_detail(broker.account_id, coid))
                  .get("orders") or [{}])[0].get("filled_price")
            print(f"    FILLED {st['filled']:.0f}/{st['total']:.0f} @ {fp} (limit {limit}, live ask {ask})")
            emit(sig.symbol, "filled", f"FILLED {pick.contract_symbol} x{st['filled']:.0f} @ {fp}")
            return _Outcome(True, filled=True)

        broker.cancel(coid)          # unfilled (or partial): pull it before re-pricing
        _time.sleep(2)
        st = broker.order_state(coid)
        if st["filled"] > 0:
            print(f"    PARTIAL FILL {st['filled']:.0f}/{st['total']:.0f}; remainder cancelled")
            emit(sig.symbol, "filled", f"PARTIAL {pick.contract_symbol} {st['filled']:.0f}/{st['total']:.0f}")
            return _Outcome(True, filled=True)
        print(f"    no fill in {timeout:.0f}s -> cancelled ({st['status']})")
        emit(sig.symbol, "cancelled", f"no fill in {timeout:.0f}s at {limit}; cancelled")
        if attempt == max_reprices:
            break

        q = broker.option_quote(pick.contract_symbol)
        new_ask = q["ask"] if q and q["ask"] > 0 else None
        if not new_ask:
            print("    no live quote to re-price from"); break
        if new_ask > ref_ask * (1 + max_chase / 100):
            print(f"    ask {new_ask} is >{max_chase:.0f}% above signal ask {ref_ask}; not chasing")
            break
        limit = round(new_ask * (1 + cushion), 2)
        contracts = min(contracts, size_contracts(limit))
        if contracts < 1:
            break

    print(f"    NOT FILLED — standing down on {sig.symbol} {right} for today")
    emit(sig.symbol, "not_filled", f"{right} not filled; standing down for today")
    return _Outcome(True, filled=False)


def load_bars(feed, sym, days):
    return feed.intraday_bars(sym, lookback_days=days)


def cmd_scan(a, feed, broker, cfg):
    today = date.today()
    for sym in symbols():
        bars = feed.session_bars(sym, today)
        print(f"\n{sym}: {len(bars)} bars for {today}")
        if bars.empty:
            print("  no bars for today yet (pre-open, or not a trading day)"); continue
        orr = opening_range(bars, cfg, session_date=today)
        if orr:
            print(f"  OR high/low {orr[0]:.2f}/{orr[1]:.2f}  vol-baseline {orr[2]:.0f}")
        else:
            print(f"  opening range still building ({cfg.or_minutes}m window)")
        sig = evaluate(sym, bars, cfg, session_date=today)
        if sig:
            describe_signal(sig)
            if a.mode in ("once", "preview"):
                act_on_signal(sig, feed, broker, a.arm_live, place=(a.mode == "once"))
        else:
            print("  no signal")


def cmd_backtest(a, feed, cfg):
    for sym in symbols():
        bars = feed.intraday_bars(sym, lookback_days=a.days)
        if bars.empty:
            print(f"{sym}: no data"); continue
        # group by session date
        for day, day_bars in bars.groupby(bars.index.date):
            orr = opening_range(day_bars, cfg)
            sig = evaluate(sym, day_bars, cfg)
            line = f"{sym} {day}: "
            if orr:
                line += f"OR {orr[0]:.2f}/{orr[1]:.2f} "
            line += ("-> " + f"{sig.side}@{sig.trigger_price:.2f} {sig.bar_time.time()}" if sig else "no signal")
            print(line)


def _load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_state(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, default=str)
    os.replace(tmp, path)      # atomic: the dashboard never reads a half-written file


def cmd_live_loop(a, feed, broker, cfg):
    global _EVENTS
    today = date.today()
    eod = _parse_time(os.getenv("ORB_EOD_FLAT", "15:55"), time(15, 55))
    open_t, close_t = time(9, 30), time(16, 0)
    syms = symbols()
    print(f"Live loop {today} | paper={broker.is_paper} | symbols={syms}")
    print(f"OR window {open_t.strftime('%H:%M')}-{cfg.or_minutes}m | entry cutoff "
          f"{cfg.entry_cutoff.strftime('%H:%M')} | EOD {eod.strftime('%H:%M')} | Ctrl-C to stop")

    # Persisted per-day state: survives restarts so a taken side is never re-bought.
    os.makedirs(STATE_DIR, exist_ok=True)
    spath = os.path.join(STATE_DIR, f"{today}.json")
    prev = _load_state(spath) or {}
    taken = {s: set(prev.get("taken", {}).get(s, [])) for s in syms}
    attempts = {tuple(k.split(":")): v for k, v in prev.get("attempts", {}).items()}
    _EVENTS = prev.get("events", [])
    if any(taken.values()):
        print("Restored taken sides: " + ", ".join(f"{s} {sorted(v)}" for s, v in taken.items() if v))
    MAX_ATTEMPTS = 3
    warned_eod = False
    snap = {s: {"status": "starting"} for s in syms}

    def save(phase):
        _save_state(spath, {
            "date": str(today), "updated_at": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(), "paper": broker.is_paper, "phase": phase, "symbols_order": syms,
            "config": {"or_minutes": cfg.or_minutes, "volume_mult": cfg.volume_mult,
                       "buffer_pct": cfg.buffer_pct,
                       "entry_cutoff": cfg.entry_cutoff.strftime("%H:%M"), "eod": eod.strftime("%H:%M"),
                       "stop_pct": float(os.getenv("ORB_STOP_PCT", "30")),
                       "take_profit_pct": float(os.getenv("ORB_TAKE_PROFIT_PCT", "50"))},
            "symbols": snap, "taken": {s: sorted(v) for s, v in taken.items()},
            "attempts": {f"{k[0]}:{k[1]}": v for k, v in attempts.items()},
            "events": _EVENTS})

    # Only act on breakouts that print after startup, so a mid-session start
    # doesn't buy into a move that already happened.
    not_before = pd.Timestamp.now(tz=ET).floor("min") - pd.Timedelta(minutes=1)
    if datetime.now().time() > open_t:
        print(f"Started mid-session: ignoring breakouts before {not_before:%H:%M}")
    emit("*", "start", f"loop started, {len(syms)} symbols"
         + (f", ignoring breakouts before {not_before:%H:%M}" if datetime.now().time() > open_t else ""))

    while True:
        now = datetime.now()
        clock = now.time()

        if now.date() != today:
            print("date rolled over; stopping."); return
        if clock < open_t:
            mins = (datetime.combine(today, open_t) - now).total_seconds() / 60
            print(f"[{clock:%H:%M:%S}] pre-market — {mins:.0f} min to open", end="\r")
            save("pre-market")
            _time.sleep(min(60, max(5, mins * 6))); continue
        if clock >= close_t:
            print(f"\n[{clock:%H:%M:%S}] session closed. Done for today.")
            emit("*", "stop", "session closed"); save("closed")
            return
        if clock >= eod and not warned_eod:
            warned_eod = True
            print(f"\n[{clock:%H:%M:%S}] EOD {eod:%H:%M} reached — automated exits are NOT "
                  f"implemented yet; close any open positions manually.")
            emit("*", "eod", "EOD reached: close open positions manually")

        for sym in syms:
            try:
                bars = feed.session_bars(sym, today)       # TODAY only — never stale sessions
                if bars.empty:
                    snap[sym] = {"status": "no-data"}
                    print(f"[{clock:%H:%M:%S}] {sym}: no bars for {today} yet"); continue
                last = float(bars["Close"].iloc[-1])
                orr = opening_range(bars, cfg, session_date=today)
                if orr is None:
                    snap[sym] = {"status": "building", "price": last, "bars": len(bars)}
                    print(f"[{clock:%H:%M:%S}] {sym}: building opening range "
                          f"({len(bars)} bars, need {cfg.or_minutes}m)"); continue
                done = bars[bars["Volume"] > 0]
                vol_ratio = float(done["Volume"].iloc[-1]) / orr[2] if len(done) and orr[2] else None
                state = "above" if last > orr[0] else "below" if last < orr[1] else "inside"
                snap[sym] = {"status": "watching", "price": last, "or_high": orr[0], "or_low": orr[1],
                             "vol_baseline": orr[2], "vol_ratio": vol_ratio, "state": state,
                             "prev_close": float(bars["Close"].iloc[0])}
                sig = evaluate(sym, bars, cfg, taken_sides=taken[sym], session_date=today,
                               not_before=not_before)
                if not sig:
                    print(f"[{clock:%H:%M:%S}] {sym}: {last:.2f} {state} OR "
                          f"{orr[1]:.2f}-{orr[0]:.2f} | no signal")
                    continue
                if sig.bar_time.date() != today:
                    print(f"  IGNORED stale signal dated {sig.bar_time.date()}"); continue
                key = (sym, sig.side)
                if attempts.get(key, 0) >= MAX_ATTEMPTS:
                    continue
                describe_signal(sig)
                emit(sym, "signal", f"{sig.side} breakout @ {sig.trigger_price:.2f} "
                                    f"(vol {sig.bar_volume/sig.vol_baseline:.1f}x)")
                save("open")
                res = act_on_signal(sig, feed, broker, a.arm_live, place=True)
                if res and res.ok:
                    taken[sym].add(sig.side)
                    print(f"  {sym} {sig.side} done for today "
                          f"({'filled' if res.filled else 'not filled'}); no further {sig.side} entries.")
                else:
                    attempts[key] = attempts.get(key, 0) + 1
                    left = MAX_ATTEMPTS - attempts[key]
                    print(f"  {sym} {sig.side} not placed ({attempts[key]}/{MAX_ATTEMPTS}); "
                          + (f"{left} retries left." if left else "giving up on this side today."))
                save("open")
            except Exception as e:                        # one bad fetch must not kill the day
                snap[sym] = {**snap.get(sym, {}), "status": "error", "error": str(e)[:160]}
                print(f"[{clock:%H:%M:%S}] {sym}: ERROR {type(e).__name__}: {str(e)[:160]} (continuing)")
        save("entries" if clock < cfg.entry_cutoff else "manage")
        _time.sleep(60)


def main():
    p = argparse.ArgumentParser(description="ORB options strategy runner")
    p.add_argument("--mode", choices=["scan", "preview", "once", "backtest", "live-loop"],
                   default="scan",
                   help="scan=signal only, preview=preview order, once=preview+place(paper), "
                        "backtest=historical, live-loop=poll each minute")
    p.add_argument("--days", type=int, default=1, help="lookback days (backtest/scan)")
    p.add_argument("--arm-live", action="store_true", help="permit placing on a LIVE (non-sandbox) endpoint")
    a = p.parse_args()

    cfg = cfg_from_env()
    feed = YFinanceFeed()

    if a.mode == "backtest":
        cmd_backtest(a, feed, cfg)
        return

    from broker import WebullOptionsBroker
    broker = WebullOptionsBroker()
    print(f"Endpoint: {broker.endpoint}  paper={broker.is_paper}  account={broker.account_id}")

    if a.mode == "live-loop":
        cmd_live_loop(a, feed, broker, cfg)
    else:
        cmd_scan(a, feed, broker, cfg)


if __name__ == "__main__":
    main()
