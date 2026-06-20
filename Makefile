.PHONY: dev init init-local test fmt fmt-check lint ready image-build verify up down

CONTAINER_TOOL ?= $(shell which podman >/dev/null 2>&1 && echo podman || echo docker)
COMPOSE_TOOL ?= $(CONTAINER_TOOL) compose

dev:
	uv run daphne

init:
	uv run daphne init

init-local:
	uv run daphne init --local

test:
	uv run python -m unittest discover tests

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

ready: fmt lint test

image-build:
	$(CONTAINER_TOOL) build -t daphne:latest .

verify: image-build
	$(CONTAINER_TOOL) run --rm daphne:latest --help | grep "Daphne - Telegram Media Converter"

up:
	$(COMPOSE_TOOL) -f docker-compose.local.yml up -d --build

down:
	$(COMPOSE_TOOL) -f docker-compose.local.yml down
