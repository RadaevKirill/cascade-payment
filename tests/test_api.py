import pytest
from fastapi.testclient import TestClient
import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.db"))
    with TestClient(main.app) as client:
        yield client


def create(client, **kwargs):
    return client.post("/api/v1/payments", headers={"Idempotency-Key": "key-1"}, json={"order_id": "order-1", **kwargs})


def test_cascade_and_idempotency(client):
    response = create(client)
    assert response.status_code == 201
    payment = response.json()
    assert payment["status"] == "AWAITING_PAYMENT"
    assert [a["status"] for a in payment["attempts"]] == ["DECLINED", "AWAITING_PAYMENT"]
    assert create(client).json() == payment
    assert create(client, provider_a="success").status_code == 409
    assert client.post("/api/v1/payments", headers={"Idempotency-Key": "other"}, json={"order_id": "order-1"}).status_code == 409


@pytest.mark.parametrize("outcome, expected", [("hard_decline", "FAILED"), ("unknown", "REVIEW_REQUIRED"), ("action_required", "AWAITING_PAYMENT")])
def test_stop_cascade(client, outcome, expected):
    payment = create(client, provider_a=outcome).json()
    assert payment["status"] == expected
    assert len(payment["attempts"]) == 1


def test_webhook(client):
    payment = create(client).json()
    payload = {"event_id": "event-1", "attempt_id": payment["attempts"][-1]["id"], "status": "succeeded", "amount": "100.00", "currency": "USD"}
    url = "/api/v1/webhooks/test"
    assert client.post(url, headers={"X-Test-Secret": "wrong"}, json=payload).status_code == 401
    assert client.post(url, headers={"X-Test-Secret": "demo-secret"}, json={**payload, "amount": "1"}).status_code == 400
    assert client.post(url, headers={"X-Test-Secret": "demo-secret"}, json=payload).json()["status"] == "accepted"
    assert client.post(url, headers={"X-Test-Secret": "demo-secret"}, json=payload).json()["status"] == "duplicate"
    assert client.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "SUCCEEDED"
    assert client.post(url, headers={"X-Test-Secret": "demo-secret"}, json={**payload, "event_id": "event-2", "status": "failed"}).status_code == 200
    assert client.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "REVIEW_REQUIRED"


def test_stripe_checkout_and_signed_webhook(client, monkeypatch):
    import hashlib
    import hmac
    import json
    import time
    from types import SimpleNamespace

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_example")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_example")
    calls = []
    def create_session(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id="cs_test_123", url="https://checkout.stripe.com/test", livemode=False)
    monkeypatch.setattr(main.stripe.checkout.Session, "create", create_session)
    headers = {"Idempotency-Key": "stripe-key"}
    response = client.post("/api/v1/payments/stripe", headers=headers, json={"order_id": "order-1"})
    assert response.status_code == 201
    payment = response.json()
    attempt = payment["attempts"][0]
    assert attempt["payment_url"] == "https://checkout.stripe.com/test"
    assert calls[0]["line_items"][0]["price_data"]["unit_amount"] == 10000
    assert client.post("/api/v1/payments/stripe", headers=headers, json={"order_id": "order-1"}).json() == payment
    assert len(calls) == 1
    fake_payload = {"event_id": "fake", "attempt_id": attempt["id"], "status": "succeeded", "amount": "100", "currency": "USD"}
    assert client.post("/api/v1/webhooks/test", headers={"X-Test-Secret": "demo-secret"}, json=fake_payload).status_code == 400
    event = {"id": "evt_123", "object": "event", "livemode": False, "type": "checkout.session.completed", "data": {"object": {"id": "cs_test_123", "object": "checkout.session", "amount_total": 10000, "currency": "usd", "payment_status": "paid", "metadata": {"payment_id": payment["id"], "attempt_id": attempt["id"]}}}}
    payload = json.dumps(event).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(b"whsec_example", timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    webhook_headers = {"Stripe-Signature": f"t={timestamp},v1={signature}", "Content-Type": "application/json"}
    assert client.post("/api/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": "invalid"}).status_code == 400
    assert client.post("/api/v1/webhooks/stripe", content=payload, headers=webhook_headers).json()["status"] == "accepted"
    assert client.post("/api/v1/webhooks/stripe", content=payload, headers=webhook_headers).json()["status"] == "duplicate"
    assert client.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "SUCCEEDED"


def test_stripe_uncertain_result_stops(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_example")
    def fail(**kwargs):
        raise main.stripe.APIConnectionError("timeout")
    monkeypatch.setattr(main.stripe.checkout.Session, "create", fail)
    response = client.post("/api/v1/payments/stripe", headers={"Idempotency-Key": "stripe-key"}, json={"order_id": "order-1"})
    assert response.json()["status"] == "REVIEW_REQUIRED"
    assert response.json()["attempts"][0]["status"] == "UNKNOWN"
