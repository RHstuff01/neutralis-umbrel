FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEUTRALIS_DATA_DIR=/data \
    PORT=8787

WORKDIR /opt/neutralis
COPY --chown=1000:1000 app/ ./app/

RUN mkdir -p /data && chown 1000:1000 /data
USER 1000:1000

EXPOSE 8787
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).read()"]

CMD ["python", "app/server.py"]
