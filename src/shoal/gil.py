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
    import_error: str = ""

    @property
    def clean(self) -> bool:
        return self.freethreaded and self.gil_on_after_import is False

    @property
    def curable(self) -> bool:
        """Did something re-enable the GIL that we could act on?"""
        return self.freethreaded and self.gil_on_after_import is True


_CHILD = r'''
import json, sys, sysconfig, warnings
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
print("@@SHOAL@@" + json.dumps({{
    "ft": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    "gil": (g() if g else None),
    "warnings": captured[:40],
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
        import_error=d.get("err", ""),
    )


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


def render(report: GilReport, target: str, *, tty: bool = True) -> str:
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
        return "\n".join(L)

    if report.clean:
        L += [f"  {c('CLEAN', G)}  the GIL stayed off after importing {target}.",
              "  Nothing is standing in the way of the collapse.", ""]
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

    L.append(f"  {c('Two ways forward.', B)}")
    L.append("")
    L.append(f"  {c('1. Replace the dependency', G)} -- the real fix.")
    for m in report.culprits or ["<module>"]:
        L.append(f"       pip index versions {top_level(m)}    # is there a newer build?")
    L.append("     Compatibility tracker: https://py-free-threading.github.io/tracking/")
    L.append("")
    L.append(f"  {c('2. Override CPython', Y)} -- keeps the collapse, accepts the risk.")
    L.append("       PYTHON_GIL=0 shoal serve ...        # or: shoal serve --force-gil-off")
    L.append(f"     {c('This runs the extension without the GIL it asked for.', R)} Safe only if")
    L.append("     that extension does not mutate shared state from multiple threads.")
    L.append("     Test under load before trusting it.")
    L.append("")
    return "\n".join(L)
