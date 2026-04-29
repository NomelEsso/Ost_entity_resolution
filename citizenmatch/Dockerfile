FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py .
COPY cleaners.py .
COPY bq_udfs.py .
COPY pipeline.py .
COPY main.py .

ENV PORT=8080

CMD exec gunicorn \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --timeout 3600 \
    main:app
