FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app
# Served at GET /waiter for agents to fetch. The waiter only makes HTTP requests
# and prints results. client/switchboard.py — which runs commands and invokes
# models — is deliberately NOT copied: the bus serves a tool, never a daemon.
COPY client/waiter.py ./client/waiter.py

# Mount point for the ledger. Bind-mounted to ./data on the host in compose.
RUN mkdir -p /app/data

EXPOSE 5585

# /health returns 503 until the gateway is connected and the channel resolves,
# so a dropped websocket marks the container unhealthy and autoheal restarts it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.getenv('PORT','5585')+'/health', timeout=4)"

CMD ["python", "-m", "app"]
