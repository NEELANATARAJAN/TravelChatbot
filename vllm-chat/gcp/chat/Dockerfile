# The chat web app (no GPU). deploy_gcp_chat.sh copies the needed project files next to this
# Dockerfile in a temporary folder and builds it with Cloud Build.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static ./static
COPY prompts ./prompts
COPY models.json .

ENV MODELS_FILE=/app/models.json PORT=8080 PYTHONUNBUFFERED=1
RUN useradd --system --no-create-home app && chown -R app /app
USER app

# One worker on purpose: the rate limiter keeps its counters in memory.
CMD exec uvicorn server:app --host 0.0.0.0 --port "$PORT"
