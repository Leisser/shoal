# shoal

**Stop forking thirty-two processes.**

*A shoal is thousands of individuals moving as one body. That is the whole idea:
one Python process doing the work of the fleet you currently run.*

A typical Python service runs dozens of near-identical worker processes because
the GIL left no alternative. Each one duplicates the interpreter, the imports
and the application. Node count, the cluster that schedules them, and the team
that runs the cluster all follow from that duplication.

`shoal` collapses it.

## Status

Pre-alpha. Only `shoal doctor` works today — deliberately, because it answers
the question everything else depends on: *can your application do this at all?*

```console
$ shoal doctor

  shoal doctor   python 3.13.1   darwin

  interpreter      GIL build - threads share memory but not CPU; no collapse available
  subinterpreters  available

  dependency       version    free-threading          isolation tier
  --------------------------------------------------------------------------
  numpy            2.3.0      no free-threaded wheel  ABORTS PROCESS
  json                        stdlib                  ok

  BLOCKED  1 dependency(s) prevent free-threading: numpy
```

## Why the doctor comes first

On a free-threaded build, importing a C extension that is not marked
free-thread-safe **re-enables the GIL for the lifetime of the process** —
silently, with no error. You deploy on `python3.14t`, everything starts, and you
measure no gain whatsoever.

The doctor runs every import in an isolated child process and reports
`sys._is_gil_enabled()` *after* the import, which is the only way to catch this.
The same isolation lets it survive dependencies that abort the interpreter
outright: numpy under a subinterpreter dies on `SIGABRT`, not an exception.

## Measuring the claim

`bench/collapse.py` measures PSS across the process tree under each execution
model. Linux only for trustworthy numbers — macOS memory compression makes its
counters unusable for shared-page accounting, and the harness refuses to report
there without `--force`.

**Case A — memory.** Does the fleet collapse?

```console
$ python bench/collapse.py caseA --n 32 --imports numpy,django

  mode           total PSS   per unit      spawn
  processes        224.9 MB    28.11 MB     1.36 ms
  threads           28.5 MB     3.56 MB     0.03 ms   87% less than processes
  fork              61.2 MB     7.65 MB     0.33 ms   73% less than processes
  subinterp             --          --          --   died on SIGABRT
```

**Case C — throughput.** Do those threads actually do any work?

```console
$ python bench/collapse.py caseC --n 4

  mode            elapsed    speedup   efficiency
  serial            0.33s      1.00x         100%
  processes         0.09s      3.69x          92%
  threads           0.32s      1.01x          25%

  Case C: 1.01x thread speedup  ->  NOT USABLE. Expected on a GIL build.
```

Case C matters as much as Case A. Memory that collapses while throughput
collapses with it is worthless — the fleet is smaller and does less. Threads
share memory on *any* build; only a free-threaded build lets them run. CI
publishes both numbers on every push.

**Case C — throughput.** Do those threads actually do any work?

The verdict normalises against *usable* cores — cgroup quota first, then
scheduler affinity, then `cpu_count()` — because `os.cpu_count()` reports the
host's cores, not what a container was granted. The load-bearing number is
thread/process **parity**: both meet the same hardware ceiling, so their ratio
isolates the interpreter from the machine.

```console
$ python bench/collapse.py caseC --n 8

  units=8   python=3.13.1   GIL build
  usable cores=8 (process_cpu_count)   parallelism ceiling=8x

  mode           speedup   of ceiling
  serial           1.00x          12%
  processes        4.64x          58%
  threads          1.02x          13%

  thread/process parity: 0.22
  Case C: NOT USABLE -- threads do not parallelise. Expected on a GIL build.
```

**Scaling sweep.** Where does speedup plateau, and do threads track processes?

```console
$ python bench/collapse.py sweep --n 8

   units    threads   processes   parity   efficiency
       1      0.98x       0.98x     1.00          98%
       2      1.02x       2.00x     0.51          51%  <- plateau
       4      0.98x       3.25x     0.30          25%  <- plateau
       8      0.99x       4.46x     0.22          12%  <- plateau

  DOES NOT SCALE -- threads fall behind processes as units increase.
```

Scaling needs cores. A 2-core hosted runner caps every speedup at ~2x, which
says nothing about the interpreter — processes hit the same wall. To answer the
scaling question in CI, point the `scaling` job at a bigger machine:

```console
gh variable set LARGE_RUNNER --body "ubuntu-latest-8-cores"   # GitHub larger runner
gh variable set LARGE_RUNNER --body "self-hosted"             # your own
```

Unset, the job still runs and reports honestly that the ceiling was too low.

## Install

```console
pip install shoal
```

Requires Python 3.12+. The full feature set wants free-threaded 3.14.

## Licence

Apache-2.0
