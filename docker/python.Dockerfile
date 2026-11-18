# One image for all Python services (gateway, approval service, mocks); the command differs.
FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock .
RUN pip install -r requirements.lock
COPY govlib ./govlib
COPY gateway ./gateway
COPY approval_service ./approval_service
COPY mocks ./mocks
COPY telemetry/__init__.py telemetry/otlp.py ./telemetry/
COPY telemetry/evidence_store ./telemetry/evidence_store
COPY cp_compile ./cp_compile
RUN useradd --uid 10001 --create-home app
USER 10001
