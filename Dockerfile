# App image on the workstation-owned media base (containers/media-base in
# harus-workstation; local-only, never pushed). uv runs only in the builder
# stage to sync the locked environment on the same Python as the runtime, so
# the runtime image carries no uv. Override with --build-arg BASE_IMAGE=<ref>.
ARG BASE_IMAGE=azusachino.com/media-base:py3.15.2-v3
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${UV_IMAGE} AS uv
FROM ${BASE_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy

WORKDIR /app

# Copy lockfile and project metadata, then source for the editable project install
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Sync dependencies without dev dependencies
RUN uv sync --frozen --no-dev

FROM ${BASE_IMAGE}

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/src ./src

# Place the virtual environment's bin directory on the PATH (yt-dlp, gallery-dl, daphne)
ENV PATH="/app/.venv/bin:$PATH"

# Set entrypoint to use tini for graceful signal propagation
ENTRYPOINT ["/sbin/tini", "--", "daphne"]
