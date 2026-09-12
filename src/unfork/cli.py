"""unfork command line."""
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="unfork", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    doc = sub.add_parser("doctor", help="can this application collapse, and what is stopping it")
    doc.add_argument("--imports", default="",
                     help="comma-separated modules to probe (default: scan common ones)")
    doc.add_argument("--no-subinterp", action="store_true", help="skip the isolation-tier probe")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    doc.add_argument("--no-colour", action="store_true")
    doc.set_defaults(func=_doctor)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
