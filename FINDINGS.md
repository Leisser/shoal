# Findings

Measurements, including the ones that contradict the premise. Every number here
came from CI on Linux with PSS from `/proc/smaps_rollup`; reproduce with the
commands shown.

## 1. Free-threading works. Threads run in parallel.

`bench/collapse.py sweep`, 4-core runner, pure-Python CPU work:

| units | 3.14 (GIL) parity | 3.14t parity |
|------:|------------------:|-------------:|
| 1     | 0.91              | 0.97 |
| 2     | 0.50              | 0.97 |
| 4     | 0.45              | 0.94 |
| 8     | 0.46              | 0.98 |
| 16    | 0.46              | 0.97 |

Parity is thread speedup over process speedup. Both meet the same hardware
ceiling, so the ratio isolates the interpreter. **On 3.14t threads match
separate processes.** That part of the premise holds.

## 2. The memory collapse does not survive contact with a real workload.

`bench/realapp.py sweep` — same app, same load, PSS sampled while serving:

| workers | 3.14 proc | 3.14 thr | | 3.14t proc | 3.14t thr | |
|--------:|----------:|---------:|------:|-----------:|----------:|------:|
| 4       | 29.3 MB   | 17.5 MB  | −40%  | 41.3 MB    | 53.8 MB   | **+30%** |
| 8       | 39.7 MB   | 17.9 MB  | −55%  | 51.6 MB    | 78.0 MB   | **+51%** |
| 16      | 60.4 MB   | 18.7 MB  | −69%  | 71.7 MB    | 126.5 MB  | **+76%** |

On the free-threaded build **threads use more memory than processes, and the gap
widens with every worker added.** There is no worker count where they win.

Two things cause it:

- **Pre-forked processes already share almost everything.** Fork from a parent
  that has imported the application and copy-on-write does the job; PSS counts
  those shared pages once. The "N processes means N copies of everything" model
  the project was premised on is wrong for any server that preloads. Processes
  cost ~2 MB each here, not 30.
- **Free-threaded CPython charges per thread.** Each thread gets its own
  allocator arenas, costing ~7.5 MB apiece under allocation-heavy load. That is
  roughly four times what a forked process costs.

## 3. Why the earlier numbers looked so good

`bench/collapse.py caseA` reported 80–92%. Those threads were idle — they
allocated nothing, so they cost 35 KB each. Idle threads were never the
workload. The lesson is not that Case A was wrong but that it measured the wrong
thing, and nothing built on it should be trusted until Case D or E agrees.

## 4. Known limitation: the throughput figures in Case D and E are client-bound

Request rates come out at 98 / 194 / 388 r/s for 4 / 8 / 16 workers — exactly
proportional to worker count, on both builds. That is the load generator's
connection count being measured, not server capacity. **Do not read `1.00x` as
"free-threading gives no throughput benefit"** — this harness cannot tell.
Case C measures parallelism properly; these two do not.

## Where that leaves the premise

"Free-threading collapses a fleet's memory" is **falsified under load** on
3.14t as it stands today. What survives is narrower: free-threading gives real
parallelism at *comparable* memory to pre-forked processes, in one process
instead of many.

Open questions, in the order worth answering:

1. Is the ~7.5 MB per thread tunable, or inherent to CPython's mimalloc arenas?
2. Does it shrink with a less allocation-heavy request path?
3. Fix the client-bound load generator, then re-ask whether free-threading buys
   throughput a pre-fork server cannot.
