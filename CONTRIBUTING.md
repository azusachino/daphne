# Contributing to Daphne

Thank you for your interest in contributing to Daphne! Here are some guidelines to help you get started.

## Development Setup

Daphne uses [uv](https://github.com/astral-sh/uv) for Python dependency management.

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/daphne.git
   cd daphne
   ```

2. Sync dependencies:
   ```bash
   uv sync
   ```

3. Initialize configuration files:
   ```bash
   uv run daphne init --local
   ```

## Coding Style

We use `ruff` to format and lint our code.

* Format the code:
  ```bash
  make fmt
  ```
* Lint the code:
  ```bash
  make lint
  ```

## Running Tests

Please make sure all tests pass before opening a pull request.

```bash
make test
```

## Creating Pull Requests

1. Create a branch for your changes:
   ```bash
   git checkout -b feat/your-feature-name
   ```
2. Make your changes and commit them following conventional commit style (e.g. `feat(media): add support for new platform`).
3. Ensure format, lint, and tests are passing (`make ready`).
4. Push to your fork and submit a Pull Request to the main repository.
