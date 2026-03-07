# Stage 1: Builder — install dependencies
FROM python:3.11-slim AS builder

WORKDIR /build

COPY pyproject.toml .
COPY oasisagent/ oasisagent/
COPY known_fixes/ known_fixes/
COPY README.md .
COPY LICENSE .

RUN pip install --no-cache-dir --prefix=/install .

# Stage 2: Runtime — lean image with no build tools
FROM python:3.11-slim AS runtime

RUN groupadd --gid 1000 oasis && \
    useradd --uid 1000 --gid oasis --create-home oasis

COPY --from=builder /install /usr/local
COPY --from=builder /build/known_fixes /app/known_fixes

WORKDIR /app

USER oasis

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "oasisagent"]
