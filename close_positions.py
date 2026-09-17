#!/usr/bin/env python3
"""Sell-to-close open option positions at a marketable limit.

Used both for manual closes and as the basis for automated exits.
Prices off Webull's live bid, then re-prices down if it doesn't fill
(the paper simulator fills at its own reference price, not the live quote).

    ./.venv/bin/python close_positions.py            # close everything
    ./.venv/bin/python close_positions.py SPY IWM    # only these underlyings
    ./.venv/bin/python close_positions.py --wait-for-open
"""
import logging
import os
import sys
import time
from datetime import datetime, time as dtime

logging.getLogger("webull").setLevel(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from broker import WebullOptionsBroker  # noqa: E402

CUSHION = float(os.getenv("ORB_LIMIT_CUSHION_PCT", "10")) / 100
TIMEOUT = float(os.getenv("ORB_FILL_TIMEOUT_SEC", "45"))
MAX_REPRICES = int(os.getenv("ORB_MAX_REPRICES", "2"))


def occ(symbol, expiry, right, strike):
    y, m, d = expiry.split("-")
    return f"{symbol}{y[2:]}{m}{d}{'C' if right == 'CALL' else 'P'}{int(round(float(strike)*1000)):08d}"


def wait_for_open():
    while True:
        now = datetime.now()
        if dtime(9, 30) <= now.time() < dtime(16, 0):
            print(f"[{now:%H:%M:%S}] market open"); return True
        if now.time() >= dtime(16, 0):
            print(f"[{now:%H:%M:%S}] market closed for the day; not placing."); return False
        secs = (datetime.combine(now.date(), dtime(9, 30)) - now).total_seconds()
        print(f"[{now:%H:%M:%S}] waiting for open — {secs/60:.1f} min", flush=True)
        time.sleep(min(60, max(2, secs)))


SELL_CUSHION = [0.10, 0.30, 0.50]      # escalate: get out, don't haggle


def wait_cancelled(b, coid, timeout=30):
    """Cancel and wait until the order truly clears the book.

    The order's own status flips before it leaves open_orders(), and placing
    while anything is still working is rejected as a position reversal
    (OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION). Confirmed live 2026-09-16.
    """
    b.cancel(coid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = b.order_state(coid)
        if st["filled"] > 0:
            return st
        if st["status"] != "SUBMITTED" and not (b.open_orders() or []):
            return st
        time.sleep(2)
    return b.order_state(coid)


def cancel_working_orders(b, occ_symbol=None):
    """Clear any working order that would block a close."""
    for c in b.open_orders() or []:
        o = (c.get("orders") or [{}])[0]
        leg = (o.get("legs") or [{}])[0]
        if occ_symbol and leg.get("symbol") and leg["symbol"] not in occ_symbol:
            continue
        wait_cancelled(b, c.get("client_order_id"))


def close_one(b, p):
    leg = (p.get("legs") or [{}])[0]
    sym, qty = p["symbol"], int(float(p["quantity"]))
    right, strike, expiry = leg["option_type"], leg["option_exercise_price"], leg["option_expire_date"]
    sym_occ = occ(sym, expiry, right, strike)
    cost = float(p.get("cost_price") or 0)
    for attempt in range(MAX_REPRICES + 1):
        cancel_working_orders(b, sym_occ)
        q = b.option_quote(sym_occ) or {}
        bid = q.get("bid") or 0
        if not bid:
            print(f"  {sym_occ}: no live bid; skipping"); return False
        # Sell into the simulator's own mark, which can sit well below the live bid.
        mark = q.get("last") or 0
        ref = min([v for v in (bid, mark) if v] or [bid])
        limit = round(max(0.01, ref * (1 - SELL_CUSHION[min(attempt, len(SELL_CUSHION)-1)])), 2)
        try:
            coid, order = b.build_option_order(sym, right, strike, expiry, qty, limit, side="SELL")
        except Exception as e:
            print(f"  {sym_occ}: blocked: {e}"); return False
        res = b.place(order)
        print(f"  SELL {sym_occ} x{qty} @ {limit} (bid {bid}) attempt {attempt+1} ok={res.ok}"
              + ("" if res.ok else f" {res.detail}"), flush=True)
        if not res.ok:
            return False
        deadline = time.time() + TIMEOUT
        st = b.order_state(coid)
        while time.time() < deadline and st["filled"] < st["total"] and st["status"] == "SUBMITTED":
            time.sleep(5); st = b.order_state(coid)
        if st["filled"] >= st["total"] and st["total"]:
            d = b._run(lambda: b.tc.order_v2.get_order_detail(b.account_id, coid))
            fp = (d.get("orders") or [{}])[0].get("filled_price")
            pl = (float(fp) - cost) * qty * 100 if fp else None
            print(f"  CLOSED {sym_occ} x{qty} @ {fp} (cost {cost}) "
                  + (f"P&L ${pl:+.2f}" if pl is not None else ""), flush=True)
            return True
        wait_cancelled(b, coid)
        st = b.order_state(coid)
        if st["filled"] > 0:
            print(f"  PARTIAL {st['filled']:.0f}/{st['total']:.0f}; remainder cancelled"); return True
        print(f"  no fill in {TIMEOUT:.0f}s; re-pricing lower", flush=True)
    print(f"  {sym_occ}: NOT CLOSED after {MAX_REPRICES+1} attempts — needs manual action")
    return False


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--wait-for-open" in sys.argv and not wait_for_open():
        return
    b = WebullOptionsBroker()
    rows = [p for p in (b.positions() or []) if p.get("instrument_type") == "OPTION"]
    if args:
        rows = [p for p in rows if p["symbol"].upper() in {a.upper() for a in args}]
    if not rows:
        print("No matching open option positions."); return
    print(f"Closing {len(rows)} position(s) on {b.endpoint} (paper={b.is_paper}):", flush=True)
    for p in rows:
        close_one(b, p)
    print("Remaining positions:", [(x['symbol'], x.get('quantity')) for x in (b.positions() or [])])


if __name__ == "__main__":
    main()
