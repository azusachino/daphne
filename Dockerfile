FROM ghcr.io/astral-sh/uv:python3.13-alpine

# Install coreutils and ffmpeg
RUN apk add --no-cache coreutils ffmpeg

# Set the working directory
WORKDIR /app

# Copy lockfile and configuration files
COPY pyproject.toml uv.lock ./

# Copy source code files
COPY src/ ./src/

# Sync dependencies without dev dependencies
RUN uv sync --frozen --no-dev

# Place the virtual environment's bin directory on the PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set entrypoint
ENTRYPOINT ["daphne"]
