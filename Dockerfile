FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8005
ENV PYTHONUNBUFFERED=1
ENV NEW_RELIC_APP_NAME=payment
ENV NEW_RELIC_DISTRIBUTED_TRACING_ENABLED=true
ENV NEW_RELIC_LOG=stderr
ENV NEW_RELIC_LOG_LEVEL=warning
CMD ["newrelic-admin", "run-program", "uvicorn", "main:app", "--workers", "5", "--host", "0.0.0.0", "--port", "8005", "--no-access-log", "--backlog", "2048"]
