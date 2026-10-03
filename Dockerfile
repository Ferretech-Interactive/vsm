# syntax=docker/dockerfile:1

# ---- web UI ----
FROM node:22-alpine AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# vite.config.ts writes the build to ../vsm/static
RUN npm run build

# ---- runtime ----
FROM python:3.13-slim-bookworm

# lib32gcc/lib32stdc++ are required by SteamCMD; gosu drops root in the entrypoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates lib32gcc-s1 lib32stdc++6 gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash vsm

WORKDIR /app
COPY pyproject.toml README.md ./
COPY vsm ./vsm
COPY --from=web /src/vsm/static ./vsm/static
RUN pip install --no-cache-dir ".[discord]"

COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

ENV VSM_DATA_DIR=/data \
    VSM_HOST=0.0.0.0 \
    VSM_PORT=8810 \
    VSM_AUTO_INSTALL=true \
    VSM_STOP_SERVER_ON_EXIT=true \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8810/tcp 7777/udp 27015/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('VSM_PORT', '8810'), timeout=3)"

WORKDIR /data
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "vsm"]
