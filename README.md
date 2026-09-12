# shoal

**Your Python workers are not sharing memory they could be sharing.**

A shoal is thousands of individuals moving as one body. Most Python fleets are
the opposite: N workers, each holding its own private copy of an interpreter and
every module you import.

Measured on a real application under load, 8 workers, PSS:

| how the workers are started | 3.14 | 3.14t |
|---|---:|---:|
| `gunicorn --workers 8` | 132 MB | 233 MB |
| **`--preload` + `gc.freeze()`** | **40 MB** | **51 MB** |

**70-78% saved**, on any CPython, with no code changes and no dependency audit.
`shoal serve` does it for you and explains what it chose.

> This project began on a different premise -- that free-threading would collapse
> a fleet's memory -- and measured its way out of it. Free-threading gives real
> parallelism, but threads cost *more* memory than pre-forked workers (78 MB
> against 51 MB at 8 workers). [FINDINGS.md](FINDINGS.md) has the numbers,
> including the ones that killed the original idea.

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

## `shoal gil` — find what turned it back on, and fix it

Detection alone is diagnosis. When an extension re-enables the GIL there are
exactly two cures, and `shoal gil` names the culprit and offers both:

```console
$ shoal gil myproject.wsgi

  GIL RE-ENABLED  importing myproject.wsgi turned the GIL back on.
  Every thread in this process now runs one at a time.

  blamed by CPython:
    - psycopg2._psycopg

  Two ways forward.

  1. Replace the dependency -- the real fix.
       pip index versions psycopg2    # is there a newer build?
     Compatibility tracker: https://py-free-threading.github.io/tracking/

  What you get:
    one copy of this application costs 248 MB.
    today   16 processes x 248 MB = 3,968 MB
    after   1 process + 16 threads = 249 MB
    saved   3,719 MB (94%) -- and threads keep pace with processes (parity 0.97).
    Baseline only: per-request working set does not collapse.

  RECOMMENDED  Replace the dependency.
     You get the saving above with no caveat attached: the extension declares
     itself thread-safe, CPython leaves the GIL off, and you are running a
     supported configuration you can upgrade into.

       pip index versions psycopg2
     Tracker: https://py-free-threading.github.io/tracking/

  If you cannot  override CPython -- same saving, real risk.
       shoal serve --force-gil-off ...      # PYTHON_GIL=0
     Corruption here is silent, not a crash. Treat it as a bridge until the
     dependency catches up -- not a destination.
```

Every command that finds an opportunity says what it is worth in megabytes on
your machine, and recommends one path rather than presenting a menu. The safe
cure is always listed first; the override is always framed as temporary. Where
shoal cannot measure, it prints no number rather than an invented one.

CPython names the offending module when it enables the GIL, but the warning
lands in stderr during startup where nobody reads it, and everything afterwards
silently runs single-threaded. `shoal gil` imports your app in a child with
`-X warn_default_gil`, captures that warning, and puts the name in front of you.

`shoal serve --force-gil-off` applies the second cure — `PYTHON_GIL=0`, which
overrides CPython's module-slot logic and keeps the GIL off. It is genuinely
unsafe in proportion to what the extension does with shared state, so it is
opt-in, announced loudly at startup, and never chosen for you.

## `shoal serve` -- the saving, without touching your code

```console
$ shoal serve myproject.wsgi:application --dry-run

  interpreter    GIL build - threads share memory but not CPU
  cores          8 (cgroup v2 quota)
  application    WSGI
  strategy       preforked + preload
  topology       8 process(es) x 1 thread(s)  = 8 concurrent
  why            pre-fork with preload: shares the interpreter and imports by
                 copy-on-write, measured 70-78% below spawning workers separately

  gunicorn --bind 127.0.0.1:8000 --workers 8 --threads 1 --preload \
           -c /tmp/shoal-xxxx/shoal_gunicorn_conf.py myproject.wsgi:application
```

The generated config is four lines and is the part most people miss:

```python
def when_ready(server):
    gc.freeze()      # before the fork, so the collector stops copying pages apart
```

`--preload` alone is not enough. The collector writes to the header of every
object it visits, so copy-on-write duplicates those pages into each child and
the sharing evaporates within minutes. `gc.freeze()` moves everything imported
so far into a generation the collector never touches.

If you want one heap -- shared caches, a single connection pool -- ask for it
with `--shared-state`, and shoal will tell you plainly that it costs more memory
than pre-fork rather than pretending otherwise.

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
