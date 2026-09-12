#!/usr/bin/env python3
"""
Fleet-collapse measurement harness.

Measures the real memory cost of running N units of work under each execution
model, using PSS (proportional set size) so shared pages are counted once.

  Case A (thesis)     : processes  vs  threads     -- does free-threading collapse the fleet?
  Case B (beachhead)  : spawn      vs  fork        -- does CoW prewarming help JupyterHub?
  Case C (usability)  : throughput                 -- do those threads actually do work?
  Control             : subinterp                  -- is the isolation tier usable at all?

Case C matters as much as Case A. Memory that collapses while throughput
collapses with it is worthless: the fleet is smaller and does less. Threads
share memory on any build, but only a free-threaded build lets them run.

Linux only for trustworthy numbers. macOS memory compression makes its counters
unreliable for shared-page accounting; the harness will refuse to report there
unless --force is given.

Usage:
    python collapse.py all       --n 32 --imports numpy,pandas,django
    python collapse.py threads   --n 32 --imports numpy
    python collapse.py fork      --n 50 --imports numpy,pandas   # JupyterHub case
"""
from __future__ import annotations
import argparse, gc, json, os, signal, subprocess, sys, time

# ---------------------------------------------------------------- measurement

def _pss_kb(pid: int) -> int:
    """PSS for one process. Shared pages counted / number of sharers."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except FileNotFoundError:
        pass
    # fall back to summing the per-mapping smaps
    try:
        total = 0
        with open(f"/proc/{pid}/smaps") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    total += int(line.split()[1])
        return total
    except FileNotFoundError:
        return 0


def _rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/statm") as fh:
            return int(fh.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (FileNotFoundError, IndexError):
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
        return int(out) if out.isdigit() else 0


def _children(pid: int) -> list[int]:
    """Direct children of pid, without psutil."""
    kids = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/status") as fh:
                    for line in fh:
                        if line.startswith("PPid:"):
                            if int(line.split()[1]) == pid:
                                kids.append(int(entry))
                            break
            except (FileNotFoundError, ProcessLookupError):
                continue
    except FileNotFoundError:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
        kids = [int(x) for x in out.split()]
    return kids


def tree_memory(pid: int) -> tuple[int, int]:
    """(total PSS kB, total RSS kB) for pid and all descendants."""
    seen, stack, pss, rss = set(), [pid], 0, 0
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        pss += _pss_kb(p)
        rss += _rss_kb(p)
        stack.extend(_children(p))
    return pss, rss


LINUX = sys.platform.startswith("linux")

# ---------------------------------------------------------------- workloads

def _import_src(mods: list[str]) -> str:
    return "\n".join(f"import {m}" for m in mods)


def _child_src(mods: list[str]) -> str:
    """A unit of work that imports the stack then idles, holding its memory."""
    return f"{_import_src(mods)}\nimport signal\nsignal.pause()\n"


# ---------------------------------------------------------------- runners
# Each runner creates N units, lets them settle, and returns the measurement.

def run_processes(n: int, mods: list[str], settle: float) -> dict:
    """Today's deployment: N independent worker processes."""
    t0 = time.perf_counter()
    procs = [subprocess.Popen([sys.executable, "-c", _child_src(mods)])
             for _ in range(n)]
    spawn_ms = (time.perf_counter() - t0) / n * 1000
    time.sleep(settle)
    pss, rss = tree_memory(os.getpid())
    for p in procs:
        p.kill()
    for p in procs:
        p.wait()
    return {"pss_kb": pss, "rss_kb": rss, "spawn_ms": spawn_ms}


def run_threads(n: int, mods: list[str], settle: float) -> dict:
    """The thesis: one process, N threads, one copy of every module."""
    import threading
    for m in mods:
        __import__(m)
    threading.stack_size(256 * 1024)
    stop = threading.Event()
    t0 = time.perf_counter()
    threads = [threading.Thread(target=stop.wait, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    spawn_ms = (time.perf_counter() - t0) / n * 1000
    time.sleep(settle)
    pss, rss = tree_memory(os.getpid())
    stop.set()
    for t in threads:
        t.join()
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    return {"pss_kb": pss, "rss_kb": rss, "spawn_ms": spawn_ms, "gil_enabled": gil}


def run_fork(n: int, mods: list[str], settle: float) -> dict:
    """JupyterHub Case B: warm parent imports once, gc.freeze(), fork per unit."""
    for m in mods:
        __import__(m)
    gc.freeze()                      # keep startup objects out of the collector
    kids = []
    t0 = time.perf_counter()
    for _ in range(n):
        pid = os.fork()
        if pid == 0:
            signal.pause()           # child idles on the parent's shared pages
            os._exit(0)
        kids.append(pid)
    spawn_ms = (time.perf_counter() - t0) / n * 1000
    time.sleep(settle)
    pss, rss = tree_memory(os.getpid())
    for p in kids:
        os.kill(p, signal.SIGKILL)
        os.waitpid(p, 0)
    return {"pss_kb": pss, "rss_kb": rss, "spawn_ms": spawn_ms}


def run_subinterp(n: int, mods: list[str], settle: float) -> dict:
    """Control: is the isolation tier usable with this dependency tree?"""
    try:
        import concurrent.interpreters as I          # 3.14+
        create, run = I.create, lambda i, s: i.exec(s)
    except ImportError:
        try:
            import _interpreters as I                # 3.12/3.13 private
        except ImportError:
            return {"error": "no subinterpreter module"}
        create, run = I.create, I.run_string
    src = _import_src(mods)
    made = []
    t0 = time.perf_counter()
    for _ in range(n):
        i = create()
        try:
            run(i, src)
        except Exception as e:                        # a clean failure is a result
            return {"error": f"{type(e).__name__}: {e}", "created": len(made)}
        made.append(i)
    spawn_ms = (time.perf_counter() - t0) / n * 1000
    time.sleep(settle)
    pss, rss = tree_memory(os.getpid())
    return {"pss_kb": pss, "rss_kb": rss, "spawn_ms": spawn_ms}



# ---------------------------------------------------------------- throughput
# Memory sharing happens on any build. Parallelism does not. Case C separates
# the two by giving every unit identical CPU-bound work and timing the total.

def cpu_work(iterations: int) -> int:
    """Pure-Python integer work: no C library, no I/O, no GIL release."""
    total = 0
    for i in range(iterations):
        total += i * i
    return total


def _chunk(total_work: int, n: int) -> int:
    return max(1, total_work // n)


def run_throughput(mode: str, n: int, total_work: int) -> dict:
    """Wall-clock to complete `total_work` split across n units."""
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

    per = _chunk(total_work, n)
    pool = ThreadPoolExecutor if mode == "threads" else ProcessPoolExecutor
    with pool(max_workers=n) as ex:
        list(ex.map(cpu_work, [per] * n))          # warm the pool
        t0 = time.perf_counter()
        list(ex.map(cpu_work, [per] * n))
        elapsed = time.perf_counter() - t0
    return {"elapsed_s": elapsed, "units": n, "work_per_unit": per}


def run_serial(total_work: int) -> dict:
    per = _chunk(total_work, 1)
    cpu_work(per // 100)                            # warm
    t0 = time.perf_counter()
    cpu_work(per)
    return {"elapsed_s": time.perf_counter() - t0, "units": 1, "work_per_unit": per}


def case_c(n: int, total_work: int) -> None:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    ft = bool(__import__("sysconfig").get_config_var("Py_GIL_DISABLED"))
    print(f"\n  units={n}   work={total_work:,} iterations   "
          f"python={sys.version.split()[0]}   "
          f"{'free-threaded, GIL off' if ft and not gil else 'GIL build' if not ft else 'free-threaded, GIL ON'}")
    print()
    serial = run_serial(total_work)
    print(f"  {'mode':<12} {'elapsed':>10} {'speedup':>10} {'efficiency':>12}")
    print("  " + "-" * 48)
    print(f"  {'serial':<12} {serial['elapsed_s']:9.2f}s {1.0:9.2f}x {'100%':>12}")

    results = {}
    for mode in ("processes", "threads"):
        r = run_throughput(mode, n, total_work)
        speedup = serial["elapsed_s"] / r["elapsed_s"] if r["elapsed_s"] else 0.0
        eff = speedup / n * 100
        results[mode] = speedup
        print(f"  {mode:<12} {r['elapsed_s']:9.2f}s {speedup:9.2f}x {eff:11.0f}%")

    print()
    t = results.get("threads", 0.0)
    if t >= n * 0.6:
        v = "USABLE -- threads deliver real parallelism"
    elif t >= 2.0:
        v = "PARTIAL -- some parallelism, well short of linear"
    else:
        v = ("NOT USABLE -- threads do not parallelise. "
             + ("Expected on a GIL build." if not ft else "GIL is on: an import re-enabled it."))
    print(f"  Case C: {t:.2f}x thread speedup on {n} units  ->  {v}")
    print()


RUNNERS = {"processes": run_processes, "threads": run_threads,
           "fork": run_fork, "spawn": run_processes, "subinterp": run_subinterp}

# ---------------------------------------------------------------- driver

def measure(mode: str, n: int, mods: list[str], settle: float) -> dict:
    """Run one mode in a clean child so imports never leak between modes."""
    payload = json.dumps({"mode": mode, "n": n, "mods": mods, "settle": settle})
    out = subprocess.run([sys.executable, __file__, "--worker", payload],
                         capture_output=True, text=True)
    line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    if not line.startswith("{"):
        rc = out.returncode
        if rc < 0:
            name = signal.Signals(-rc).name
            err = f"process died on {name} (interpreter abort, not an exception)"
        else:
            err = (out.stderr.strip().splitlines() or ["no output"])[-1][:200]
        return {"error": err, "crashed": rc != 0, "returncode": rc}
    return json.loads(line)


def report(results: dict[str, dict], n: int, mods: list[str]) -> None:
    print(f"\n  units={n}   imports={','.join(mods) or 'none'}   "
          f"python={sys.version.split()[0]}   platform={sys.platform}")
    if not LINUX:
        print("  !! PSS unavailable off Linux -- RSS double-counts shared pages.")
        print("     Numbers below are NOT valid for shared-page accounting.")
    print()
    label = "total PSS" if LINUX else "total RSS"
    print(f"  {'mode':<12} {label:>11} {'per unit':>10} {'spawn':>10}   note")
    print("  " + "-" * 66)
    key = "pss_kb" if LINUX else "rss_kb"
    base = results.get("processes", {}).get(key)
    for mode, r in results.items():
        if "error" in r:
            print(f"  {mode:<12} {'--':>11} {'--':>10} {'--':>10}   FAILED: {r['error'][:46]}")
            continue
        pss, per = r[key], r[key] / n
        note = ""
        if mode == "threads" and not r.get("gil_enabled", True):
            note = "free-threaded"
        elif mode == "threads":
            note = "GIL build: memory valid, throughput NOT"
        if base and mode != "processes":
            note = (f"{(1 - pss / base) * 100:.0f}% less than processes" + (f", {note}" if note else ""))
        print(f"  {mode:<12} {pss/1024:9.1f} MB {per/1024:8.2f} MB {r['spawn_ms']:8.2f} ms   {note}")
    print()
    if base and key in results.get("threads", {}):
        cut = (1 - results["threads"][key] / base) * 100
        verdict = ("THESIS HOLDS" if cut >= 60 else
                   "MARGINAL -- below the 60% target" if cut >= 40 else
                   "THESIS FAILS on this workload")
        print(f"  Case A: {cut:.0f}% memory reduction, threads vs processes  ->  {verdict}")
    if base and key in results.get("fork", {}):
        cut = (1 - results["fork"][key] / base) * 100
        print(f"  Case B: {cut:.0f}% memory reduction, CoW fork vs independent processes")
    print()


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        cfg = json.loads(sys.argv[2])
        print(json.dumps(RUNNERS[cfg["mode"]](cfg["n"], cfg["mods"], cfg["settle"])))
        return

    ap = argparse.ArgumentParser(description="Fleet-collapse measurement harness")
    ap.add_argument("case", choices=["all", "caseA", "caseB", "caseC", "throughput", *RUNNERS])
    ap.add_argument("--n", type=int, default=32, help="units of work (default 32)")
    ap.add_argument("--imports", default="", help="comma-separated modules, e.g. numpy,pandas")
    ap.add_argument("--settle", type=float, default=3.0, help="seconds before measuring")
    ap.add_argument("--work", type=int, default=20_000_000,
                    help="total CPU iterations for caseC (default 20M)")
    ap.add_argument("--force", action="store_true", help="report anyway off Linux")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    a = ap.parse_args()

    if a.case in ("caseC", "throughput"):
        case_c(a.n, a.work)          # timing is trustworthy on any platform
        return

    if not LINUX and not a.force:
        sys.exit("Refusing to report: PSS needs Linux (/proc/smaps_rollup).\n"
                 "macOS memory compression makes shared-page accounting unreliable.\n"
                 "Run on Linux, or pass --force to see indicative RSS numbers.")

    mods = [m.strip() for m in a.imports.split(",") if m.strip()]
    modes = {"all": ["processes", "threads", "fork", "subinterp"],
             "caseA": ["processes", "threads"],
             "caseB": ["processes", "fork"]}.get(a.case, [a.case])

    results = {m: measure(m, a.n, mods, a.settle) for m in modes}
    print(json.dumps(results, indent=2)) if a.json else report(results, a.n, mods)
    if a.case == "all":
        case_c(a.n, a.work)


if __name__ == "__main__":
    main()
