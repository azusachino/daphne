<div align="center">

# 🌸 Daphne (ダフニー · 沈丁花)

**A fast, stateless Telegram bot that turns raw media links into native, Telegram-friendly media.**

Twitter/X · Pixiv · Bluesky · TikTok · Instagram · Bilibili · YouTube — pasted as a link, delivered as playable media.

[![Python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.5.0-blue)](https://github.com/azusachino/daphne/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Lint & format: ruff](https://img.shields.io/badge/lint%20%26%20format-ruff-000000?logo=ruff&logoColor=white)](https://github.com/astral-sh/ruff)
[![Package manager: uv](https://img.shields.io/badge/deps-uv-DE5FE9?logo=uv&logoColor=white)](https://github.com/astral-sh/uv)
[![Tests](https://img.shields.io/badge/tests-190%20passing-brightgreen)](tests/)

</div>

---

## Table of Contents

- [Why Daphne](#why-daphne)
- [Features](#features)
- [Supported Platforms](#supported-platforms)
- [Commands & Modes](#commands--modes)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Access Control (RBAC)](#access-control-rbac)
- [Development](#development)
- [Architecture](#architecture)
- [License](#license)

## Why Daphne

Daphne turns a pasted link into media in the chat. It uses **purpose-built handlers per platform** for clean native rendering, `yt-dlp` and its fallbacks for YouTube, and its own download chain for Bilibili ([docs/bilibili-download-chain.md](docs/bilibili-download-chain.md)). It has **no database**: secrets live in the environment, settings in a single `config.toml` (see [RBAC.md](RBAC.md)), and the only on-disk state is a short-lived Bilibili media cache in the temp directory.

## Features

- **🔗 Paste a link** — Twitter/X, Pixiv, Bluesky, Instagram, TikTok, YouTube, and Bilibili links convert in the chat. Nothing else is needed.
- **🎯 Per-platform handlers** — photos, GIFs, videos, and carousels render natively instead of dumping a link.
- **🎬 Video** — YouTube through `yt-dlp` (two passes), then `you-get` and `lux`. A file that is shorter than its metadata says is retried.
- **📺 Bilibili** — its own chain: disk cache → native player API (480p anonymous) → yt-dlp → you-get → lux → yutto (last). Every engine's file must pass the duration check. See [docs/bilibili-download-chain.md](docs/bilibili-download-chain.md).
- **🃏 Media card** — when the size is unknown or too large for a direct send, the bot posts a card with title, uploader, duration, and **Download video** and **Open source** buttons.
- **🔐 Role-based access control** — per user and per chat, configured statically in `config.toml`, with quotas per permission. See [RBAC.md](RBAC.md).
- **🚦 Concurrency guard** — global and per-user download limits, so one large transfer never starves the others. Users see a _Queued…_ notice.
- **👀 Live feedback** — message reactions (👀 working → 👍 done / 😢 failed) and upload chat actions.
- **🎨 Rich HTML captions** — title, uploader, duration, source link, a platform hashtag, an author hashtag, and requester attribution. X captions show each hashtag once.
- **🛟 Safety-first** — the original message is deleted **only** after a successful upload, so a link is never lost on error.

## Supported Platforms

| Platform            | Method                                       | Media                                                    |
| ------------------- | -------------------------------------------- | -------------------------------------------------------- |
| **Twitter / X**     | FxTwitter API                                | Photos, GIFs, videos, Articles (preview + up to 3 media) |
| **Instagram**       | `parth-dl` (GraphQL), `yt-dlp` fallback      | Images, carousels, reels                                 |
| **YouTube**         | `yt-dlp` (two passes), then `you-get`, `lux` | Video                                                    |
| **Bilibili · b23**  | Native API → yt-dlp → you-get → lux → yutto  | Video (480p anonymous)                                   |
| **Pixiv**           | Pixiv API + CDN / pixiv.cat fallback         | Photos and media groups                                  |
| **Bluesky**         | XRPC identity + HLS parsing                  | Image carousels, videos                                  |
| **TikTok / Douyin** | TikWM API (+ `yt-dlp` fallback)              | Direct video                                             |

## Commands

| Command      | Description                                                   |
| ------------ | ------------------------------------------------------------- |
| Paste a link | Detect the platform and convert it in the chat                |
| `/start`     | Welcome message; answers even before RBAC has whitelisted you |
| `/help`      | Show usage                                                    |

## Quick Start

Daphne uses [uv](https://github.com/astral-sh/uv) for dependencies and runs cleanly in containers (Podman / Docker) with an optional [local Telegram Bot API](https://github.com/tdlib/telegram-bot-api) sidecar for 2 GB uploads.

```bash
# 1. Generate local config + env templates (untracked)
make init-local

# 2. Fill in your bot credentials and allowed user/chat IDs
#    .daphne.local.env  and  .daphne.config.local.toml

# 3. Spin up the stack (bot + local Bot API sidecar)
make up

# 4. Tear it down
make down
```


### Container image

The image builds from public registries only. It needs Docker or Podman and no local toolchain:

```bash
docker build -t daphne:latest .        # or: podman build -t daphne:latest .
```

The image runs as an unprivileged user (UID 10001) and reads its configuration from `/app/config.toml`. Provide `config/config.toml` and a `.env` with your bot credentials, as `docker-compose.yml` expects, then run:

```bash
docker run -d --name daphne --restart unless-stopped \
  --env-file ./.env \
  -v "$PWD/config/config.toml:/app/config.toml:ro" \
  daphne:latest
```

For 2 GB uploads, run the local Telegram Bot API sidecar from `docker-compose.yml` alongside it.

### Secrets (environment)

| Variable                                | Purpose                                             |
| --------------------------------------- | --------------------------------------------------- |
| `DAPHNE_BOT_TOKEN`                      | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Credentials for the local Bot API sidecar           |

## Configuration

Non-secret runtime settings live in `config.toml`:

```toml
[app]
# telegram_api_url = "http://localhost:8081"
video_upload_limit_mb = 256

# Heavy-download concurrency guard (yt-dlp, you-get, lux, yutto):
# max_concurrent_downloads = 3        # across the whole bot
# max_user_concurrent_downloads = 1   # per user

[rbac]
public_commands = ["help"]

[rbac.roles.admin]
permissions = ["*"]

# Example non-admin roles, graduated from minimal to full. Available
# permissions: convert_link, fetch_metadata, preview_video, download_video.
# [rbac.roles.default]
# permissions = ["convert_link"]
# [rbac.roles.power_user]
# permissions = ["convert_link", "preview_video", "fetch_metadata", "download_video"]

[rbac.users]
# 111111111 = "admin"  # Replace with your Telegram user ID

[rbac.chats]
# -1001111111111 = "power_user"  # Replace with your chat ID
```

If a detected video exceeds `video_upload_limit_mb`, Daphne replies with a decorated HTML info card (with a direct-download button) instead of uploading the file.

## Access Control (RBAC)

Daphne resolves access in order: **admin bypass → public commands → chat-level role → user-level role**. Full authorization flows and fallback mechanics are documented in [RBAC.md](RBAC.md).

## Development

```bash
make fmt      # Format code (ruff)
make lint     # Lint check (ruff)
make test     # Run unit tests
make ready    # fmt + lint + test
```

- **mise** pins Python and uv (`.mise.toml`); `uv` is the local workspace tool. The image runs the locked environment synced in its builder stage and carries uv only so `uvx` can start yt-dlp, you-get and yutto.
- Every `subprocess` call to an external downloader (`yt-dlp`, `you-get`, `lux`, `yutto`, `ffprobe`) carries an explicit timeout to protect the executor pool.

## Architecture

- **Python 3.14**, `aiogram`, polling-based.
- **Stateless**: no database; secrets in env, everything else in `config.toml`. The Bilibili cache lives in the temp directory.
- Deployed as a systemd user service or a Podman/Docker stack with a local Bot API sidecar (TZ `Asia/Tokyo`, no token-bearing HTTP logs).

## License

Released under the [MIT License](LICENSE).
