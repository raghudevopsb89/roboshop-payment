.PHONY: build run docker-build clean

build:
	pip install -r requirements.txt

run:
	AMQP_HOST=localhost CART_URL=http://localhost:8003 USER_URL=http://localhost:8001 uvicorn main:app --host 0.0.0.0 --port 8005 --reload

docker-build:
	docker build -t raghudevopsb89.azurecr.io/roboshop-payment .
	docker push raghudevopsb89.azurecr.io/roboshop-payment

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
