"""The doctor's job is to be right about the interpreter. Test that hard."""
from __future__ import annotations

import sys

import pytest

from shoal import _build
from shoal.doctor import diagnose, render
from shoal.probe import ModuleReport, _has_freethreaded_tag, probe


# ----------------------------------------------------------------- build facts

def test_build_detection_is_self_consistent():
    b = _build.detect()
    assert b.version == sys.version.split()[0]
    assert b.freethreaded == bool(__import__("sysconfig").get_config_var("Py_GIL_DISABLED"))
    if not b.freethreaded:
        assert not b.collapse_capable, "a GIL build can never be collapse-capable"


def test_collapse_capable_requires_gil_actually_off():
    off = _build.Build("3.14.0", freethreaded=True, gil_on=False,
                       subinterpreters=True, platform="linux")
    on = _build.Build("3.14.0", freethreaded=True, gil_on=True,
                      subinterpreters=True, platform="linux")
    gil = _build.Build("3.12.0", freethreaded=False, gil_on=True,
                       subinterpreters=True, platform="linux")
    assert off.collapse_capable
    assert not on.collapse_capable, "GIL re-enabled at runtime must not count as capable"
    assert not gil.collapse_capable


# ------------------------------------------------------------------ ABI tags

@pytest.mark.parametrize("name,expected", [
    ("_core.cpython-314t-x86_64-linux-gnu.so", True),
    ("_core.cpython-313t-darwin.so", True),
    ("_core.cpython-314-x86_64-linux-gnu.so", False),
    ("_core.cpython-312-darwin.so", False),
    ("mod.cp314t-win_amd64.pyd", True),
    ("mod.cp314-win_amd64.pyd", False),
    ("plain.so", False),
])
def test_freethreaded_abi_tag_detection(name, expected):
    assert _has_freethreaded_tag([name]) is expected


# --------------------------------------------------------------- verdict logic

def test_stdlib_never_blocks():
    r = ModuleReport(name="json", importable=True, stdlib=True, pure_python=True)
    assert not r.blocks_freethreading
    assert r.verdict == "stdlib"


def test_pure_python_never_blocks():
    r = ModuleReport(name="attrs", importable=True, pure_python=True)
    assert not r.blocks_freethreading


def test_extension_without_freethreaded_wheel_blocks():
    r = ModuleReport(name="numpy", importable=True, pure_python=False,
                     freethreaded_wheel=False)
    assert r.blocks_freethreading
    assert r.verdict == "no free-threaded wheel"


def test_measured_gil_reenable_dominates_every_other_signal():
    r = ModuleReport(name="x", importable=True, pure_python=True,
                     freethreaded_wheel=True, gil_reenabled=True)
    assert r.blocks_freethreading, "a measured re-enable outranks wheel inspection"
    assert r.verdict == "RE-ENABLES GIL"


# ------------------------------------------------------------ probes (real)

def test_probe_stdlib_module():
    r = probe("json", subinterp=False)
    assert r.importable and r.stdlib and not r.blocks_freethreading


def test_probe_missing_module_reports_cleanly():
    r = probe("definitely_not_a_real_module_xyz", subinterp=False)
    assert not r.importable
    assert r.error == "not installed"


def test_probe_survives_a_module_that_aborts_the_process():
    """numpy SIGABRTs under subinterpreters. The harness must live through it."""
    pytest.importorskip("numpy")
    r = probe("numpy", subinterp=True)
    assert r.importable, "the plain import must still succeed"
    assert r.subinterpreter in {"ok", "abort"} or r.subinterpreter.startswith("error:")


def test_gil_build_never_reports_a_reenable():
    """On a GIL build _is_gil_enabled() is always True; that is not a re-enable."""
    if _build.is_freethreaded_build():
        pytest.skip("only meaningful on a GIL build")
    r = probe("json", subinterp=False)
    assert not r.gil_reenabled


# ------------------------------------------------------------------ rendering

def test_render_is_plain_text_without_a_tty():
    out = render(diagnose(["json"], subinterp=False), tty=False)
    assert "\033[" not in out, "no escape codes when not a tty"
    assert "shoal doctor" in out


def test_render_names_the_blocker():
    d = diagnose(["json"], subinterp=False)
    d.modules.append(ModuleReport(name="badlib", importable=True,
                                  pure_python=False, freethreaded_wheel=False))
    out = render(d, tty=False)
    assert "BLOCKED" in out and "badlib" in out
