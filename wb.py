#!/usr/bin/env python3
"""Webull OpenAPI CLI — research + trading with built-in safety guardrails.

Read commands (accounts / balance / positions / orders / quote) run freely.
Write commands (preview / place / cancel) are guarded:
  * `place` requires an explicit --confirm flag (never places silently)
  * symbol must be in WEBULL_ALLOWED_SYMBOLS
  * order quantity <= WEBULL_MAX_ORDER_QTY
  * notional (price * qty) <= WEBULL_MAX_ORDER_VALUE

Everything is driven by .env. Point WEBULL_API_ENDPOINT at the sandbox/paper
host to keep it on simulated money.
"""
import argparse
import json
import logging
import os
import sys
import uuid

logging.getLogger("webull").setLevel(logging.CRITICAL)

from dotenv import load_dotenv
from webull.core.client import ApiClient
from webull.core.exception.exceptions import ClientException, ServerException
from webull.trade.trade_client import TradeClient

load_dotenv()

APP_KEY = os.getenv("WEBULL_APP_KEY")
APP_SECRET = os.getenv("WEBULL_APP_SECRET")
REGION = os.getenv("WEBULL_REGION_ID", "us")
ENDPOINT = os.getenv("WEBULL_API_ENDPOINT", "api.webull.com")
ACCOUNT_ID = os.getenv("WEBULL_ACCOUNT_ID") or None

ALLOWED = {s.strip().upper() for s in os.getenv("WEBULL_ALLOWED_SYMBOLS", "").split(",") if s.strip()}
MAX_VALUE = float(os.getenv("WEBULL_MAX_ORDER_VALUE", "0") or 0)
MAX_QTY = float(os.getenv("WEBULL_MAX_ORDER_QTY", "0") or 0)

IS_PAPER = "sandbox" in ENDPOINT


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def client():
    if not APP_KEY or not APP_SECRET:
        die("WEBULL_APP_KEY / WEBULL_APP_SECRET missing from .env")
    api = ApiClient(APP_KEY, APP_SECRET, REGION)
    api.add_endpoint(REGION, ENDPOINT)
    return TradeClient(api)


def show(label, res):
    ok = getattr(res, "status_code", None) == 200
    body = res.json() if hasattr(res, "json") else res
    print(f"[{'OK' if ok else 'FAIL'}] {label} (endpoint={ENDPOINT}, paper={IS_PAPER})")
    print(json.dumps(body, indent=2))
    return ok, body


def resolve_account(tc):
    if ACCOUNT_ID:
        return ACCOUNT_ID
    res = tc.account_v2.get_account_list()
    if res.status_code != 200:
        die(f"could not list accounts: {res.status_code} {res.json()}")
    data = res.json()
    accounts = data.get("data") or data.get("accounts") or data if isinstance(data, list) else data.get("data", [])
    try:
        first = (accounts[0] if isinstance(accounts, list) else data[0])
        return first.get("account_id") or first.get("accountId")
    except Exception:
        die(f"no account_id found in: {json.dumps(data)}")


# ---------- read commands ----------
def cmd_accounts(a):
    show("account list", client().account_v2.get_account_list())


def cmd_balance(a):
    tc = client()
    show("balance", tc.account_v2.get_account_balance(resolve_account(tc)))


def cmd_positions(a):
    tc = client()
    show("positions", tc.account_v2.get_account_position(resolve_account(tc)))


def cmd_orders(a):
    tc = client()
    show("open orders", tc.order_v2.get_order_open(account_id=resolve_account(tc)))


# ---------- guarded write commands ----------
def build_order(a):
    symbol = a.symbol.upper()
    qty = float(a.qty)
    price = float(a.price)

    if ALLOWED and symbol not in ALLOWED:
        die(f"symbol {symbol} not in allow-list {sorted(ALLOWED)} (edit WEBULL_ALLOWED_SYMBOLS)")
    if MAX_QTY and qty > MAX_QTY:
        die(f"qty {qty} exceeds WEBULL_MAX_ORDER_QTY={MAX_QTY}")
    if MAX_VALUE and price * qty > MAX_VALUE:
        die(f"notional {price*qty:.2f} exceeds WEBULL_MAX_ORDER_VALUE={MAX_VALUE}")

    coid = uuid.uuid4().hex
    order = [{
        "combo_type": "NORMAL",
        "client_order_id": coid,
        "symbol": symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": a.order_type,
        "limit_price": str(price),
        "quantity": str(int(qty) if qty.is_integer() else qty),
        "support_trading_session": "N",
        "side": a.side,
        "time_in_force": a.tif,
        "entrust_type": "QTY",
    }]
    return coid, order


def cmd_preview(a):
    tc = client()
    _, order = build_order(a)
    show("preview order", tc.order_v2.preview_order(resolve_account(tc), order))


def cmd_place(a):
    tc = client()
    coid, order = build_order(a)
    print(f"About to PLACE ({'PAPER' if IS_PAPER else 'LIVE $$$'}):")
    print(json.dumps(order, indent=2))
    if not IS_PAPER and not a.i_understand_live:
        die("refusing to place a LIVE order without --i-understand-live")
    if not a.confirm:
        die("dry run — re-run with --confirm to actually place this order")
    ok, body = show("place order", tc.order_v2.place_order(resolve_account(tc), order))
    if ok:
        print(f"client_order_id = {coid}")


def cmd_cancel(a):
    tc = client()
    show("cancel order", tc.order_v2.cancel_order(resolve_account(tc), a.client_order_id))


def main():
    p = argparse.ArgumentParser(description="Webull OpenAPI CLI (guarded)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("accounts").set_defaults(fn=cmd_accounts)
    sub.add_parser("balance").set_defaults(fn=cmd_balance)
    sub.add_parser("positions").set_defaults(fn=cmd_positions)
    sub.add_parser("orders").set_defaults(fn=cmd_orders)

    def order_args(sp):
        sp.add_argument("symbol")
        sp.add_argument("side", choices=["BUY", "SELL"])
        sp.add_argument("qty")
        sp.add_argument("price")
        sp.add_argument("--order-type", default="LIMIT")
        sp.add_argument("--tif", default="DAY", choices=["DAY", "GTC", "IOC"])

    order_args(sub.add_parser("preview"))
    sub.choices["preview"].set_defaults(fn=cmd_preview)

    pp = sub.add_parser("place")
    order_args(pp)
    pp.add_argument("--confirm", action="store_true", help="actually place (otherwise dry run)")
    pp.add_argument("--i-understand-live", action="store_true", help="required for non-sandbox endpoints")
    pp.set_defaults(fn=cmd_place)

    cp = sub.add_parser("cancel")
    cp.add_argument("client_order_id")
    cp.set_defaults(fn=cmd_cancel)

    a = p.parse_args()
    try:
        a.fn(a)
    except ServerException as e:
        die(f"{getattr(e, 'error_code', '')}: {getattr(e, 'message', str(e))}")
    except ClientException as e:
        die(str(e))


if __name__ == "__main__":
    main()
