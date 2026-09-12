# Findings

Measurements, including the ones that contradict the premise the project started
from. Every number came from CI on Linux with PSS from `/proc/smaps_rollup`.

## Summary

| | 3.14 (GIL) | 3.14t (free-threaded) |
|---|---:|---:|
| spawned — N interpreters, no sharing | 132.2 MB | 232.7 MB |
| **preforked — preload + `gc.freeze()`** | **39.9 MB** | **50.5 MB** |
| threads — one process, N threads | 18.1 MB | 78.0 MB |

Same application, same load, 8 workers, PSS sampled while serving.

Two things fall out, and the second is not what this project was built on.

## 1. The largest win is preloading, and it has nothing to do with free-threading

**Forking from a preloaded parent costs 70–78% less than spawning independent
workers**, on both builds. `gunicorn --workers 8` gives you the 132 MB row.
`gunicorn --preload --workers 8`, with `gc.freeze()` before the fork, gives you
the 40 MB row. It is the same application serving the same traffic.

`gc.freeze()` is not optional garnish. Without it the collector's mark phase
writes to object headers in every child, copy-on-write copies the pages, and the
sharing you preloaded for evaporates within minutes.

This works today, on every CPython, with no ecosystem risk and no dependency
audit. It is a bigger, safer and more portable saving than anything below.

## 2. The free-threading memory thesis is falsified, and it is not tunable

On 3.14t, threads cost **more** than pre-forked processes, and the gap widens
with every worker added:

| workers | preforked | threads | |
|--------:|----------:|--------:|------:|
| 4 | 41.3 MB | 53.8 MB | +30% |
| 8 | 51.6 MB | 78.0 MB | +51% |
| 16 | 71.7 MB | 126.5 MB | +76% |

There is no crossover to tune towards. Two causes:

- **Pre-forked processes already share nearly everything.** "N processes means N
  copies" is simply untrue of a server that preloads — a forked worker costs
  ~2 MB, not a full copy.
- **Free-threaded CPython charges per thread.** Roughly 7.5 MB apiece under
  allocation-heavy load; spawning a bare thread alone costs 2 MB against 21 KB
  on the GIL build.

### The allocator cannot be swapped

The per-thread cost is mimalloc's arenas, and free-threaded CPython requires
mimalloc. `bench/threadcost.py` on 3.14t:

| allocator | result |
|---|---|
| default (mimalloc) | 12.47 MB/thread retained |
| `PYTHONMALLOC=malloc` | **rejected — interpreter aborts** |
| `PYTHONMALLOC=pymalloc` | **rejected — interpreter aborts** |

PEP 703's lock-free containers depend on mimalloc, so there is nothing to tune.
The cost is structural in CPython 3.14t as it ships.

## 3. Free-threading does deliver parallelism

`bench/collapse.py sweep`, parity = thread speedup ÷ process speedup:

| units | 3.14 | 3.14t |
|------:|-----:|------:|
| 2 | 0.50 | 0.97 |
| 8 | 0.46 | 0.98 |
| 16 | 0.46 | 0.97 |

On 3.14t threads match separate processes for CPU work. That half of the premise
holds — it is the memory half that does not.

## 4. Why the early numbers looked so good

`caseA` reported 80–92%. Those threads were idle and allocated nothing, so they
cost 35 KB each. Idle threads were never the workload, and nothing built on that
figure should be trusted.

## 5. Known limitation: throughput in Cases D and E is client-bound

Request rates come out exactly proportional to worker count on both builds —
that is the load generator being measured, not the server. **Do not read
`1.00x` as evidence about free-threading either way.** Case C measures
parallelism properly; these two do not.

## Where this leaves the project

The premise it was built on — free-threading collapses a fleet's memory — is
**falsified and not recoverable by tuning**. What replaced it is better
evidenced and more useful: most deployments are leaving 70–78% on the table by
not preloading, and that is fixable today on any interpreter.
