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
# The measured ranking (FINDINGS.md, 8 workers, PSS under load):
#   spawned  132 MB (GIL) / 233 MB (ft)
#   prefork   40 MB       /  51 MB
#   threaded  18 MB       /  78 MB
# Pre-fork with preload wins on both builds. These tests pin that, because the
# project's original assumption was the opposite and it would be easy to drift
# back to it.

def test_default_is_preforked_with_preload_on_a_gil_build():
    t = plan(GIL, cores=8, kind="wsgi")
    assert t.strategy == "preforked"
    assert t.preload, "preload is the whole saving; without it this is the worst option"
    assert (t.processes, t.threads) == (8, 1)


def test_default_is_preforked_with_preload_on_a_free_threaded_build_too():
    """Threads cost MORE than pre-fork here: 78 MB against 51 MB at 8 workers."""
    t = plan(FT_OFF, cores=8, kind="wsgi")
    assert t.strategy == "preforked"
    assert t.preload


def test_every_default_plan_preloads():
    for build in (FT_OFF, FT_ON, GIL):
        for kind in ("wsgi", "asgi"):
            assert plan(build, 8, kind).preload, (build, kind)


def test_threads_only_on_request_and_only_for_shared_state():
    t = plan(FT_OFF, cores=8, kind="wsgi", want_shared_state=True)
    assert t.strategy == "threaded"
    assert t.processes == 1 and t.threads > 1
    assert "shared" in t.reason


def test_shared_state_on_a_gil_build_says_the_threads_will_not_parallelise():
    t = plan(GIL, cores=8, kind="wsgi", want_shared_state=True)
    assert t.strategy == "threaded"
    assert "without running in parallel" in t.reason


def test_wsgi_gets_more_threads_than_asgi_when_threaded():
    """WSGI blocks per request; an ASGI loop already multiplexes I/O."""
    a = plan(FT_OFF, 8, "asgi", want_shared_state=True)
    w = plan(FT_OFF, 8, "wsgi", want_shared_state=True)
    assert w.threads > a.threads
    assert a.threads == 8


def test_a_reenabled_gil_still_preforks_and_says_so():
    t = plan(FT_ON, cores=8, kind="wsgi")
    assert t.strategy == "preforked"
    assert "GIL is on" in t.reason


def test_overrides_keep_preloading():
    """A user pinning worker counts should not silently lose the 70-78%."""
    t = plan(FT_OFF, 8, "wsgi", processes=4, threads=2)
    assert (t.processes, t.threads) == (4, 2)
    assert t.preload, "overriding the topology must not turn off preloading"


def test_override_to_one_process_is_recognised_as_threaded():
    assert plan(GIL, 8, "wsgi", processes=1, threads=16).strategy == "threaded"


def test_single_core_never_yields_zero_workers():
    for build in (FT_OFF, FT_ON, GIL):
        t = plan(build, cores=1, kind="wsgi")
        assert t.processes >= 1 and t.threads >= 1


def test_every_plan_shares_memory_somehow():
    for build in (FT_OFF, FT_ON, GIL):
        assert plan(build, 8, "wsgi").shares_memory


# -------------------------------------------------------------------- servers

def test_server_choice_expresses_the_topology(monkeypatch):
    monkeypatch.setattr("shoal.serve._which", lambda *n: "gunicorn" if "gunicorn" in n else None)
    c = choose_server("wsgi", Topology(1, 16, True, "threaded", "x"),
                      "app:app", "0.0.0.0", 8000)
    assert c.name == "gunicorn"
    assert c.argv[c.argv.index("--workers") + 1] == "1"
    assert c.argv[c.argv.index("--threads") + 1] == "16"
    assert c.argv[-1] == "app:app"


def test_gunicorn_gets_preload_and_the_freeze_config(monkeypatch):
    monkeypatch.setattr("shoal.serve._which", lambda *n: "gunicorn" if "gunicorn" in n else None)
    c = choose_server("wsgi", Topology(8, 1, True, "preforked", "x"),
                      "app:app", "0.0.0.0", 8000)
    assert "--preload" in c.argv
    conf = c.argv[c.argv.index("-c") + 1]
    body = open(conf).read()
    assert "gc.freeze()" in body
    assert "when_ready" in body, "must freeze in the parent, before the fork"


def test_no_preload_means_no_flag(monkeypatch):
    monkeypatch.setattr("shoal.serve._which", lambda *n: "gunicorn" if "gunicorn" in n else None)
    c = choose_server("wsgi", Topology(8, 1, False, "preforked", "x"),
                      "app:app", "0.0.0.0", 8000)
    assert "--preload" not in c.argv


def test_uvicorn_admits_it_cannot_preload(monkeypatch):
    monkeypatch.setattr("shoal.serve._which", lambda *n: "uvicorn" if "uvicorn" in n else None)
    c = choose_server("asgi", Topology(8, 1, True, "preforked", "x"),
                      "app:app", "h", 1)
    assert c.name == "uvicorn"
    assert "cannot preload" in c.note


def test_no_server_installed_is_reported_not_crashed(monkeypatch):
    monkeypatch.setattr("shoal.serve._which", lambda *n: None)
    c = choose_server("asgi", Topology(1, 8, True, "threaded", "x"),
                      "app:app", "127.0.0.1", 8000)
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

def test_plan_explains_the_saving_it_is_taking():
    t = plan(GIL, 8, "wsgi")
    out = render_plan(GIL, 8, "cpu_count", "wsgi", t,
                      choose_server("wsgi", t, "a:b", "h", 1), "a:b")
    assert "gc.freeze()" in out and "70-78%" in out


def test_threaded_plan_warns_it_costs_more_memory():
    t = plan(FT_OFF, 8, "wsgi", want_shared_state=True)
    out = render_plan(FT_OFF, 8, "cpu_count", "wsgi", t,
                      choose_server("wsgi", t, "a:b", "h", 1), "a:b")
    assert "cost MORE memory" in out
    assert "Choose" in out and "not to save memory" in out


def test_a_plan_without_preload_is_called_out():
    t = Topology(8, 1, False, "preforked", "manual")
    out = render_plan(GIL, 8, "cpu_count", "wsgi", t,
                      choose_server("wsgi", t, "a:b", "h", 1), "a:b")
    assert "not preloading" in out and "factor of three" in out


def test_usable_cores_is_sane():
    n, why = usable_cores()
    assert n >= 1 and why
