"""Probe what a dependency does to the interpreter.

Everything here runs the import in a *separate process*, because the failure
modes are not exceptions.  numpy under a subinterpreter aborts the process with
SIGABRT; an extension without free-thread-safety silently re-enables the GIL.
Neither is catchable in-process, so we fork, ask, and read the answer back.

Detection happens in the child, where the module is actually loaded -- installed
metadata is too often missing or fileless to rely on.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
from dataclasses import dataclass

TIMEOUT = 120


@dataclass
class ModuleReport:
    name: str
    importable: bool = False
    error: str = ""
    version: str = ""
    stdlib: bool = False
    pure_python: bool | None = None         # no compiled extensions at all
    freethreaded_wheel: bool | None = None  # extensions carry a cp3XXt ABI tag
    gil_reenabled: bool = False             # measured, free-threaded builds only
    subinterpreter: str = "untested"        # ok | abort | error: ... | untested

    @property
    def blocks_freethreading(self) -> bool:
        if self.gil_reenabled:
            return True                     # measured: it turned the GIL back on
        if self.stdlib or self.pure_python:
            return False
        return self.freethreaded_wheel is False

    @property
    def verdict(self) -> str:
        if not self.importable:
            return "import failed"
        if self.gil_reenabled:
            return "RE-ENABLES GIL"
        if self.stdlib:
            return "stdlib"
        if self.pure_python:
            return "pure python"
        if self.freethreaded_wheel:
            return "free-threaded wheel"
        if self.freethreaded_wheel is False:
            return "no free-threaded wheel"
        return "unknown"


_CHILD = r'''
import json, os, sys, sysconfig
top = {mod!r}.split(".")[0]
out = {{}}
try:
    import {mod} as _m
    g = getattr(sys, "_is_gil_enabled", None)
    out["ok"] = True
    out["gil"] = g() if g else None
    out["ft_build"] = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    out["stdlib"] = top in getattr(sys, "stdlib_module_names", ())
    out["version"] = str(getattr(_m, "__version__", "") or "")
    if not out["version"]:
        try:
            from importlib import metadata
            out["version"] = metadata.version(top)
        except Exception:
            pass
    exts, f = [], getattr(_m, "__file__", None)
    if f and not out["stdlib"]:
        base = os.path.dirname(f)
        for root, dirs, files in os.walk(base):
            for fn in files:
                if fn.endswith((".so", ".pyd", ".dylib")):
                    exts.append(fn)
            if len(exts) >= 40:
                break
    out["exts"] = exts[:40]
except BaseException as e:
    out = {{"ok": False, "err": f"{{type(e).__name__}}: {{e}}"[:200]}}
print(json.dumps(out))
'''


def _run(code: str) -> tuple[int, str, str]:
    try:
        p = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=TIMEOUT)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -99, "", f"timed out after {TIMEOUT}s"


def _signal_name(rc: int) -> str:
    try:
        return signal.Signals(-rc).name
    except (ValueError, TypeError):
        return f"signal {-rc}"


def _has_freethreaded_tag(exts: list[str]) -> bool:
    """Free-threaded extensions carry a 't' ABI tag.

    Two spellings in the wild:
        _core.cpython-314t-x86_64-linux-gnu.so    (posix)
        mod.cp314t-win_amd64.pyd                  (windows)
    """
    for fn in exts:
        for part in fn.replace("-", ".").split("."):
            if part.startswith("cp") and part.endswith("t") and part[2:-1].isdigit():
                return True                       # cp314t
            if len(part) >= 4 and part.endswith("t") and part[:-1].isdigit():
                return True                       # 314t, from cpython-314t
    return False


def probe_import(module: str) -> ModuleReport:
    r = ModuleReport(name=module)
    rc, out, err = _run(_CHILD.format(mod=module))
    if rc < 0:
        r.error = f"process died on {_signal_name(rc)}"
        return r
    if not out.startswith("{"):
        r.error = (err.splitlines() or ["no output"])[-1][:200]
        return r

    d = json.loads(out)
    if not d.get("ok"):
        err = d.get("err", "unknown")
        r.error = "not installed" if err.startswith("ModuleNotFoundError") else err
        return r

    r.importable = True
    r.stdlib = bool(d.get("stdlib"))
    r.version = "" if r.stdlib else d.get("version", "")
    exts = d.get("exts") or []

    if r.stdlib or not exts:
        r.pure_python = True
    else:
        r.pure_python = False
        r.freethreaded_wheel = _has_freethreaded_tag(exts)

    # A GIL build always reports the GIL as on; only a free-threaded build can
    # tell us anything, and only there does True mean "this import re-enabled it".
    if d.get("ft_build") and d.get("gil") is True:
        r.gil_reenabled = True
    return r


def probe_subinterpreter(module: str) -> str:
    """'ok' | 'abort' | 'error: ...' - a hard abort is the common case."""
    code = (
        "import sys\n"
        "try:\n"
        "    import concurrent.interpreters as I\n"
        "    i = I.create(); run = i.exec\n"
        "except ImportError:\n"
        "    import _interpreters as I\n"
        "    _i = I.create(); run = lambda s: I.run_string(_i, s)\n"
        f"run('import {module}')\n"
        "print('ok')\n"
    )
    rc, out, err = _run(code)
    if rc < 0:
        return "abort"
    if out == "ok":
        return "ok"
    return f"error: {(err.splitlines() or ['unknown'])[-1][:80]}"


def probe(module: str, *, subinterp: bool = True) -> ModuleReport:
    r = probe_import(module)
    if r.importable and subinterp:
        r.subinterpreter = probe_subinterpreter(module)
    return r
