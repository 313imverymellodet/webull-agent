#!/usr/bin/env python3
"""Local live dashboard for the ORB loop.

Serves live.html and /api/state, which merges the loop's state file
(state/YYYY-MM-DD.json) with live Webull positions and today's orders.
Binds to 127.0.0.1 only: it exposes account data, so it's not for the network.

    ./.venv/bin/python dashboard_server.py    # then open http://localhost:8787
"""
import json
import logging
import os
import time
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.getLogger("webull").setLevel(logging.CRITICAL)
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
from broker import WebullOptionsBroker  # noqa: E402  (after chdir so .env loads)

PORT = int(os.getenv("DASHBOARD_PORT", "8787"))
_broker = None
_cache = {}


def broker():
    global _broker
    if _broker is None:
        _broker = WebullOptionsBroker()
    return _broker


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = {"ok": True, "data": fn()}
    except Exception as e:
        val = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    _cache[key] = (time.time(), val)
    return val


def positions():
    rows = broker().positions() or []
    out = []
    for p in rows if isinstance(rows, list) else []:
        leg = (p.get("legs") or [{}])[0]
        f = lambda v: float(v) if v not in (None, "") else None
        out.append({
            "symbol": p.get("symbol"), "type": p.get("instrument_type"),
            "right": leg.get("option_type"), "strike": leg.get("option_exercise_price"),
            "expiry": leg.get("option_expire_date"), "qty": f(p.get("quantity")),
            "cost_price": f(p.get("cost_price")), "last_price": f(p.get("last_price")),
            "cost": f(p.get("cost")), "market_value": f(p.get("market_value")),
            "upl": f(p.get("unrealized_profit_loss")), "upl_rate": f(p.get("unrealized_profit_loss_rate")),
        })
    return out


def orders_today():
    b = broker()
    hist = b._run(lambda: b.tc.order_v2.get_order_history(b.account_id))
    today = date.today()
    out = []
    for combo in hist if isinstance(hist, list) else []:
        for o in combo.get("orders") or []:
            ts = o.get("place_time")
            if not ts:
                continue
            placed = datetime.fromtimestamp(int(ts) / 1000)
            if placed.date() != today:
                continue
            leg = (o.get("legs") or [{}])[0]
            out.append({
                "time": placed.strftime("%H:%M:%S"), "symbol": o.get("symbol"), "side": o.get("side"),
                "right": leg.get("option_type"), "strike": leg.get("strike_price"),
                "expiry": leg.get("option_expire_date"), "qty": o.get("total_quantity"),
                "filled": o.get("filled_quantity"), "limit": o.get("limit_price"),
                "fill_price": o.get("filled_price"), "status": o.get("status"),
                "intent": o.get("position_intent"),
            })
    return sorted(out, key=lambda r: r["time"], reverse=True)


def account():
    bal = broker().balance()
    cur = (bal.get("account_currency_assets") or [{}])[0]
    return {"net_liq": bal.get("total_net_liquidation_value"), "cash": bal.get("total_cash_balance"),
            "day_pl": bal.get("total_day_profit_loss"), "upl": bal.get("total_unrealized_profit_loss"),
            "buying_power": cur.get("buying_power")}


def loop_state():
    """Prefer the EMA runner's state; fall back to the ORB day file."""
    paths = [os.path.join(HERE, "state", "ema_state.json"),
             os.path.join(HERE, "state", f"{date.today()}.json")]
    path = next((p for p in paths if os.path.exists(p)), None)
    if path is None:
        return None
    with open(path) as f:
        st = json.load(f)
    st["age_sec"] = time.time() - os.path.getmtime(path)
    return st


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "live.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path.startswith("/api/state"):
            payload = {
                "server_time": datetime.now().strftime("%H:%M:%S"),
                "loop": loop_state(),
                "positions": cached("pos", 4, positions),
                "orders": cached("orders", 8, orders_today),
                "account": cached("acct", 10, account),
            }
            return self._send(200, json.dumps(payload, default=str), "application/json")
        self._send(404, "not found", "text/plain")


if __name__ == "__main__":
    print(f"ORB live dashboard on http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
