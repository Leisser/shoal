# unfork

**Stop forking thirty-two processes.**

A typical Python service runs dozens of near-identical worker processes because
the GIL left no alternative. Each one duplicates the interpreter, the imports
and the application. Node count, the cluster that schedules them, and the team
that runs the cluster all follow from that duplication.

`unfork` collapses it.

## Status

Pre-alpha. Only `unfork doctor` works today — deliberately, because it answers
the question everything else depends on: *can your application do this at all?*

```console
$ unfork doctor

  unfork doctor   python 3.13.1   darwin

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

```console
$ python bench/collapse.py all --n 32 --imports numpy,django

  mode           total PSS   per unit      spawn
  processes        224.9 MB    28.11 MB     1.36 ms
  threads           28.5 MB     3.56 MB     0.03 ms   87% less than processes
  fork              61.2 MB     7.65 MB     0.33 ms   73% less than processes
  subinterp             --          --          --   died on SIGABRT
```

## Install

```console
pip install unfork
```

Requires Python 3.12+. The full feature set wants free-threaded 3.14.

## Licence

Apache-2.0
