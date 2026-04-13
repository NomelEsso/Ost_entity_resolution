FROM python:3.10-slim

# Prevent Python from buffering stdout/stderr (important for Cloud Run logs)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY config.py .
COPY cleaners.py .
COPY pipeline.py .
COPY main.py .

# Cloud Run injects PORT env var (default 8080)
ENV PORT=8080

# Run with gunicorn — 1 worker, 60-min timeout for long pipeline runs
CMD exec gunicorn \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --timeout 3600 \
    main:app