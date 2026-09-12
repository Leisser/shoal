"""`shoal doctor` - can this application collapse, and what is stopping it?

Answers three questions, in the order that matters:
  1. Is this interpreter capable of the collapse at all?
  2. Which dependencies would silently prevent it?
  3. Which of them can also use the isolation tier?
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from . import _build
from .probe import ModuleReport, probe

# Wide, dull, and load-bearing: the things most Python services import.
COMMON = [
    "numpy", "pandas", "scipy", "pyarrow", "polars",
    "django", "fastapi", "starlette", "flask", "pydantic",
    "sqlalchemy", "psycopg", "psycopg2", "asyncpg", "redis",
    "granian", "uvicorn", "gunicorn", "celery", "saq",
    "lxml", "PIL", "cryptography", "orjson", "msgspec", "yaml",
]


@dataclass
class Diagnosis:
    build: _build.Build
    modules: list[ModuleReport] = field(default_factory=list)

    @property
    def blockers(self) -> list[ModuleReport]:
        return [m for m in self.modules if m.blocks_freethreading]

    @property
    def aborts(self) -> list[ModuleReport]:
        return [m for m in self.modules if m.subinterpreter == "abort"]

    @property
    def ready(self) -> bool:
        return self.build.freethreaded and not self.blockers


def diagnose(modules: list[str] | None = None, *, subinterp: bool = True) -> Diagnosis:
    names = modules if modules is not None else COMMON
    found = []
    for name in names:
        r = probe(name, subinterp=subinterp)
        # skip things that simply are not installed, unless explicitly asked for
        if modules is None and not r.importable and "No module named" in r.error:
            continue
        found.append(r)
    return Diagnosis(build=_build.detect(), modules=found)


# --------------------------------------------------------------- presentation

G, Y, R, DIM, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def _c(s: str, colour: str, tty: bool) -> str:
    return f"{colour}{s}{X}" if tty else s


def render(d: Diagnosis, *, tty: bool = True) -> str:
    L: list[str] = []
    b = d.build
    L.append("")
    L.append(f"  {_c('shoal doctor', B, tty)}   python {b.version}   {b.platform}")
    L.append("")

    state = b.summary()
    colour = G if b.collapse_capable else (R if not b.freethreaded else Y)
    L.append(f"  {'interpreter':<16} {_c(state, colour, tty)}")
    if b.gil_on is None:
        L.append(f"  {'':<16} {_c('(this build cannot report GIL state)', DIM, tty)}")
    L.append(f"  {'subinterpreters':<16} {'available' if b.subinterpreters else 'not available'}")
    L.append("")

    if not d.modules:
        L.append(f"  {_c('no dependencies probed', DIM, tty)}")
        return "\n".join(L) + "\n"

    L.append(f"  {'dependency':<16} {'version':<10} {'free-threading':<23} isolation tier")
    L.append("  " + "-" * 74)
    for m in sorted(d.modules, key=lambda x: (not x.blocks_freethreading, x.name)):
        if not m.importable:
            L.append(f"  {m.name:<16} {'-':<10} {_c(m.error[:23], DIM, tty)}")
            continue
        v = m.verdict
        col = R if m.blocks_freethreading else (G if "wheel" in v or v == "pure python" else Y)
        iso = {"ok": "ok", "abort": _c("ABORTS PROCESS", R, tty),
               "untested": _c("-", DIM, tty)}.get(m.subinterpreter,
                                                  _c(m.subinterpreter[:20], Y, tty))
        pad = " " * max(0, 23 - len(v))
        L.append(f"  {m.name:<16} {m.version[:9]:<10} {_c(v, col, tty)}{pad} {iso}")
    L.append("")

    # ------------------------------------------------------------- the verdict
    if d.blockers:
        names = ", ".join(m.name for m in d.blockers)
        L.append(f"  {_c('BLOCKED', R, tty)}  {len(d.blockers)} dependency(s) prevent free-threading: {names}")
        L.append(f"  {'':<9} On a free-threaded build these re-enable the GIL for the whole")
        L.append(f"  {'':<9} process - silently, with no error. You would measure no gain.")
    elif not b.freethreaded:
        L.append(f"  {_c('READY', G, tty)}    No dependency blocks free-threading.")
        L.append("")
        L.append(f"  {'':<9} {_c('RECOMMENDED', G, tty)}  install a free-threaded interpreter.")
        L.append(f"  {'':<9}   uv python install 3.14t     (or python.org 3.14 'free-threaded')")
        L.append("")
        L.append(f"  {'':<9} What that buys you: one process holding one copy of your")
        L.append(f"  {'':<9} application instead of one per core, with threads keeping")
        L.append(f"  {'':<9} pace with processes (measured parity 0.97). For the figure")
        L.append(f"  {'':<9} in megabytes on this machine:")
        L.append(f"  {'':<9}   shoal gil <your.app.module>")
        L.append(f"  {'':<9} Then `shoal serve` takes it, with no code changes.")
    elif b.gil_on:
        L.append(f"  {_c('GIL RE-ENABLED', R, tty)}  free-threaded build, but something turned the GIL on.")
        L.append(f"  {'':<9} Re-run inside your app's venv so its imports are probed too.")
    else:
        L.append(f"  {_c('COLLAPSE AVAILABLE', G, tty)}  free-threaded, GIL off, no blockers.")
        L.append(f"  {'':<9} Serve with threads instead of processes to realise it.")

    if d.aborts:
        names = ", ".join(m.name for m in d.aborts)
        L.append("")
        L.append(f"  {_c('note', DIM, tty)}     isolation tier unavailable: {names} abort under subinterpreters.")
        L.append(f"  {'':<9} Threads still carry the full memory collapse; only per-heap GC is lost.")
    L.append("")
    return "\n".join(L) + "\n"
