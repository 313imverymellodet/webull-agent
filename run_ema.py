#!/usr/bin/env python3
"""EMA Crossover Swing runner — signal (yfinance bars) -> option order (Webull).

Replaces the ORB runner. Key differences, all deliberate:
  * Stops/targets are levels on the UNDERLYING, checked against Webull's
    real-time quote — never a percentage of option value.
  * 30-45 DTE contracts, because swing holds run days and short-dated
    contracts bleed theta.
  * Size by RISK (delta x stop distance x 100), so a 0.3% stop and a 3% stop
    don't get the same dollar exposure.
  * Positions persist to disk, so a restart never loses a stop.

Modes:  --scan (no orders)   --once (one pass)   --live (poll during RTH)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time as _time
import traceback
from datetime import date, datetime, time

from dotenv import load_dotenv

logging.getLogger("webull").setLevel(logging.CRITICAL)
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

from feed import YFinanceFeed                      # noqa: E402
from strategy_ema import EMAConfig, HTF_RULE, indicators, signal_at, exit_check  # noqa: E402

STATE_DIR, POS_FILE, SNAP_FILE = "state", "state/ema_positions.json", "state/ema_state.json"
EVENTS: list = []


def env(k, d): return os.getenv(k, d)
def envf(k, d): return float(os.getenv(k, d))
def envi(k, d): return int(float(os.getenv(k, d)))


def cfg_from_env() -> EMAConfig:
    return EMAConfig(rr=envf("EMA_RR", "2.0"), adx_min=envf("EMA_ADX_MIN", "20"),
                     stop_ema=env("EMA_STOP_EMA", "ema2"),
                     use_stack=env("EMA_USE_STACK", "1") == "1",
                     use_htf=env("EMA_USE_HTF", "1") == "1",
                     use_adx=env("EMA_USE_ADX", "1") == "1",
                     allow_long=env("EMA_ALLOW_LONG", "1") == "1",
                     allow_short=env("EMA_ALLOW_SHORT", "1") == "1",
                     min_risk_pct=envf("EMA_MIN_RISK_PCT", "0.5"),
                     max_risk_pct=envf("EMA_MAX_RISK_PCT", "0"))


def symbols(): return [s.strip().upper() for s in env("EMA_SYMBOLS", "SPY,QQQ,IWM,AAPL,AMD,INTC,TSLA,IREN").split(",") if s.strip()]
def parse_hm(s, d):
    try:
        h, m = s.split(":"); return time(int(h), int(m))
    except Exception:
        return d


def emit(sym, kind, text):
    EVENTS.append({"t": datetime.now().strftime("%H:%M:%S"), "sym": sym, "kind": kind, "text": text})
    del EVENTS[:-200]
    print(f"  [{kind}] {sym}: {text}", flush=True)


def load_json(p, default):
    try:
        with open(p) as f: return json.load(f)
    except Exception:
        return default


def save_json(p, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, default=str)
    os.replace(tmp, p)


def dte(expiry): return (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days


def size_contracts(delta, risk_price, premium):
    """Contracts so that (stock hitting the stop) ~= the risk budget, clamped to caps."""
    budget = envf("EMA_RISK_PER_TRADE", "500")
    loss_per_contract = max(0.01, abs(delta) * risk_price * 100)
    n = int(budget // loss_per_contract)
    max_qty = envf("WEBULL_MAX_ORDER_QTY", "0")
    max_val = envf("WEBULL_MAX_ORDER_VALUE", "0")
    if max_qty: n = min(n, int(max_qty))
    if max_val: n = min(n, int(max_val // max(0.01, premium * 100)))
    return max(0, n), loss_per_contract


def _wait_cancelled(broker, coid, timeout=30):
    """Cancel and wait until the order truly clears the book.

    The order's own status flips before it leaves open_orders(), and placing
    while anything is still working is rejected as a position reversal
    (OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION). Confirmed live 2026-09-16.
    """
    broker.cancel(coid)
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        st = broker.order_state(coid)
        if st["filled"] > 0:
            return st
        if st["status"] != "SUBMITTED" and not (broker.open_orders() or []):
            return st
        _time.sleep(2)
    return broker.order_state(coid)


def _clear_working(broker, occ_symbol):
    for c in broker.open_orders() or []:
        leg = ((c.get("orders") or [{}])[0].get("legs") or [{}])[0]
        if leg.get("symbol") and leg["symbol"] in occ_symbol:
            _wait_cancelled(broker, c.get("client_order_id"))


def place_with_reprice(broker, pick, contracts, side="BUY"):
    """Marketable limit with cushion; cancel + re-price if it doesn't fill."""
    cushion = envf("ORB_LIMIT_CUSHION_PCT", "10") / 100
    timeout, max_rp = envf("ORB_FILL_TIMEOUT_SEC", "45"), envi("ORB_MAX_REPRICES", "2")
    for attempt in range(max_rp + 1):
        _clear_working(broker, pick.contract_symbol)
        q = broker.option_quote(pick.contract_symbol) or {}
        if side == "BUY":
            ref = q.get("ask")
        else:   # sell into the simulator's mark, which can sit below the live bid
            ref = min([v for v in (q.get("bid"), q.get("last")) if v] or [0])
        if not ref:
            emit(pick.underlying, "blocked", "no live quote"); return None
        sell_cush = [0.10, 0.30, 0.50][min(attempt, 2)]
        limit = round(ref * (1 + cushion) if side == "BUY" else max(0.01, ref * (1 - sell_cush)), 2)
        try:
            coid, order = broker.build_option_order(pick.underlying, pick.right, pick.strike,
                                                    pick.expiry, contracts, limit, side=side)
        except Exception as e:
            emit(pick.underlying, "blocked", f"guardrail: {e}"); return None
        res = broker.place(order)
        if not res.ok:
            emit(pick.underlying, "blocked", f"order rejected: {str(res.detail)[:110]}"); return None
        emit(pick.underlying, "placed", f"{side} {pick.contract_symbol} x{contracts} @ {limit}")
        deadline = _time.time() + timeout
        st = broker.order_state(coid)
        while _time.time() < deadline and st["filled"] < st["total"] and st["status"] == "SUBMITTED":
            _time.sleep(5); st = broker.order_state(coid)
        if st["total"] and st["filled"] >= st["total"]:
            d = broker._run(lambda: broker.tc.order_v2.get_order_detail(broker.account_id, coid))
            fp = float((d.get("orders") or [{}])[0].get("filled_price") or limit)
            emit(pick.underlying, "filled", f"{side} filled x{st['filled']:.0f} @ {fp}")
            return {"coid": coid, "fill": fp, "qty": int(st["filled"])}
        _wait_cancelled(broker, coid)
        st = broker.order_state(coid)
        if st["filled"] > 0:
            emit(pick.underlying, "filled", f"partial {st['filled']:.0f}/{st['total']:.0f}")
            return {"coid": coid, "fill": limit, "qty": int(st["filled"])}
        emit(pick.underlying, "cancelled", f"no fill at {limit}; re-pricing")
    emit(pick.underlying, "not_filled", f"{side} not filled after {max_rp+1} tries")
    return None


class Runner:
    def __init__(self, live=True):
        self.feed = YFinanceFeed()
        self.cfg = cfg_from_env()
        self.tf = env("EMA_TIMEFRAME", "1d")
        self.htf = HTF_RULE.get(self.tf, "W")
        self.positions = load_json(POS_FILE, [])
        self.snap = {}
        self.broker = None
        if live:
            from broker import WebullOptionsBroker
            self.broker = WebullOptionsBroker()

    # ---------- data ----------
    def bars(self, sym):
        period = "2y" if self.tf == "1d" else "60d"
        try:
            return self.feed.bars(sym, self.tf, period)
        except Exception:
            _time.sleep(3)                      # transient Yahoo failures; retry once
            return self.feed.bars(sym, self.tf, period)

    def price(self, sym, bars=None):
        q = self.broker.stock_quote(sym) if self.broker else None
        if q and q.get("price"):
            return q["price"], "live"
        if bars is not None and len(bars):
            return float(bars.Close.iloc[-1]), "bars"
        return None, None

    # ---------- exits ----------
    def manage(self):
        keep = []
        for p in self.positions:
            px, src = self.price(p["symbol"])
            if px is None:
                keep.append(p); continue
            p["last_px"] = px
            reason = exit_check(p["side"], px, p["stop"], p["target"])
            if not reason and dte(p["expiry"]) <= envi("EMA_MIN_DTE_EXIT", "7"):
                reason = "expiry"
            if not reason:
                keep.append(p); continue
            emit(p["symbol"], "exit", f"{reason.upper()} at {px:.2f} "
                                      f"(stop {p['stop']:.2f} / target {p['target']:.2f})")
            from feed import OptionPick
            pick = OptionPick(p["symbol"], p["side"], p["strike"], p["expiry"], 0, 0, 0, p["occ"])
            r = place_with_reprice(self.broker, pick, p["qty"], side="SELL")
            if r:
                pl = (r["fill"] - p["entry_premium"]) * p["qty"] * 100
                emit(p["symbol"], "closed", f"{reason} · {p['entry_premium']} -> {r['fill']} "
                                            f"= ${pl:+.0f}")
                p["closed"] = {"reason": reason, "fill": r["fill"], "pl": pl,
                               "at": datetime.now().isoformat(timespec="seconds")}
                self.log_closed(p)
            else:
                emit(p["symbol"], "warn", "exit order did not fill — still open, retrying next pass")
                keep.append(p)
        self.positions = keep

    def log_closed(self, p):
        hist = load_json("state/ema_closed.json", [])
        hist.append(p); save_json("state/ema_closed.json", hist)

    # ---------- entries ----------
    def evaluate(self, place=True):
        held = {p["symbol"] for p in self.positions}
        maxpos = envi("EMA_MAX_POSITIONS", "3")
        for sym in symbols():
            try:
                b = self.bars(sym)
                if len(b) < 250:
                    self.snap[sym] = {"status": "no-data"}; continue
                ind = indicators(b, self.cfg, self.htf)
                row = ind.iloc[-1]
                px, _ = self.price(sym, b)
                self.snap[sym] = {
                    "status": "watching", "price": px, "close": float(row.close),
                    "e1": float(row.e1), "e2": float(row.e2), "e3": float(row.e3),
                    "adx": float(row.adx), "stack_bull": bool(row.e1 > row.e2 > row.e3),
                    "stack_bear": bool(row.e1 < row.e2 < row.e3),
                    "htf_bull": bool(row.htf_bull), "htf_bear": bool(row.htf_bear),
                    "held": sym in held, "bar": str(ind.index[-1]),
                }
                sig = signal_at(sym, b, self.cfg, self.htf, -1)
                if not sig:
                    continue
                self.snap[sym]["signal"] = sig.side
                # opposite-signal exit
                for p in list(self.positions):
                    if p["symbol"] == sym and p["side"] != sig.side and self.cfg.opp_exit:
                        emit(sym, "exit", "opposite signal — closing")
                        p["stop"] = p["target"] = sig.entry   # force exit_check next pass
                if sym in held or len(self.positions) >= maxpos:
                    continue
                emit(sym, "signal", f"{sig.side} {sig.reason}")
                if not place:
                    continue
                self.enter(sig)
            except Exception as e:
                tb = traceback.extract_tb(e.__traceback__)[-1]
                where = f"{os.path.basename(tb.filename)}:{tb.lineno}"
                self.snap[sym] = {"status": "error", "error": f"{str(e)[:140]} @ {where}"}
                emit(sym, "error", f"{type(e).__name__}: {str(e)[:100]} @ {where}")

    def capital(self):
        """Trade as if the account holds EMA_ACCOUNT_SIZE, not Webull's paper $1M."""
        size = envf("EMA_ACCOUNT_SIZE", "0")
        deployed = sum(p["entry_premium"] * p["qty"] * 100 for p in self.positions)
        return size, deployed, (size - deployed if size else float("inf"))

    def enter(self, sig):
        pick = self.feed.pick_option_dte(sig.symbol, sig.side, envi("EMA_TARGET_DTE", "35"),
                                         env("EMA_MONEYNESS", "ATM"), spot=sig.entry)
        q = self.broker.option_quote(pick.contract_symbol) or {}
        ask = q.get("ask") or pick.ask
        delta = abs(float(q.get("delta") or 0.5))
        if not ask:
            emit(sig.symbol, "blocked", "no option quote"); return
        n, loss_per = size_contracts(delta, sig.risk, ask)
        size, deployed, available = self.capital()
        if size:
            max_pos = size * envf("EMA_MAX_POSITION_PCT", "100") / 100
            n = min(n, int(min(available, max_pos) // max(0.01, ask * 100)))
        if n < 1:
            emit(sig.symbol, "skipped",
                 f"1 contract of {pick.contract_symbol} costs ${ask*100:.0f} and risks "
                 f"${loss_per:.0f} at the stop — over the caps "
                 f"(account ${size:.0f}, available ${available:.0f}, "
                 f"order cap ${envf('WEBULL_MAX_ORDER_VALUE','0'):.0f})")
            return
        emit(sig.symbol, "sizing", f"{pick.contract_symbol} {dte(pick.expiry)}DTE delta {delta:.2f} "
                                   f"ask {ask} -> x{n} (~${loss_per*n:.0f} at stop)")
        r = place_with_reprice(self.broker, pick, n, side="BUY")
        if not r:
            return
        self.positions.append({
            "symbol": sig.symbol, "side": sig.side, "occ": pick.contract_symbol,
            "strike": pick.strike, "expiry": pick.expiry, "qty": r["qty"],
            "entry_premium": r["fill"], "entry_px": sig.entry, "stop": sig.stop,
            "target": sig.target, "risk": sig.risk, "risk_pct": sig.risk_pct, "delta": delta,
            "opened_at": datetime.now().isoformat(timespec="seconds"), "coid": r["coid"],
        })

    # ---------- state ----------
    def save(self, phase):
        save_json(POS_FILE, self.positions)
        save_json(SNAP_FILE, {
            "strategy": "ema", "timeframe": self.tf, "htf": self.htf,
            "updated_at": datetime.now().isoformat(timespec="seconds"), "phase": phase,
            "pid": os.getpid(), "paper": bool(self.broker and self.broker.is_paper),
            "symbols_order": symbols(), "symbols": self.snap, "positions": self.positions,
            "config": {"rr": self.cfg.rr, "adx_min": self.cfg.adx_min, "stop_ema": self.cfg.stop_ema,
                       "target_dte": envi("EMA_TARGET_DTE", "35"),
                       "risk_per_trade": envf("EMA_RISK_PER_TRADE", "500"),
                       "max_positions": envi("EMA_MAX_POSITIONS", "3"),
                       "entry_window": env("EMA_ENTRY_WINDOW", "15:45"),
                       "min_risk_pct": self.cfg.min_risk_pct,
                       "account_size": self.capital()[0],
                       "max_position_pct": envf("EMA_MAX_POSITION_PCT", "100")},
            "capital": {"size": self.capital()[0], "deployed": self.capital()[1],
                        "available": self.capital()[2]},
            "events": EVENTS})


def main():
    ap = argparse.ArgumentParser(description="EMA swing options runner")
    ap.add_argument("--mode", choices=["scan", "once", "live"], default="scan")
    a = ap.parse_args()
    r = Runner(live=a.mode != "scan")
    print(f"EMA runner | tf={r.tf} htf={r.htf} | symbols={symbols()} | "
          f"rr={r.cfg.rr} adx>={r.cfg.adx_min} | dte~{envi('EMA_TARGET_DTE','35')}")
    if a.mode in ("scan", "once"):
        if r.broker: r.manage()
        r.evaluate(place=a.mode == "once")
        r.save("once")
        print(json.dumps({"positions": r.positions,
                          "signals": {k: v.get("signal") for k, v in r.snap.items() if v.get("signal")}},
                         indent=1, default=str))
        return

    entry_t = parse_hm(env("EMA_ENTRY_WINDOW", "15:45"), time(15, 45))
    open_t, close_t = time(9, 30), time(16, 0)
    last_entry_day = None
    print(f"Live. Entry check daily at {entry_t:%H:%M} ET; exits monitored every minute.", flush=True)
    while True:
        now = datetime.now(); clock = now.time()
        if clock < open_t:
            mins = (datetime.combine(now.date(), open_t) - now).total_seconds() / 60
            print(f"[{clock:%H:%M:%S}] pre-market — {mins:.0f}m to open", end="\r", flush=True)
            (r.evaluate(place=False) if not r.snap else None)
            r.save("pre-market"); _time.sleep(min(60, max(5, mins * 6))); continue
        if clock >= close_t:
            print(f"\\n[{clock:%H:%M:%S}] session closed.")
            r.save("closed"); return
        r.manage()                                  # stops/targets every minute
        due = (r.tf != "1d") or (clock >= entry_t and last_entry_day != now.date())
        if due:
            r.evaluate(place=True)
            ok = sum(1 for v in r.snap.values() if v.get("status") == "watching")
            sigs = [f"{k} {v['signal']}" for k, v in r.snap.items() if v.get("signal")]
            emit("*", "check", f"entry check: {ok}/{len(symbols())} symbols evaluated, "
                               f"signals: {', '.join(sigs) or 'none'}")
            if r.tf == "1d": last_entry_day = now.date()
        else:
            r.evaluate(place=False)                 # refresh dashboard only
        held = ", ".join(f"{p['symbol']} {p['side']}" for p in r.positions) or "flat"
        print(f"[{clock:%H:%M:%S}] {held} | {len(r.positions)}/{envi('EMA_MAX_POSITIONS','3')} positions", flush=True)
        r.save("entries" if due else "monitor")
        _time.sleep(60)


if __name__ == "__main__":
    main()
