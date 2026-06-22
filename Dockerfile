# CaCs – cardAndCalSyncer (vdirsyncer + FastAPI web UI/scheduler)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -r -u 1000 -m cacs

# Modules live flat in /app (main.py imports "from db import ...")
COPY app/ /app/

ENV CACS_DATA=/data \
    SYNC_INTERVAL=300 \
    LOG_LEVEL=INFO

USER cacs
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
