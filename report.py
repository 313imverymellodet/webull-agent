#!/usr/bin/env python3
"""Forward results from REAL fills.

Reads filled option orders from Webull (the broker's record, not our bookkeeping),
pairs opens with closes, and reports whether each strategy clears the win rate its
own payoff ratio demands. Refuses to draw conclusions below MIN_SAMPLE trades.

    ./.venv/bin/python report.py             # last 90 days
    ./.venv/bin/python report.py --days 30
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

logging.getLogger("webull").setLevel(logging.CRITICAL)
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from broker import WebullOptionsBroker  # noqa: E402

MIN_SAMPLE = 30          # below this, report but draw no conclusion


def filled_option_orders(b, days):
    """Every filled option order in the window, oldest first."""
    start = (date.today() - timedelta(days=days)).isoformat()
    out, seen, cursor = [], set(), None
    for _ in range(20):                       # page until exhausted
        res = b._run(lambda: b.tc.order_v2.get_order_history(
            b.account_id, page_size=50, start_date=start,
            end_date=date.today().isoformat(), last_client_order_id=cursor))
        combos = res if isinstance(res, list) else []
        if not combos:
            break
        for c in combos:
            for o in c.get("orders") or []:
                cid = o.get("client_order_id")
                if cid in seen or o.get("instrument_type") != "OPTION":
                    continue
                seen.add(cid)
                if float(o.get("filled_quantity") or 0) <= 0:
                    continue
                leg = (o.get("legs") or [{}])[0]
                out.append({
                    "coid": cid, "symbol": o.get("symbol"), "side": o.get("side"),
                    "intent": o.get("position_intent"), "qty": float(o["filled_quantity"]),
                    "price": float(o.get("filled_price") or 0),
                    "when": datetime.fromtimestamp(int(o["place_time"]) / 1000),
                    "occ": f"{leg.get('strike_price','?')} {leg.get('option_type','?')}"
                           f" {leg.get('option_expire_date','?')}",
                })
        nxt = combos[-1].get("client_order_id")
        if nxt == cursor:
            break
        cursor = nxt
    return sorted(out, key=lambda r: r["when"])


def attribution():
    """coid -> strategy, from whatever the runners recorded."""
    who = {}
    for path, name in (("state/ema_closed.json", "EMA"), ("state/ema_positions.json", "EMA"),
                       ("state/miyagi_state.json", "Miyagi")):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        rows = data.get("positions", data) if isinstance(data, dict) else data
        for p in rows if isinstance(rows, list) else []:
            if p.get("coid"):
                who[p["coid"]] = name
    return who


def round_trips(orders, who):
    """Pair opens with closes per contract, FIFO."""
    open_lots, trips = defaultdict(list), []
    for o in orders:
        closing = o["side"] == "SELL" or (o["intent"] or "").startswith("SELL")
        if not closing:
            open_lots[o["occ"]].append(o)
            continue
        qty = o["qty"]
        while qty > 0 and open_lots[o["occ"]]:
            lot = open_lots[o["occ"]][0]
            take = min(qty, lot["qty"])
            trips.append({
                "symbol": o["symbol"], "occ": o["occ"], "qty": take,
                "entry": lot["price"], "exit": o["price"],
                "pl": (o["price"] - lot["price"]) * take * 100,
                "pct": (o["price"] / lot["price"] - 1) * 100 if lot["price"] else 0,
                "opened": lot["when"], "closed": o["when"],
                "strategy": who.get(lot["coid"], who.get(o["coid"], "unattributed")),
            })
            lot["qty"] -= take
            qty -= take
            if lot["qty"] <= 0:
                open_lots[o["occ"]].pop(0)
    still_open = [l for lots in open_lots.values() for l in lots if l["qty"] > 0]
    return trips, still_open


def summarise(label, trips):
    n = len(trips)
    if n == 0:
        print(f"\n{label}: no closed trades yet")
        return
    wins = [t for t in trips if t["pl"] > 0]
    losses = [t for t in trips if t["pl"] <= 0]
    aw = sum(t["pct"] for t in wins) / len(wins) if wins else 0
    al = sum(t["pct"] for t in losses) / len(losses) if losses else 0
    pf = (sum(t["pl"] for t in wins) / abs(sum(t["pl"] for t in losses))) if losses and sum(t["pl"] for t in losses) else float("inf")
    need = (abs(al) / (aw + abs(al)) * 100) if (aw + abs(al)) else float("nan")
    got = len(wins) / n * 100
    print(f"\n{label}   ({n} closed trade{'s' if n != 1 else ''})")
    print(f"  net P&L        ${sum(t['pl'] for t in trips):+,.2f}")
    print(f"  win rate       {got:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  avg win/loss   {aw:+.1f}% / {al:+.1f}%   profit factor {pf:.2f}")
    print(f"  break-even     needs {need:.1f}% wins to cover that payoff ratio"
          f"  ->  {'CLEARS' if got >= need else 'DOES NOT CLEAR'}")
    if n < MIN_SAMPLE:
        print(f"  ** {n} trades is too few to conclude anything (need {MIN_SAMPLE}+). "
              f"Treat the numbers above as a log, not a verdict. **")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args()
    b = WebullOptionsBroker()
    orders = filled_option_orders(b, a.days)
    trips, still_open = round_trips(orders, attribution())
    bal = b.balance()
    print("=" * 68)
    print(f"FORWARD RESULTS — real fills, last {a.days} days"
          f"{'  (PAPER: simulator prices, not live fills)' if b.is_paper else ''}")
    print("=" * 68)
    print(f"account net liq ${float(bal.get('total_net_liquidation_value') or 0):,.2f}   "
          f"filled option orders: {len(orders)}   open positions: {len(still_open)}")
    summarise("ALL STRATEGIES", trips)
    by = defaultdict(list)
    for t in trips:
        by[t["strategy"]].append(t)
    if len(by) > 1:                     # per-strategy only adds information when there is a mix
        for name in sorted(by):
            summarise(name, by[name])
    if trips:
        print("\ntrade log:")
        for t in sorted(trips, key=lambda x: x["closed"]):
            print(f"  {t['closed']:%Y-%m-%d} {t['symbol']:5} {t['occ']:>22} x{t['qty']:.0f} "
                  f"{t['entry']:>6.2f} -> {t['exit']:>6.2f}  ${t['pl']:+8.2f} ({t['pct']:+6.1f}%)  {t['strategy']}")
    if still_open:
        print("\nstill open (not counted):")
        for l in still_open:
            print(f"  {l['symbol']:5} {l['occ']:>22} x{l['qty']:.0f} @ {l['price']:.2f}")


if __name__ == "__main__":
    main()
