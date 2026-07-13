"""Component integration test for the payment service against a REAL RabbitMQ.

The unit tests (tests/test_payment.py) mock the pika channel entirely. Here we stand up
an actual RabbitMQ broker in a container via Testcontainers and let the app's own
``connect_rabbitmq()`` declare the real ``roboshop`` exchange + ``orders`` queue and
publish through it. The cross-service HTTP calls (user/cart) are the only thing mocked --
per the "use your OWN real dependency, mock the OTHER services" principle.

We then CONSUME from the ``orders`` queue with an independent pika connection and assert
the published message shape -- proving the publish really reaches a live broker on the
right exchange/queue/routing-key, exactly what the orders service's listener will read.

Run with:   pytest -m integration
Excluded from the default unit run via the ``-m "not integration"`` addopts.
"""
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pika
import pytest
from fastapi.testclient import TestClient
from testcontainers.rabbitmq import RabbitMqContainer

import main


def _http_resp(status_code=200, payload=None):
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=payload if payload is not None else {})
    return r


# Image is overridable so offline/air-gapped runners can point at a locally cached tag;
# CI uses the default management image.
RABBITMQ_IMAGE = os.getenv("TC_RABBITMQ_IMAGE", "rabbitmq:3-management")


@pytest.fixture(scope="session")
def rabbitmq_container():
    with RabbitMqContainer(RABBITMQ_IMAGE) as rabbit:
        yield rabbit


@pytest.fixture
def wired(rabbitmq_container, monkeypatch):
    """Point the app at the real broker and mock only the cross-service HTTP client.

    main.connect_rabbitmq() builds pika.ConnectionParameters WITHOUT a port (defaults to
    5672), but the container maps 5672 to a random host port, so we inject the mapped port
    by wrapping pika.ConnectionParameters. The app's real exchange/queue/binding
    declarations then run against the container.
    """
    host = rabbitmq_container.get_container_host_ip()
    port = int(rabbitmq_container.get_exposed_port(rabbitmq_container.port))

    monkeypatch.setattr(main, "AMQP_HOST", host)
    monkeypatch.setattr(main, "AMQP_USER", rabbitmq_container.username)
    monkeypatch.setattr(main, "AMQP_PASS", rabbitmq_container.password)

    real_conn_params = pika.ConnectionParameters

    def conn_params_with_port(**kwargs):
        kwargs.setdefault("port", port)
        return real_conn_params(**kwargs)

    monkeypatch.setattr(main.pika, "ConnectionParameters", conn_params_with_port)

    # Real connection + real exchange/queue/binding declaration against the container.
    main.connect_rabbitmq()

    # Mock ONLY the cross-service HTTP: valid user, then a non-empty cart.
    http = AsyncMock()
    user = _http_resp(200, {"email": "ann@example.com", "firstName": "Ann"})
    cart = _http_resp(200, {"items": [
        {"price": 100, "quantity": 2},   # 200
        {"price": 50, "quantity": 3},    # 150
    ]})
    http.get = AsyncMock(side_effect=[user, cart])
    http.delete = AsyncMock(return_value=_http_resp(200, {}))
    monkeypatch.setattr(main, "http_client", http)

    # Drain the queue so "exactly one message" is unambiguous.
    consume_params = rabbitmq_container.get_connection_params()
    drain = pika.BlockingConnection(consume_params)
    try:
        drain.channel().queue_purge("orders")
    finally:
        drain.close()

    # Plain TestClient (no lifespan) -- we already connected rabbit + set http_client.
    client = TestClient(main.app)
    client.consume_params = consume_params
    return client


@pytest.mark.integration
def test_process_payment_publishes_real_message_to_orders_queue(wired):
    resp = wired.post("/payment/process", json={"userId": "u42", "cityId": 7})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["total"] == 350
    assert body["transactionId"].startswith("TXN-")

    # Independently consume from the REAL 'orders' queue on the same broker.
    conn = pika.BlockingConnection(wired.consume_params)
    try:
        channel = conn.channel()
        method, _props, raw = channel.basic_get(queue="orders", auto_ack=True)
        assert method is not None, "expected a message on the 'orders' queue"

        event = json.loads(raw)
        assert event["userId"] == "u42"
        assert event["total"] == 350          # computed from the mocked cart
        assert event["status"] == "PAID"
        assert event["transactionId"].startswith("TXN-")
        assert event["userEmail"] == "ann@example.com"
        assert event["cityId"] == 7

        # Exactly one message: the queue is now empty.
        method2, _p2, _b2 = channel.basic_get(queue="orders", auto_ack=True)
        assert method2 is None, "expected exactly one message on 'orders'"
    finally:
        conn.close()
