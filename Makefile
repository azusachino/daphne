.PHONY: dev init init-local test fmt fmt-check lint ready image-base image-base-push image-base-cross image-build image-push verify up down

CONTAINER_TOOL ?= $(shell which podman >/dev/null 2>&1 && echo podman || echo docker)
COMPOSE_TOOL ?= $(CONTAINER_TOOL) compose
IMAGE ?= docker.io/azusachino/daphne
VERSION ?= $(shell rg -m1 -o '^version = "([^"]+)"' -r '$$1' pyproject.toml)
# Base image (OS tools + lux); tagged by toolchain, bumped only when tools change.
BASE_IMAGE ?= docker.io/azusachino/daphne-base
BASE_TAG ?= py3.14-lux0.24.1
BASE_REF := $(BASE_IMAGE):$(BASE_TAG)
# Arches to build for the cross (multi-arch) base; default app build stays amd64.
PLATFORMS ?= linux/amd64,linux/arm64

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

image-base:
	$(CONTAINER_TOOL) build -f Dockerfile.base -t $(BASE_REF) .

image-base-push: image-base
	$(CONTAINER_TOOL) push $(BASE_REF)

# Build and push a multi-arch base in one shot (requires docker buildx).
image-base-cross:
	docker buildx build -f Dockerfile.base --platform $(PLATFORMS) -t $(BASE_REF) --push .

image-build: image-base
	$(CONTAINER_TOOL) build --build-arg BASE_IMAGE=$(BASE_REF) -t daphne:latest .

image-push: image-build
	$(CONTAINER_TOOL) tag daphne:latest $(IMAGE):$(VERSION)
	$(CONTAINER_TOOL) push $(IMAGE):$(VERSION)

verify: image-build
	$(CONTAINER_TOOL) run --rm daphne:latest --help | grep "Daphne - Telegram Media Converter"

up:
	$(COMPOSE_TOOL) -f docker-compose.local.yml up -d --build

down:
	$(COMPOSE_TOOL) -f docker-compose.local.yml down
