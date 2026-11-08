# Web viewer for Hik-Connect cameras (python -m hik_web).
# Build from the repository root:  docker build -f deploy/Dockerfile -t hik-web .
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir aiohttp==3.14.5 requests==2.34.2 \
    pycryptodome==4.4.0 pyezvizapi==1.0.5.0

WORKDIR /app
COPY hik_cloud.py ./
COPY hik_web ./hik_web

# The Hik-Connect login and the device verification codes live in /data.
ENV HIK_CONFIG=/data/hikvision_monitor.json \
    XDG_CACHE_HOME=/data/cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8765
USER 1000:1000
CMD ["python", "-m", "hik_web", "--host", "0.0.0.0", "--port", "8765"]
