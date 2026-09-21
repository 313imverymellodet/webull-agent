"""Exclusive per-strategy lock.

Two runners on one account double every trade. Restarts are frequent enough
here (and a stale process quiet enough) that this is cheap insurance.
"""
import fcntl
import os
import sys

_held = []          # keep file objects alive for the process lifetime


def single_instance(name: str, quiet: bool = False):
    os.makedirs("state", exist_ok=True)
    path = os.path.join("state", f"{name}.lock")
    f = open(path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        other = f.read().strip() or "unknown pid"
        sys.exit(f"REFUSING TO START: another {name} is already running ({other}). "
                 f"Stop it first, or remove {path} if it is stale.")
    f.seek(0); f.truncate(); f.write(f"pid {os.getpid()}\n"); f.flush()
    _held.append(f)
    if not quiet:
        print(f"[lock] {name} (pid {os.getpid()})", flush=True)
    return f
