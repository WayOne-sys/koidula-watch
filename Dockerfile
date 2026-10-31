# Работает и на ARM (Oracle Ampere A1), и на x86 (E2.1.Micro): образ python:slim мультиархитектурный.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Tallinn \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY koidula_watch ./koidula_watch

RUN useradd --create-home --uid 10001 watcher \
    && mkdir -p /data && chown watcher:watcher /data
USER watcher
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=2 \
    CMD ["python", "-m", "koidula_watch", "healthcheck"]

ENTRYPOINT ["python", "-m", "koidula_watch"]
CMD ["run"]
