# Self-contained image: builds from public registries only, so anyone who
# clones the repo can build it. uv syncs the locked environment in the builder,
# and the runtime image keeps uv so uvx can start yt-dlp, you-get and yutto at their
# latest releases without a rebuild. deno is yt-dlp's JS runtime for YouTube's
# EJS challenges.
ARG PYTHON_IMAGE=docker.io/library/python:3.14.10-alpine@sha256:cfe4b5079e5852da965ae14f74a6e2b137ece05b918d27c575fb70de3c85db2b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0
# lux is built from source: upstream's release binaries are compiled with an old
# Go toolchain and carry dozens of stdlib CVEs into the image (2026-11-13 image
# audit). Building the pinned tag with a current pinned Go removes them all.
ARG LUX_VERSION=v0.24.1
ARG GO_IMAGE=docker.io/library/golang:1.27-alpine@sha256:ea30e25680a5470bac02b08bb0e0a735fb75bd0ab7d431b9b95ee0abef362586

FROM ${UV_IMAGE} AS uv

FROM ${GO_IMAGE} AS lux-builder
ARG LUX_VERSION
RUN apk add --no-cache ca-certificates \
    && mkdir /src && cd /src \
    && go mod init luxbuild \
    && go get "github.com/iawia002/lux@${LUX_VERSION}" \
    && go get golang.org/x/net@latest golang.org/x/text@latest \
           github.com/buger/jsonparser@latest github.com/antchfx/xpath@latest \
    && go build -o /out/lux github.com/iawia002/lux

FROM ${PYTHON_IMAGE} AS base

RUN apk add --no-cache ca-certificates curl deno ffmpeg libexpat tar tini tzdata
COPY --from=lux-builder /out/lux /usr/local/bin/lux

FROM base AS builder

# `matcha` is a git dependency (see pyproject.toml), and uv shells out to git to fetch it.
# It is installed here only, so the runtime image stays without a git client.
RUN apk add --no-cache git

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

# uvx starts yt-dlp, you-get and yutto on demand, from PyPI, at the latest release that is at
# least three days old (UV_EXCLUDE_NEWER, the same window as uv.lock). uv keeps
# its cache and tool environments in /tmp, since /app stays read-only.
ENV UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/tmp/uv-cache UV_TOOL_DIR=/tmp/uv-tools \
    UV_EXCLUDE_NEWER=P3D

# Place the virtual environment's bin directory on the PATH (daphne, gallery-dl)
ENV PATH="/app/.venv/bin:$PATH"

# Run as an unprivileged user. /app stays root-owned, so the bot can read but not modify it.
RUN addgroup -S -g 10001 daphne && \
    adduser -S -D -u 10001 -G daphne -h /home/daphne daphne && \
    mkdir -p /home/daphne && chown daphne:daphne /home/daphne
USER daphne

# Set entrypoint to use tini for graceful signal propagation
ENTRYPOINT ["/sbin/tini", "--", "daphne"]
