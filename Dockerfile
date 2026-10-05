# Python image shared by the paper loop and the read-only API.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install '.[api]'
COPY config ./config

# Run as an unprivileged user; data and reports live on a volume.
RUN useradd --create-home --uid 1000 jev && mkdir -p data reports && chown -R jev:jev data reports
USER jev

VOLUME ["/app/data", "/app/reports"]
CMD ["python", "-m", "jevtrade.paper", "run"]
