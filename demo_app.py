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
from fastapi import HTTPException, status
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
    """
    Retrieves the list of users with strict timeout enforcement, 
    data validation to prevent null/malformed leaks, and generic error responses.
    """
    try:
        # 1. Execute retrieval with a strict timeout to prevent hanging the event loop
        # We use asyncio.to_thread to ensure the blocking dict access doesn't block the loop
        raw_users = await asyncio.wait_for(
            asyncio.to_thread(lambda: list(fake_users.values())),
            timeout=5.0
        )

        # 2. Validate data integrity (Handles 'null_fields' and 'malformed_json' failure modes)
        # We ensure every user object conforms to a schema before it leaves the service boundary
        validated_users = []
        for u in raw_users:
            # Basic validation to ensure required fields exist and are not null
            if not all(k in u and u[k] is not None for k in ("id", "name", "email")):
                logger.error(f"Data integrity error: User object contains null or missing required fields: {u}")
                continue  # Skip corrupted records instead of crashing the whole request
            validated_users.append(u)

        return validated_users

    except asyncio.TimeoutError as e:
        # Handles 'slow_response' and 'http_timeout'
        logger.error(f"Database timeout during list_users: {str(e)}")
        raise HTTPException(
            status_code=503,
            detail="The request timed out. Please try again later."
        )
    except (KeyError, TypeError) as e:
        # Handles potential internal data corruption or 'db_connection_drop' symptoms
        logger.error(f"Data access error in list_users: {str(e)}")
        raise HTTPException(
            status_code=503,
            detail="Internal data error. Please contact support."
        )
    except Exception as e:
        # Catch-all for unexpected errors (e.g., system errors) to prevent leaking tracebacks
        logger.exception("Unhandled exception in list_users")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable."
        )
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
    """
    Retrieves notes with timeout protection, data integrity validation,
    and sanitized error responses to prevent leaking internal state.
    """
    try:
        # 1. Handle 'slow_response' and 'http_timeout' by wrapping the data access in a timeout.
        # We use to_thread because list(dict.values()) is a blocking operation.
        raw_notes = await asyncio.wait_for(
            asyncio.to_thread(lambda: list(fake_notes.values())),
            timeout=5.0
        )

        # 2. Handle 'null_fields' and 'malformed_json' by validating data integrity.
        # We ensure required keys exist and are not null before returning them to the client.
        validated_notes = []
        for n in raw_notes:
            try:
                if all(k in n and n[k] is not None for k in ("id", "user_id", "title")):
                    validated_notes.append(n)
                else:
                    logger.warning(f"Skipping malformed note record: {n}")
            except (KeyError, TypeError) as e:
                logger.error(f"Integrity error parsing note: {e}")
                continue

        # 3. Apply filtering logic on the validated set
        if user_id:
            validated_notes = [n for n in validated_notes if n["user_id"] == user_id]

        return validated_notes

    except asyncio.TimeoutError:
        # Specific handling for 'slow_response'
        logger.error("Timeout occurred while fetching notes")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The request timed out. Please try again later."
        )
    except Exception as e:
        # Handle 'db_connection_drop' or other unexpected failures without leaking details
        logger.exception("Unexpected error in list_notes")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="An internal error occurred while retrieving notes."
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
    
    try:
        new_id = max(fake_notes.keys()) + 1 if fake_notes else 1
        note = {"id": new_id, **validated_data}
        fake_notes[new_id] = note
        return note
    except Exception as e:
        logger.exception("Unexpected error creating note")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. Please retry later."
        ) from e


@app.delete("/notes/{note_id}")
async def delete_note(note_id: int):
    # VULNERABILITY: Raw KeyError if note doesn't exist
    del fake_notes[note_id]
    return {"deleted": note_id}


@app.post("/payments/charge")
async def charge_payment(body: PaymentRequest):
    """
    Calls Stripe API with timeouts, retries for rate limits, and sanitized error handling.
    """
    # 1. Input Validation: Prevent negative or zero amounts
    if body.amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Amount must be greater than zero"
        )

    max_retries = 3
    retry_delay = 1.0  # seconds

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for attempt in range(max_retries):
                try:
                    response = await client.post(
                        "https://api.stripe.com/v1/charges",
                        headers={"Authorization": "Bearer sk_test_fake_key"},
                        data={
                            "amount": int(body.amount * 100),
                            "currency": body.currency,
                            "source": "tok_visa",
                        },
                    )

                    # 2. Handle Rate Limiting (429) with exponential backoff
                    if response.status_code == 429 and attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (2 ** attempt))
                        continue
                    
                    # Raise for other HTTP errors (4xx, 5xx)
                    response.raise_for_status()
                    return response.json()

                except httpx.HTTPStatusError as e:
                    # If it's a 4xx error (except 429), don't retry, just handle it
                    if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                        logger.warning(f"Payment rejected by provider: {e.response.text}")
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Payment processing failed. Please check your credentials."
                        )
                    # If it's a 5xx error, we might want to retry once more if we haven't exhausted retries
                    if e.response.status_code >= 500 and attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (2 ** attempt))
                        continue
                    raise e

    except httpx.TimeoutException:
        logger.error("Stripe API request timed out")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Payment provider timed out. Please check your statement before retrying."
        )
    except httpx.ConnectError:
        logger.error("Failed to connect to Stripe API")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment service is currently unreachable."
        )
    except Exception as e:
        # 3. Sanitize Errors: Never return str(e) to the client to avoid leaking internals
        logger.exception("Unexpected error during payment processing")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing your payment."
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


@app.get("/crash")
async def crash():
    data = None
    title = data["title"]  # Throws TypeError
    return {"ok": True}