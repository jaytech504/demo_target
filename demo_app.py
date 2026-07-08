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
from fastapi import HTTPException
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
        logger.exception("Failed to retrieve users from data store")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
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
    notes = list(fake_notes.values())
    if user_id:
        notes = [n for n in notes if n["user_id"] == user_id]
    return notes


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
    
    def _write_note():
        # Synchronous dict write operation runs in thread pool to avoid blocking the event loop
        new_id = max(fake_notes.keys()) + 1 if fake_notes else 1
        note = {"id": new_id, **validated_data}
        fake_notes[new_id] = note
        return note
    
    try:
        # Enforce 5-second timeout to prevent slow database responses from hanging the request
        note = await asyncio.wait_for(
            asyncio.to_thread(_write_note),
            timeout=5.0
        )
        return note
    except asyncio.TimeoutError:
        logger.error("Timeout creating note - operation exceeded 5s limit")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
        )
    except Exception as e:
        logger.exception("Unexpected error creating note")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
        ) from e
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
        )
    except Exception as e:
        logger.exception("Unexpected error creating note")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
        ) from e
                timeout=5.0
            )
            return note
        except asyncio.TimeoutError:
            # Transient failure — likely a connection drop, safe to retry
            logger.warning(
                "Timeout creating note (attempt %d/%d)",
                attempt + 1, max_retries
            )
            if attempt < max_retries - 1:
                # Exponential backoff: 0.1s, 0.2s — keeps retry window short
                await asyncio.sleep(min(2 ** attempt * 0.1, 1.0))
                continue
            # All retries exhausted — return safe generic message
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please retry later."
            )
        except Exception:
            # Non-transient error — do not retry, fail fast to avoid data corruption
            logger.exception("Unexpected error creating note")
            # No 'from e' chaining — prevents exception details (DB URIs,
            # table names, internal stack traces) from leaking to the client
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable. Please retry later."
            )


@app.delete("/notes/{note_id}")
async def delete_note(note_id: int):
    # VULNERABILITY: Raw KeyError if note doesn't exist
    del fake_notes[note_id]
    return {"deleted": note_id}


@app.post("/payments/charge")
async def charge_payment(body: PaymentRequest):
    """
    Calls Stripe API with proper timeout, retry, validation, and error sanitization.
    """
    # FIX: Validate amount — reject negative or zero amounts before hitting Stripe
    if body.amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="Payment amount must be greater than zero"
        )

    # FIX: Explicit timeouts prevent hanging on slow external responses
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
    max_retries = 3

    for attempt in range(max_retries):
        try:
            # FIX: timeout parameter ensures requests never hang indefinitely
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    "https://api.stripe.com/v1/charges",
                    headers={"Authorization": "Bearer sk_test_fake_key"},
                    data={
                        "amount": int(body.amount * 100),
                        "currency": body.currency,
                        "source": "tok_visa",
                    },
                )

                # FIX: Retry on 429 rate limiting with exponential backoff
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        retry_after = int(response.headers.get("Retry-After", str(2 ** attempt)))
                        logger.warning(
                            "Stripe rate limit hit, retrying in %ds (attempt %d/%d)",
                            retry_after, attempt + 1, max_retries
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    # Exhausted retries on 429 — return safe generic message
                    logger.error("Stripe rate limit exhausted after %d retries", max_retries)
                    raise HTTPException(
                        status_code=503,
                        detail="Payment service is temporarily unavailable due to high demand. Please try again later."
                    )

                # FIX: raise_for_status triggers HTTPStatusError for non-2xx (except 429 handled above)
                response.raise_for_status()

                return response.json()

        except httpx.TimeoutException:
            # FIX: Retry on timeout with exponential backoff before giving up
            logger.error(
                "Timeout calling Stripe API (attempt %d/%d)",
                attempt + 1, max_retries
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise HTTPException(
                status_code=504,
                detail="Payment service timed out. Please try again later."
            )

        except httpx.ConnectError:
            # FIX: Retry on connection failure — Stripe may be temporarily unreachable
            logger.error(
                "Connection error to Stripe API (attempt %d/%d)",
                attempt + 1, max_retries
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise HTTPException(
                status_code=503,
                detail="Payment service is currently unavailable. Please try again later."
            )

        except httpx.HTTPStatusError as e:
            # FIX: Log the actual status code internally, return sanitized message to client
            logger.error(
                "Stripe API returned HTTP %d (attempt %d/%d)",
                e.response.status_code, attempt + 1, max_retries
            )
            raise HTTPException(
                status_code=502,
                detail="Payment processing failed. Please verify your payment details and try again."
            )

        except HTTPException:
            # Re-raise our own HTTPExceptions unchanged (e.g. 400 from validation, 503 from exhausted 429 retries)
            raise

        except Exception as e:
            # FIX: Catch-all logs the real error internally, returns only a safe generic message
            logger.error("Unexpected error in payment processing: %s: %s", type(e).__name__, str(e))
            raise HTTPException(
                status_code=500,
                detail="An unexpected error occurred processing your payment."
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