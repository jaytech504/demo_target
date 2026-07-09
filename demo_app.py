"""
demo_app.py — Deliberately Vulnerable FastAPI Application

This is the TARGET app the Chaos Agent runs against.
It has intentionally bad error handling so the agent can find real issues.

Run with:
    uvicorn demo_target.demo_app:app --port 8001 --reload

Then point the Chaos Agent at http://localhost:8001
"""

import httpx
import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from chaos_middleware import ChaosMiddleware
import logging
import time
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Knowbite API (Demo Target)",
    description="Deliberately vulnerable app for Chaos Agent demo",
    version="1.0.0",
)

# ── Add chaos middleware so the Chaos Agent can simulate failures ─────────────
app.add_middleware(ChaosMiddleware)

# ── Fake in-memory database ───────────────────────────────────────────────────
fake_users = {
    1: {"id": 1, "name": "Jason", "email": "jason@knowbite.fun", "plan": "pro"},
    2: {"id": 2, "name": "Alice", "email": "alice@knowbite.fun", "plan": "free"},
}

fake_notes = {
    1: {"id": 1, "user_id": 1, "title": "FastAPI Notes", "content": "Study notes..."},
}

# ── Pydantic models ───────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    name: str
    email: str
    plan: Optional[str] = "free"

class NoteCreate(BaseModel):
    user_id: int
    title: str
    content: str

class PaymentRequest(BaseModel):
    user_id: int
    amount: float
    currency: str = "USD"


# ── Routes — intentionally vulnerable error handling ──────────────────────────

@app.get("/")
async def root():
    return {"service": "Knowbite API", "version": "1.0.0", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/users")
async def list_users():
    max_retries = 3
    # Overall deadline prevents the endpoint from hanging indefinitely.
    # Worst case without this: 3 retries x (5s timeout + 4s backoff) = ~27s.
    overall_deadline = time.monotonic() + 10.0
    start_time = time.monotonic()

    for attempt in range(max_retries):
        # Check overall budget before each attempt
        remaining = overall_deadline - time.monotonic()
        if remaining <= 0:
            elapsed = time.monotonic() - start_time
            logger.error(
                "Overall deadline exceeded after %.1fs retrieving users (attempt %d/%d)",
                elapsed, attempt + 1, max_retries
            )
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please retry later.",
                headers={"Retry-After": "5"}
            )

        # Use the smaller of per-attempt timeout or remaining overall budget
        per_attempt_timeout = min(5.0, remaining)

        try:
            users = await asyncio.wait_for(
                asyncio.to_thread(lambda: list(fake_users.values())),
                timeout=per_attempt_timeout
            )
            elapsed = time.monotonic() - start_time
            logger.info("Retrieved users in %.2fs (attempt %d)", elapsed, attempt + 1)
            return users
        except asyncio.TimeoutError:
            logger.error(
                "Timeout retrieving users - attempt %d/%d exceeded %.1fs limit",
                attempt + 1, max_retries, per_attempt_timeout
            )
            if attempt < max_retries - 1:
                backoff = min(2 ** attempt, 4)
                if time.monotonic() + backoff < overall_deadline:
                    await asyncio.sleep(backoff)
                continue
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please retry later.",
                headers={"Retry-After": "5"}
            )
        except (ConnectionError, OSError) as e:
            logger.warning(
                "Connection drop retrieving users - attempt %d/%d: %s",
                attempt + 1, max_retries, type(e).__name__
            )
            if attempt < max_retries - 1:
                backoff = min(2 ** attempt, 4)
                if time.monotonic() + backoff < overall_deadline:
                    await asyncio.sleep(backoff)
                continue
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please retry later.",
                headers={"Retry-After": "5"}
            )
        except Exception as e:
            logger.exception("Unexpected error retrieving users list")
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please retry later.",
                headers={"Retry-After": "5"}
            ) from e


@app.get("/users/{user_id}")
async def get_user(user_id: int):
    # VULNERABILITY: KeyError leaks if user not found instead of 404
    # VULNERABILITY: No timeout handling on external profile enrichment call
    user = fake_users.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Simulates calling an external enrichment service (LinkedIn, etc.)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            # This will timeout/fail — no proper handling
            profile = await client.get(
                f"https://api.enrichment-service.internal/profile/{user['email']}"
            )
            user["enriched"] = profile.json()
    except Exception as e:
        # VULNERABILITY: Raw exception message exposed to caller
        user["enriched_error"] = str(e)

    return user


@app.post("/users")
async def create_user(body: UserCreate):
    # VULNERABILITY: No validation that email is unique
    # VULNERABILITY: No error handling for database constraint violations
    new_id = max(fake_users.keys()) + 1
    user = {"id": new_id, **body.model_dump()}
    fake_users[new_id] = user
    return user


@app.delete("/users/{user_id}")
async def delete_user(user_id: int):
    # VULNERABILITY: No check if user exists — KeyError exposed raw
    del fake_users[user_id]
    return {"deleted": user_id}


@app.get("/notes")
async def list_notes(user_id: Optional[int] = None):
    try:
        notes = await asyncio.wait_for(
            asyncio.to_thread(lambda: list(fake_notes.values())),
            timeout=5.0
        )
        if user_id is not None:
            notes = [n for n in notes if n["user_id"] == user_id]
        return notes
    except asyncio.TimeoutError:
        logger.error("Timeout retrieving notes - operation exceeded 5s limit")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
        )
    except Exception:
        logger.exception("Unexpected error retrieving notes list")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
        )


@app.get("/notes/{note_id}")
async def get_note(note_id: int):
    note = fake_notes.get(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.post("/notes")
async def create_note(body: NoteCreate):
    # Payload validation is handled natively by FastAPI before we reach here
    validated_data = body.model_dump()

    # Hard deadline prevents the endpoint from hanging indefinitely under load
    # or when chaos middleware injects artificial delays
    overall_deadline = time.monotonic() + 5.0
    start_time = time.monotonic()

    def _create_note_sync():
        new_id = max(fake_notes.keys()) + 1 if fake_notes else 1
        note = {"id": new_id, **validated_data}
        fake_notes[new_id] = note
        return note

    try:
        remaining = overall_deadline - time.monotonic()
        if remaining <= 0:
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please retry later.",
                headers={"Retry-After": "5"}
            )

        note = await asyncio.wait_for(
            asyncio.to_thread(_create_note_sync),
            timeout=remaining
        )
        elapsed = time.monotonic() - start_time
@app.get("/users")
async def list_users():
    try:
        # Wrap data retrieval with timeout to prevent hangs on slow backends
        # In production, this wraps the actual async DB query
        users = await asyncio.wait_for(
            asyncio.to_thread(lambda: list(fake_users.values())),
            timeout=5.0
        )
        return users
    except asyncio.TimeoutError:
        logger.error("Timeout retrieving users - operation exceeded 5s limit")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
        )
    except Exception:
        logger.exception("Unexpected error retrieving users list")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
        )


@app.get("/notes/{note_id}")
async def get_note(note_id: int):
    # Wrap retrieval with timeout to prevent hangs under chaos middleware delays
    def _lookup_note():
        note = fake_notes.get(note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="Note not found")
        return note

    try:
        note = await asyncio.wait_for(
            asyncio.to_thread(_lookup_note),
            timeout=5.0
        )
        return note
    except asyncio.TimeoutError:
        logger.error(
            "Timeout retrieving note id=%s - operation exceeded 5s limit",
            note_id
        )
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later.",
            headers={"Retry-After": "5"}
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error retrieving note id=%s", note_id)
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later.",
            headers={"Retry-After": "5"}
        )


@app.get("/analytics/summary")
async def analytics_summary():
    """
    Calls internal analytics service — no resilience at all.
    """
    # VULNERABILITY: No timeout, no retry, no fallback
    # VULNERABILITY: If analytics service is down, entire endpoint fails with raw error
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://analytics.internal/summary",
            timeout=None,   # No timeout!
        )
        return response.json()


@app.get("/ai/recommend/{user_id}")
async def get_recommendations(user_id: int):
    """
    Calls AI recommendation service.
    """
    # VULNERABILITY: No fallback if AI service is slow or down
    # VULNERABILITY: No caching — hammers AI API on every request
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.post(
                "https://ai-service.internal/recommend",
                json={"user_id": user_id, "limit": 10},
            )
            return response.json()
    except httpx.TimeoutException:
        # VULNERABILITY: Returns 500 instead of fallback recommendations
        raise HTTPException(status_code=500, detail="AI service timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI service error: {str(e)}")