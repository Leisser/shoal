"""Measuring what preloading is worth for one application."""
from __future__ import annotations

import pytest

from shoal.measure import Result, measure, render


def test_saving_is_the_gap_between_the_two_fleets():
    r = Result(workers=8, spawned_kb=132_000, preforked_kb=40_000)
    assert r.saved_kb == 92_000
    assert r.saved_pct == pytest.approx(69.7, abs=0.1)


def test_a_preforked_fleet_that_costs_more_reports_no_saving_not_a_negative():
    r = Result(workers=8, spawned_kb=40_000, preforked_kb=50_000)
    assert r.saved_kb == 0
    assert r.saved_pct == 0.0


def test_empty_measurement_does_not_divide_by_zero():
    assert Result(8, 0, 0).saved_pct == 0.0


def test_render_leads_with_the_users_own_number():
    out = render(Result(8, 132_000, 40_000), "myapp:app", tty=False)
    assert "YOUR SAVING" in out
    assert "89.8 MB (70%)" in out
    assert "shoal serve myapp:app" in out, "must say how to take the saving"


def test_render_states_what_it_did_not_measure():
    out = render(Result(8, 132_000, 40_000), "myapp:app", tty=False)
    assert "working set does not share" in out


def test_render_is_honest_when_there_is_nothing_to_share():
    out = render(Result(8, 40_000, 40_000), "tiny:app", tty=False)
    assert "No saving here" in out
    assert "YOUR SAVING" not in out


def test_render_surfaces_the_error_rather_than_printing_zeros():
    out = render(Result(8, 0, 0, error="ImportError: boom"), "bad:app", tty=False)
    assert "could not measure" in out and "boom" in out
    assert "YOUR SAVING" not in out


def test_off_linux_refuses_rather_than_guessing(monkeypatch):
    monkeypatch.setattr("shoal.measure.LINUX", False)
    r = measure("json:dumps", workers=2)
    assert r.error and "Linux" in r.error


def test_a_bad_target_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr("shoal.measure.LINUX", True)
    r = measure("definitely_not_a_module_xyz:thing", workers=2)
    assert r.error, "an unimportable target must come back as an error, not an exception"


def test_render_has_no_escape_codes_without_a_tty():
    assert "\033[" not in render(Result(8, 132_000, 40_000), "a:b", tty=False)
