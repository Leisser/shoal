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


# ------------------------------------------------------- the prize, and advice

def test_estimate_scales_with_cores():
    from shoal.gil import estimate

    pz = estimate(rss_mb=250.0, cores=16)
    assert pz.today_mb == 250.0 * 16
    assert pz.collapsed_mb < 251.0, "one copy plus thread stacks, not sixteen copies"
    assert 90 < pz.saved_pct < 100


def test_estimate_declines_to_guess_without_a_measurement():
    from shoal.gil import estimate

    assert estimate(0.0, 8) is None
    assert estimate(250.0, 0) is None


def test_single_core_saves_nothing_and_says_so():
    from shoal.gil import estimate

    pz = estimate(rss_mb=250.0, cores=1)
    assert pz.saved_mb == 0.0, "one process collapsed to one process is not a saving"


def test_render_recommends_replacing_the_dependency_first():
    out = render(GilReport(True, True, culprits=["psycopg2._psycopg"], rss_mb=248.0),
                 "myapp", tty=False, cores=16)
    assert "RECOMMENDED" in out
    assert out.index("RECOMMENDED") < out.index("force-gil-off"), \
        "the safe cure must be presented before the risky one"
    assert "no caveat attached" in out


def test_render_quantifies_what_the_user_gets():
    out = render(GilReport(True, True, culprits=["x"], rss_mb=248.0),
                 "myapp", tty=False, cores=16)
    assert "What you get" in out
    assert "3,968 MB" in out, "today: 16 copies"
    assert "94%" in out
    assert "working set does not collapse" in out, "the estimate must state its limits"


def test_render_omits_the_prize_when_it_cannot_measure():
    out = render(GilReport(True, True, culprits=["x"], rss_mb=0.0),
                 "myapp", tty=False, cores=16)
    assert "What you get" not in out, "no invented numbers"


def test_override_is_framed_as_a_bridge_not_a_destination():
    out = render(GilReport(True, True, culprits=["x"], rss_mb=100.0),
                 "myapp", tty=False, cores=8)
    assert "bridge" in out and "silent" in out


def test_clean_report_points_at_the_next_step():
    out = render(GilReport(True, False, rss_mb=100.0), "myapp", tty=False, cores=8)
    assert "CLEAN" in out and "shoal serve" in out
