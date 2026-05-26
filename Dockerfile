FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/DmitriyLyalyuev/tgmesh-bridge"
LABEL org.opencontainers.image.description="Meshtastic-Telegram bridge"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ /app/src/

RUN useradd --uid 1000 --no-create-home --shell /sbin/nologin tgmesh-bridge && \
    chown -R tgmesh-bridge /app && \
    mkdir -p /data && chown tgmesh-bridge:tgmesh-bridge /data && chmod 0775 /data

ENV XDG_CACHE_HOME=/tmp/cache

VOLUME /data

USER tgmesh-bridge

HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD find /tmp/tgmesh_bridge.ready -mmin -2 | grep -q . || exit 1

CMD ["python", "-u", "src/tgmesh_bridge.py"]
