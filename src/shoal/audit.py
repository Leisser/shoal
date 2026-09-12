"""`shoal audit` -- is this running deployment sharing memory, and what is it costing?

`doctor` asks whether an application *could* collapse. This asks what a server
already running on this machine is actually doing, which is the more useful
question now that the largest measured saving comes from how workers are started
rather than which interpreter they run on.

Two signals, and they corroborate each other:

  declared   the master's command line and config -- is --preload set, is
             gc.freeze() called before the fork
  measured   PSS against RSS across the process tree -- pages shared between
             workers are counted once in PSS and N times in RSS, so the gap
             between them is the sharing, whatever anyone declared

Linux only: /proc/<pid>/smaps_rollup is the only place this is knowable.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

LINUX = sys.platform.startswith("linux")

SERVERS = ("gunicorn", "uwsgi", "uvicorn", "granian", "waitress", "hypercorn")


@dataclass
class Proc:
    pid: int
    ppid: int
    cmdline: list[str]
    pss_kb: int = 0
    rss_kb: int = 0
    shared_kb: int = 0
    private_kb: int = 0

    @property
    def name(self) -> str:
        for part in self.cmdline:
            base = os.path.basename(part)
            for s in SERVERS:
                if base == s or base.startswith(s + "-"):
                    return s
        return os.path.basename(self.cmdline[0]) if self.cmdline else "?"


@dataclass
class Audit:
    master: Proc | None = None
    workers: list[Proc] = field(default_factory=list)
    server: str = ""
    preload_declared: bool | None = None     # None: cannot tell for this server
    freeze_declared: bool = False
    config_path: str = ""
    note: str = ""

    @property
    def total_pss_kb(self) -> int:
        return sum(p.pss_kb for p in self.all_procs)

    @property
    def total_rss_kb(self) -> int:
        return sum(p.rss_kb for p in self.all_procs)

    @property
    def all_procs(self) -> list[Proc]:
        return ([self.master] if self.master else []) + self.workers

    @property
    def sharing_ratio(self) -> float:
        """0.0 = every worker holds a private copy; 1.0 = everything is shared."""
        if not self.total_rss_kb:
            return 0.0
        return 1.0 - (self.total_pss_kb / self.total_rss_kb)

    @property
    def private_per_worker_kb(self) -> float:
        return (sum(w.private_kb for w in self.workers) / len(self.workers)
                if self.workers else 0.0)


# ------------------------------------------------------------------ /proc

def read_mem(pid: int) -> tuple[int, int, int, int]:
    """(pss, rss, shared, private) in kB from smaps_rollup."""
    vals = {"Pss": 0, "Rss": 0, "Shared_Clean": 0, "Shared_Dirty": 0,
            "Private_Clean": 0, "Private_Dirty": 0}
    try:
        for line in open(f"/proc/{pid}/smaps_rollup"):
            k, _, rest = line.partition(":")
            if k in vals:
                vals[k] = int(rest.split()[0])
    except OSError:
        return 0, 0, 0, 0
    return (vals["Pss"], vals["Rss"],
            vals["Shared_Clean"] + vals["Shared_Dirty"],
            vals["Private_Clean"] + vals["Private_Dirty"])


def read_proc(pid: int) -> Proc | None:
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
        cmd = [c.decode("utf-8", "replace") for c in raw.split(b"\0") if c]
        ppid = 0
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("PPid:"):
                ppid = int(line.split()[1])
                break
    except OSError:
        return None
    if not cmd:
        return None
    p = Proc(pid=pid, ppid=ppid, cmdline=cmd)
    p.pss_kb, p.rss_kb, p.shared_kb, p.private_kb = read_mem(pid)
    return p


def all_procs() -> list[Proc]:
    out = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            p = read_proc(int(entry))
            if p:
                out.append(p)
    return out


def find_masters(procs: list[Proc]) -> list[Proc]:
    """A master is a server process whose children are also server processes."""
    by_ppid: dict[int, list[Proc]] = {}
    for p in procs:
        by_ppid.setdefault(p.ppid, []).append(p)

    masters = []
    for p in procs:
        if p.name not in SERVERS:
            continue
        kids = [k for k in by_ppid.get(p.pid, []) if k.name in SERVERS or k.ppid == p.pid]
        if kids and p.ppid not in {q.pid for q in procs if q.name in SERVERS}:
            masters.append(p)
    return masters


# ----------------------------------------------------------- declared config

_PRELOAD_FLAGS = ("--preload", "--preload-app")


def inspect_declaration(master: Proc) -> tuple[bool | None, bool, str, str]:
    """(preload declared, gc.freeze declared, config path, note)."""
    argv = master.cmdline
    server = master.name

    conf = ""
    for flag in ("-c", "--config"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                conf = argv[i + 1].removeprefix("file:")

    freeze = False
    conf_preload = False
    if conf and os.path.exists(conf):
        try:
            body = open(conf, encoding="utf-8", errors="replace").read()
            freeze = "gc.freeze()" in body
            conf_preload = bool(re.search(r"^\s*preload_app\s*=\s*True", body, re.M))
        except OSError:
            pass

    if server == "gunicorn":
        declared = any(f in argv for f in _PRELOAD_FLAGS) or conf_preload
        return declared, freeze, conf, ""
    if server == "uwsgi":
        # uwsgi preloads by default; --lazy-apps turns it off
        return ("--lazy-apps" not in argv), freeze, conf, "uwsgi preloads unless --lazy-apps"
    if server in ("uvicorn", "hypercorn"):
        return False, freeze, conf, f"{server} has no preload mode: every worker imports separately"
    return None, freeze, conf, f"cannot determine preload for {server}"


# ---------------------------------------------------------------- assemble

def audit_pid(pid: int | None = None) -> Audit:
    if not LINUX:
        return Audit(note="sharing is only knowable from /proc/<pid>/smaps_rollup")
    procs = all_procs()
    if pid is not None:
        master = next((p for p in procs if p.pid == pid), None)
        if master is None:
            return Audit(note=f"no process {pid}")
    else:
        masters = find_masters(procs)
        if not masters:
            return Audit(note="no running server found "
                              f"({', '.join(SERVERS)}); pass --pid to point at one")
        master = max(masters, key=lambda m: sum(
            1 for p in procs if p.ppid == m.pid))

    workers = [p for p in procs if p.ppid == master.pid]
    declared, freeze, conf, note = inspect_declaration(master)
    return Audit(master=master, workers=workers, server=master.name,
                 preload_declared=declared, freeze_declared=freeze,
                 config_path=conf, note=note)


# --------------------------------------------------------------- reporting

# From this project's own benchmark (FINDINGS.md): 8 workers, real app, PSS
# under load. Spawned 132 MB -> preforked 40 MB on 3.14; 233 -> 51 on 3.14t.
BENCH_SAVING = (0.70, 0.78)


def estimate_saving_kb(a: Audit) -> tuple[int, int]:
    """Range of PSS a non-sharing deployment could expect to give back."""
    lo, hi = BENCH_SAVING
    return int(a.total_pss_kb * lo), int(a.total_pss_kb * hi)


def render(a: Audit, *, tty: bool = True) -> str:
    G, Y, R, B, DIM, X = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[2m", "\033[0m"
    c = (lambda s, col: f"{col}{s}{X}") if tty else (lambda s, col: s)
    L = ["", f"  {c('shoal audit', B)}", ""]

    if not LINUX:
        L += [f"  {c('needs Linux', Y)} -- sharing is only knowable from "
              "/proc/<pid>/smaps_rollup.", ""]
        return "\n".join(L)
    if a.master is None:
        L += [f"  {a.note}", ""]
        return "\n".join(L)

    n = len(a.workers)
    L.append(f"  {'server':<14} {a.server}  (master {a.master.pid}, {n} worker"
             f"{'s' if n != 1 else ''})")
    if a.config_path:
        L.append(f"  {'config':<14} {a.config_path}")
    L.append("")

    # --- what it says it does
    if a.preload_declared is True:
        L.append(f"  {'preload':<14} {c('declared', G)}")
    elif a.preload_declared is False:
        L.append(f"  {'preload':<14} {c('NOT set', R)}"
                 + (f"  -- {a.note}" if a.note else ""))
    else:
        L.append(f"  {'preload':<14} {c('unknown', Y)}  -- {a.note}")
    L.append(f"  {'gc.freeze()':<14} "
             + (c("declared", G) if a.freeze_declared else c("NOT set", R)))
    L.append("")

    # --- what it actually does
    if not n:
        L += [f"  {c('no workers', Y)} -- nothing to compare; is the server idle?", ""]
        return "\n".join(L)

    L.append(f"  {'memory':<14} {a.total_pss_kb/1024:.1f} MB actually used (PSS)")
    L.append(f"  {'':<14} {a.total_rss_kb/1024:.1f} MB if you count shared pages "
             "once per worker (RSS)")
    L.append(f"  {'sharing':<14} {a.sharing_ratio*100:.0f}% of resident pages are shared")
    L.append(f"  {'':<14} {a.private_per_worker_kb/1024:.1f} MB private per worker")
    L.append("")

    sharing_looks_real = a.sharing_ratio >= 0.40

    if a.preload_declared and a.freeze_declared and sharing_looks_real:
        L.append(f"  {c('GOOD', G)}  preloading, frozen, and the pages are staying shared.")
        L.append("        Nothing to change here.")
    elif a.preload_declared and not a.freeze_declared:
        L.append(f"  {c('DECAYING', Y)}  preloading, but not calling gc.freeze().")
        L.append("        The collector writes to the header of every object it visits,")
        L.append("        so copy-on-write duplicates those pages into each worker and")
        L.append("        the sharing you preloaded for is gone within minutes.")
        L.append(f"        Sharing is {a.sharing_ratio*100:.0f}% now; expect it to fall.")
        L.append("")
        L.append(f"  {c('RECOMMENDED', G)}  add four lines to your gunicorn config:")
        L.append("")
        L.append("            import gc")
        L.append("            def when_ready(server):")
        L.append("                gc.freeze()")
        L.append("")
        L.append("        Or let shoal generate it:  shoal serve <app> --dry-run")
    elif a.preload_declared is False:
        lo, hi = estimate_saving_kb(a)
        L.append(f"  {c('NOT SHARING', R)}  every worker imported the application for itself.")
        L.append("")
        L.append(f"  {c('RECOMMENDED', G)}  preload, and freeze before the fork.")
        L.append(f"        On this project's benchmark that returned 70-78% of PSS.")
        L.append(f"        Here that would be roughly "
                 f"{c(f'{lo/1024:.0f}-{hi/1024:.0f} MB', G)} back, leaving "
                 f"{(a.total_pss_kb-hi)/1024:.0f}-{(a.total_pss_kb-lo)/1024:.0f} MB.")
        L.append(f"        {c('That range is from our benchmark, not your app.', DIM)}")
        L.append("")
        L.append("            shoal serve <your.wsgi:app> --dry-run")
        L.append("")
        L.append("        prints the exact command, with the freeze config generated.")
    else:
        L.append(f"  {c('UNCLEAR', Y)}  cannot read this server's preload setting.")
        L.append(f"        Measured sharing is {a.sharing_ratio*100:.0f}%. Below about")
        L.append("        40% the workers are probably not sharing an import.")
    L.append("")
    return "\n".join(L)
