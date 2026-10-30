# SAS SpeedyStore Studio - server pilot image: the hub + the Studio app (no notebook environment, no Claude).
#
#   docker build -f deploy/Dockerfile -t speedystore-studio:pilot .
#   docker run --rm -p 8800:8800 -v studio-data:/data --read-only --tmpfs /tmp \
#     -e STUDIO_VIYA_URL=https://<viya> -e STUDIO_MASTER_KEY=<secret> speedystore-studio:pilot
#
# Runs as uid 10001, works with a read-only root filesystem (writes only to /data and /tmp).
# Build arg INSTALL_CLAUDE=1 adds Claude Code for "Ask Claude" (off unless STUDIO_ASSISTANT=1 and an API key is set).

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first (cached while only the app changes), then the app itself (not editable: no source needed later).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
ARG INSTALL_CLAUDE=0
RUN if [ "$INSTALL_CLAUDE" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
      && npm install -g @anthropic-ai/claude-code && npm cache clean --force \
      && apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi \
    && useradd --uid 10001 --user-group --create-home --home-dir /home/studio --shell /usr/sbin/nologin studio \
    && mkdir -p /data && chown 10001:10001 /data && chmod 0700 /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/studio \
    TMPDIR=/tmp \
    STUDIO_DATA_DIR=/data \
    STUDIO_HUB_BIND=0.0.0.0 \
    STUDIO_HUB_PORT=8800 \
    STUDIO_TRUST_FORWARDED=1
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "singlestore_mcp.hub"]
