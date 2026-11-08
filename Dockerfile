# Project Udyam Digital — Airtel SYNC 2026, Track A
# Tesseract is a system binary the KYC pipeline shells out to, so this image
# exists mainly to install it. A plain Python buildpack cannot.

FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# One worker, deliberately. Advisor sessions and orders are held in memory
# (app/advisor.py:_SESSIONS, app/orders.py:_ORDERS), so a second worker would
# drop every other request into a session it cannot see.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
