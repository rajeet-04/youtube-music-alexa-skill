FROM ghcr.io/astral-sh/uv:0.12.23 AS uv-bin

FROM python:3.12-slim

COPY --from=uv-bin /uv /uvx /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ffmpeg \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && apt-get purge -y curl unzip \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/bin:${PATH}"
# yt-dlp's player/solver cache lives on the persistent data volume: a redeploy then skips
# re-downloading the player JS and challenge solver on the first extractions.
ENV XDG_CACHE_HOME=/data/cache

WORKDIR /app

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY . .

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health/live', timeout=3)" || exit 1
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--threads=16", "server:app"]
