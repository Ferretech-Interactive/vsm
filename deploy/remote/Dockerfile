# syntax=docker/dockerfile:1
# The image the Windows app builds on a Linux server it deploys to (Servers > Deploy to a Linux
# server). It installs the package the app uploaded, which already contains the built web UI, so
# unlike the Dockerfile at the top of the repository it needs neither the source nor Node.js.
# Keep the runtime part in step with that file.
FROM python:3.13-slim-bookworm

# lib32gcc/lib32stdc++ are required by SteamCMD; gosu drops root in the entrypoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates lib32gcc-s1 lib32stdc++6 gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash vsm

COPY vein_server_manager-*.whl /tmp/
RUN pip install --no-cache-dir "$(ls /tmp/vein_server_manager-*.whl)[discord]" && rm /tmp/vein_server_manager-*.whl

COPY entrypoint.sh /entrypoint.sh
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
