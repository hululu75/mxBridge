FROM python:3.12-alpine AS builder

RUN apk add --no-cache build-base libffi-dev openssl-dev sqlcipher-dev

WORKDIR /install

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install/deps -r requirements.txt

FROM python:3.12-alpine

RUN apk add --no-cache libffi openssl sqlcipher-libs

WORKDIR /app

COPY --from=builder /install/deps /usr/local

COPY main.py ./
COPY scripts/ scripts/
COPY backends/ backends/
COPY bridge/ bridge/

RUN adduser -D -H appuser \
    && mkdir -p /app/config /app/store /app/media \
    && find /usr/local/lib -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true \
    && find /usr/local/lib -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null; true \
    && chown -R appuser:appuser /app

USER appuser

VOLUME ["/app/config", "/app/store", "/app/media"]

ENTRYPOINT ["python", "main.py", "/app/config/config.yaml"]
