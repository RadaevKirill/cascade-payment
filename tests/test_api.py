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
