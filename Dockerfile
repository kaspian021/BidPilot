# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# lxml needs libxml2/libxslt at build time
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

COPY config ./config
COPY deploy/entrypoint.sh ./deploy/entrypoint.sh
RUN chmod +x ./deploy/entrypoint.sh && mkdir -p /app/data

# run as a non-root user
RUN useradd --create-home --uid 10001 bidpilot && chown -R bidpilot:bidpilot /app
USER bidpilot

ENTRYPOINT ["./deploy/entrypoint.sh"]
CMD ["run"]
