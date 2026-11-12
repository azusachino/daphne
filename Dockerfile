# Self-contained image: builds from public registries plus the workstation's
# local-only media-base (containers/media-base in harus-workstation; same
# recipe as upstream registries: floating, refreshed via REFRESH=1). uv syncs
# the locked environment in the builder, and the runtime image keeps uv so
# uvx can start yt-dlp, you-get and yutto at their latest releases without a
# rebuild. media-base provides python, ffmpeg, deno and tini.
ARG BASE_IMAGE=azusachino.com/media-base:py3.14-trixie-r1
ARG UV_IMAGE=ghcr.io/astral-sh/uv:latest
# lux is built from source at the latest release tag: upstream's published
# binaries are compiled with an old Go toolchain and carry stdlib CVEs into
# the image (2026-11-13 image audit). Floating Go patch level via the
# golang:1.27-alpine tag.
ARG GO_IMAGE=docker.io/library/golang:1.27-alpine

FROM ${UV_IMAGE} AS uv

FROM ${GO_IMAGE} AS lux-builder
RUN apk add --no-cache ca-certificates \
    && mkdir /src && cd /src \
    && go mod init luxbuild \
    && LUX_VERSION="$(wget -qO- https://api.github.com/repos/iawia002/lux/releases/latest | grep -o '"tag_name": *"[^"]*"' | cut -d'"' -f4)" \
    && go get "github.com/iawia002/lux@${LUX_VERSION}" \
    && go get golang.org/x/net@latest golang.org/x/text@latest \
           github.com/buger/jsonparser@latest github.com/antchfx/xpath@latest \
    && go build -o /out/lux github.com/iawia002/lux

FROM ${BASE_IMAGE} AS base

COPY --from=lux-builder /out/lux /usr/local/bin/lux

FROM base AS builder

# `matcha` is a git dependency (see pyproject.toml), and uv shells out to git to fetch it.
# It is installed here only, so the runtime image stays without a git client.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

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
RUN groupadd -g 10001 daphne \
    && useradd -u 10001 -g daphne -d /home/daphne -M -s /usr/sbin/nologin daphne \
    && mkdir -p /home/daphne && chown daphne:daphne /home/daphne
USER daphne

# Set entrypoint to use tini for graceful signal propagation (tini lives at
# /usr/bin/tini on the Debian-based base)
ENTRYPOINT ["/usr/bin/tini", "--", "daphne"]
