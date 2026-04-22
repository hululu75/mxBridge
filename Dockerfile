FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py backfill.py repair_media.py encrypt_tool.py ./
COPY backends/ backends/
COPY bridge/ bridge/
COPY store/ store/

RUN useradd -r -m appuser
RUN mkdir -p /app/store /app/media && chown -R appuser:appuser /app
USER appuser

VOLUME ["/app/store", "/app/media"]

ENTRYPOINT ["python", "main.py"]
