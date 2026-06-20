.PHONY: dev init test fmt lint ready image-build verify

CONTAINER_TOOL ?= $(shell which podman >/dev/null 2>&1 && echo podman || echo docker)

dev:
	uv run daphne

init:
	uv run daphne init

test:
	uv run python -m unittest discover tests

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

ready: fmt lint test

image-build:
	$(CONTAINER_TOOL) build -t daphne:latest .

verify: image-build
	$(CONTAINER_TOOL) run --rm daphne:latest --help | grep "Daphne - Telegram Media Converter"
