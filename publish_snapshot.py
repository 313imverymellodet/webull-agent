#!/usr/bin/env python3
"""Push dashboard snapshots from this Mac to the Vercel dashboard.

Webull keys stay here: this builds the same payload the local dashboard serves
(runner state + positions + today's orders + account) and POSTs it to
DASHBOARD_URL/api/snapshot with DASHBOARD_INGEST_TOKEN. Vercel only stores it.

    ./.venv/bin/python publish_snapshot.py            # every 30s in market hours
    ./.venv/bin/python publish_snapshot.py --once
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, time as dtime

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
sys.path.insert(0, HERE)
import dashboard_server as ds  # noqa: E402  (reuses the local dashboard's data code)

URL = os.getenv("DASHBOARD_URL", "").rstrip("/")
TOKEN = os.getenv("DASHBOARD_INGEST_TOKEN", "")


def build():
    loop = ds.loop_state()
    if loop:
        loop.pop("pid", None)
    return {
        "server_time": datetime.now().strftime("%H:%M:%S"),
        "loop": loop,
        "loop_age_sec": loop.get("age_sec") if loop else None,
        "positions": ds.cached("pos", 0, ds.positions),
        "orders": ds.cached("orders", 0, ds.orders_today),
        "account": ds.cached("acct", 0, ds.account),
    }


def push():
    body = json.dumps(build(), default=str)
    r = requests.post(f"{URL}/api/snapshot", data=body, timeout=15,
                      headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return len(body)


def interval():
    now = datetime.now()
    in_session = now.weekday() < 5 and dtime(9, 0) <= now.time() <= dtime(16, 15)
    return 30 if in_session else 900


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if not a.once:
        from singleton import single_instance
        single_instance("publisher")
    if not URL or not TOKEN:
        sys.exit("Set DASHBOARD_URL and DASHBOARD_INGEST_TOKEN in .env")
    while True:
        try:
            n = push()
            print(f"[{datetime.now():%H:%M:%S}] pushed {n/1024:.1f} KB to {URL}", flush=True)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] push failed: {e}", flush=True)
            if a.once:
                sys.exit(1)
        if a.once:
            return
        time.sleep(interval())


if __name__ == "__main__":
    main()
