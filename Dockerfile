FROM ghcr.io/astral-sh/uv:python3.13-alpine

# Set the working directory
WORKDIR /app

# Copy lockfile and configuration files
COPY pyproject.toml uv.lock ./

# Copy source code files
COPY main.py database.py exchange.py scheduler.py bot.py rbac.py ./

# Sync dependencies without dev dependencies
RUN uv sync --frozen --no-dev

# Place the virtual environment's bin directory on the PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set entrypoint
ENTRYPOINT ["daphne"]
