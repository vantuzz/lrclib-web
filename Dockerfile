FROM python:3.12-slim

LABEL org.opencontainers.image.title="lrclib-web" \
      org.opencontainers.image.description="Self-hosted web UI for downloading synced lyrics (.lrc) from LRCLIB" \
      org.opencontainers.image.source="https://github.com/vantuzz/lrclib-web" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSIC_DIR=/music \
    DATA_DIR=/config \
    TZ=UTC

RUN pip install --no-cache-dir mutagen==1.47.0

WORKDIR /app
COPY lrclib_web/ /app/lrclib_web/

RUN mkdir -p /config && chown 99:100 /config

VOLUME ["/music", "/config"]
EXPOSE 8080

USER 99:100

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4)"

CMD ["python", "-m", "lrclib_web"]
