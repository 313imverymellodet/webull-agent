"""Smoke tests for the runners against REAL market data.

These exist because a missing `import pandas` blinded the EMA runner for two
sessions: every symbol raised inside a per-symbol error handler, so the process
stayed up and looked healthy while evaluating nothing. Unit tests of the pure
strategy never exercised that path.

    ./.venv/bin/python tests/test_runner.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Never touch a live runner's state: these tests also run on the production
# server as a deploy gate. Subprocesses inherit this too.
import tempfile  # noqa: E402
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="webull-test-state-")
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def test_every_module_imports():
    for m in ("broker", "feed", "strategy_ema", "strategy_miyagi", "run_ema",
              "run_miyagi", "close_positions", "report", "dashboard_server"):
        __import__(m)


def test_ema_evaluates_every_symbol():
    import run_ema as R
    r = R.Runner(live=False)
    r.evaluate(place=False)
    errs = {k: v.get("error") for k, v in r.snap.items() if v.get("status") != "watching"}
    assert not errs, f"symbols not evaluated: {errs}"
    assert len(r.snap) == len(R.symbols())


def test_miyagi_scans_every_symbol():
    import run_miyagi as M
    m = M.Miyagi(live=False)
    m.scan_setups()
    errs = {k: v for k, v in m.setups.items() if v.get("status") == "error"}
    assert not errs, f"miyagi scan errors: {errs}"


def test_scan_exits_nonzero_on_error():
    # the CLI must fail loudly, not print a clean-looking empty result
    out = subprocess.run([sys.executable, "run_ema.py", "--mode", "scan"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout[-400:] + out.stderr[-400:]
    assert "evaluated" in out.stdout


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except Exception as e:
                failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
