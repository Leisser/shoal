"""The topology decision is the product. Test every branch of it."""
from __future__ import annotations

import pytest

from shoal._app import AppError, detect_kind, load
from shoal._build import Build
from shoal.serve import Topology, choose_server, plan, render_plan, usable_cores

FT_OFF = Build("3.14.0", freethreaded=True, gil_on=False, subinterpreters=True, platform="linux")
FT_ON = Build("3.14.0", freethreaded=True, gil_on=True, subinterpreters=True, platform="linux")
GIL = Build("3.12.0", freethreaded=False, gil_on=True, subinterpreters=True, platform="linux")


# ------------------------------------------------------------------ topology

def test_freethreaded_collapses_to_one_process():
    t = plan(FT_OFF, cores=8, kind="wsgi")
    assert t.processes == 1, "the entire point is one heap"
    assert t.collapsed


def test_wsgi_gets_more_threads_than_asgi():
    """WSGI blocks per request; ASGI already multiplexes I/O on its loop."""
    assert plan(FT_OFF, 8, "wsgi").threads > plan(FT_OFF, 8, "asgi").threads


def test_asgi_threads_match_cores():
    assert plan(FT_OFF, 8, "asgi").threads == 8


def test_gil_build_falls_back_to_processes():
    t = plan(GIL, cores=8, kind="wsgi")
    assert (t.processes, t.threads) == (8, 1)
    assert not t.collapsed
    assert "GIL build" in t.reason


def test_silently_reenabled_gil_is_treated_as_a_gil_build():
    """The trap: free-threaded build, but an import turned the GIL back on."""
    t = plan(FT_ON, cores=8, kind="wsgi")
    assert (t.processes, t.threads) == (8, 1)
    assert not t.collapsed
    assert "re-enabled" in t.reason


def test_overrides_win_and_are_labelled():
    t = plan(FT_OFF, 8, "wsgi", processes=4, threads=2)
    assert (t.processes, t.threads) == (4, 2)
    assert t.reason == "explicitly overridden"


def test_override_to_single_process_still_counts_as_collapsed():
    assert plan(GIL, 8, "wsgi", processes=1, threads=16).collapsed


def test_single_core_never_yields_zero_workers():
    for build in (FT_OFF, FT_ON, GIL):
        t = plan(build, cores=1, kind="wsgi")
        assert t.processes >= 1 and t.threads >= 1


# -------------------------------------------------------------------- servers

def test_server_choice_expresses_the_topology(monkeypatch):
    monkeypatch.setattr("shoal.serve._which", lambda *n: "gunicorn" if "gunicorn" in n else None)
    c = choose_server("wsgi", Topology(1, 16, "x", True), "app:app", "0.0.0.0", 8000)
    assert c.name == "gunicorn"
    assert "--workers" in c.argv and c.argv[c.argv.index("--workers") + 1] == "1"
    assert c.argv[c.argv.index("--threads") + 1] == "16"
    assert c.argv[-1] == "app:app"


def test_no_server_installed_is_reported_not_crashed(monkeypatch):
    monkeypatch.setattr("shoal.serve._which", lambda *n: None)
    c = choose_server("asgi", Topology(1, 8, "x", True), "app:app", "127.0.0.1", 8000)
    assert c.argv == [] and "no supported server" in c.note


# ------------------------------------------------------------ app detection

def _wsgi(environ, start_response): ...
async def _asgi(scope, receive, send): ...


def test_detect_wsgi_by_signature():
    assert detect_kind(_wsgi) == "wsgi"


def test_detect_asgi_by_signature():
    assert detect_kind(_asgi) == "asgi"


def test_detect_unknown_is_not_guessed():
    assert detect_kind(lambda: None) == "unknown"


def test_load_rejects_a_target_without_a_colon():
    with pytest.raises(AppError, match="module:attribute"):
        load("myproject.wsgi")


def test_load_reports_a_missing_attribute():
    with pytest.raises(AppError, match="no attribute"):
        load("json:not_a_real_attribute")


# ------------------------------------------------------------------ reporting

def test_plan_warns_loudly_when_not_collapsed():
    out = render_plan(GIL, 8, "cpu_count", "wsgi", plan(GIL, 8, "wsgi"),
                      choose_server("wsgi", plan(GIL, 8, "wsgi"), "a:b", "h", 1), "a:b")
    assert "NOT a collapsed fleet" in out


def test_plan_is_quiet_when_collapsed():
    t = plan(FT_OFF, 8, "wsgi")
    out = render_plan(FT_OFF, 8, "cpu_count", "wsgi", t,
                      choose_server("wsgi", t, "a:b", "h", 1), "a:b")
    assert "NOT a collapsed fleet" not in out


def test_usable_cores_is_sane():
    n, why = usable_cores()
    assert n >= 1 and why
