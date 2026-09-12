"""shoal command line."""
from __future__ import annotations

import argparse
import sys


def _doctor(args: argparse.Namespace) -> int:
    from .doctor import diagnose, render

    mods = [m.strip() for m in args.imports.split(",") if m.strip()] or None
    d = diagnose(mods, subinterp=not args.no_subinterp)
    if args.json:
        import dataclasses, json
        print(json.dumps({
            "build": dataclasses.asdict(d.build),
            "modules": [dataclasses.asdict(m) for m in d.modules],
            "ready": d.ready,
        }, indent=2))
    else:
        sys.stdout.write(render(d, tty=sys.stdout.isatty() and not args.no_colour))
    return 0 if d.ready or not d.blockers else 1


def _gil(args: argparse.Namespace) -> int:
    from .gil import inspect_target, render

    from .serve import usable_cores
    cores, _ = usable_cores()
    report = inspect_target(args.target)
    if args.json:
        import dataclasses, json
        print(json.dumps(dataclasses.asdict(report), indent=2))
    else:
        sys.stdout.write(render(report, args.target, cores=cores,
                                tty=sys.stdout.isatty() and not args.no_colour))
    return 1 if report.curable else 0


def _serve(args: argparse.Namespace) -> int:
    import os

    from ._app import AppError, detect_kind, load
    from ._build import Build, detect
    from .serve import choose_server, plan, render_plan, usable_cores

    try:
        app = load(args.target)
    except AppError as e:
        print(f"shoal: {e}", file=sys.stderr)
        return 2

    kind = args.kind or detect_kind(app)
    if kind == "unknown":
        print("shoal: cannot tell whether this is a WSGI or ASGI app; "
              "pass --kind wsgi|asgi", file=sys.stderr)
        return 2

    # Detect the build *after* importing the app: on a free-threaded build one
    # of its dependencies may have silently re-enabled the GIL, and that changes
    # the correct topology entirely.
    build = detect()
    cores, why = usable_cores()

    # A free-threaded build whose GIL came back on is recoverable -- but only if
    # the user has said they accept what overruling the extension costs.
    forced = False
    if build.freethreaded and build.gil_on and args.force_gil_off:
        from .gil import FORCE_VARS
        os.environ.update(FORCE_VARS)
        build = Build(build.version, True, False, build.subinterpreters, build.platform)
        forced = True

    topo = plan(build, cores, kind, args.processes, args.threads,
                want_shared_state=args.shared_state)
    server = choose_server(kind, topo, args.target, args.host, args.port, args.server)

    sys.stdout.write(render_plan(build, cores, why, kind, topo, server, args.target))

    if forced:
        print("  FORCED  PYTHON_GIL=0 -- an extension asked for the GIL and was overruled.")
        print(f"          Run `shoal gil {args.target.split(':')[0]}` to see which, "
              "and test under load.\n")
    elif build.freethreaded and build.gil_on:
        print(f"  Run `shoal gil {args.target.split(':')[0]}` to find what re-enabled it,")
        print("  or --force-gil-off to overrule the extension and keep the collapse.\n")

    if args.dry_run:
        return 0
    if not server.argv:
        return 1
    sys.stdout.flush()
    os.execvpe(server.argv[0], server.argv, os.environ)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shoal", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    doc = sub.add_parser("doctor", help="can this application collapse, and what is stopping it")
    doc.add_argument("--imports", default="",
                     help="comma-separated modules to probe (default: scan common ones)")
    doc.add_argument("--no-subinterp", action="store_true", help="skip the isolation-tier probe")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    doc.add_argument("--no-colour", action="store_true")
    doc.set_defaults(func=_doctor)

    g = sub.add_parser("gil", help="find what re-enabled the GIL, and how to fix it")
    g.add_argument("target", help="module to import, e.g. myproject.wsgi or numpy")
    g.add_argument("--json", action="store_true")
    g.add_argument("--no-colour", action="store_true")
    g.set_defaults(func=_gil)

    srv = sub.add_parser("serve", help="run an app with the right topology for this build")
    srv.add_argument("target", help="module:attribute, e.g. myproject.wsgi:application")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.add_argument("--kind", choices=["wsgi", "asgi"], help="override app-type detection")
    srv.add_argument("--server", choices=["gunicorn", "granian", "uvicorn", "waitress"],
                     help="force a particular server")
    srv.add_argument("--processes", type=int, help="override the process count")
    srv.add_argument("--threads", type=int, help="override the thread count")
    srv.add_argument("--shared-state", action="store_true",
                     help="run one process with threads: shared caches and a single "
                          "connection pool, at higher memory than pre-fork")
    srv.add_argument("--force-gil-off", action="store_true",
                     help="keep the GIL off even if an extension asked for it "
                          "(fast, and unsafe in proportion to what that extension does)")
    srv.add_argument("-n", "--dry-run", action="store_true",
                     help="print the plan and the command, run nothing")
    srv.set_defaults(func=_serve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
