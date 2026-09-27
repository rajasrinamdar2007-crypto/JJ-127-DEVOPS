FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Real check, not an import: verifies this container can actually reach the broker with
# its own credentials. An import-only healthcheck passes even when the agent is
# wedged, which is the one failure mode that matters here.
HEALTHCHECK --interval=15s --timeout=10s --retries=3 CMD ["python", "-m", "app.health"]

CMD ["python", "-m", "app.main"]
