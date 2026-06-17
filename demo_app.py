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
    # VULNERABILITY: No error handling — if DB fails, raw exception leaks
    return list(fake_users.values())


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
    # VULNERABILITY: No check that user_id exists
    # VULNERABILITY: No error handling at all
    new_id = max(fake_notes.keys()) + 1 if fake_notes else 1
    note = {"id": new_id, **body.model_dump()}
    fake_notes[new_id] = note
    return note


@app.delete("/notes/{note_id}")
async def delete_note(note_id: int):
    # VULNERABILITY: Raw KeyError if note doesn't exist
    del fake_notes[note_id]
    return {"deleted": note_id}


@app.post("/payments/charge")
async def charge_payment(body: PaymentRequest):
    """
    Calls Stripe API — multiple vulnerabilities here.
    """
    # VULNERABILITY: No timeout on external payment API call
    # VULNERABILITY: Raw Stripe error details leaked to caller
    # VULNERABILITY: No retry logic on 429
    # VULNERABILITY: Amount not validated (negative amounts allowed)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.stripe.com/v1/charges",
                headers={"Authorization": "Bearer sk_test_fake_key"},
                data={
                    "amount": int(body.amount * 100),
                    "currency": body.currency,
                    "source": "tok_visa",
                },
            )
            return response.json()
    except Exception as e:
        # VULNERABILITY: Full exception including internal details returned
        return {"error": True, "detail": str(e), "type": type(e).__name__}


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
