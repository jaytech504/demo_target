"""
CHAOS AGENT — Demo Target App

This is a deliberately vulnerable FastAPI application.
It has intentional error handling gaps so the Chaos Agent
has real bugs to find and fix.

Run this on port 8001:
    uvicorn demo_target.app:app --port 8001 --reload

Then point the Chaos Agent at: http://localhost:8001
"""

import httpx
import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from fastapi import HTTPException
import logging

app = FastAPI(
    title="Knowbite API (Demo Target)",
    description="Deliberately vulnerable demo app for Chaos Agent testing",
    version="1.0.0",
)

# ── Fake in-memory database ───────────────────────────────────────────────────

USERS = {
    "1": {"id": "1", "name": "Jason", "email": "jason@example.com", "plan": "free"},
    "2": {"id": "2", "name": "Alice", "email": "alice@example.com", "plan": "pro"},
}

COURSES = {
    "cs101": {"id": "cs101", "title": "Intro to CS", "instructor_id": "1"},
    "ml201": {"id": "ml201", "title": "Machine Learning", "instructor_id": "2"},
}


# ── Models ────────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    name: str
    email: str
    plan: Optional[str] = "free"


class EnrollRequest(BaseModel):
    user_id: str
    course_id: str


class PaymentRequest(BaseModel):
    user_id: str
    amount: float
    currency: str = "USD"


# ── Routes — all deliberately missing error handling ─────────────────────────

@app.get("/")
async def root():
    return {"service": "Knowbite API", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}


# BUG 1: No exception handling — database errors will crash this
@app.get("/users/{user_id}")
async def get_user(user_id: str):
    logger = logging.getLogger(__name__)
    try:
        user = USERS.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        return user
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to retrieve user '%s': %s", user_id, e)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred while retrieving the user. Please try again later.",
        )


# BUG 3: Calls external API with no timeout or error handling
@app.get("/users/{user_id}/recommendations")
async def get_recommendations(user_id: str):
    user = USERS.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # BUG: no timeout, no retry, no error handling on external call
    # If this external service is down, the whole endpoint crashes
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.example-recommendations.com/v1/recommend",
            params={"user_id": user_id, "plan": user["plan"]},
        )
        return response.json()


# BUG 4: Exposes internal error details to users
@app.post("/enroll")
async def enroll_user(body: EnrollRequest):
    try:
        user = USERS[body.user_id]   # KeyError if not found
        course = COURSES[body.course_id]

        # Simulate DB constraint violation
        if body.user_id == "already_enrolled":
            raise Exception("duplicate key value violates unique constraint "
                            "\"enrollments_user_id_course_id_key\"")

        return {"enrolled": True, "user": user["name"], "course": course["title"]}

    except Exception as e:
        # BUG: leaks raw exception message to user (exposes DB schema)
        raise HTTPException(status_code=500, detail=str(e))


# BUG 5: No rate limiting awareness — 429s from payment processor crash the app
@app.post("/payments/process")
async def process_payment(body: PaymentRequest):
    # BUG: calls external payment API with no timeout, retry, or 429 handling
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.stripe.example.com/v1/charges",
            json={
                "amount": int(body.amount * 100),
                "currency": body.currency,
                "customer": body.user_id,
            },
            headers={"Authorization": "Bearer sk_test_fake_key"},
        )
        # BUG: doesn't check response status before returning
        return response.json()


# BUG 6: Timeout on slow external call with no handling
@app.get("/courses/{course_id}/content")
async def get_course_content(course_id: str):
    course = COURSES.get(course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # BUG: fetches from a slow content CDN with no timeout
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://cdn.example-content.com/courses/{course_id}",
            timeout=None,   # BUG: no timeout set
        )
        return response.json()


@app.get("/courses/{course_id}/analytics")
async def get_course_analytics(course_id: str):
    _logger = logging.getLogger(__name__)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"https://analytics.example.com/courses/{course_id}"
            )
            if response.status_code != 200:
                _logger.warning(
                    "Analytics service returned status %s for course %s",
                    response.status_code, course_id
                )
                raise HTTPException(
                    status_code=502,
                    detail="Analytics service returned an error. Please try again later."
                )
            try:
                data = response.json()
            except (ValueError, TypeError):
                _logger.warning(
                    "Analytics service returned non-JSON body for course %s",
                    course_id
                )
                raise HTTPException(
                    status_code=502,
                    detail="Analytics service returned invalid data. Please try again later."
                )
            metrics = data.get("metrics") if isinstance(data, dict) else None
            if metrics is None or not isinstance(metrics, dict):
                _logger.warning(
                    "Analytics response missing metrics for course %s",
                    course_id
                )
                raise HTTPException(
                    status_code=502,
                    detail="Analytics data is incomplete. Please try again later."
                )
            return {
                "course_id": course_id,
                "views": metrics.get("views", 0),
                "completions": metrics.get("completions", 0),
            }
    except httpx.TimeoutException:
        _logger.warning("Timeout fetching analytics for course %s", course_id)
        raise HTTPException(
            status_code=504,
            detail="Analytics service timed out. Please try again later."
        )
    except httpx.ConnectError:
        _logger.warning("Analytics service unreachable for course %s", course_id)
        raise HTTPException(
            status_code=502,
            detail="Analytics service is currently unavailable. Please try again later."
        )
    except httpx.HTTPError as exc:
        _logger.exception("HTTP error fetching analytics for course %s: %s", course_id, exc)
        raise HTTPException(
            status_code=502,
            detail="Analytics service is currently unavailable. Please try again later."
        )
    except HTTPException:
        raise
    except Exception as exc:
        _logger.exception("Unexpected error fetching analytics for course %s: %s", course_id, exc)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again later."
        )