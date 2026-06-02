FROM python:3.12-slim

LABEL maintainer="nas-bridge" \
      description="NAS Bridge — read-only HTTP API for a Synology NAS Jobs share"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN addgroup --system bridge && adduser --system --ingroup bridge bridge

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

USER bridge

CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8080} --workers 1 --log-level ${LOG_LEVEL:-info}"]
