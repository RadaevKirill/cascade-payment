"""A small payment cascade with deliberately fake providers and orders."""
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from enum import Enum
from uuid import uuid4

import stripe
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DATABASE_PATH", "payments.db")
app = FastAPI(title="Payment cascading demo")

# The order catalogue stands in for an Orders service. Amounts never come from the client.
ORDERS = {
    "order-1": ("100.00", "USD"),
    "order-2": ("49.90", "EUR"),
    "order-3": ("250.00", "USD"),
}


@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with database() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
            amount TEXT NOT NULL, currency TEXT NOT NULL, method TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, request_data TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id TEXT PRIMARY KEY, payment_id TEXT NOT NULL, number INTEGER NOT NULL,
            provider TEXT NOT NULL, status TEXT NOT NULL, external_id TEXT,
            payment_url TEXT, decline_code TEXT, category TEXT,
            UNIQUE(payment_id, number), FOREIGN KEY(payment_id) REFERENCES payments(id)
        );
        CREATE TABLE IF NOT EXISTS webhook_events (
            event_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, status TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL
        );
        """)


class Outcome(str, Enum):
    success = "success"
    soft_decline = "soft_decline"
    hard_decline = "hard_decline"
    unknown = "unknown"
    action_required = "action_required"


class PaymentRequest(BaseModel):
    order_id: str
    method: str = Field(default="card", pattern="^card$")
    # These fields control the fake providers and are never sent to a real provider.
    provider_a: Outcome = Outcome.soft_decline
    provider_b: Outcome = Outcome.success


class WebhookRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    attempt_id: str
    status: str = Field(pattern="^(succeeded|failed)$")
    amount: Decimal = Field(gt=0)
    currency: str


def payment_data(db, payment_id):
    payment = db.execute("SELECT id, order_id, amount, currency, method, status FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not payment:
        raise HTTPException(404, "Payment not found")
    data = dict(payment)
    data["attempts"] = [dict(row) for row in db.execute(
        "SELECT id, number, provider, status, external_id, payment_url, decline_code, category FROM attempts WHERE payment_id = ? ORDER BY number", (payment_id,)
    )]
    return data


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    with database() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/api/v1/payments", status_code=201)
def create_payment(body: PaymentRequest, idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200)):
    if body.order_id not in ORDERS:
        raise HTTPException(404, "Order not found")
    amount, currency = ORDERS[body.order_id]
    with database() as db:
        # Serializing writes also prevents parallel requests from creating duplicate attempts.
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM payments WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if existing:
            if existing["request_data"] != body.model_dump_json():
                raise HTTPException(409, "Idempotency key was used with different parameters")
            return payment_data(db, existing["id"])
        if db.execute("SELECT 1 FROM payments WHERE order_id = ?", (body.order_id,)).fetchone():
            raise HTTPException(409, "A payment already exists for this order")
        payment_id = str(uuid4())
        db.execute("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (payment_id, body.order_id, amount, currency, body.method, idempotency_key, body.model_dump_json(), "PROCESSING"))
        for number, (provider, outcome) in enumerate((("provider-a", body.provider_a), ("provider-b", body.provider_b)), 1):
            attempt_id = str(uuid4())
            if outcome in (Outcome.success, Outcome.action_required):
                status = "AWAITING_PAYMENT"
                external_id = str(uuid4())
                url = f"https://example.com/pay/{external_id}"
                category = "ACTION_REQUIRED" if outcome == Outcome.action_required else None
                db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (attempt_id, payment_id, number, provider, status, external_id, url, None, category))
                db.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))
                break
            category = outcome.value.upper()
            status = "UNKNOWN" if outcome == Outcome.unknown else "DECLINED"
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)", (attempt_id, payment_id, number, provider, status, outcome.value, category))
            if outcome != Outcome.soft_decline:
                db.execute("UPDATE payments SET status = ? WHERE id = ?", ("REVIEW_REQUIRED" if outcome == Outcome.unknown else "FAILED", payment_id))
                break
        else:
            db.execute("UPDATE payments SET status = 'FAILED' WHERE id = ?", (payment_id,))
        return payment_data(db, payment_id)


@app.get("/api/v1/payments/{payment_id}")
def get_payment(payment_id: str):
    with database() as db:
        return payment_data(db, payment_id)


@app.get("/api/v1/payments/{payment_id}/attempts")
def get_attempts(payment_id: str):
    with database() as db:
        return payment_data(db, payment_id)["attempts"]


@app.post("/api/v1/webhooks/test")
def test_webhook(body: WebhookRequest, x_test_secret: str = Header(alias="X-Test-Secret")):
    # A shared secret is sufficient only for this local, simulated integration.
    import hmac
    if not hmac.compare_digest(x_test_secret, os.getenv("TEST_WEBHOOK_SECRET", "demo-secret")):
        raise HTTPException(401, "Invalid webhook secret")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        event = db.execute("SELECT * FROM webhook_events WHERE event_id = ?", (body.event_id,)).fetchone()
        if event:
            if event["attempt_id"] != body.attempt_id or event["status"] != body.status or Decimal(event["amount"]) != body.amount or event["currency"] != body.currency.upper():
                raise HTTPException(409, "Event ID was used for another event")
            return {"status": "duplicate"}
        attempt = db.execute("SELECT * FROM attempts WHERE id = ?", (body.attempt_id,)).fetchone()
        if not attempt:
            raise HTTPException(404, "Attempt not found")
        if attempt["provider"] not in ("provider-a", "provider-b"):
            raise HTTPException(400, "This webhook is only for simulated providers")
        payment = db.execute("SELECT * FROM payments WHERE id = ?", (attempt["payment_id"],)).fetchone()
        if Decimal(payment["amount"]) != body.amount or payment["currency"] != body.currency.upper():
            raise HTTPException(400, "Amount or currency mismatch")
        target = "SUCCEEDED" if body.status == "succeeded" else "FAILED"
        if attempt["status"] == "AWAITING_PAYMENT":
            db.execute("UPDATE attempts SET status = ? WHERE id = ?", (target, body.attempt_id))
            db.execute("UPDATE payments SET status = ? WHERE id = ?", (target, payment["id"]))
        elif attempt["status"] != target:
            # Preserve confirmed success, but flag contradictory events for review.
            db.execute("UPDATE payments SET status = 'REVIEW_REQUIRED' WHERE id = ?", (payment["id"],))
        db.execute("INSERT INTO webhook_events VALUES (?, ?, ?, ?, ?)", (body.event_id, body.attempt_id, body.status, str(body.amount), body.currency.upper()))
        return {"status": "accepted"}


class StripePaymentRequest(BaseModel):
    order_id: str


def stripe_settings():
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key.startswith("sk_test_"):
        raise HTTPException(503, "Set STRIPE_SECRET_KEY to a Stripe test secret key")
    base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    return key, base_url


@app.post("/api/v1/payments/stripe", status_code=201)
def create_stripe_payment(body: StripePaymentRequest, idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200)):
    key, base_url = stripe_settings()
    if body.order_id not in ORDERS:
        raise HTTPException(404, "Order not found")
    amount, currency = ORDERS[body.order_id]
    request_data = "stripe:" + body.model_dump_json()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM payments WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if existing:
            if existing["request_data"] != request_data:
                raise HTTPException(409, "Idempotency key was used with different parameters")
            return payment_data(db, existing["id"])
        if db.execute("SELECT 1 FROM payments WHERE order_id = ?", (body.order_id,)).fetchone():
            raise HTTPException(409, "A payment already exists for this order")
        payment_id, attempt_id = str(uuid4()), str(uuid4())
        db.execute("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (payment_id, body.order_id, amount, currency, "card", idempotency_key, request_data, "PROCESSING"))
        db.execute("INSERT INTO attempts VALUES (?, ?, 1, 'stripe', 'SUBMITTING', NULL, NULL, NULL, NULL)", (attempt_id, payment_id))
    # Persist the attempt before the network call. An uncertain result must not start a new attempt.
    try:
        session = stripe.checkout.Session.create(
            api_key=key,
            idempotency_key=attempt_id,
            mode="payment",
            payment_method_types=["card"],
            line_items=[{"price_data": {"currency": currency.lower(), "unit_amount": int(Decimal(amount) * 100), "product_data": {"name": f"Order {body.order_id}"}}, "quantity": 1}],
            success_url=f"{base_url}/checkout/success?payment_id={payment_id}",
            cancel_url=f"{base_url}/checkout/cancel?payment_id={payment_id}",
            metadata={"payment_id": payment_id, "attempt_id": attempt_id},
        )
        if session.livemode or not session.id or not session.url:
            raise ValueError("Invalid Stripe test session")
    except (stripe.StripeError, ValueError):
        with database() as db:
            db.execute("UPDATE attempts SET status = 'UNKNOWN', category = 'UNKNOWN' WHERE id = ? AND status = 'SUBMITTING'", (attempt_id,))
            db.execute("UPDATE payments SET status = 'REVIEW_REQUIRED' WHERE id = ? AND status = 'PROCESSING'", (payment_id,))
            return payment_data(db, payment_id)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        attempt = db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        # A webhook can arrive before the create call returns.
        if attempt["external_id"] and attempt["external_id"] != session.id:
            db.execute("UPDATE payments SET status = 'REVIEW_REQUIRED' WHERE id = ?", (payment_id,))
        else:
            db.execute("UPDATE attempts SET external_id = ?, payment_url = ?, status = CASE WHEN status = 'SUBMITTING' THEN 'AWAITING_PAYMENT' ELSE status END WHERE id = ?", (session.id, session.url, attempt_id))
            db.execute("UPDATE payments SET status = 'AWAITING_PAYMENT' WHERE id = ? AND status = 'PROCESSING'", (payment_id,))
        return payment_data(db, payment_id)


@app.get("/checkout/{result}")
def checkout_return(result: str, payment_id: str):
    if result not in ("success", "cancel"):
        raise HTTPException(404, "Not found")
    # A redirect is not proof of payment; the signed webhook updates the status.
    return {"message": "Check the payment status via the API; the webhook confirms payment.", "payment_id": payment_id, "result": result}


@app.post("/api/v1/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(alias="Stripe-Signature")):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "STRIPE_WEBHOOK_SECRET is not configured")
    try:
        event = stripe.Webhook.construct_event(await request.body(), stripe_signature, secret)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, "Invalid Stripe webhook")
    if event.get("livemode"):
        raise HTTPException(400, "Live events are not supported")
    event_type = event["type"]
    if event_type not in ("checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed", "checkout.session.expired"):
        return {"status": "ignored"}
    session = event["data"]["object"]
    metadata = session.get("metadata") or {}
    attempt_id = metadata.get("attempt_id")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM webhook_events WHERE event_id = ?", (event["id"],)).fetchone():
            return {"status": "duplicate"}
        attempt = db.execute("SELECT * FROM attempts WHERE id = ? AND provider = 'stripe'", (attempt_id,)).fetchone()
        if not attempt:
            raise HTTPException(400, "Unknown Stripe attempt")
        payment = db.execute("SELECT * FROM payments WHERE id = ?", (attempt["payment_id"],)).fetchone()
        if (metadata.get("payment_id") != payment["id"] or (attempt["external_id"] and attempt["external_id"] != session.get("id"))
            or session.get("amount_total") != int(Decimal(payment["amount"]) * 100)
            or (session.get("currency") or "").upper() != payment["currency"]):
            raise HTTPException(400, "Stripe session does not match payment")
        if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and session.get("payment_status") == "paid":
            target = "SUCCEEDED"
        elif event_type == "checkout.session.expired":
            target = "EXPIRED"
        elif event_type == "checkout.session.async_payment_failed":
            target = "FAILED"
        else:
            # A completed session may still be unpaid; wait for a later event.
            target = None
        if target:
            if attempt["status"] in ("SUBMITTING", "AWAITING_PAYMENT", "UNKNOWN"):
                db.execute("UPDATE attempts SET status = ?, external_id = ? WHERE id = ?", (target, session["id"], attempt_id))
                db.execute("UPDATE payments SET status = ? WHERE id = ?", (target, payment["id"]))
            elif attempt["status"] != target:
                db.execute("UPDATE payments SET status = 'REVIEW_REQUIRED' WHERE id = ?", (payment["id"],))
        db.execute("INSERT INTO webhook_events VALUES (?, ?, ?, ?, ?)", (event["id"], attempt_id, event_type, payment["amount"], payment["currency"]))
    return {"status": "accepted"}
