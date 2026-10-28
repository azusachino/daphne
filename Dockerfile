# Self-contained image: builds from public registries only, so anyone who
# clones the repo can build it. uv syncs the locked environment in the builder,
# and the runtime image keeps uv so uvx can start yt-dlp and yutto at their
# latest releases without a rebuild. deno is yt-dlp's JS runtime for YouTube's
# EJS challenges.
ARG PYTHON_IMAGE=docker.io/library/python:3.14.10-alpine@sha256:cfe4b5079e5852da965ae14f74a6e2b137ece05b918d27c575fb70de3c85db2b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS base

RUN apk add --no-cache ca-certificates curl deno ffmpeg tini tzdata

FROM base AS builder

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy

WORKDIR /app

# Copy lockfile and project metadata, then source for the editable project install
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Sync dependencies without dev dependencies
RUN uv sync --frozen --no-dev

FROM base

WORKDIR /app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/src ./src

# uvx starts yt-dlp and yutto on demand, from PyPI, at the latest release. uv keeps
# its cache and tool environments in /tmp, since /app stays read-only.
ENV UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/tmp/uv-cache UV_TOOL_DIR=/tmp/uv-tools

# Place the virtual environment's bin directory on the PATH (daphne, gallery-dl)
ENV PATH="/app/.venv/bin:$PATH"

# Run as an unprivileged user. /app stays root-owned, so the bot can read but not modify it.
RUN addgroup -S -g 10001 daphne && \
    adduser -S -D -u 10001 -G daphne -h /home/daphne daphne && \
    mkdir -p /home/daphne && chown daphne:daphne /home/daphne
USER daphne

# Set entrypoint to use tini for graceful signal propagation
ENTRYPOINT ["/sbin/tini", "--", "daphne"]
