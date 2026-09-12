"""Load an application from `module:attribute` and work out what it is.

WSGI and ASGI need different servers and, more importantly, different
topologies: an ASGI app already multiplexes I/O on one loop, so threads buy it
much less than they buy a blocking WSGI app.
"""
from __future__ import annotations

import importlib
import inspect
from typing import Any

ASGI_MODULES = ("fastapi", "starlette", "django.core.handlers.asgi",
                "quart", "litestar", "blacksheep", "sanic")
WSGI_MODULES = ("flask", "django.core.handlers.wsgi", "bottle", "pyramid")


class AppError(RuntimeError):
    pass


def load(target: str) -> Any:
    """`myproject.wsgi:application` -> the object."""
    if ":" not in target:
        raise AppError(f"expected 'module:attribute', got {target!r}")
    mod_name, _, attr = target.partition(":")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as e:
        raise AppError(f"cannot import {mod_name!r}: {e}") from e
    try:
        return getattr(mod, attr)
    except AttributeError as e:
        raise AppError(f"{mod_name!r} has no attribute {attr!r}") from e


def _callable_signature(app: Any) -> list[str]:
    fn = app if (inspect.isfunction(app) or inspect.ismethod(app)) else getattr(app, "__call__", None)
    if fn is None:
        return []
    try:
        return list(inspect.signature(fn).parameters)
    except (ValueError, TypeError):
        return []


def detect_kind(app: Any) -> str:
    """'asgi' | 'wsgi' | 'unknown' -- by provenance first, shape second."""
    origin = f"{type(app).__module__}.{type(app).__qualname__}".lower()
    for m in ASGI_MODULES:
        if origin.startswith(m):
            return "asgi"
    for m in WSGI_MODULES:
        if origin.startswith(m):
            return "wsgi"

    params = _callable_signature(app)
    named = set(params)
    if {"scope", "receive", "send"} <= named:
        return "asgi"
    if {"environ", "start_response"} <= named:
        return "wsgi"

    fn = app if inspect.isfunction(app) else getattr(app, "__call__", None)
    if fn is not None and inspect.iscoroutinefunction(fn):
        return "asgi"

    # positional-only signatures: 3 args is ASGI's shape, 2 is WSGI's
    if len(params) == 3:
        return "asgi"
    if len(params) == 2:
        return "wsgi"
    return "unknown"
