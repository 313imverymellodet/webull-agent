"""Webull options broker wrapper with safety guardrails.

Paper (sandbox endpoint): orders can be placed automatically.
Live (api.webull.com):     place() refuses unless explicitly armed per call.

Guardrails (from .env), enforced before every order:
  * underlying must be in WEBULL_ALLOWED_SYMBOLS
  * contracts <= WEBULL_MAX_ORDER_QTY
  * premium notional (limit_price * contracts * 100) <= WEBULL_MAX_ORDER_VALUE
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from webull.core.client import ApiClient
from webull.core.exception.exceptions import ClientException, ServerException
from webull.data.data_client import DataClient
from webull.data.common.category import Category
from webull.trade.trade_client import TradeClient

logging.getLogger("webull").setLevel(logging.CRITICAL)
load_dotenv()


class GuardrailError(Exception):
    pass


QUOTE_MAX_AGE_SEC = float(os.getenv("QUOTE_MAX_AGE_SEC", "60"))
# Options on these keys carry delay_minutes=15 (no real-time options entitlement),
# so a 60s ceiling would refuse every option quote and the runner would never
# trade. Allow the known delay plus headroom; a dead feed is still caught.
OPTION_MAX_AGE_SEC = float(os.getenv("QUOTE_MAX_AGE_SEC_OPTION", "1200"))


def quote_age(row: dict) -> Optional[float]:
    """Seconds since the broker timestamped this quote, or None if it has none.

    A poll can return in 250ms while the data behind it is hours old -- that is
    what this catches. The timestamp already reflects the feed's own delay
    (options here are published on a 15-minute delay, in ~15-minute blocks), so
    delay_minutes is reported separately rather than added again."""
    for k in ("quote_time", "last_trade_time"):
        v = row.get(k)
        if v:
            try:
                return time.time() - int(v) / 1000.0
            except (TypeError, ValueError):
                continue
    return None


@dataclass
class OrderResult:
    ok: bool
    detail: dict
    client_order_id: Optional[str] = None
    ambiguous: bool = False      # send failed in a way that may still have reached Webull


class WebullOptionsBroker:
    def __init__(self):
        self.key = os.getenv("WEBULL_APP_KEY")
        self.secret = os.getenv("WEBULL_APP_SECRET")
        self.region = os.getenv("WEBULL_REGION_ID", "us")
        self.endpoint = os.getenv("WEBULL_API_ENDPOINT", "api.webull.com")
        self.account_id = os.getenv("WEBULL_ACCOUNT_ID") or None
        self.allowed = {s.strip().upper() for s in os.getenv("WEBULL_ALLOWED_SYMBOLS", "").split(",") if s.strip()}
        self.max_qty = float(os.getenv("WEBULL_MAX_ORDER_QTY", "0") or 0)
        self.max_value = float(os.getenv("WEBULL_MAX_ORDER_VALUE", "0") or 0)
        self.is_paper = "sandbox" in self.endpoint
        if not self.key or not self.secret:
            raise RuntimeError("WEBULL_APP_KEY / WEBULL_APP_SECRET missing from .env")
        api = ApiClient(self.key, self.secret, self.region)
        api.add_endpoint(self.region, self.endpoint)
        self.tc = TradeClient(api)
        self._api = api
        self._dc = None
        self.quote_rejects = {}          # symbol -> why the last quote was refused
        if not self.account_id:
            self.account_id = self._first_account()

    def _first_account(self) -> str:
        res = self.tc.account_v2.get_account_list()
        data = res.json()
        first = data[0] if isinstance(data, list) else (data.get("data") or [{}])[0]
        return first.get("account_id") or first.get("accountId")

    # ---- reads ----
    def balance(self):
        return self.tc.account_v2.get_account_balance(self.account_id).json()

    def positions(self):
        return self.tc.account_v2.get_account_position(self.account_id).json()

    def open_orders(self):
        return self.tc.order_v2.get_order_open(account_id=self.account_id).json()

    # ---- live quotes (Webull snapshot; real-time even on paper keys) ----
    def option_quote(self, occ_symbol: str, allow_stale: bool = False) -> Optional[dict]:
        """Real-time bid/ask/greeks for one OCC option symbol.

        Fails closed: a quote with no broker timestamp, or one older than
        QUOTE_MAX_AGE_SEC, returns None rather than a price we might trade on."""
        try:
            if self._dc is None:
                self._dc = DataClient(self._api)
            rows = self._dc.option_market_data.get_option_snapshot(
                occ_symbol, Category.US_OPTION.name).json()
            r = rows[0] if isinstance(rows, list) and rows else None
            if not r:
                return None
            age = quote_age(r)
            if not allow_stale and (age is None or age > OPTION_MAX_AGE_SEC):
                self.quote_rejects[occ_symbol] = ("no timestamp" if age is None
                                                  else f"stale {age:.0f}s")
                return None
            f = lambda k: float(r[k]) if r.get(k) not in (None, "") else 0.0
            return {"bid": f("bid"), "ask": f("ask"), "last": f("price"),
                    "delta": r.get("delta"), "gamma": r.get("gamma"),
                    "open_interest": r.get("open_interest"), "iv": r.get("imp_vol"),
                    "age_sec": age, "delay_min": float(r.get("delay_minutes") or 0)}
        except Exception:
            return None

    def stock_quote(self, symbol: str, allow_stale: bool = False) -> Optional[dict]:
        """Real-time underlying quote used for stop/target checks. Fails closed on
        a missing or stale broker timestamp."""
        try:
            if self._dc is None:
                self._dc = DataClient(self._api)
            rows = self._dc.market_data.get_snapshot(symbol, Category.US_STOCK.name).json()
            r = rows[0] if isinstance(rows, list) and rows else None
            if not r:
                return None
            age = quote_age(r)
            if not allow_stale and (age is None or age > QUOTE_MAX_AGE_SEC):
                self.quote_rejects[symbol] = ("no timestamp" if age is None
                                              else f"stale {age:.0f}s")
                return None
            f = lambda k: float(r[k]) if r.get(k) not in (None, "") else 0.0
            return {"price": f("price"), "bid": f("bid"), "ask": f("ask"),
                    "volume": f("volume"), "age_sec": age}
        except Exception:
            return None

    def order_state(self, client_order_id: str) -> dict:
        """{'status', 'filled', 'total'} for an option order."""
        d = self._run(lambda: self.tc.order_v2.get_order_detail(self.account_id, client_order_id))
        o = (d.get("orders") or [{}])[0] if isinstance(d, dict) else {}
        return {"status": o.get("status", "UNKNOWN"),
                "filled": float(o.get("filled_quantity") or 0),
                "total": float(o.get("total_quantity") or 0)}

    # ---- order construction + guardrails ----
    def _check(self, underlying: str, contracts: int, limit_price: float):
        u = underlying.upper()
        if self.allowed and u not in self.allowed:
            raise GuardrailError(f"underlying {u} not in allow-list {sorted(self.allowed)}")
        if self.max_qty and contracts > self.max_qty:
            raise GuardrailError(f"contracts {contracts} exceeds WEBULL_MAX_ORDER_QTY={self.max_qty}")
        notional = limit_price * contracts * 100
        if self.max_value and notional > self.max_value:
            raise GuardrailError(f"premium notional ${notional:.2f} exceeds WEBULL_MAX_ORDER_VALUE={self.max_value}")

    def build_option_order(self, underlying, right, strike, expiry, contracts,
                           limit_price, side="BUY", tif="DAY", position_intent=None):
        self._check(underlying, contracts, limit_price)
        coid = uuid.uuid4().hex
        # Webull rejects a bare SELL on an existing long as a position reversal
        # (OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION); the intent must be explicit.
        intent = position_intent or ("BUY_TO_OPEN" if side == "BUY" else "SELL_TO_CLOSE")
        order = [{
            "client_order_id": coid,
            "position_intent": intent,
            "combo_type": "NORMAL",
            "order_type": "LIMIT",
            "quantity": str(int(contracts)),
            "limit_price": str(limit_price),
            "option_strategy": "SINGLE",
            "side": side,
            "time_in_force": tif,
            "entrust_type": "QTY",
            "legs": [{
                "side": side,
                "quantity": str(int(contracts)),
                "symbol": underlying.upper(),
                "strike_price": str(strike),
                "option_expire_date": expiry,
                "instrument_type": "OPTION",
                "option_type": right,
                "market": "US",
            }],
        }]
        return coid, order

    def preview(self, order) -> dict:
        return self._run(lambda: self.tc.order_v2.preview_option(self.account_id, order))

    def place(self, order, arm_live: bool = False) -> OrderResult:
        if not self.is_paper and not arm_live:
            raise GuardrailError("refusing LIVE options order without arm_live=True (human confirmation)")
        detail = self._run(lambda: self.tc.order_v2.place_option(self.account_id, order))
        coid = order[0].get("client_order_id")
        ok = not detail.get("_error")
        return OrderResult(ok=ok, detail=detail, client_order_id=coid,
                           ambiguous=bool(detail.get("_ambiguous")))

    def order_exists(self, client_order_id: str) -> Optional[bool]:
        """True/False if Webull has this order, None if we cannot tell.

        Our client_order_id is generated locally, so after an ambiguous send this
        answers definitively whether the order reached the broker."""
        d = self._run(lambda: self.tc.order_v2.get_order_detail(self.account_id, client_order_id))
        if d.get("_error"):
            # Webull answers "Order not present" for an id it never saw: that is a
            # definitive no, not an unknown.
            msg = str(d.get("msg", "")).lower()
            if not d.get("_ambiguous") and ("not present" in msg or "not found" in msg):
                return False
            return None
        return bool(d.get("orders"))

    def cancel(self, client_order_id: str) -> dict:
        return self._run(lambda: self.tc.order_v2.cancel_option(self.account_id, client_order_id))

    @staticmethod
    def _run(fn) -> dict:
        try:
            res = fn()
            return res.json() if hasattr(res, "json") else res
        except ServerException as e:
            # Webull answered and refused: definitive, nothing was submitted.
            return {"_error": True, "_ambiguous": False,
                    "code": getattr(e, "error_code", ""), "msg": str(e)}
        except ClientException as e:
            # Transport failure (timeout, connection): the order MAY have landed.
            # Never auto-resend on this -- a replayed order is a duplicate position.
            return {"_error": True, "_ambiguous": True, "code": "CLIENT", "msg": str(e)}
