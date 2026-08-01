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
            detail="An internal error occurred while retrieving the user. Please try again later.", timeout=5.0,
        )
        if user is None:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        return user
    except asyncio.TimeoutError:
        logger.warning("Timeout fetching user %s", user_id)
        raise HTTPException(
            status_code=504,
            detail="Request timed out. Please try again later.",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error fetching user %s: %s", user_id, e)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again later.",
        )
    except HTTPException:
        raise
    except Exception as e:
        # Log full traceback server-side for debugging
        logger.exception("Database error while fetching user %s: %s", user_id, e)
        # Never expose internal details or stack traces to the client
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again later.",
        )


# BUG 2: No input validation — null fields crash the handler
@app.post("/users")
async def create_user(body: UserCreate):

    logger = logging.getLogger(__name__)

    try:
        # Check for duplicate email before attempting insertion
        for existing in USERS.values():
            if existing.get("email") == body.email:
                raise ValueError(f"Email '{body.email}' already registered")

        # Attempt to persist the new user
        new_id = str(len(USERS) + 1)
        user = {"id": new_id, **body.model_dump()}
        USERS[new_id] = user
        return user

    except ValueError as e:
        # Known business-logic error (e.g., duplicate email) — return 409 Conflict
        raise HTTPException(status_code=409, detail=str(e))

    except Exception as e:
        # Unknown failure (connection drop, serialization error, etc.)
        # Log the full traceback server-side for debugging
        logger.exception("Failed to create user: %s", e)
        # Never expose internal details or stack traces to the client
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again later.",
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
    logger = logging.getLogger(__name__)
    max_retries = 3
    retry_delay = 1.0

    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(max_retries):
            try:
                response = await client.post(
                    "https://api.stripe.example.com/v1/charges",
                    json={
                        "amount": int(body.amount * 100),
                        "currency": body.currency,
                        "customer": body.user_id,
                    },
                    headers={"Authorization": "Bearer sk_test_fake_key"},
                )

                # Handle Rate Limiting (429) specifically before raise_for_status
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (2 ** attempt))
                        continue
                    raise HTTPException(status_code=429, detail="Payment processor is busy. Please try again later.")

                # Trigger HTTPStatusError for non-2xx codes to use standard httpx handling
                response.raise_for_status()

                # Handle valid responses
                try:
                    data = response.json()
                    if data is None:
                        raise ValueError("Empty response")
                    return data
                except (ValueError, KeyError) as e:
                    logger.error(f"Malformed JSON response from provider: {str(e)}")
                    raise HTTPException(status_code=502, detail="Invalid response format from payment provider.")

            except HTTPException:
                # CRITICAL: Re-raise FastAPI HTTPExceptions so they aren't caught by 'except Exception'
                raise
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                logger.warning(f"Attempt {attempt + 1} failed: Connection/Timeout issue: {str(e)}")
                if attempt == max_retries - 1:
                    raise HTTPException(status_code=504, detail="Payment gateway timed out. Please check your balance.")
                await asyncio.sleep(retry_delay * (2 ** attempt))
            except httpx.HTTPStatusError as e:
                # Handles non-2xx status codes raised by raise_for_status()
                logger.error(f"Payment API status error: {e.response.status_code} - {e.response.text}")
                raise HTTPException(status_code=e.response.status_code, detail="Failed to process payment with provider.")
            except Exception as e:
                # Catch-all for unexpected system failures only
                logger.exception(f"Unexpected error in process_payment: {str(e)}")
                raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")
                )

                # Handle Rate Limiting (429) with exponential backoff
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        logger.warning(f"Rate limited by provider. Retrying attempt {attempt + 1}...")
                        await asyncio.sleep(retry_delay * (2 ** attempt))
                        continue
                    raise HTTPException(status_code=429, detail="Payment processor is busy. Please try again later.")

                # Check for other non-2xx status codes
                if response.status_code != 200:
                    logger.error(f"Payment API error: {response.status_code} - {response.text}")
                    # Return the actual provider error status if it is a client error (4xx)
                    if 400 <= response.status_code < 500:
                        raise HTTPException(status_code=response.status_code, detail="The payment provider rejected the request.")
                    # For 5xx, treat as provider degradation
                    raise HTTPException(status_code=502, detail="Payment provider is experiencing issues.")

                # Handle JSON parsing and empty response bodies
                try:
                    data = response.json()
                except (ValueError, TypeError) as e:
                    logger.error(f"Malformed JSON response from provider: {str(e)}")
                    raise HTTPException(status_code=502, detail="Received invalid response from payment provider.")

                if data is None:
                    logger.error("Provider returned null JSON body")
                    raise HTTPException(status_code=502, detail="Empty response from payment provider.")
                
                return data

            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
                logger.warning(f"Attempt {attempt + 1} failed: Network/Timeout issue: {str(e)}")
                if attempt == max_retries - 1:
                    raise HTTPException(status_code=504, detail="Payment gateway connection timed out. Please try again later.")
                await asyncio.sleep(retry_delay * (2 ** attempt))

            except HTTPException:
                # Re-raise FastAPI HTTPExceptions
                raise

            except Exception as e:
                # Catch-all for unhandled logic errors to prevent leaking traces
                logger.exception(f"Unexpected error in process_payment: {str(e)}")
                raise HTTPException(status_code=500, detail="An internal error occurred while processing your payment.")