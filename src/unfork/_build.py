"""Facts about the interpreter we are running on.

The whole thesis rests on the free-threaded build, so knowing precisely what we
are on -- and whether the GIL is *actually* off right now -- is load-bearing.
"""
from __future__ import annotations

import sys
import sysconfig
from dataclasses import dataclass


def is_freethreaded_build() -> bool:
    """True if this interpreter was *built* without the GIL (python3.13t+)."""
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def gil_enabled() -> bool | None:
    """Is the GIL active *right now*?  None if the interpreter cannot say.

    On a free-threaded build this can flip to True at runtime: importing a C
    extension that is not marked free-thread-safe re-enables the GIL for the
    lifetime of the process, silently and without error.  That makes the value
    of this call meaningless until after the application's imports have run.
    """
    probe = getattr(sys, "_is_gil_enabled", None)
    return probe() if probe is not None else None


def subinterpreters_available() -> bool:
    for mod in ("concurrent.interpreters", "_interpreters", "_xxsubinterpreters"):
        try:
            __import__(mod)
            return True
        except ImportError:
            continue
    return False


@dataclass(frozen=True)
class Build:
    version: str
    freethreaded: bool
    gil_on: bool | None
    subinterpreters: bool
    platform: str

    @property
    def collapse_capable(self) -> bool:
        """Can this interpreter actually collapse a fleet of processes?"""
        return self.freethreaded and self.gil_on is False

    def summary(self) -> str:
        if not self.freethreaded:
            return "GIL build - threads share memory but not CPU; no collapse available"
        if self.gil_on:
            return "free-threaded build, but the GIL is ON - an import re-enabled it"
        return "free-threaded, GIL off - collapse available"


def detect() -> Build:
    return Build(
        version=sys.version.split()[0],
        freethreaded=is_freethreaded_build(),
        gil_on=gil_enabled(),
        subinterpreters=subinterpreters_available(),
        platform=sys.platform,
    )
