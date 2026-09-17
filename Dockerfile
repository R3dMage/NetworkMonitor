# syntax=docker/dockerfile:1
FROM golang:1.26-alpine AS cron-build
ARG SUPERCRONIC_VERSION=v0.2.48
RUN CGO_ENABLED=0 go install github.com/aptible/supercronic@${SUPERCRONIC_VERSION}

FROM python:3.12-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY --from=cron-build /go/bin/supercronic /usr/local/bin/supercronic
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /data && chown app:app /data
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install .
USER app
EXPOSE 8080
ENTRYPOINT ["network-history"]
CMD ["serve"]

FROM app AS test
USER root
RUN apt-get update && apt-get install --no-install-recommends -y sqlite3 \
    && rm -rf /var/lib/apt/lists/*
COPY tests/ ./tests/
RUN pip install ".[test]"
USER app
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "-p", "no:cacheprovider"]

FROM app AS production
