# SAS SpeedyStore Studio - server pilot image: the hub + the Studio app + a shared notebook environment (no Claude).
#
#   docker build -f deploy/Dockerfile -t speedystore-studio:pilot .
#   docker run --rm -p 8800:8800 -v studio-data:/data --read-only --tmpfs /tmp \
#     -e STUDIO_VIYA_URL=https://<viya> -e STUDIO_MASTER_KEY=<secret> speedystore-studio:pilot
#
# Runs as uid 10001, works with a read-only root filesystem (writes only to /data and /tmp).
# Build arg INSTALL_CLAUDE=1 adds Claude Code for "Ask Claude" (off unless STUDIO_ASSISTANT=1 and an API key is set).
# Build arg INSTALL_NOTEBOOKS=1 (default) adds the notebook environment /opt/notebook-env (notebook_kernel.PACKAGES:
# ipykernel, pandas, singlestoredb, saspy, swat, ...) used by STUDIO_NOTEBOOKS=local; read-only at run time, so
# `%pip install` in a notebook can't change it for everyone. INSTALL_NOTEBOOKS=0 leaves it out (~1 GB smaller).
# tini is PID 1: it forwards SIGTERM to the hub and reaps orphaned processes (a crashed Studio's kernels).

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first (cached while only the app changes), then the app itself (not editable: no source needed later).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
# The shared notebook environment, from the app's own package list (one list for the desktop and the server).
ARG INSTALL_NOTEBOOKS=1
RUN if [ "$INSTALL_NOTEBOOKS" = "1" ]; then \
      uv venv /opt/notebook-env --python /usr/local/bin/python3.12 --seed \
      && uv pip install --python /opt/notebook-env/bin/python \
           $(/app/.venv/bin/python -c "from singlestore_mcp.notebook_kernel import PACKAGES; print(' '.join(PACKAGES))") \
      && touch /opt/notebook-env/.s2-ready; \
    else mkdir -p /opt/notebook-env; fi

FROM python:3.12-slim
ARG INSTALL_CLAUDE=0
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/* \
    && if [ "$INSTALL_CLAUDE" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
      && npm install -g @anthropic-ai/claude-code && npm cache clean --force \
      && apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi \
    && useradd --uid 10001 --user-group --create-home --home-dir /home/studio --shell /usr/sbin/nologin studio \
    && mkdir -p /data && chown 10001:10001 /data && chmod 0700 /data
COPY --from=build /app/.venv /app/.venv
COPY --from=build /opt/notebook-env /opt/notebook-env
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/studio \
    TMPDIR=/tmp \
    STUDIO_DATA_DIR=/data \
    STUDIO_HUB_BIND=0.0.0.0 \
    STUDIO_HUB_PORT=8800 \
    STUDIO_TRUST_FORWARDED=1 \
    STUDIO_NOTEBOOK_PYTHON=/opt/notebook-env/bin/python \
    MPLBACKEND=Agg
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "singlestore_mcp.hub"]
