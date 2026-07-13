"""Unit tests for the payment FastAPI service.

All external systems are mocked:
  * New Relic is neutralised in conftest.py before import.
  * RabbitMQ (pika) is never dialled - we patch main.connect_rabbitmq and set
    main.rabbitmq_channel to a MagicMock.
  * The httpx client is replaced with an AsyncMock returning canned responses.

The FastAPI TestClient is used WITHOUT the ``with`` context-manager form on
purpose: entering the context manager would fire the ``startup`` event which
connects to real RabbitMQ and builds a real httpx client.  Plain instantiation
skips the lifespan so no network is touched.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import main


def _resp(status_code=200, payload=None):
    """Build a stand-in for an httpx.Response (only the bits main.py uses)."""
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=payload if payload is not None else {})
    return r


@pytest.fixture
def client(monkeypatch):
    """A TestClient with rabbitmq + http_client fully mocked."""
    # Never let the startup path dial RabbitMQ.
    monkeypatch.setattr(main, "connect_rabbitmq", MagicMock())

    channel = MagicMock()
    monkeypatch.setattr(main, "rabbitmq_channel", channel)

    http = AsyncMock()
    monkeypatch.setattr(main, "http_client", http)

    tc = TestClient(main.app)
    # Expose the mocks so individual tests can configure / assert on them.
    tc.mock_channel = channel
    tc.mock_http = http
    return tc


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "OK", "service": "payment"}


def test_invalid_user_returns_400(client):
    # validate returns non-200 -> 400 "Invalid user"
    client.mock_http.get = AsyncMock(return_value=_resp(status_code=404))

    resp = client.post("/payment/process", json={"userId": "u1", "cityId": 5})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid user"
    client.mock_channel.basic_publish.assert_not_called()


def test_empty_cart_returns_400(client):
    user_resp = _resp(200, {"email": "a@b.c", "firstName": "Ann"})
    cart_resp = _resp(200, {"items": []})
    client.mock_http.get = AsyncMock(side_effect=[user_resp, cart_resp])

    resp = client.post("/payment/process", json={"userId": "u1", "cityId": 5})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Cart is empty"
    client.mock_channel.basic_publish.assert_not_called()


def test_cart_bad_status_returns_400(client):
    user_resp = _resp(200, {"email": "a@b.c", "firstName": "Ann"})
    cart_resp = _resp(500, {})
    client.mock_http.get = AsyncMock(side_effect=[user_resp, cart_resp])

    resp = client.post("/payment/process", json={"userId": "u1", "cityId": 5})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Failed to get cart"


def test_happy_path_returns_success_and_publishes(client):
    user_resp = _resp(200, {"email": "ann@example.com", "firstName": "Ann"})
    cart_resp = _resp(200, {"items": [
        {"price": 100, "quantity": 2},   # 200
        {"price": 50, "quantity": 3},    # 150
    ]})
    client.mock_http.get = AsyncMock(side_effect=[user_resp, cart_resp])
    client.mock_http.delete = AsyncMock(return_value=_resp(200, {}))

    resp = client.post("/payment/process", json={"userId": "u42", "cityId": 7})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["total"] == 350
    assert body["transactionId"].startswith("TXN-")

    # Order was published to RabbitMQ exactly once, on the "orders" routing key.
    client.mock_channel.basic_publish.assert_called_once()
    kwargs = client.mock_channel.basic_publish.call_args.kwargs
    assert kwargs["routing_key"] == main.ROUTING_KEY
    assert kwargs["exchange"] == main.EXCHANGE
    import json as _json
    event = _json.loads(kwargs["body"])
    assert event["total"] == 350
    assert event["userId"] == "u42"
    assert event["userEmail"] == "ann@example.com"

    # Cart was cleared afterwards.
    client.mock_http.delete.assert_awaited_once()
