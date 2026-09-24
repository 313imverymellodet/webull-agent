"""Deterministic tests for the runner's safety behaviour (stub broker, no network).

Each test pins down a failure found in the 2026-09-24 audit."""
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import run_ema as R                                   # noqa: E402
from strategy_ema import EMASignal                    # noqa: E402


class StubBroker:
    is_paper = True

    def __init__(self, positions=None, fail=False):
        self._positions, self.fail, self.calls = positions or [], fail, 0
        self.quote_rejects, self.rate_limited = {}, 0

    def positions(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("TOO_MANY_REQUESTS")
        return self._positions

    def stock_quote(self, sym, allow_stale=False):
        return {"price": 100.0}


def broker_pos(sym, strike, right, exp, cost=500.0):
    return {"instrument_type": "OPTION", "symbol": sym, "cost": str(cost),
            "legs": [{"option_exercise_price": str(strike), "option_type": right,
                      "option_expire_date": exp}]}


def runner(broker):
    r = R.Runner(live=False)
    r.broker = broker
    r.positions = []
    R._DEPLOYED.update(t=0.0, v=None)
    R.EVENTS.clear()
    return r


def my_pos(sym="IWM", strike=283.0, right="CALL", exp="2026-10-30", minutes_old=60):
    return {"symbol": sym, "strike": strike, "side": right, "expiry": exp, "qty": 1,
            "occ": f"{sym}X", "stop": 90.0, "target": 120.0, "entry_premium": 5.0,
            "opened_at": (datetime.now() - timedelta(minutes=minutes_old)).isoformat()}


def test_save_reads_positions_once():
    b = StubBroker([broker_pos("IWM", 283, "CALL", "2026-10-30")])
    r = runner(b)
    r.save("test")
    assert b.calls == 1, f"save() hit the broker {b.calls}x (was 4x before the fix)"
    r.save("test")                                   # cached within the TTL
    assert b.calls == 1, "second save within the cache window should not refetch"


def test_unreadable_positions_defer_instead_of_skipping():
    r = runner(StubBroker(fail=True))
    size, deployed, avail = r.capital()
    assert deployed is None and avail is None, "must be 'unknown', not infinity"
    sig = EMASignal("IWM", "CALL", pd.Timestamp.now(), 100, 98, 104, 2, 2.0, 25, "t")
    r.feed.pick_option_dte = lambda *a, **k: R.__dict__.get("_fake_pick")
    from feed import OptionPick
    R._fake_pick = OptionPick("IWM", "CALL", 100, "2026-10-30", 0, 5, 0, "IWMX")
    r.feed.next_earnings = lambda s: None
    r.broker.option_quote = lambda occ: {"ask": 5.0, "iv": 0.2, "delta": 0.6}
    try:
        r.enter(sig)
        raise AssertionError("enter() should defer when capital is unknown")
    except R.DeferredEntry:
        pass


def _synthetic_bars(n=320):
    idx = pd.date_range(end=pd.Timestamp.now(tz="America/New_York").normalize(), periods=n, freq="D")
    c = pd.Series(np.linspace(90, 110, n), index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6})


def _with_signal_on(r, sym, side):
    bars = _synthetic_bars()
    for s in R.symbols():
        r._bars_cache[s] = (time.time(), bars)
    R.signal_at = lambda s, b, cfg, htf, i: (EMASignal(s, side, pd.Timestamp.now(), 100, 98, 104,
                                                       2, 2.0, 25, "t") if s == sym else None)
    r.enter = lambda sig: None


def test_intraday_flicker_does_not_exit_or_rewrite():
    r = runner(StubBroker())
    p = my_pos("IWM", right="CALL"); r.positions = [p]
    _with_signal_on(r, "IWM", "PUT")                 # opposite of the held CALL
    r.evaluate(place=False)                          # every-minute refresh on a forming bar
    assert "force_exit" not in p, "an intraday partial-bar signal must not close the trade"
    assert (p["stop"], p["target"]) == (90.0, 120.0), "stop/target must never be overwritten"
    assert not [e for e in R.EVENTS if e["kind"] == "signal"], "no signal spam on refreshes"


def test_opposite_signal_at_close_flags_exit_without_rewriting():
    r = runner(StubBroker())
    p = my_pos("IWM", right="CALL"); r.positions = [p]
    _with_signal_on(r, "IWM", "PUT")
    r.evaluate(place=True)                           # the real end-of-day check
    assert p.get("force_exit"), "opposite signal at the close should flag an exit"
    assert (p["stop"], p["target"]) == (90.0, 120.0), "original stop/target preserved for the record"


def test_reconcile_never_drops_on_failed_read():
    r = runner(StubBroker(fail=True)); r.positions = [my_pos()]
    for _ in range(5):
        r.reconcile()
    assert len(r.positions) == 1, "a failed broker read must change nothing"


def test_reconcile_drops_only_after_repeated_misses():
    r = runner(StubBroker([]))                        # broker says we hold nothing
    r.positions = [my_pos(minutes_old=60)]
    r.reconcile(); r.reconcile()
    assert len(r.positions) == 1, "two misses is not enough"
    r.reconcile()
    assert len(r.positions) == 0, "three consecutive misses on an old position -> drop"


def test_reconcile_keeps_fresh_fill_even_if_missing():
    r = runner(StubBroker([]))
    r.positions = [my_pos(minutes_old=1)]             # just filled; broker can lag
    for _ in range(5):
        r.reconcile()
    assert len(r.positions) == 1, "a fresh fill must not be dropped for broker lag"


def test_reconcile_flags_unmanaged_positions_once():
    r = runner(StubBroker([broker_pos("TSLA", 350, "PUT", "2026-10-30")]))
    r.reconcile(); r.reconcile()
    warns = [e for e in R.EVENTS if "UNMANAGED" in e["text"]]
    assert len(warns) == 1, f"orphan should be flagged exactly once, got {len(warns)}"


if __name__ == "__main__":
    import run_ema
    orig = run_ema.signal_at
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            run_ema.signal_at = orig
            try:
                fn(); print(f"PASS {name}")
            except Exception as e:
                failed += 1; print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
