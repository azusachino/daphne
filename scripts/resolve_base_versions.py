#!/usr/bin/env python3
"""
resolve_base_versions.py — Resolve the latest release for the base image's
pinned toolchain and, optionally, build Dockerfile.base against it.

lux is fetched from a version-templated GitHub release asset (see
Dockerfile.base's ARG LUX_VERSION), so unlike deno -- installed via
`apk add deno`, which always floats to whatever version Alpine's `community`
index currently carries, no scripting needed -- resolving "latest" for lux
needs an actual API call to construct the download filename. That's the
whole reason this script exists; deno is reported here only for visibility.

Usage:
    uv run scripts/resolve_base_versions.py            # report only
    uv run scripts/resolve_base_versions.py --build     # report, then build
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE_BASE = REPO_ROOT / "Dockerfile.base"


def latest_github_release(repo: str) -> str:
    resp = httpx.get(
        f"https://api.github.com/repos/{repo}/releases/latest", timeout=10.0
    )
    resp.raise_for_status()
    return resp.json()["tag_name"].lstrip("v")


def current_lux_version() -> str:
    match = re.search(r"ARG LUX_VERSION=([\d.]+)", DOCKERFILE_BASE.read_text())
    if not match:
        sys.exit("Could not find ARG LUX_VERSION in Dockerfile.base")
    return match.group(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build", action="store_true", help="Build the base image after resolving"
    )
    parser.add_argument("--container-tool", default="podman")
    args = parser.parse_args()

    current_lux = current_lux_version()
    latest_lux = latest_github_release("iawia002/lux")
    latest_deno = latest_github_release("denoland/deno")

    lux_status = "up to date" if current_lux == latest_lux else "UPDATE AVAILABLE"
    print(f"lux:  pinned={current_lux}  latest={latest_lux}  ({lux_status})")
    print(
        f"deno: installed via apk, floats with Alpine's community index -- "
        f"upstream latest={latest_deno} (informational only, not pinned)"
    )

    if not args.build:
        return

    ref = f"azusachino.com/daphne-base:py3.14-lux{latest_lux}-deno"
    cmd = [
        args.container_tool,
        "build",
        "-f",
        "Dockerfile.base",
        "--build-arg",
        f"LUX_VERSION={latest_lux}",
        "-t",
        ref,
        ".",
    ]
    print(f"Building {ref} ...")
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)
    print(f"Built {ref}")


if __name__ == "__main__":
    main()
