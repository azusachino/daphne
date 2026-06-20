import os
import tomllib
from functools import lru_cache
from typing import Any

ENV_CONFIG_PATH = "DAPHNE_CONFIG_PATH"


def _default_config_path() -> str:
    return os.path.expanduser("~/.config/daphne/config.toml")


def get_config_path() -> str | None:
    path = os.environ.get(ENV_CONFIG_PATH)
    if path:
        return path
    if os.path.exists("config.toml"):
        return "config.toml"
    default_path = _default_config_path()
    if os.path.exists(default_path):
        return default_path
    return None


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    path = get_config_path()
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def app_config() -> dict[str, Any]:
    return load_config().get("app", {})


def rbac_config() -> dict[str, Any]:
    return load_config().get("rbac", {})


def telegram_api_url() -> str | None:
    url = app_config().get("telegram_api_url")
    if url is None:
        return None
    return str(url)


def video_upload_limit_mb() -> int:
    value = app_config().get("video_upload_limit_mb", 512)
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 512
    return max(1, limit)
