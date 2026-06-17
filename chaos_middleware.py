"""
chaos_middleware.py

Drop this file into any FastAPI app you want to chaos-test.
It intercepts X-Chaos-Mode headers from the Chaos Agent proxy
and simulates the requested failure for that request.

Usage:
    from chaos_middleware import ChaosMiddleware
    app.add_middleware(ChaosMiddleware)

The Chaos Agent sends headers like:
    X-Chaos-Mode: http_500
    X-Chaos-Mode: db_connection_drop
    X-Chaos-Mode: slow_response
    X-Chaos-Delay: 5000

This middleware intercepts those and simulates the failure
so the Chaos Agent can observe how your app responds.
"""

import asyncio
import json
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse


class ChaosMiddleware(BaseHTTPMiddleware):
    """
    Simulates dependency failures when X-Chaos-Mode header is present.
    Only activates when the header is set — zero impact on normal requests.
    """

    FAILURE_RESPONSES = {
        # Dependency HTTP failures — simulates what your app gets back from a bad upstream
        "http_500": (500, {"error": "upstream_service_error",
                           "message": "The upstream service returned an internal error"}),
        "http_429": (429, {"error": "rate_limited",
                           "message": "Too many requests to upstream service",
                           "retry_after": 60}),
        "http_503": (503, {"error": "service_unavailable",
                           "message": "Upstream service temporarily unavailable"}),
        "http_401": (401, {"error": "unauthorized",
                           "message": "Upstream service rejected credentials"}),
        "http_404": (404, {"error": "not_found",
                           "message": "Requested resource not found in upstream service"}),

        # Database failures
        "db_connection_drop": (503, {"error": "database_error",
                                     "detail": "connection to server at localhost (127.0.0.1), "
                                               "port 5432 failed: Connection refused\n"
                                               "Is the server running on that host and accepting TCP/IP connections?"}),
        "db_timeout": (504, {"error": "database_timeout",
                             "detail": "canceling statement due to statement timeout"}),
        "db_constraint_violation": (409, {"error": "constraint_violation",
                                          "detail": "duplicate key value violates unique constraint"}),

        # Data failures
        "empty_response": (200, {}),
        "partial_response": (206, {"data": "partial"}),
        "null_fields": (200, {"id": None, "name": None, "email": None, "created_at": None}),
    }

    async def dispatch(self, request: Request, call_next):
        chaos_mode = request.headers.get("X-Chaos-Mode")
        chaos_delay = request.headers.get("X-Chaos-Delay")

        # No chaos header — normal request
        if not chaos_mode:
            return await call_next(request)

        # Simulate delay first (tests slow upstream handling)
        if chaos_delay:
            try:
                delay_ms = int(chaos_delay)
                await asyncio.sleep(delay_ms / 1000)
            except ValueError:
                pass

        # Wrong content type simulation
        if chaos_mode == "wrong_content_type":
            return Response(
                content="<html><body><h1>Service Error</h1></body></html>",
                status_code=200,
                media_type="text/html",
            )

        # Connection reset — abruptly close with empty response
        if chaos_mode == "connection_reset":
            return Response(content=b"", status_code=500)

        # DNS failure simulation
        if chaos_mode == "dns_failure":
            return JSONResponse(
                status_code=502,
                content={"error": "bad_gateway",
                         "detail": "Name or service not known: upstream-service.internal"}
            )

        # Slow response (already handled delay above, now pass through)
        if chaos_mode == "slow_response":
            return await call_next(request)

        # HTTP timeout — don't respond at all for timeout simulation
        # (The proxy handles this by setting a very short timeout on its end)
        if chaos_mode == "http_timeout":
            await asyncio.sleep(30)  # Hang indefinitely
            return Response(content=b"", status_code=504)

        # Check known failure responses
        if chaos_mode in self.FAILURE_RESPONSES:
            status_code, body = self.FAILURE_RESPONSES[chaos_mode]
            return JSONResponse(status_code=status_code, content=body)

        # Unknown chaos mode — pass through normally
        return await call_next(request)
