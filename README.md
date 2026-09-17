# Webull Agent

Guard-railed automated options trading on the Webull OpenAPI: signals from
market data, orders and exits through Webull. Currently pointed at **PaperTrade
(sandbox)** — simulated money only.

## Status
- ✅ Auth working with PaperTrade keys against `api.sandbox.webull.com`
- ✅ Read: accounts, balance, positions, open orders
- ✅ Order path verified (place/preview/cancel) — orders only fill during US market hours (9:30–16:00 ET)
- ✅ Safety guardrails enforced before any order

## Setup
```bash
python3 -m venv .venv
./.venv/bin/pip install webull-openapi-python-sdk python-dotenv
```
Credentials and guardrails live in `.env` (git-ignored). Never commit it.

## Usage
```bash
./.venv/bin/python wb.py accounts          # list accounts
./.venv/bin/python wb.py balance           # cash / buying power
./.venv/bin/python wb.py positions         # open positions
./.venv/bin/python wb.py orders            # open orders

# Preview never sends an order:
./.venv/bin/python wb.py preview AAPL BUY 1 180

# Place is a DRY RUN unless --confirm is passed:
./.venv/bin/python wb.py place AAPL BUY 1 180            # dry run
./.venv/bin/python wb.py place AAPL BUY 1 180 --confirm  # actually places

./.venv/bin/python wb.py cancel <client_order_id>
```

## Guardrails (in `.env`)
Every `place`/`preview` is blocked unless it passes:
- `WEBULL_ALLOWED_SYMBOLS` — symbol must be in this list
- `WEBULL_MAX_ORDER_QTY` — max shares per order
- `WEBULL_MAX_ORDER_VALUE` — max notional (price × qty) per order
- `place` also requires `--confirm`, and `--i-understand-live` on any non-sandbox endpoint

## Going live (real money)
1. Generate **live** keys in the Webull developer portal (don't paste them into chat).
2. Put them in `.env` and set `WEBULL_API_ENDPOINT=api.webull.com`.
3. Re-run `wb.py accounts` to get your live `account_id`; set `WEBULL_ACCOUNT_ID`.
4. Live `place` requires `--confirm --i-understand-live`.

## EMA Crossover Swing (current strategy)

Port of the TradingView Pine strategy: 9/21 EMA cross gated by EMA stack, a
higher-timeframe stack, and ADX. Stop = the stop EMA's value at entry; target = 2R.

- [strategy_ema.py](strategy_ema.py) — pure rules (EMA, Wilder ADX, HTF stack). Unit-tested,
  including a no-lookahead check: signals on a growing window match the full series.
- [run_ema.py](run_ema.py) — runner. `--scan` (no orders) / `--once` / `--live`.
- Exits are automatic: stops and targets are **levels on the underlying**, checked
  each minute against Webull's real-time quote.

```bash
./.venv/bin/python run_ema.py --mode scan     # signals + sizing, no orders
./.venv/bin/python run_ema.py --mode live     # monitors exits, enters near the close
./.venv/bin/python dashboard_server.py        # http://localhost:8787
./.venv/bin/python close_positions.py         # close everything now
```

### Why it differs from ORB (all deliberate)
| Choice | Reason |
|---|---|
| Stop/target on the **stock**, not option % | A −30% option stop equalled a 0.25% SPY move — inside the noise |
| **30–45 DTE** contracts | Median hold is days; weeklies bled theta (both ORB trades) |
| Size by **risk** (delta × stop distance × 100) | Stop distance ranges 0.03%–3.9%; fixed sizing gave wildly uneven risk |
| **Min 0.5% stop distance** | Tighter stops are noise. Filtering lifted PF 1.32 → 1.53 (1H) |
| Positions persisted to disk | A restart must never lose a stop |

### Measured expectations (shares, before option costs)
| Setup | Trades | Win | PF | Avg/trade |
|---|---|---|---|---|
| 1H chart / 4H confirm | 276 | 32.6% | 1.22 | +0.24% |
| Daily / weekly confirm | 79 | 38% | 1.12 | +0.24% |
| As 30–45 DTE options (Black-Scholes) | 276 | 32% | 1.19–1.25 | +1.9% to +3.0% |

Caveats: 730 days is one market regime; per-symbol samples are 5–39 trades (don't
pick symbols from them); the option model assumes fills at fair value and static IV.

## Hosted dashboard (Vercel)

Read-only, password-protected copy of the local dashboard. **Push, not pull:**
this Mac builds snapshots and POSTs them to Vercel; Vercel only stores and serves
them. Webull keys and the account ID never leave the Mac.

```
publish_snapshot.py (Mac) --Bearer INGEST_TOKEN--> /api/snapshot --> Upstash Redis
browser --password cookie--> /api/state <-- Redis
```

- [web/](web/) — static pages + API routes (`snapshot`, `state`, `login`, `logout`)
- Snapshots expire after 3 days, so a dead runner can't leave stale numbers up
- Nothing on the hosted page can place or change orders

One-time setup:
```bash
cd web
npx vercel login                                    # you
./setup-vercel.sh                                   # link project, set INGEST_TOKEN + SESSION_SECRET
npx vercel env add DASHBOARD_PASSWORD production    # you: choose a password
# Vercel dashboard → Storage → Upstash Redis → Connect to project   (you)
npx vercel --prod
```
Then set `DASHBOARD_URL` in `.env` to the production URL and run alongside the runner:
```bash
./.venv/bin/python publish_snapshot.py
```
Local end-to-end test without Vercel: `cd web && npm run dev:local` (set the three env vars).

## ORB strategy (retired 2026-09-16)

Kept for reference in [strategy.py](strategy.py) / [run_orb.py](run_orb.py); no longer run.
Testing over 18 sessions x 8 symbols found 30 signals, 43% win, **−0.12% average** — no
edge, and the option-level simulation was worse (−2.3%/trade). Its volume filter compared
each bar to the opening 15 minutes, the busiest stretch of the day, which made entries
rare and late.

## Connecting to Claude
Point Claude Desktop / Claude Code at this folder as a working directory. Claude
can then run `wb.py` commands in conversation ("show my positions", "preview 2
shares of MSFT"). Claude will draft and preview orders; **you** run the final
`--confirm` to place — trades and money movement stay in your hands.
