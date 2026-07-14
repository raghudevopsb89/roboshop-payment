.PHONY: build run unit-test integration-test coverage docker-build clean

build:
	pip install -r requirements.txt

run:
	AMQP_HOST=localhost CART_URL=http://localhost:8003 USER_URL=http://localhost:8001 uvicorn main:app --host 0.0.0.0 --port 8005 --reload

unit-test:
	pytest -m "not integration"

integration-test:
	pytest -m integration

coverage:
	pytest -m "not integration" --cov=. --cov-report=xml --cov-report=term-missing

docker-build:
	env
	docker build -t raghudevopsb89.azurecr.io/roboshop-payment:${GITHUB_SHA} .

docker-push:
	docker push raghudevopsb89.azurecr.io/roboshop-payment:${GITHUB_SHA}

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

sonar_token := $(shell az keyvault secret show --name sonarqube-token --vault-name roboshopb89 --query "value" -o tsv)

sonar-scan:
	echo /home/runner/sonar-scanner-7.1.0.4889-linux-x64/bin/sonar-scanner -Dsonar.projectKey=roboshop-payment -Dsonar.host.url=http://10.1.0.46:9000 -Dsonar.token=$(sonar_token) -Dsonar.qualitygate.wait=true -Dsonar.python.coverage.reportPaths=coverage.xml
