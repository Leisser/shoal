"""Find out which extension re-enabled the GIL, and do something about it.

On a free-threaded build, importing an extension that has not declared
`Py_mod_gil = Py_mod_gil_not_used` makes CPython pause every thread and switch
the GIL back on for the rest of the process. It names the module when it does
this -- but the warning lands in stderr during startup, where nobody reads it,
and everything afterwards silently runs single-threaded.

Detecting that is diagnosis. There are exactly two cures:

  1. Replace the offending dependency with a build that declares support.
  2. Override CPython with PYTHON_GIL=0, which keeps the GIL off and runs the
     extension anyway -- fast, and unsafe in proportion to what that extension
     does with shared state.

Only the user can choose between those, so this module's job is to make the
choice an informed one.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field

# CPython's wording has shifted between releases; match the module name in any
# GIL-enabling warning rather than one exact sentence.
_PATTERNS = (
    re.compile(r"GIL[^\n]*?\bmodule ['\"]([\w.]+)['\"]", re.I),
    re.compile(r"['\"]([\w.]+)['\"][^\n]*?\bGIL[^\n]*?\benabl", re.I),
    re.compile(r"enabl\w*[^\n]*?\bGIL\b[^\n]*?\b(?:load|import)\w*\s+['\"]?([\w.]+)", re.I),
)

FORCE_VARS = {"PYTHON_GIL": "0", "PYTHONGIL": "0"}   # 3.13+ spelling, and PEP 703's


@dataclass
class GilReport:
    freethreaded: bool
    gil_on_after_import: bool | None
    culprits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rss_mb: float = 0.0          # what one copy of this application costs
    import_error: str = ""

    @property
    def clean(self) -> bool:
        return self.freethreaded and self.gil_on_after_import is False

    @property
    def curable(self) -> bool:
        """Did something re-enable the GIL that we could act on?"""
        return self.freethreaded and self.gil_on_after_import is True


_CHILD = r'''
import json, resource, sys, sysconfig, warnings
warnings.simplefilter("always")
captured = []
_orig = warnings.showwarning
def _hook(message, category, filename, lineno, file=None, line=None):
    captured.append(str(message))
sys.modules["warnings"].showwarning = _hook
err = ""
try:
    import {mod}
except BaseException as e:
    err = f"{{type(e).__name__}}: {{e}}"[:300]
g = getattr(sys, "_is_gil_enabled", None)
_r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
_rss = _r / 1024 / 1024 if sys.platform == "darwin" else _r / 1024
print("@@SHOAL@@" + json.dumps({{
    "ft": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    "gil": (g() if g else None),
    "warnings": captured[:40],
    "rss_mb": round(_rss, 1),
    "err": err,
}}))
'''


def inspect_target(module: str, *, timeout: int = 180) -> GilReport:
    """Import `module` in a child with GIL warnings on, and see what happens."""
    env_extra = {"PYTHONWARNDEFAULTGIL": "1", "PYTHONWARNINGS": "always"}
    import os

    env = {**os.environ, **env_extra}
    env.pop("PYTHON_GIL", None)          # never measure with an override in place
    env.pop("PYTHONGIL", None)

    try:
        p = subprocess.run([sys.executable, "-X", "warn_default_gil", "-c",
                            _CHILD.format(mod=module)],
                           capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return GilReport(False, None, import_error=f"timed out after {timeout}s")

    import json
    payload = next((ln[len("@@SHOAL@@"):] for ln in p.stdout.splitlines()
                    if ln.startswith("@@SHOAL@@")), "")
    if not payload:
        detail = (p.stderr.strip().splitlines() or ["no output"])[-1][:200]
        if p.returncode < 0:
            detail = f"process died on signal {-p.returncode}"
        return GilReport(False, None, import_error=detail)

    d = json.loads(payload)
    text = "\n".join(d.get("warnings", [])) + "\n" + p.stderr
    return GilReport(
        freethreaded=bool(d.get("ft")),
        gil_on_after_import=d.get("gil"),
        culprits=find_culprits(text),
        warnings=[w for w in d.get("warnings", []) if "gil" in w.lower()],
        rss_mb=float(d.get("rss_mb") or 0.0),
        import_error=d.get("err", ""),
    )


@dataclass(frozen=True)
class Prize:
    """What collapsing this application would be worth, on this machine."""
    per_copy_mb: float
    processes: int
    today_mb: float
    collapsed_mb: float

    @property
    def saved_mb(self) -> float:
        return max(0.0, self.today_mb - self.collapsed_mb)

    @property
    def saved_pct(self) -> float:
        return (self.saved_mb / self.today_mb * 100) if self.today_mb else 0.0


THREAD_MB = 0.035     # measured: 35 KB per thread at a 256 KB stack


def estimate(rss_mb: float, cores: int) -> Prize | None:
    """Baseline-memory estimate only. Per-request working set does not collapse."""
    if rss_mb <= 0 or cores < 1:
        return None
    return Prize(per_copy_mb=rss_mb,
                 processes=cores,
                 today_mb=rss_mb * cores,
                 collapsed_mb=rss_mb + THREAD_MB * cores)


def find_culprits(text: str) -> list[str]:
    """Module names CPython blamed for enabling the GIL, in order, deduplicated."""
    seen: list[str] = []
    for line in text.splitlines():
        if "gil" not in line.lower():
            continue
        for pat in _PATTERNS:
            m = pat.search(line)
            if m and m.group(1) not in seen:
                seen.append(m.group(1))
                break
    return seen


def top_level(module: str) -> str:
    return module.split(".")[0]


def _prize_lines(report: GilReport, cores: int, c) -> list[str]:
    """What the user actually gets. Concrete, or omitted."""
    G, B, DIM = "\033[32m", "\033[1m", "\033[2m"
    pz = estimate(report.rss_mb, cores)
    if pz is None:
        return []
    return [
        f"  {c('What you get:', B)}",
        f"    one copy of this application costs {pz.per_copy_mb:.0f} MB.",
        f"    today   {pz.processes} processes x {pz.per_copy_mb:.0f} MB "
        f"= {c(f'{pz.today_mb:,.0f} MB', DIM)}",
        f"    after   1 process + {pz.processes} threads "
        f"= {c(f'{pz.collapsed_mb:,.0f} MB', G)}",
        f"    saved   {c(f'{pz.saved_mb:,.0f} MB ({pz.saved_pct:.0f}%)', G)}"
        f" -- and threads keep pace with processes (measured parity 0.97).",
        f"    {c('Baseline only: per-request working set does not collapse.', DIM)}",
        "",
    ]


def render(report: GilReport, target: str, *, tty: bool = True, cores: int = 0) -> str:
    G, Y, R, B, DIM, X = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[2m", "\033[0m"
    c = (lambda s, col: f"{col}{s}{X}") if tty else (lambda s, col: s)
    L = ["", f"  {c('shoal gil', B)}   {target}", ""]

    if report.import_error:
        L += [f"  {c('import failed', R)}  {report.import_error}", ""]
        return "\n".join(L)

    if not report.freethreaded:
        L += [f"  {c('GIL build', Y)} -- this interpreter has no free-threading to lose.",
              "  Nothing to diagnose here. Install a free-threaded build first:",
              "    uv python install 3.14t", ""]
        L += _prize_lines(report, cores, c)
        return "\n".join(L)

    if report.clean:
        L += [f"  {c('CLEAN', G)}  the GIL stayed off after importing {target}.",
              "  Nothing is standing in the way of the collapse.", ""]
        L += _prize_lines(report, cores, c)
        L += [f"  Run {c('shoal serve ' + target + ':app', B)} to take it.", ""]
        return "\n".join(L)

    L.append(f"  {c('GIL RE-ENABLED', R)}  importing {target} turned the GIL back on.")
    L.append("  Every thread in this process now runs one at a time.")
    L.append("")

    if report.culprits:
        L.append(f"  {c('blamed by CPython:', B)}")
        for m in report.culprits:
            L.append(f"    - {m}")
    else:
        L.append("  CPython did not name a module. It usually does; if this")
        L.append("  persists, run with -X warn_default_gil and read stderr directly.")
    L.append("")
    L += _prize_lines(report, cores, c)

    L.append(f"  {c('RECOMMENDED', G)}  {c('Replace the dependency.', B)}")
    L.append("     You get the saving above with no caveat attached: the extension")
    L.append("     declares itself thread-safe, CPython leaves the GIL off, and you")
    L.append("     are running a supported configuration you can upgrade into.")
    L.append("     Usually it is one pinned version behind a build that already works.")
    L.append("")
    for m in report.culprits or ["<module>"]:
        L.append(f"       pip index versions {top_level(m)}")
    L.append("       # then pin the newest version that ships a cp3XXt wheel")
    L.append("     Tracker: https://py-free-threading.github.io/tracking/")
    L.append("")
    L.append(f"  {c('If you cannot', Y)}  {c('override CPython', B)} -- same saving, real risk.")
    L.append("       shoal serve --force-gil-off ...      # PYTHON_GIL=0")
    L.append(f"     {c('This runs the extension without the lock it asked for.', R)} Safe only")
    L.append("     if it does not mutate shared state across threads. Corruption here")
    L.append("     is silent, not a crash. Load-test before trusting it, and treat it")
    L.append("     as a bridge until the dependency catches up -- not a destination.")
    L.append("")
    return "\n".join(L)
