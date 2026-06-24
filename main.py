import newrelic.agent
newrelic.agent.initialize()

import os
import json
import uuid
import time
import signal
import logging
import pika
import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator

class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": "payment",
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)
        return json.dumps(payload)

_handler = logging.StreamHandler()
_handler.setFormatter(JsonLogFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
logger = logging.getLogger("payment")

def jlog(level, msg, **extra):
    rec = logger.makeRecord(logger.name, getattr(logging, level.upper()), "", 0, msg, None, None)
    rec.extra_fields = extra
    logger.handle(rec)

app = FastAPI(title="RoboShop Payment Service")
Instrumentator().instrument(app).expose(app, include_in_schema=False, should_gzip=True)

_req_seq = 0

@app.middleware("http")
async def request_logger(request: Request, call_next):
    global _req_seq
    if request.url.path in ("/metrics", "/health"):
        return await call_next(request)
    _req_seq += 1
    req_id = request.headers.get("x-request-id") or f"{os.getpid()}-{_req_seq}"
    start = time.monotonic()
    jlog("info", "req.start", reqId=req_id, method=request.method, path=request.url.path,
         remote=request.client.host if request.client else None)
    status = 0
    event = "finish"
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["x-request-id"] = req_id
        return response
    except Exception as e:
        event = "error"
        jlog("error", "req.error", reqId=req_id, path=request.url.path, error=str(e))
        raise
    finally:
        dur_ms = round((time.monotonic() - start) * 1000, 1)
        jlog("info", f"req.{event}", reqId=req_id, method=request.method, path=request.url.path,
             status=status, durMs=dur_ms)

AMQP_HOST = os.getenv("AMQP_HOST", "rabbitmq")
AMQP_USER = os.getenv("AMQP_USER", "guest")
AMQP_PASS = os.getenv("AMQP_PASS", "guest")
CART_URL = os.getenv("CART_URL", "http://cart:8003")
USER_URL = os.getenv("USER_URL", "http://user:8001")

EXCHANGE = "roboshop"
ROUTING_KEY = "orders"

rabbitmq_connection = None
rabbitmq_channel = None


def connect_rabbitmq():
    global rabbitmq_connection, rabbitmq_channel
    credentials = pika.PlainCredentials(AMQP_USER, AMQP_PASS)
    for i in range(30):
        try:
            rabbitmq_connection = pika.BlockingConnection(
                pika.ConnectionParameters(host=AMQP_HOST, credentials=credentials)
            )
            rabbitmq_channel = rabbitmq_connection.channel()
            rabbitmq_channel.exchange_declare(exchange=EXCHANGE, exchange_type="direct", durable=True)
            rabbitmq_channel.queue_declare(queue="orders", durable=True)
            rabbitmq_channel.queue_bind(queue="orders", exchange=EXCHANGE, routing_key=ROUTING_KEY)
            logger.info("Connected to RabbitMQ")
            return
        except Exception as e:
            logger.warning(f"RabbitMQ connection attempt {i+1}/30 failed: {e}")
            time.sleep(2)
    raise Exception("Failed to connect to RabbitMQ")


class PaymentRequest(BaseModel):
    userId: str
    cityId: int


@app.on_event("startup")
async def startup():
    connect_rabbitmq()
    jlog("info", "server.listen", pid=os.getpid())


@app.on_event("shutdown")
async def shutdown_event():
    jlog("warn", "server.shutdown.done", pid=os.getpid())


def _sig_handler(signum, _frame):
    jlog("warn", "server.shutdown.start", signal=signal.Signals(signum).name)

signal.signal(signal.SIGTERM, _sig_handler)
signal.signal(signal.SIGINT, _sig_handler)


@app.get("/health")
def health():
    return {"status": "OK", "service": "payment"}


@app.post("/payment/process")
async def process_payment(request: PaymentRequest):
    # Validate user
    async with httpx.AsyncClient() as client:
        try:
            user_resp = await client.get(f"{USER_URL}/validate/{request.userId}")
            if user_resp.status_code != 200:
                raise HTTPException(status_code=400, detail="Invalid user")
            user = user_resp.json()
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="User service unavailable")

        # Get cart
        try:
            cart_resp = await client.get(f"{CART_URL}/cart/{request.userId}")
            if cart_resp.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to get cart")
            cart = cart_resp.json()
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="Cart service unavailable")

    if not cart.get("items"):
        raise HTTPException(status_code=400, detail="Cart is empty")

    # Mock payment processing
    total = sum(item["price"] * item["quantity"] for item in cart["items"])
    transaction_id = f"TXN-{uuid.uuid4().hex[:8].upper()}"

    # Build order event
    order_event = {
        "userId": request.userId,
        "userEmail": user.get("email", ""),
        "userName": user.get("firstName", "Customer"),
        "items": cart["items"],
        "total": total,
        "cityId": request.cityId,
        "transactionId": transaction_id,
        "status": "PAID",
    }

    # Publish to RabbitMQ
    try:
        rabbitmq_channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=ROUTING_KEY,
            body=json.dumps(order_event),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        logger.info(f"Payment processed: {transaction_id} for user {request.userId}")
    except Exception as e:
        logger.error(f"Failed to publish order event: {e}")
        connect_rabbitmq()
        rabbitmq_channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=ROUTING_KEY,
            body=json.dumps(order_event),
            properties=pika.BasicProperties(delivery_mode=2),
        )

    # Clear cart after payment
    async with httpx.AsyncClient() as client:
        try:
            await client.delete(f"{CART_URL}/cart/{request.userId}")
        except Exception:
            logger.warning("Failed to clear cart after payment")

    return {
        "status": "SUCCESS",
        "transactionId": transaction_id,
        "total": total,
    }
