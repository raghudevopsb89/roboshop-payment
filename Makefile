.PHONY: build run docker-build clean

build:
	pip install -r requirements.txt

run:
	AMQP_HOST=localhost CART_URL=http://localhost:8003 USER_URL=http://localhost:8001 uvicorn main:app --host 0.0.0.0 --port 8005 --reload

docker-build:
	env
	docker build -t raghudevopsb89.azurecr.io/roboshop-payment:${GITHUB_SHA} .

docker-push:
	docker push raghudevopsb89.azurecr.io/roboshop-payment:${GITHUB_SHA}

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
