FROM ghcr.io/astral-sh/uv:0.11.19-python3.14-alpine

ARG LUX_VERSION=0.24.1

# Install runtime tools used by downloader fallbacks.
RUN apk add --no-cache ca-certificates coreutils curl ffmpeg tar tzdata \
    && curl -fsSL "https://github.com/iawia002/lux/releases/download/v${LUX_VERSION}/lux_${LUX_VERSION}_Linux_x86_64.tar.gz" \
        | tar -xz -C /tmp \
    && install -m 0755 /tmp/lux /usr/local/bin/lux \
    && rm -f /tmp/lux

ENV TZ=Asia/Tokyo

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
