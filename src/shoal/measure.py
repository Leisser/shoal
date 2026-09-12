"""`shoal measure` -- what preloading is worth for *this* application.

`audit` can only quote this project's benchmark at a user, which is a real range
from real measurements but is not their measurement. This runs the comparison
against their own imports and gives them their own number.

Nothing running is touched. Two throwaway fleets are started, measured at rest,
and killed:

    spawned     N interpreters, each importing the application for itself
    preforked   one import, gc.freeze(), then fork -- pages shared by CoW

Measured at rest rather than under load, deliberately. What shares is the
interpreter and the imported modules, and those are resident the moment import
finishes; per-request working set does not share and is not counted. On this
project's own benchmark the at-rest and under-load figures for the process
models agreed to within a megabyte.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from .audit import LINUX, read_mem


@dataclass(frozen=True)
class Result:
    workers: int
    spawned_kb: int
    preforked_kb: int
    error: str = ""

    @property
    def saved_kb(self) -> int:
        return max(0, self.spawned_kb - self.preforked_kb)

    @property
    def saved_pct(self) -> float:
        return (self.saved_kb / self.spawned_kb * 100) if self.spawned_kb else 0.0


def _tree_pss_kb(pid: int) -> int:
    """PSS of pid and its children. Shared pages counted once, which is the point."""
    total, seen, stack = 0, set(), [pid]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        total += read_mem(p)[0]
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    for line in open(f"/proc/{entry}/status"):
                        if line.startswith("PPid:"):
                            if int(line.split()[1]) == p:
                                stack.append(int(entry))
                            break
                except OSError:
                    continue
        except OSError:
            pass
    return total


# The child of a spawned fleet: import, announce, idle.
_SPAWN_CHILD = (
    "import importlib, signal, sys\n"
    "m, _, a = sys.argv[1].partition(':')\n"
    "mod = importlib.import_module(m)\n"
    "getattr(mod, a)\n"
    "print('@@UP@@', flush=True)\n"
    "signal.pause()\n"
)

# The parent of a preforked fleet: import once, freeze, fork, idle.
_FORK_PARENT = (
    "import gc, importlib, os, signal, sys\n"
    "m, _, a = sys.argv[1].partition(':')\n"
    "mod = importlib.import_module(m)\n"
    "getattr(mod, a)\n"
    "gc.freeze()\n"                      # the half everyone forgets
    "n = int(sys.argv[2])\n"
    "kids = []\n"
    "for _ in range(n):\n"
    "    pid = os.fork()\n"
    "    if pid == 0:\n"
    "        signal.pause()\n"
    "        os._exit(0)\n"
    "    kids.append(pid)\n"
    "print('@@UP@@', flush=True)\n"
    "signal.pause()\n"
)


def _measure_spawned(target: str, n: int, settle: float) -> int:
    kids = [subprocess.Popen([sys.executable, "-c", _SPAWN_CHILD, target],
                             stdout=subprocess.PIPE, text=True)
            for _ in range(n)]
    try:
        for k in kids:
            if not (k.stdout.readline() or "").startswith("@@UP@@"):
                raise RuntimeError("a worker exited before importing the application")
        time.sleep(settle)
        return sum(_tree_pss_kb(k.pid) for k in kids)
    finally:
        for k in kids:
            k.kill()
        for k in kids:
            k.wait()


def _measure_preforked(target: str, n: int, settle: float) -> int:
    p = subprocess.Popen([sys.executable, "-c", _FORK_PARENT, target, str(n)],
                         stdout=subprocess.PIPE, text=True)
    try:
        if not (p.stdout.readline() or "").startswith("@@UP@@"):
            raise RuntimeError("the parent exited before forking")
        time.sleep(settle)
        return _tree_pss_kb(p.pid)
    finally:
        p.send_signal(signal.SIGKILL)
        p.wait()


def measure(target: str, workers: int = 8, settle: float = 1.5) -> Result:
    if not LINUX:
        return Result(workers, 0, 0,
                      error="needs Linux: PSS comes from /proc/<pid>/smaps_rollup")
    try:
        spawned = _measure_spawned(target, workers, settle)
        preforked = _measure_preforked(target, workers, settle)
    except Exception as e:
        return Result(workers, 0, 0, error=f"{type(e).__name__}: {e}")
    return Result(workers, spawned, preforked)


def render(r: Result, target: str, *, tty: bool = True) -> str:
    G, R, B, DIM, X = "\033[32m", "\033[31m", "\033[1m", "\033[2m", "\033[0m"
    c = (lambda s, col: f"{col}{s}{X}") if tty else (lambda s, col: s)
    L = ["", f"  {c('shoal measure', B)}   {target}", ""]

    if r.error:
        L += [f"  {c('could not measure', R)}  {r.error}", ""]
        return "\n".join(L)

    L.append(f"  {'workers':<14} {r.workers}")
    L.append("")
    L.append(f"  {'spawned':<14} {r.spawned_kb/1024:8.1f} MB   "
             "every worker imports for itself")
    L.append(f"  {'preforked':<14} {r.preforked_kb/1024:8.1f} MB   "
             "one import, gc.freeze(), then fork")
    L.append("")
    if r.saved_kb:
        L.append(f"  {c('YOUR SAVING', G)}    "
                 f"{c(f'{r.saved_kb/1024:.1f} MB ({r.saved_pct:.0f}%)', G)}"
                 f"  at {r.workers} workers")
        L.append("")
        L.append(f"  Take it:  {c(f'shoal serve {target}', B)}")
    else:
        L.append("  No saving here: this application imports little enough that")
        L.append("  there is nothing meaningful to share. Preloading still costs")
        L.append("  nothing, so there is no reason not to.")
    L.append("")
    L.append(f"  {c('Measured at rest. Per-request working set does not share '
                    'and is not counted.', DIM)}")
    L.append("")
    return "\n".join(L)
