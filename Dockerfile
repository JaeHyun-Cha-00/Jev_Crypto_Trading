# Python image for the read-only API (docker compose). Any jevtrade CLI runs in it too.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JEVTRADE_CONFIG=/app/config/default.yaml

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install '.[api]'
COPY config ./config

# Run as an unprivileged user; data and reports live on a volume.
RUN useradd --create-home --uid 1000 jev && mkdir -p data reports && chown -R jev:jev data reports
USER jev

VOLUME ["/app/data", "/app/reports"]
CMD ["python", "-m", "jevtrade.api", "--host", "0.0.0.0", "--port", "8000"]
