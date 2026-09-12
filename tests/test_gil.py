"""Parsing CPython's GIL warning, and deciding what to tell the user about it.

None of this can be exercised on a GIL build, so the parser is tested against
the wording CPython actually emits.
"""
from __future__ import annotations

import pytest

from shoal.gil import FORCE_VARS, GilReport, find_culprits, render, top_level

# CPython 3.13/3.14 wording, plus plausible variants -- the phrasing has moved
# between releases and we should not be pinned to one sentence.
REAL = ("RuntimeWarning: The global interpreter lock (GIL) has been enabled to load "
        "module 'foo._core', which has not declared that it can run safely without "
        "the GIL. To override this behavior and keep the GIL disabled (at your own "
        "risk), run with PYTHON_GIL=0 or -Xgil=0.")


def test_parses_the_real_cpython_warning():
    assert find_culprits(REAL) == ["foo._core"]


@pytest.mark.parametrize("text,expected", [
    ("The GIL has been enabled to load module 'numpy._core._multiarray_umath'",
     ["numpy._core._multiarray_umath"]),
    ("'psycopg2._psycopg' did not declare support; the GIL was enabled",
     ["psycopg2._psycopg"]),
    ("nothing interesting here", []),
    ("", []),
])
def test_parses_variants(text, expected):
    assert find_culprits(text) == expected


def test_multiple_culprits_keep_order_and_deduplicate():
    text = "\n".join([REAL,
                      REAL.replace("foo._core", "bar._ext"),
                      REAL])           # foo again
    assert find_culprits(text) == ["foo._core", "bar._ext"]


def test_lines_without_gil_are_ignored():
    """Other warnings mention modules too; only GIL lines may blame one."""
    assert find_culprits("DeprecationWarning: module 'old_thing' is deprecated") == []


def test_top_level_strips_submodules():
    assert top_level("numpy._core._multiarray_umath") == "numpy"
    assert top_level("psycopg2") == "psycopg2"


# ------------------------------------------------------------------- verdicts

def test_clean_requires_freethreaded_and_gil_off():
    assert GilReport(True, False).clean
    assert not GilReport(True, True).clean
    assert not GilReport(False, True).clean, "a GIL build is not 'clean', it is irrelevant"


def test_curable_is_only_the_recoverable_case():
    assert GilReport(True, True).curable, "free-threaded with GIL back on is fixable"
    assert not GilReport(True, False).curable, "nothing to cure"
    assert not GilReport(False, True).curable, "a GIL build cannot be cured by us"


def test_force_vars_cover_both_spellings():
    """3.13+ documents PYTHON_GIL; PEP 703 used PYTHONGIL. Set both."""
    assert FORCE_VARS == {"PYTHON_GIL": "0", "PYTHONGIL": "0"}


# ------------------------------------------------------------------ rendering

def test_render_names_the_culprit_and_offers_both_cures():
    out = render(GilReport(True, True, culprits=["foo._core"]), "myapp", tty=False)
    assert "GIL RE-ENABLED" in out
    assert "foo._core" in out
    assert "pip index versions foo" in out, "cure 1: replace the dependency"
    assert "PYTHON_GIL=0" in out, "cure 2: overrule the extension"
    assert "at your own risk" in out.lower() or "unsafe" in out.lower() or "Safe only" in out


def test_render_is_honest_when_cpython_named_nobody():
    out = render(GilReport(True, True, culprits=[]), "myapp", tty=False)
    assert "did not name a module" in out


def test_render_says_nothing_to_do_on_a_gil_build():
    out = render(GilReport(False, True), "myapp", tty=False)
    assert "no free-threading to lose" in out
    assert "GIL RE-ENABLED" not in out


def test_render_reports_a_clean_run():
    out = render(GilReport(True, False), "myapp", tty=False)
    assert "CLEAN" in out and "GIL RE-ENABLED" not in out


def test_render_surfaces_an_import_failure():
    out = render(GilReport(False, None, import_error="ImportError: boom"), "myapp", tty=False)
    assert "import failed" in out and "boom" in out


def test_render_has_no_escape_codes_without_a_tty():
    out = render(GilReport(True, True, culprits=["x"]), "myapp", tty=False)
    assert "\033[" not in out
