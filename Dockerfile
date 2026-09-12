# App image: built FROM the prebuilt base (OS tools + lux + deno). Only the
# Python dependency sync and source copy live here, so day-to-day rebuilds
# skip the slow tool layers. See Dockerfile.base. The base is local-only
# (built by `make image-base`, never pushed to a registry) — override with
# --build-arg if you built it under a different tag.
ARG BASE_IMAGE=azusachino.com/daphne-base:py3.14-lux0.24.1-deno
FROM ${BASE_IMAGE}

WORKDIR /app

# Copy lockfile and configuration files
COPY pyproject.toml uv.lock ./

# Copy source code files
COPY src/ ./src/

# Sync dependencies without dev dependencies
RUN uv sync --frozen --no-dev

# Place the virtual environment's bin directory on the PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set entrypoint to use tini for graceful signal propagation
ENTRYPOINT ["/sbin/tini", "--", "daphne"]
