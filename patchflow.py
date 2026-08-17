"""
PatchFlow Agent SDK — Python
============================

One-line setup for FastAPI, Flask, Starlette, or any WSGI/ASGI framework.

Usage
-----
FastAPI / Starlette:
    import patchflow
    patchflow.init(api_key="pf_live_...")
    # That's it — middleware is installed automatically.

Flask:
    import patchflow
    patchflow.init(api_key="pf_live_...", app=flask_app)

Manual (any Python app):
    import patchflow
    pf = patchflow.init(api_key="pf_live_...")
    try:
        risky_code()
    except Exception as e:
        pf.capture_exception(e)

What it captures
----------------
- Unhandled exceptions in FastAPI / Starlette routes (via ASGI middleware)
- Unhandled exceptions in Flask routes (via Flask error handler)
- Any exception passed to capture_exception()

What it sends to PatchFlow
--------------------------
- Exception type and message (PII auto-redacted server-side)
- Stack frames (file, line, function, context lines)
- Endpoint path and HTTP method
- Framework name and SDK version
- Environment name

Privacy
-------
All data is sent over HTTPS. PatchFlow redacts emails, IPs, passwords,
tokens, and connection strings from error messages and stack frames
before storing or processing them.
"""

from __future__ import annotations

import inspect
import os
import sys
import threading
import traceback
from typing import Callable, Any

try:
    import httpx as _httpx
    _HTTP = "httpx"
except ImportError:
    import urllib.request as _urllib
    import json as _json
    _HTTP = "urllib"

__version__ = "0.1.0"
_DEFAULT_HOST = "https://patchflow-backend-xax6.onrender.com"

# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: "PatchFlow | None" = None


def init(
    api_key: str,
    host: str | None = None,
    environment: str | None = None,
    app: Any = None,           # Flask app, optional
    debug: bool = False,
) -> "PatchFlow":
    """
    Initialise the PatchFlow SDK.

    Parameters
    ----------
    api_key:     Your site API key from the PatchFlow dashboard (pf_live_...)
    host:        PatchFlow API host (defaults to https://api.patchflow.dev)
    environment: Environment name (defaults to PATCHFLOW_ENV or APP_ENV env vars, or 'production')
    app:         Flask application instance — pass this for Flask projects
    debug:       Print SDK debug logs to stdout
    """
    global _instance
    _instance = PatchFlow(
        api_key=api_key,
        host=host or os.getenv("PATCHFLOW_HOST", _DEFAULT_HOST),
        environment=environment or os.getenv("PATCHFLOW_ENV") or os.getenv("APP_ENV", "production"),
        debug=debug,
    )

    # ── Auto-install Flask error handler ─────────────────────────────────────
    if app is not None:
        _install_flask(app, _instance)
        if debug:
            print(f"[PatchFlow] Flask error handler installed on {app}")

    # ── Auto-install FastAPI / Starlette ASGI middleware ──────────────────────
    # Detect if we're inside a FastAPI app by checking the call stack for
    # a FastAPI or Starlette Application object.
    else:
        _try_install_fastapi(_instance, debug)

    # ── Fallback: install sys.excepthook for non-web scripts ─────────────────
    _install_excepthook(_instance)

    # ── Automatic startup heartbeat ping to mark SDK active in dashboard ──────
    _instance.ping()

    if debug:
        print(f"[PatchFlow] Initialised. host={_instance.host} env={_instance.environment}")

    return _instance


# ── Core class ────────────────────────────────────────────────────────────────

class PatchFlow:
    def __init__(self, api_key: str, host: str, environment: str, debug: bool):
        self.api_key = api_key
        self.host = host.rstrip("/")
        self.environment = environment
        self.debug = debug
        self._framework = _detect_framework()

    def ping(self) -> None:
        """
        Send a heartbeat ping to confirm connection and mark the SDK as active.
        Runs in a background thread so it never blocks startup.
        """
        if not self.api_key:
            return

        def _do_ping():
            url = f"{self.host}/api/sdk/ping"
            headers = {
                "X-PatchFlow-Key": self.api_key,
                "User-Agent": f"patchflow-python/{__version__}",
            }
            try:
                if _HTTP == "httpx":
                    with _httpx.Client(timeout=5.0) as client:
                        r = client.post(url, headers=headers)
                        if self.debug:
                            print(f"[PatchFlow] Heartbeat ping response: {r.status_code}")
                else:
                    req = _urllib.Request(url, data=b"", headers=headers, method="POST")
                    with _urllib.urlopen(req, timeout=5.0) as r:
                        if self.debug:
                            print(f"[PatchFlow] Heartbeat ping response: {r.status}")
            except Exception as e:
                if self.debug:
                    print(f"[PatchFlow] Heartbeat ping failed: {e}")

        threading.Thread(target=_do_ping, daemon=True).start()

    def capture_exception(
        self,
        exc: BaseException,
        endpoint: str = "",
        method: str = "",
        status_code: int | None = None,
    ) -> None:
        """
        Capture and send an exception to PatchFlow.
        This is non-blocking — the HTTP call is made in a background thread.
        """
        try:
            payload = _build_payload(
                exc=exc,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                framework=self._framework,
                environment=self.environment,
            )
            threading.Thread(
                target=self._send,
                args=(payload,),
                daemon=True,
            ).start()
        except Exception as send_err:
            if self.debug:
                print(f"[PatchFlow] Failed to capture exception: {send_err}")

    def _send(self, payload: dict) -> None:
        url = f"{self.host}/api/sdk/errors"
        headers = {
            "X-PatchFlow-Key": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": f"patchflow-python/{__version__}",
        }
        try:
            if _HTTP == "httpx":
                with _httpx.Client(timeout=10) as client:
                    r = client.post(url, json=payload, headers=headers)
                    if self.debug:
                        print(f"[PatchFlow] Sent error — status={r.status_code}")
            else:
                import json
                data = json.dumps(payload).encode()
                req = _urllib.Request(url, data=data, headers=headers, method="POST")
                with _urllib.urlopen(req, timeout=10) as r:
                    if self.debug:
                        print(f"[PatchFlow] Sent error — status={r.status}")
        except Exception as e:
            if self.debug:
                print(f"[PatchFlow] Send failed: {e}")


# ── Payload builder ───────────────────────────────────────────────────────────

def _build_payload(
    exc: BaseException,
    endpoint: str,
    method: str,
    status_code: int | None,
    framework: str,
    environment: str,
) -> dict:
    tb = exc.__traceback__
    frames = []
    if tb:
        extracted = traceback.extract_tb(tb)
        for frame in extracted:
            frames.append({
                "filename": frame.filename,
                "lineno": frame.lineno,
                "function": frame.name,
                "context_line": frame.line or "",
                "pre_context": [],
                "post_context": [],
                "vars": {},
            })

    # Top frame for culprit
    top = frames[-1] if frames else {}
    culprit = endpoint or f"{top.get('filename','')}:{top.get('lineno','')}"

    return {
        "error_type": type(exc).__name__,
        "error_message": str(exc)[:1000],
        "culprit": culprit,
        "endpoint": endpoint,
        "method": method.upper() if method else "",
        "status_code": status_code,
        "stack_frames": frames,
        "framework": framework,
        "environment": environment,
        "sdk_version": __version__,
    }


# ── Framework detection ───────────────────────────────────────────────────────

def _detect_framework() -> str:
    if "fastapi" in sys.modules:
        return "fastapi"
    if "flask" in sys.modules:
        return "flask"
    if "starlette" in sys.modules:
        return "starlette"
    if "django" in sys.modules:
        return "django"
    return "python"


# ── FastAPI / Starlette ASGI middleware ───────────────────────────────────────

def _try_install_fastapi(pf: "PatchFlow", debug: bool) -> None:
    """
    Look for a FastAPI or Starlette app in the calling module's globals
    and install the middleware automatically.
    """
    try:
        # Walk up the call stack looking for a FastAPI/Starlette app
        frame = inspect.currentframe()
        while frame:
            for name, obj in frame.f_globals.items():
                if _is_asgi_app(obj):
                    obj.add_middleware(PatchFlowASGIMiddleware, patchflow=pf)
                    if debug:
                        print(f"[PatchFlow] ASGI middleware installed on {name}")
                    return
            frame = frame.f_back
    except Exception:
        pass  # Silently skip — user can install middleware manually


def _is_asgi_app(obj: Any) -> bool:
    try:
        from starlette.applications import Starlette
        if isinstance(obj, Starlette):
            return True
    except ImportError:
        pass
    try:
        from fastapi import FastAPI
        if isinstance(obj, FastAPI):
            return True
    except ImportError:
        pass
    return False


class PatchFlowASGIMiddleware:
    """ASGI middleware that captures unhandled exceptions from FastAPI/Starlette."""

    def __init__(self, app: Any, patchflow: "PatchFlow"):
        self.app = app
        self.pf = patchflow

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        endpoint = scope.get("path", "")
        method = scope.get("method", "")

        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            self.pf.capture_exception(exc, endpoint=endpoint, method=method, status_code=500)
            raise  # re-raise so FastAPI's own error handling still runs


# ── Flask integration ─────────────────────────────────────────────────────────

def _install_flask(app: Any, pf: "PatchFlow") -> None:
    """Register a Flask after_request + errorhandler hook."""
    try:
        @app.errorhandler(Exception)
        def _pf_flask_error(exc: Exception):
            # Get request context
            try:
                from flask import request as flask_req
                endpoint = flask_req.path
                method = flask_req.method
            except Exception:
                endpoint, method = "", ""
            pf.capture_exception(exc, endpoint=endpoint, method=method, status_code=500)
            # Re-raise so Flask's own error handling continues
            raise exc
    except Exception:
        pass


# ── sys.excepthook fallback ───────────────────────────────────────────────────

def _install_excepthook(pf: "PatchFlow") -> None:
    """Capture unhandled exceptions in non-web scripts."""
    original = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            original(exc_type, exc_value, exc_tb)
            return
        pf.capture_exception(exc_value)
        original(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


# ── Convenience top-level functions ───────────────────────────────────────────

def capture_exception(exc: BaseException, **kwargs) -> None:
    """Capture an exception using the globally initialised SDK instance."""
    if _instance:
        _instance.capture_exception(exc, **kwargs)


# ── FastAPI convenience decorator ─────────────────────────────────────────────

def monitor(func: Callable) -> Callable:
    """
    Decorator for FastAPI route functions.
    Catches and reports exceptions without swallowing them.

    Usage:
        @app.get("/users")
        @patchflow.monitor
        async def get_users():
            ...
    """
    import functools

    @functools.wraps(func)
    async def _async_wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            if _instance:
                _instance.capture_exception(exc)
            raise

    @functools.wraps(func)
    def _sync_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if _instance:
                _instance.capture_exception(exc)
            raise

    import asyncio
    if asyncio.iscoroutinefunction(func):
        return _async_wrapper
    return _sync_wrapper
