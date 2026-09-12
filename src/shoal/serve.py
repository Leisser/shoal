"""`shoal serve` -- run an existing app with the right topology for this build.

This is the whole Tier 1 story and it changes no application code. A WSGI app
served as 32 processes and the same app served as 1 process with 32 threads are
the same program; only the second one shares its heap. Which is correct depends
entirely on whether the GIL is off, so the decision is made here rather than
left in a deployment script written years ago.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

from ._build import Build, detect


def usable_cores() -> tuple[int, str]:
    """Cores this process may actually use -- cgroup quota before cpu_count()."""
    import os

    limits: list[tuple[int, str]] = []
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        if quota != "max":
            limits.append((max(1, int(int(quota) / int(period))), "cgroup v2 quota"))
    except (OSError, ValueError):
        pass
    try:
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        per = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if q > 0:
            limits.append((max(1, q // per), "cgroup v1 quota"))
    except (OSError, ValueError):
        pass
    try:
        limits.append((len(os.sched_getaffinity(0)), "sched affinity"))
    except (AttributeError, OSError):
        pass
    pcc = getattr(os, "process_cpu_count", None)
    if pcc and pcc():
        limits.append((pcc(), "process_cpu_count"))
    if os.cpu_count():
        limits.append((os.cpu_count(), "cpu_count"))
    if not limits:
        return 1, "unknown"
    return min(limits, key=lambda x: x[0])


@dataclass(frozen=True)
class Topology:
    processes: int
    threads: int
    reason: str
    collapsed: bool          # is this actually one shared heap?

    @property
    def concurrency(self) -> int:
        return self.processes * self.threads


def plan(build: Build, cores: int, kind: str,
         processes: int | None = None, threads: int | None = None) -> Topology:
    """Decide the process/thread split. Pure, so it can be tested without a server."""
    if processes is not None or threads is not None:
        p = processes if processes is not None else 1
        t = threads if threads is not None else 1
        return Topology(p, t, "explicitly overridden", collapsed=p == 1 and t > 1)

    if build.collapse_capable:
        # One heap, one copy of every module. Threads carry the parallelism.
        # ASGI already multiplexes I/O on its loop, so it wants threads for CPU
        # parallelism only; WSGI blocks per request and wants headroom above it.
        t = cores if kind == "asgi" else cores * 2
        return Topology(1, t, f"free-threaded, GIL off: one process, {t} threads", True)

    if build.freethreaded and build.gil_on:
        return Topology(cores, 1,
                        "free-threaded build but the GIL is ON -- an import re-enabled "
                        "it, so threads cannot parallelise; falling back to processes",
                        False)

    return Topology(cores, 1,
                    "GIL build: threads cannot run in parallel, so processes it is. "
                    "Run `shoal doctor` to see what a free-threaded build would save",
                    False)


@dataclass(frozen=True)
class ServerChoice:
    name: str
    argv: list[str]
    note: str = ""


def _which(*names: str) -> str | None:
    for n in names:
        if shutil.which(n):
            return n
    return None


def choose_server(kind: str, topo: Topology, target: str,
                  host: str, port: int, prefer: str | None = None) -> ServerChoice:
    """Pick an installed server and express the topology in its own flags."""
    bind = f"{host}:{port}"
    order = ([prefer] if prefer else []) + (
        ["granian", "uvicorn", "gunicorn"] if kind == "asgi"
        else ["gunicorn", "waitress", "granian"])

    for name in order:
        if name == "gunicorn" and _which("gunicorn"):
            argv = ["gunicorn", "--bind", bind,
                    "--workers", str(topo.processes),
                    "--threads", str(topo.threads)]
            if kind == "asgi":
                argv += ["--worker-class", "uvicorn.workers.UvicornWorker"]
            return ServerChoice("gunicorn", argv + [target],
                                "workers/threads map directly onto the topology")

        if name == "granian" and _which("granian"):
            return ServerChoice("granian",
                                ["granian", "--interface", kind,
                                 "--host", host, "--port", str(port),
                                 "--workers", str(topo.processes),
                                 "--blocking-threads", str(topo.threads), target],
                                "Rust core; verify flag names against your granian version")

        if name == "uvicorn" and _which("uvicorn"):
            return ServerChoice("uvicorn",
                                ["uvicorn", "--host", host, "--port", str(port),
                                 "--workers", str(topo.processes), target],
                                "uvicorn has no thread pool of its own; "
                                "threads in the topology are advisory")

        if name == "waitress" and _which("waitress-serve"):
            return ServerChoice("waitress",
                                ["waitress-serve", f"--listen={bind}",
                                 f"--threads={topo.threads}", target],
                                "threads only; ignores the process count")

    return ServerChoice("", [], "no supported server found")


def render_plan(build: Build, cores: int, why: str, kind: str,
                topo: Topology, server: ServerChoice, target: str) -> str:
    L = [""]
    L.append(f"  shoal serve   {target}")
    L.append("")
    L.append(f"  {'interpreter':<14} {build.summary()}")
    L.append(f"  {'cores':<14} {cores} ({why})")
    L.append(f"  {'application':<14} {kind.upper()}")
    L.append(f"  {'topology':<14} {topo.processes} process(es) x {topo.threads} thread(s)"
             f"  = {topo.concurrency} concurrent")
    L.append(f"  {'why':<14} {topo.reason}")
    if server.name:
        L.append(f"  {'server':<14} {server.name} - {server.note}")
        L.append("")
        L.append("  " + " ".join(server.argv))
    else:
        L.append(f"  {'server':<14} none found - install gunicorn, granian, uvicorn or waitress")
    L.append("")
    if not topo.collapsed:
        L.append("  NOTE  this is NOT a collapsed fleet: every process holds its own heap.")
        L.append("        `shoal doctor` will tell you what is standing in the way.")
        L.append("")
    return "\n".join(L)
