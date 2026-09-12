"""Auditing a running deployment: what it declares, and what it actually does."""
from __future__ import annotations

import pytest

from shoal import audit as A


def P(pid, ppid, cmd, **kw):
    return A.Proc(pid=pid, ppid=ppid, cmdline=cmd, **kw)


# --------------------------------------------------------- declared config

def test_gunicorn_preload_flag_is_detected():
    m = P(1, 0, ["gunicorn", "--preload", "--workers", "8", "app:app"])
    declared, freeze, conf, note = A.inspect_declaration(m)
    assert declared is True


def test_gunicorn_without_preload_is_detected():
    m = P(1, 0, ["gunicorn", "--workers", "8", "app:app"])
    assert A.inspect_declaration(m)[0] is False


def test_preload_app_in_a_config_file_counts(tmp_path):
    conf = tmp_path / "g.py"
    conf.write_text("preload_app = True\nworkers = 4\n")
    m = P(1, 0, ["gunicorn", "-c", str(conf), "app:app"])
    declared, freeze, path, _ = A.inspect_declaration(m)
    assert declared is True
    assert path == str(conf)
    assert freeze is False


def test_commented_out_preload_does_not_count(tmp_path):
    conf = tmp_path / "g.py"
    conf.write_text("# preload_app = True\n")
    m = P(1, 0, ["gunicorn", "-c", str(conf), "app:app"])
    assert A.inspect_declaration(m)[0] is False


def test_gc_freeze_in_the_config_is_detected(tmp_path):
    conf = tmp_path / "g.py"
    conf.write_text("import gc\ndef when_ready(server):\n    gc.freeze()\n")
    m = P(1, 0, ["gunicorn", "-c", str(conf), "--preload", "app:app"])
    declared, freeze, _, _ = A.inspect_declaration(m)
    assert declared is True and freeze is True


def test_uwsgi_preloads_unless_told_otherwise():
    """The flag is inverted here: uwsgi preloads by default."""
    assert A.inspect_declaration(P(1, 0, ["uwsgi", "--master"]))[0] is True
    assert A.inspect_declaration(P(1, 0, ["uwsgi", "--lazy-apps"]))[0] is False


def test_uvicorn_cannot_preload_at_all():
    declared, _, _, note = A.inspect_declaration(P(1, 0, ["uvicorn", "app:app"]))
    assert declared is False
    assert "no preload mode" in note


def test_unknown_server_reports_unknown_not_false():
    declared, _, _, note = A.inspect_declaration(P(1, 0, ["waitress-serve", "app:app"]))
    assert declared is None, "unknown must not be reported as 'not preloading'"


def test_server_name_survives_an_absolute_path():
    assert P(1, 0, ["/usr/local/bin/gunicorn", "app:app"]).name == "gunicorn"


# ------------------------------------------------------------- measurement

def test_sharing_ratio_is_the_gap_between_pss_and_rss():
    a = A.Audit(master=P(1, 0, ["gunicorn"], pss_kb=1000, rss_kb=1000),
                workers=[P(2, 1, ["gunicorn"], pss_kb=1000, rss_kb=9000)])
    assert a.total_pss_kb == 2000 and a.total_rss_kb == 10000
    assert a.sharing_ratio == pytest.approx(0.8)


def test_no_sharing_is_zero_not_a_crash():
    a = A.Audit(master=P(1, 0, ["gunicorn"], pss_kb=500, rss_kb=500))
    assert a.sharing_ratio == 0.0


def test_empty_audit_does_not_divide_by_zero():
    assert A.Audit().sharing_ratio == 0.0
    assert A.Audit().private_per_worker_kb == 0.0


def test_saving_estimate_uses_the_benchmarked_range():
    a = A.Audit(master=P(1, 0, ["gunicorn"], pss_kb=100_000, rss_kb=100_000))
    lo, hi = A.estimate_saving_kb(a)
    assert lo == 70_000 and hi == 78_000


# ---------------------------------------------------------------- verdicts

def _audit(preload, freeze, sharing=0.05, workers=8):
    ws = [P(i, 1, ["gunicorn"], pss_kb=10_000, rss_kb=int(10_000 / (1 - sharing)),
            private_kb=9_000) for i in range(2, 2 + workers)]
    return A.Audit(master=P(1, 0, ["gunicorn"], pss_kb=10_000, rss_kb=10_000),
                   workers=ws, server="gunicorn",
                   preload_declared=preload, freeze_declared=freeze)


def test_good_when_preloaded_frozen_and_actually_sharing(monkeypatch):
    monkeypatch.setattr(A, "LINUX", True)
    out = A.render(_audit(True, True, sharing=0.7), tty=False)
    assert "GOOD" in out and "Nothing to change" in out


def test_decaying_when_preloaded_but_not_frozen(monkeypatch):
    monkeypatch.setattr(A, "LINUX", True)
    out = A.render(_audit(True, False, sharing=0.7), tty=False)
    assert "DECAYING" in out
    assert "gc.freeze()" in out and "when_ready" in out
    assert "gone within minutes" in out


def test_not_sharing_quantifies_the_recommendation(monkeypatch):
    monkeypatch.setattr(A, "LINUX", True)
    out = A.render(_audit(False, False), tty=False)
    assert "NOT SHARING" in out and "RECOMMENDED" in out
    assert "MB back" in out
    assert "from our benchmark, not your app" in out, "the estimate must state its source"


def test_unknown_server_is_not_told_it_is_broken(monkeypatch):
    monkeypatch.setattr(A, "LINUX", True)
    out = A.render(_audit(None, False), tty=False)
    assert "UNCLEAR" in out
    assert "NOT SHARING" not in out


def test_off_linux_says_so_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(A, "LINUX", False)
    out = A.render(_audit(False, False), tty=False)
    assert "needs Linux" in out
    assert "RECOMMENDED" not in out


def test_render_has_no_escape_codes_without_a_tty(monkeypatch):
    monkeypatch.setattr(A, "LINUX", True)
    assert "\033[" not in A.render(_audit(False, False), tty=False)
