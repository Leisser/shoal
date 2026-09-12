#!/usr/bin/env python3
"""Measure a realistic WSGI app under load, in both topologies.

Everything measured so far has been synthetic: trivial CPU work, no imports, no
request state. That inflates the collapse, because the part of a real process
that actually collapses is the interpreter and the imported modules -- and a
real service also holds per-request working set, which does not.

This serves the same application two ways over an identical load:

    processes   N processes, one heap each     -- what you run today
    threads     1 process, N threads           -- what shoal serve gives you

and reports PSS *while the load is running*, so the number includes working set
rather than flattering us by measuring an idle server.

    python bench/realapp.py compare --n 8

Linux only for trustworthy memory; PSS needs /proc/smaps_rollup.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collapse import tree_memory, usable_cores          # noqa: E402

# --- imports a real service carries, and their memory with it -----------------
import base64, dataclasses, datetime, decimal, email.utils, gzip, hashlib  # noqa: E402
import hmac, logging, re, secrets, textwrap, unicodedata, urllib.parse, uuid  # noqa: E402

LINUX = sys.platform.startswith("linux")
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclasses.dataclass
class Record:
    id: str
    name: str
    amount: decimal.Decimal
    created: datetime.datetime


def _rows(n: int = 40) -> list[Record]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return [Record(id=str(uuid.uuid4()), name=f"item {i}",
                   amount=decimal.Decimal(i) / 7, created=now) for i in range(n)]


def handle_request(path: str) -> bytes:
    """Per-request work of the shape a real endpoint does."""
    rows = _rows()
    payload = {
        "path": path,
        "slug": _SLUG.sub("-", path.lower()).strip("-"),
        "items": [{"id": r.id,
                   "name": unicodedata.normalize("NFKC", r.name),
                   "amount": str(r.amount.quantize(decimal.Decimal("0.01"))),
                   "created": email.utils.format_datetime(r.created)}
                  for r in rows],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    return json.dumps({"etag": digest, "size": len(body),
                       "sample": base64.b64encode(body[:48]).decode()}).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):                                    # noqa: N802
        body = handle_request(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                           # keep the run quiet
        pass


# ----------------------------------------------------------------- serving

def _server_on(sock: socket.socket) -> HTTPServer:
    srv = HTTPServer.__new__(HTTPServer)
    TCPServer.__init__(srv, ("", 0), Handler, bind_and_activate=False)
    srv.socket = sock
    srv.server_address = sock.getsockname()
    return srv


def serve_threads(sock: socket.socket, n: int) -> None:
    """One process, exactly n worker threads, one heap.

    Deliberately not ThreadingHTTPServer: that spawns a thread per connection,
    so it would answer 32 concurrent requests with 32 workers while the process
    model got n. The comparison is only worth anything if both sides run the
    same number of workers.
    """
    import threading

    srv = _server_on(sock)
    workers = [threading.Thread(target=_accept_loop, args=(srv,), daemon=True)
               for _ in range(n)]
    for w in workers:
        w.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def _accept_loop(srv: HTTPServer) -> None:
    while True:
        try:
            srv.handle_request()
        except OSError:
            return


def serve_processes(sock: socket.socket, n: int) -> None:
    """n processes sharing one listening socket: the pre-fork model, one worker each."""
    kids = []
    for _ in range(n):
        pid = os.fork()
        if pid == 0:
            try:
                _accept_loop(_server_on(sock))
            except KeyboardInterrupt:
                pass
            os._exit(0)
        kids.append(pid)
    signal.signal(signal.SIGTERM, lambda *a: [os.kill(p, signal.SIGKILL) for p in kids])
    for p in kids:
        os.waitpid(p, 0)


def run_server(mode: str, n: int, port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(512)
    print(f"@@READY@@{sock.getsockname()[1]}", flush=True)
    (serve_threads if mode == "threads" else serve_processes)(sock, n)


# -------------------------------------------------------------------- load

def drive(port: int, requests: int, concurrency: int) -> dict:
    """Fire `requests` GETs across `concurrency` connections; return timing."""
    from concurrent.futures import ThreadPoolExecutor
    import http.client

    per = max(1, requests // concurrency)

    def worker(_):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        ok = 0
        for i in range(per):
            conn.request("GET", f"/api/items/{i}")
            r = conn.getresponse()
            r.read()
            ok += r.status == 200
        conn.close()
        return ok

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        done = sum(ex.map(worker, range(concurrency)))
    elapsed = time.perf_counter() - t0
    return {"requests": done, "elapsed_s": elapsed, "rps": done / elapsed if elapsed else 0}


# ----------------------------------------------------------------- compare

def _wait_ready(proc: subprocess.Popen, timeout: float = 30.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.startswith("@@READY@@"):
            return int(line[len("@@READY@@"):])
        if proc.poll() is not None:
            raise RuntimeError("server exited before becoming ready")
    raise TimeoutError("server never signalled ready")


def measure(mode: str, n: int, requests: int, concurrency: int) -> dict:
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "run",
         "--mode", mode, "--n", str(n), "--port", "0"],
        stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        port = _wait_ready(proc)
        drive(port, min(200, requests), concurrency)           # warm
        time.sleep(0.5)
        idle_pss, idle_rss = tree_memory(proc.pid)

        import threading
        peak = {"pss": 0, "rss": 0}
        stop = threading.Event()

        def sampler():
            while not stop.wait(0.15):
                p, r = tree_memory(proc.pid)
                peak["pss"], peak["rss"] = max(peak["pss"], p), max(peak["rss"], r)

        t = threading.Thread(target=sampler, daemon=True)
        t.start()
        result = drive(port, requests, concurrency)
        stop.set(); t.join()
        return {**result, "idle_pss_kb": idle_pss, "peak_pss_kb": peak["pss"],
                "peak_rss_kb": peak["rss"]}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def compare(n: int, requests: int, concurrency: int) -> None:
    cores, why = usable_cores()
    print(f"\n  real application under load   python={sys.version.split()[0]}   "
          f"units={n}   cores={cores} ({why})")
    print(f"  {requests} requests across {concurrency} connections, per topology")
    if not LINUX:
        print("  !! not Linux: PSS unavailable, RSS double-counts shared pages.")
    print()

    key = "peak_pss_kb" if LINUX else "peak_rss_kb"
    res = {m: measure(m, n, requests, concurrency) for m in ("processes", "threads")}

    print(f"  {'topology':<12} {'idle':>10} {'under load':>12} {'req/s':>10}")
    print("  " + "-" * 48)
    for m in ("processes", "threads"):
        r = res[m]
        idle = r["idle_pss_kb"] if LINUX else r["peak_rss_kb"]
        print(f"  {m:<12} {idle/1024:8.1f} MB {r[key]/1024:10.1f} MB {r['rps']:9.0f}")
    print()

    p, t = res["processes"], res["threads"]
    mem_cut = (1 - t[key] / p[key]) * 100 if p[key] else 0
    thr = (t["rps"] / p["rps"]) if p["rps"] else 0
    print(f"  memory under load: {mem_cut:.0f}% less on threads")
    print(f"  throughput:        {thr:.2f}x of processes")
    print()
    if mem_cut >= 50 and thr >= 0.85:
        print("  HOLDS -- substantially less memory at comparable throughput.")
    elif mem_cut >= 50:
        print(f"  MEMORY ONLY -- {mem_cut:.0f}% less memory but {thr:.2f}x throughput. "
              "Check whether the GIL is on.")
    else:
        print("  DOES NOT HOLD on this workload -- working set dominates the shared pages.")
    print()


def sweep(max_n: int, requests: int, concurrency: int) -> None:
    """Vary the worker count. Free-threading charges per thread, so there may be
    a count where threads still win -- and `shoal serve` should pick that one."""
    cores, why = usable_cores()
    key = "peak_pss_kb" if LINUX else "peak_rss_kb"
    print(f"\n  worker-count sweep   python={sys.version.split()[0]}   "
          f"cores={cores} ({why})")
    ft = bool(__import__("sysconfig").get_config_var("Py_GIL_DISABLED"))
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(f"  build: {'free-threaded, GIL off' if ft and not gil else 'GIL build' if not ft else 'free-threaded, GIL ON'}")
    if not LINUX:
        print("  !! not Linux: PSS unavailable, numbers are indicative only.")
    print()
    print(f"  {'n':>4} {'proc MB':>9} {'thr MB':>9} {'mem':>7} "
          f"{'proc r/s':>9} {'thr r/s':>9} {'thr/proc':>9}")
    print("  " + "-" * 62)

    ns, k = [], 1
    while k <= max_n:
        ns.append(k)
        k *= 2

    best = None
    for k in ns:
        pr = measure("processes", k, requests, min(concurrency, max(k, 2)))
        th = measure("threads", k, requests, min(concurrency, max(k, 2)))
        pm, tm = pr[key] / 1024, th[key] / 1024
        cut = (1 - tm / pm) * 100 if pm else 0
        ratio = (th["rps"] / pr["rps"]) if pr["rps"] else 0
        mark = ""
        if cut > 0 and ratio >= 0.85 and (best is None or cut > best[1]):
            best = (k, cut, ratio)
            mark = "  <-"
        print(f"  {k:4d} {pm:8.1f} {tm:8.1f} {cut:6.0f}% "
              f"{pr['rps']:8.0f} {th['rps']:8.0f} {ratio:8.2f}{mark}")

    print()
    if best:
        k, cut, ratio = best
        print(f"  best collapse at n={k}: {cut:.0f}% less memory at {ratio:.2f}x throughput")
    else:
        print("  No worker count where threads use less memory at comparable throughput.")
        print("  Pre-forked processes already share their pages via copy-on-write;")
        print("  free-threading's per-thread allocator arenas cost more than that saves.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="serve the app in one topology")
    r.add_argument("--mode", choices=["threads", "processes"], required=True)
    r.add_argument("--n", type=int, default=8)
    r.add_argument("--port", type=int, default=0)

    c = sub.add_parser("compare", help="serve both ways under identical load")
    c.add_argument("--n", type=int, default=8)
    c.add_argument("--requests", type=int, default=4000)
    c.add_argument("--concurrency", type=int, default=32)

    w = sub.add_parser("sweep", help="find the worker count where threads win, if any")
    w.add_argument("--max", type=int, default=16)
    w.add_argument("--requests", type=int, default=2000)
    w.add_argument("--concurrency", type=int, default=16)

    a = ap.parse_args()
    if a.cmd == "run":
        run_server(a.mode, a.n, a.port)
    elif a.cmd == "sweep":
        sweep(a.max, a.requests, a.concurrency)
    else:
        compare(a.n, a.requests, a.concurrency)


if __name__ == "__main__":
    main()
