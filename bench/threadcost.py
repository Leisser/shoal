#!/usr/bin/env python3
"""What does a thread actually cost, and can that be tuned?

The real-application sweep found threads costing ~7.5 MB each on a free-threaded
build, against ~2 MB for a forked process -- which inverts the whole premise.
Free-threaded CPython allocates through mimalloc with a heap per thread, so the
suspicion is arena reservation rather than live objects. This tells them apart.

Each thread allocates a working set, frees it, then idles. Memory measured after
the free is memory the allocator kept rather than memory the program is using:

    live      cost while the objects exist   -- inherent to the workload
    retained  cost after they are freed      -- the allocator's, possibly tunable

    python bench/threadcost.py                     # this interpreter, this env
    python bench/threadcost.py compare             # across allocator settings
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

LINUX = sys.platform.startswith("linux")


def mem_mb() -> float:
    """PSS on Linux, RSS elsewhere. Single process, so PSS needs no tree walk."""
    if LINUX:
        try:
            for line in open("/proc/self/smaps_rollup"):
                if line.startswith("Pss:"):
                    return int(line.split()[1]) / 1024
        except OSError:
            pass
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            return 0.0
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                         capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out.isdigit() else 0.0


def churn(alloc_mb: float, rounds: int = 6) -> None:
    """Allocate a working set of Python objects, then drop it. Repeatedly."""
    per_obj = 512
    n = int(alloc_mb * 1024 * 1024 / per_obj)
    for _ in range(rounds):
        held = [{"i": i, "payload": b"x" * per_obj} for i in range(n)]
        held.clear()


def run_probe(threads: int, alloc_mb: float) -> dict:
    baseline = mem_mb()

    ready, go, done = threading.Barrier(threads + 1), threading.Event(), threading.Barrier(threads + 1)
    peak = {"v": baseline}
    lock = threading.Lock()

    def worker():
        ready.wait()
        go.wait()
        churn(alloc_mb)
        with lock:
            peak["v"] = max(peak["v"], mem_mb())
        done.wait()
        # then idle, holding nothing

    ws = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for w in ws:
        w.start()
    ready.wait()
    spawned = mem_mb()

    go.set()
    done.wait()
    live = peak["v"]

    import gc
    gc.collect()
    time.sleep(1.0)
    retained = mem_mb()

    return {
        "threads": threads,
        "baseline_mb": round(baseline, 1),
        "spawned_mb": round(spawned, 1),
        "live_mb": round(live, 1),
        "retained_mb": round(retained, 1),
        "per_thread_spawn_kb": round((spawned - baseline) * 1024 / threads, 1),
        "per_thread_retained_mb": round((retained - baseline) / threads, 2),
        "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
        "freethreaded": bool(__import__("sysconfig").get_config_var("Py_GIL_DISABLED")),
        "allocator": os.environ.get("PYTHONMALLOC", "default"),
        "arena_max": os.environ.get("MALLOC_ARENA_MAX", "-"),
    }


CONFIGS = [
    ("default", {}),
    ("PYTHONMALLOC=malloc", {"PYTHONMALLOC": "malloc"}),
    ("malloc + ARENA_MAX=2", {"PYTHONMALLOC": "malloc", "MALLOC_ARENA_MAX": "2"}),
    ("PYTHONMALLOC=mimalloc", {"PYTHONMALLOC": "mimalloc"}),
    ("PYTHONMALLOC=pymalloc", {"PYTHONMALLOC": "pymalloc"}),
]


def compare(threads: int, alloc_mb: float) -> None:
    print(f"\n  thread cost   python={sys.version.split()[0]}   "
          f"{threads} threads x {alloc_mb:g} MB churn")
    ft = bool(__import__("sysconfig").get_config_var("Py_GIL_DISABLED"))
    print(f"  build: {'free-threaded' if ft else 'GIL'}   "
          f"{'PSS' if LINUX else 'RSS (not Linux -- indicative only)'}")
    print()
    print(f"  {'allocator':<24} {'spawn kB/thr':>13} {'live MB':>9} "
          f"{'retained MB':>12} {'retained/thr':>13}")
    print("  " + "-" * 76)

    rows = []
    for name, env in CONFIGS:
        e = {**os.environ, **env}
        p = subprocess.run([sys.executable, os.path.abspath(__file__),
                            "probe", "--threads", str(threads),
                            "--alloc-mb", str(alloc_mb)],
                           capture_output=True, text=True, env=e, timeout=600)
        line = next((l for l in p.stdout.splitlines() if l.startswith("{")), "")
        if not line:
            why = (p.stderr.strip().splitlines() or ["no output"])[-1][:40]
            print(f"  {name:<24} {'unsupported: ' + why:>50}")
            continue
        d = json.loads(line)
        rows.append((name, d))
        print(f"  {name:<24} {d['per_thread_spawn_kb']:12.0f}  {d['live_mb']:8.1f} "
              f"{d['retained_mb']:11.1f} {d['per_thread_retained_mb']:12.2f}")

    print()
    working = [n for n, _ in rows]
    unsupported = [n for n, _ in CONFIGS if n not in working]

    if len(rows) <= 1:
        print("  Only one allocator ran. Nothing to compare.")
    else:
        base = next((d for n, d in rows if n == "default"), rows[0][1])
        best_name, best = min(rows, key=lambda r: r[1]["per_thread_retained_mb"])
        cut = base["per_thread_retained_mb"] - best["per_thread_retained_mb"]

        # "default" and an explicit setting naming the same allocator are the
        # same allocator; a gap between them is run-to-run noise, not tuning.
        distinct = {n for n in working if n != "default"}
        only_one_allocator = distinct <= {"PYTHONMALLOC=mimalloc"} or distinct <= {"PYTHONMALLOC=pymalloc"}

        if only_one_allocator:
            print("  NOT TUNABLE -- this build supports one allocator and rejects the rest.")
            print(f"  {'':14}Cost is {base['per_thread_retained_mb']:.2f} MB per thread "
                  "and there is no other setting to move to.")
        elif best_name != "default" and cut > 0.5:
            print(f"  TUNABLE -- '{best_name}' costs {cut:.2f} MB less per thread than default:")
            print(f"  {'':11}{base['per_thread_retained_mb']:.2f} -> "
                  f"{best['per_thread_retained_mb']:.2f} MB per thread.")
        else:
            worst = max(rows, key=lambda r: r[1]["per_thread_retained_mb"])
            print("  DEFAULT IS BEST -- no setting beats it "
                  f"({base['per_thread_retained_mb']:.2f} MB per thread; "
                  f"worst is {worst[0]} at {worst[1]['per_thread_retained_mb']:.2f}).")

    if unsupported:
        print(f"  Rejected by this build: {', '.join(unsupported)}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    for name in ("probe", "compare"):
        s = sub.add_parser(name)
        s.add_argument("--threads", type=int, default=16)
        s.add_argument("--alloc-mb", type=float, default=8.0)
    a = ap.parse_args()
    if a.cmd == "probe":
        print(json.dumps(run_probe(a.threads, a.alloc_mb)))
    else:
        compare(getattr(a, "threads", 16), getattr(a, "alloc_mb", 8.0))


if __name__ == "__main__":
    main()
