# CaCs – cardAndCalSyncer (vdirsyncer + FastAPI Web-UI/Scheduler)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -r -u 1000 -m cacs

# Module liegen flach in /app (main.py importiert "from db import ...")
COPY app/ /app/

ENV CACS_DATA=/data \
    SYNC_INTERVAL=300 \
    LOG_LEVEL=INFO

USER cacs
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
