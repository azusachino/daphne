.PHONY: dev init init-local test fmt fmt-check lint ready image-base image-base-latest check-base-versions image-build image-push image-local verify up down

CONTAINER_TOOL ?= $(shell which podman >/dev/null 2>&1 && echo podman || echo docker)
COMPOSE_TOOL ?= $(CONTAINER_TOOL) compose
IMAGE ?= docker.io/azusachino/daphne
# Local-only tag the k3s deployment actually runs (imagePullPolicy: Never —
# imported straight into containerd, never pulled from a registry).
LOCAL_IMAGE ?= azusachino.icu/daphne
VERSION ?= $(shell rg -m1 -o '^version = "([^"]+)"' -r '$$1' pyproject.toml)
# Base image (OS tools + lux + deno); tagged by toolchain, bumped only when
# tools change. Local-only like LOCAL_IMAGE above — never pushed to a
# registry, just built once (image-base) and reused as a --build-arg.
BASE_IMAGE ?= azusachino.icu/daphne-base
BASE_TAG ?= py3.14-lux0.24.1-deno
BASE_REF := $(BASE_IMAGE):$(BASE_TAG)

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

check-base-versions: ## Report lux's pinned vs. latest GitHub release, and deno's latest (informational -- it floats via apk)
	uv run scripts/resolve_base_versions.py

image-base-latest: ## Resolve lux's latest release and build the base image pinned to it (deno always floats to Alpine's latest via apk)
	uv run scripts/resolve_base_versions.py --build --container-tool $(CONTAINER_TOOL)

image-build: image-base
	$(CONTAINER_TOOL) build --build-arg BASE_IMAGE=$(BASE_REF) -t daphne:latest .

image-push: image-build
	$(CONTAINER_TOOL) tag daphne:latest $(IMAGE):$(VERSION)
	$(CONTAINER_TOOL) push $(IMAGE):$(VERSION)

image-local: image-base ## Build and import straight into k3s containerd (bypasses registry push; run on the k3s node)
	$(CONTAINER_TOOL) build --build-arg BASE_IMAGE=$(BASE_REF) -t $(LOCAL_IMAGE):$(VERSION) .
	$(CONTAINER_TOOL) save $(LOCAL_IMAGE):$(VERSION) | sudo k3s ctr images import -

verify: image-build
	$(CONTAINER_TOOL) run --rm daphne:latest --help | grep "Daphne - Telegram Media Converter"

up:
	$(COMPOSE_TOOL) -f docker-compose.local.yml up -d --build

down:
	$(COMPOSE_TOOL) -f docker-compose.local.yml down
