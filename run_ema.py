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
from math import erf, exp, log, sqrt
from datetime import date, datetime, time

import pandas as pd
from dotenv import load_dotenv

logging.getLogger("webull").setLevel(logging.CRITICAL)
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

from feed import YFinanceFeed                      # noqa: E402
from strategy_ema import EMAConfig, HTF_RULE, indicators, signal_at, exit_check  # noqa: E402

# STATE_DIR is overridable so tests can never write to a live runner's files
# (a deploy-gate test once overwrote production ema_state.json).
STATE_DIR = os.getenv("STATE_DIR", "state")
POS_FILE = os.path.join(STATE_DIR, "ema_positions.json")
SNAP_FILE = os.path.join(STATE_DIR, "ema_state.json")
CLOSED_FILE = os.path.join(STATE_DIR, "ema_closed.json")
MIYAGI_FILE = os.path.join(STATE_DIR, "miyagi_state.json")
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


def _ncdf(x): return 0.5 * (1 + erf(x / sqrt(2)))


def bs_delta(S, K, T, vol, call, r=0.04):
    if T <= 0 or vol <= 0 or K <= 0:
        return 1.0 if call else -1.0
    d1 = (log(S / K) + (r + vol * vol / 2) * T) / (vol * sqrt(T))
    return _ncdf(d1) if call else -_ncdf(-d1)


def strike_for_delta(S, T, vol, call, target):
    """Strike whose delta is closest to target. 0.65D keeps ~all of the move a
    0.50D captures while losing ~7 points less when wrong (measured on 113
    historical signals); cheap OTM strikes were far worse."""
    best, bd = S, 9.0
    step = max(S * 0.0025, 0.5)
    k = S * 0.80
    while k <= S * 1.20:
        d = abs(bs_delta(S, k, T, vol, call))
        if abs(d - target) < bd:
            bd, best = abs(d - target), k
        k += step
    return best


_DEPLOYED = {"t": 0.0, "v": None}


def _invalidate_capital():
    _DEPLOYED["t"] = 0.0


class DeferredEntry(Exception):
    """Entry can't be decided right now (e.g. positions unreadable); retry soon."""


def account_deployed(broker, max_age: float = 20.0):
    """Premium tied up in ALL open option positions, or None if unreadable.

    Cached briefly: save() used to fetch positions four times per call, every
    5s before the open -- the source of every rate-limit rejection (92/92 were
    /positions/list). Returns None rather than infinity on failure: infinity
    made the budget -inf, so a single rate-limited read silently skipped the
    day's only entry check."""
    now = _time.time()
    if _DEPLOYED["v"] is not None and now - _DEPLOYED["t"] < max_age:
        return _DEPLOYED["v"]
    try:
        v = sum(float(p.get("cost") or 0) for p in (broker.positions() or [])
                if p.get("instrument_type") == "OPTION")
    except Exception:
        return None
    _DEPLOYED.update(t=now, v=v)
    return v


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
        if res.ambiguous:
            # Transport failure: the order may still have reached Webull. Resending
            # would risk a duplicate position, so verify by client_order_id and
            # never re-send blindly.
            _time.sleep(5)
            exists = broker.order_exists(coid)
            if exists:
                emit(pick.underlying, "warn", "send failed but the order IS at Webull; monitoring it")
            else:
                emit(pick.underlying, "ambiguous",
                     f"{side} send failed and the order is "
                     + ("NOT at Webull" if exists is False else "unverifiable")
                     + " - standing down, no auto-resend. Reconcile by hand.")
                return None
        elif not res.ok:
            emit(pick.underlying, "blocked", f"order rejected: {str(res.detail)[:110]}")
            return None
        emit(pick.underlying, "placed", f"{side} {pick.contract_symbol} x{contracts} @ {limit}")
        deadline = _time.time() + timeout
        st = broker.order_state(coid)
        while _time.time() < deadline and st["filled"] < st["total"] and st["status"] == "SUBMITTED":
            _time.sleep(5); st = broker.order_state(coid)
        if st["total"] and st["filled"] >= st["total"]:
            d = broker._run(lambda: broker.tc.order_v2.get_order_detail(broker.account_id, coid))
            fp = float((d.get("orders") or [{}])[0].get("filled_price") or limit)
            emit(pick.underlying, "filled", f"{side} filled x{st['filled']:.0f} @ {fp}")
            _invalidate_capital()
            return {"coid": coid, "fill": fp, "qty": int(st["filled"])}
        _wait_cancelled(broker, coid)
        st = broker.order_state(coid)
        if st["filled"] > 0:
            d = broker._run(lambda: broker.tc.order_v2.get_order_detail(broker.account_id, coid))
            fp = float((d.get("orders") or [{}])[0].get("filled_price") or limit)
            emit(pick.underlying, "filled", f"partial {st['filled']:.0f}/{st['total']:.0f} @ {fp}")
            _invalidate_capital()
            return {"coid": coid, "fill": fp, "qty": int(st["filled"])}
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
        self._bars_cache = {}
        self._ctx = {}
        self._orphans = set()
        self.broker = None
        if live:
            from broker import WebullOptionsBroker
            self.broker = WebullOptionsBroker()

    # ---------- data ----------
    def bars(self, sym, fresh=False):
        """History for signals. Cached between entry checks: refetching 2 years for
        every symbol every minute is ~11k requests/day, which gets data-center IPs
        blocked by Yahoo. Live prices come from Webull; history only needs to be
        fresh for the entry check itself."""
        period = "2y" if self.tf == "1d" else "60d"
        ttl = float(os.getenv("EMA_BARS_CACHE_SEC", "900"))
        hit = self._bars_cache.get(sym)
        if hit and not fresh and _time.time() - hit[0] < ttl:
            return hit[1]
        try:
            df = self.feed.bars(sym, self.tf, period)
        except Exception:
            _time.sleep(3)                      # transient Yahoo failures; retry once
            df = self.feed.bars(sym, self.tf, period)
        self._bars_cache[sym] = (_time.time(), df)
        return df

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
            reason = p.get("force_exit") or exit_check(p["side"], px, p["stop"], p["target"])
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

    def reconcile(self):
        """Compare what we THINK we hold with what Webull says we hold.

        Webull has no bracket orders, so a position the runner doesn't know about
        has no stop protecting it, and one it wrongly thinks it holds gets sold
        again. Conservative by design: a failed read changes nothing, and a
        position is only dropped after it has been missing on 3 consecutive
        checks and is more than 5 minutes old (fresh fills can lag)."""
        try:
            rows = self.broker.positions()
        except Exception:
            return
        if not isinstance(rows, list):
            return
        key = lambda sym, k, right, exp: (sym, round(float(k or 0), 2), right, exp)
        held = set()
        for q in rows:
            if q.get("instrument_type") != "OPTION":
                continue
            leg = (q.get("legs") or [{}])[0]
            held.add(key(q.get("symbol"), leg.get("option_exercise_price"),
                          leg.get("option_type"), leg.get("option_expire_date")))
        keep = []
        for p in self.positions:
            k = key(p["symbol"], p["strike"], p["side"], p["expiry"])
            if k in held:
                p.pop("missing", None); keep.append(p); continue
            p["missing"] = p.get("missing", 0) + 1
            age = (datetime.now() - datetime.fromisoformat(p["opened_at"])).total_seconds()
            if p["missing"] >= 3 and age > 300:
                emit(p["symbol"], "warn", f"{p['occ']} is no longer at Webull (closed outside the "
                                          f"runner?) - no longer managing it")
            else:
                keep.append(p)
        self.positions = keep
        mine = {key(p["symbol"], p["strike"], p["side"], p["expiry"]) for p in self.positions}
        others = {key(q["symbol"], q["strike"], q["side"], q["expiry"])
                  for q in (load_json(MIYAGI_FILE, {}).get("positions") or [])}
        for k in sorted(held - mine - others, key=str):
            if k not in self._orphans:
                self._orphans.add(k)
                emit(k[0], "warn", f"UNMANAGED position at Webull: {k[0]} {k[1]} {k[2]} exp {k[3]} "
                                   f"- no strategy owns it, so NO STOP is protecting it")

    def log_closed(self, p):
        hist = load_json(CLOSED_FILE, [])
        hist.append(p); save_json(CLOSED_FILE, hist)

    # ---------- entries ----------
    def evaluate(self, place=True):
        held = {p["symbol"] for p in self.positions}
        maxpos = envi("EMA_MAX_POSITIONS", "3")
        for sym in symbols():
            try:
                b = self.bars(sym, fresh=place)     # always fresh for a real entry check
                if len(b) < 250:
                    self.snap[sym] = {"status": "no-data"}; continue
                ind = indicators(b, self.cfg, self.htf)
                row = ind.iloc[-1]
                # Live quote only when it matters (a real entry check, or a held
                # position); the every-minute dashboard refresh uses the bar close.
                # Quoting all 8 symbols every minute tripped Webull's rate limit.
                px = (self.price(sym, b)[0] if (place or sym in held)
                      else float(b.Close.iloc[-1]))
                self.snap[sym] = {
                    "status": "watching", "price": px, "close": float(row.close),
                    "e1": float(row.e1), "e2": float(row.e2), "e3": float(row.e3),
                    "adx": float(row.adx), "stack_bull": bool(row.e1 > row.e2 > row.e3),
                    "stack_bear": bool(row.e1 < row.e2 < row.e3),
                    "htf_bull": bool(row.htf_bull), "htf_bear": bool(row.htf_bear),
                    "held": sym in held, "bar": str(ind.index[-1]),
                }
                tr = pd.concat([b.High - b.Low, (b.High - b.Close.shift()).abs(),
                                (b.Low - b.Close.shift()).abs()], axis=1).max(axis=1)
                atr14 = float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1])
                mov = abs(float(b.Close.iloc[-1]) - float(b.Close.iloc[-11]))
                path = float(b.Close.diff().abs().iloc[-10:].sum())
                self._ctx[sym] = {"atr": atr14, "atr_pct": atr14 / float(b.Close.iloc[-1]) * 100,
                                  "er": (mov / path) if path else None}
                sig = signal_at(sym, b, self.cfg, self.htf, -1)
                if not sig:
                    continue
                self.snap[sym]["signal"] = sig.side
                if not place:
                    # The daily bar is still forming: an intraday crossover can
                    # vanish by the close. Act on signals only at the entry check.
                    continue
                for p in self.positions:
                    if (p["symbol"] == sym and p["side"] != sig.side and self.cfg.opp_exit
                            and not p.get("force_exit")):
                        p["force_exit"] = f"opposite {sig.side} signal"
                        emit(sym, "exit", f"opposite {sig.side} signal at the close; closing {p['side']}")
                if sym in held or len(self.positions) >= maxpos:
                    continue
                emit(sym, "signal", f"{sig.side} {sig.reason}")
                self.enter(sig)
            except Exception as e:
                tb = traceback.extract_tb(e.__traceback__)[-1]
                where = f"{os.path.basename(tb.filename)}:{tb.lineno}"
                self.snap[sym] = {"status": "error", "error": f"{str(e)[:140]} @ {where}"}
                emit(sym, "error", f"{type(e).__name__}: {str(e)[:100]} @ {where}")

    def capital(self):
        """Trade as if the account holds EMA_ACCOUNT_SIZE, not Webull's paper $1M.

        Deployed capital is counted ACCOUNT-WIDE from Webull positions, so every
        strategy running against this account shares the same limit.
        """
        size = envf("EMA_ACCOUNT_SIZE", "0")
        deployed = account_deployed(self.broker) if self.broker else 0.0
        if deployed is None:
            return size, None, None
        return size, deployed, (size - deployed if size else float("inf"))

    def enter(self, sig):
        right = sig.side
        ctx = self._ctx.get(sig.symbol, {})
        pick = self.feed.pick_option_dte(sig.symbol, right, envi("EMA_TARGET_DTE", "35"),
                                         env("EMA_MONEYNESS", "ATM"), spot=sig.entry)

        # Earnings blackout: a print inside the holding window can crush the
        # option even when the stock moves our way.
        if env("EMA_EARNINGS_BLACKOUT", "1") == "1":
            ed = self.feed.next_earnings(sig.symbol)
            exp_d = datetime.strptime(pick.expiry, "%Y-%m-%d").date()
            if ed and date.today() <= ed <= exp_d:
                emit(sig.symbol, "skipped", f"earnings {ed} falls before expiry {pick.expiry}")
                return

        q = self.broker.option_quote(pick.contract_symbol) or {}
        ask, iv = q.get("ask") or pick.ask, float(q.get("iv") or 0)
        delta = abs(float(q.get("delta") or 0.5))

        # Budget first: a deeper (higher-delta) contract costs more, and at a small
        # account size the best strike is often unaffordable. Gather candidates
        # from the target-delta strike out to ATM and take the highest-delta one
        # that FITS, instead of refusing to trade.
        size, deployed, available = self.capital()
        if deployed is None:
            raise DeferredEntry("broker positions unreadable; can't size safely")
        budget = min(available if size else float("inf"),
                     size * envf("EMA_MAX_POSITION_PCT", "100") / 100 if size else float("inf"),
                     envf("WEBULL_MAX_ORDER_VALUE", "0") or float("inf"))
        target = envf("EMA_TARGET_DELTA", "0.65")
        cands = {pick.strike: (pick.contract_symbol, ask, delta, iv)}
        if iv > 0:
            T = max(dte(pick.expiry), 1) / 365.0
            want = strike_for_delta(sig.entry, T, iv, right == "CALL", target)
            near = (self.feed.candidate_contracts(sig.symbol, right, pick.expiry, want, 3)
                    + self.feed.candidate_contracts(sig.symbol, right, pick.expiry, sig.entry, 3))
            for strike, occ in near:
                if strike in cands:
                    continue
                cq = self.broker.option_quote(occ)
                if cq and cq.get("ask"):
                    cands[strike] = (occ, cq["ask"], abs(float(cq.get("delta") or 0)),
                                     float(cq.get("iv") or iv))
        fits = {k: v for k, v in cands.items() if v[1] * 100 <= budget}
        if not fits:
            cheapest = min(cands.values(), key=lambda v: v[1])
            emit(sig.symbol, "skipped",
                 f"cheapest contract ${cheapest[1]*100:.0f} exceeds the ${budget:.0f} available "
                 f"(account ${size:.0f}, deployed ${deployed:.0f})")
            return
        # closest to target among those that fit == best quality we can afford
        best_k = min(fits, key=lambda k: abs(fits[k][2] - target))
        occ, ask, delta, iv = fits[best_k]
        from feed import OptionPick
        pick = OptionPick(sig.symbol, right, best_k, pick.expiry, 0, ask, 0, occ)
        if abs(delta - target) > 0.08:
            emit(sig.symbol, "note", f"budget forced delta {delta:.2f} (wanted {target:.2f})")

        n, loss_per = size_contracts(delta, sig.risk, ask)
        n = min(n, int(budget // max(0.01, ask * 100)))
        if n < 1:
            emit(sig.symbol, "skipped",
                 f"1 contract of {pick.contract_symbol} costs ${ask*100:.0f} and risks "
                 f"${loss_per:.0f} at the stop — over the caps "
                 f"(account ${size:.0f}, available ${available:.0f}, "
                 f"order cap ${envf('WEBULL_MAX_ORDER_VALUE','0'):.0f})")
            return

        # Premium edge: expected travel vs what the option charges. Logged, NOT
        # gated on -- it tested as noise with proxy IV (p=0.10). Revisit with
        # real IV after enough live trades.
        edge = None
        if iv > 0 and ctx.get("atr"):
            implied = sig.entry * iv * sqrt(max(dte(pick.expiry), 1) / 365.0)
            travel = ctx["atr"] * 5 * max(ctx.get("er") or 0.05, 0.05)
            edge = travel / implied if implied else None
        emit(sig.symbol, "sizing",
             f"{pick.contract_symbol} {dte(pick.expiry)}DTE delta {delta:.2f} "
             f"(target {target:.2f}) IV {iv*100:.0f}% ask {ask} -> x{n} (~${loss_per*n:.0f} at stop)"
             + (f" | edge {edge:.2f} ER {ctx.get('er') or 0:.2f}" if edge else ""))

        r = place_with_reprice(self.broker, pick, n, side="BUY")
        if not r:
            return
        self.positions.append({
            "symbol": sig.symbol, "side": sig.side, "occ": pick.contract_symbol,
            "strike": pick.strike, "expiry": pick.expiry, "qty": r["qty"],
            "entry_premium": r["fill"], "entry_px": sig.entry, "stop": sig.stop,
            "target": sig.target, "risk": sig.risk, "risk_pct": sig.risk_pct, "delta": delta,
            "iv": iv, "edge": edge, "er": ctx.get("er"), "atr_pct": ctx.get("atr_pct"),
            "opened_at": datetime.now().isoformat(timespec="seconds"), "coid": r["coid"],
        })

    # ---------- state ----------
    def save(self, phase):
        cap = self.capital()                   # ONE broker read per save (was four)
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
                       "account_size": cap[0],
                       "max_position_pct": envf("EMA_MAX_POSITION_PCT", "100")},
            "capital": {"size": cap[0], "deployed": cap[1], "available": cap[2]},
            "events": EVENTS})


def main():
    ap = argparse.ArgumentParser(description="EMA swing options runner")
    ap.add_argument("--mode", choices=["scan", "once", "live"], default="scan")
    a = ap.parse_args()
    if a.mode == "live":
        from singleton import single_instance
        single_instance("ema")
    r = Runner(live=a.mode != "scan")
    print(f"EMA runner | tf={r.tf} htf={r.htf} | symbols={symbols()} | "
          f"rr={r.cfg.rr} adx>={r.cfg.adx_min} | dte~{envi('EMA_TARGET_DTE','35')}")
    if a.mode in ("scan", "once"):
        if r.broker: r.manage()
        r.evaluate(place=a.mode == "once")
        r.save("once")
        errs = {k: v.get("error") for k, v in r.snap.items() if v.get("status") == "error"}
        ok = sum(1 for v in r.snap.values() if v.get("status") == "watching")
        print(f"evaluated {ok}/{len(symbols())} symbols" + (f" | ERRORS: {errs}" if errs else ""))
        if errs:
            raise SystemExit(1)
        print(json.dumps({"positions": r.positions,
                          "signals": {k: v.get("signal") for k, v in r.snap.items() if v.get("signal")}},
                         indent=1, default=str))
        return

    entry_t = parse_hm(env("EMA_ENTRY_WINDOW", "15:45"), time(15, 45))
    open_t, close_t = time(9, 30), time(16, 0)
    last_entry_day = None
    if date.today().weekday() >= 5:
        print(f"{date.today()} is a weekend, not a trading day. Exiting."); return
    print(f"Live. Entry check daily at {entry_t:%H:%M} ET; exits monitored every minute.", flush=True)
    r.reconcile()
    last_reconcile = _time.time()
    while True:
        now = datetime.now(); clock = now.time()
        if clock < open_t:
            secs = (datetime.combine(now.date(), open_t) - now).total_seconds()
            print(f"[{clock:%H:%M:%S}] pre-market — {secs/60:.0f}m to open", end="\r", flush=True)
            (r.evaluate(place=False) if not r.snap else None)
            # Wake AT the open. The old min(60, max(5, mins*6)) polled every 5s in
            # the final minute, and each save hit Webull: 18 rejections at 09:29.
            r.save("pre-market"); _time.sleep(max(1.0, min(60.0, secs))); continue
        if clock >= close_t:
            print(f"\\n[{clock:%H:%M:%S}] session closed.")
            r.save("closed"); return
        if _time.time() - last_reconcile > 600:
            r.reconcile(); last_reconcile = _time.time()
        r.manage()                                  # stops/targets every minute
        # A fresh broker quote proves the market is actually open: on a holiday the
        # timer still fires, but no quote is current.
        market_live = False
        for x in symbols()[:4]:                     # a rate-limited reply is not "closed"
            if (r.broker.stock_quote(x) or {}).get("price"):
                market_live = True
                break
        if clock >= entry_t and last_entry_day != now.date() and not market_live:
            print(f"[{clock:%H:%M:%S}] entry check skipped: no live quotes (market closed today?)", flush=True)
        due = market_live and ((r.tf != "1d") or (clock >= entry_t and last_entry_day != now.date()))
        if due:
            r.evaluate(place=True)
            ok = sum(1 for v in r.snap.values() if v.get("status") == "watching")
            sigs = [f"{k} {v['signal']}" for k, v in r.snap.items() if v.get("signal")]
            errs = sorted(k for k, v in r.snap.items() if v.get("status") == "error")
            emit("*", "check", f"entry check: {ok}/{len(symbols())} symbols evaluated, "
                               f"signals: {', '.join(sigs) or 'none'}"
                               + (f" | ERRORS on {', '.join(errs)} - retrying next minute" if errs else ""))
            # Only mark the day done when every symbol was actually evaluated. A
            # transient failure (rate limit, data hiccup) used to forfeit the
            # day's single entry check.
            if r.tf == "1d" and not errs:
                last_entry_day = now.date()
        else:
            r.evaluate(place=False)                 # refresh dashboard only
        rejects = getattr(r.broker, "quote_rejects", {})
        if rejects:        # a silently-idle engine must not look like a healthy one
            print(f"[{clock:%H:%M:%S}] quotes refused (stale/no timestamp): {rejects}", flush=True)
            r.broker.quote_rejects = {}
        held = ", ".join(f"{p['symbol']} {p['side']}" for p in r.positions) or "flat"
        print(f"[{clock:%H:%M:%S}] {held} | {len(r.positions)}/{envi('EMA_MAX_POSITIONS','3')} positions", flush=True)
        r.save("entries" if due else "monitor")
        _time.sleep(60)


if __name__ == "__main__":
    main()
